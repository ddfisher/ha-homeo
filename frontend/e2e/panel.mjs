// Browser test of the editor panel in a real Home Assistant.
//
//   python tools/real_ha.py /tmp/realha-serve serve      # in another terminal (see its docstring)
//   node e2e/panel.mjs /tmp/realha-serve [/tmp/shots] [dark]
//
// Needs Chromium (set CHROMIUM to its executable if Playwright can't find one). Uses and changes
// the rule files `tools/real_ha.py serve` creates. Exits non-zero if a check fails.

import { readFileSync, writeFileSync } from "node:fs";
import { chromium } from "playwright";

const [config, shots, mode = "light"] = process.argv.slice(2);
if (!config) {
  console.error("usage: node e2e/panel.mjs CONFIG_DIR [SCREENSHOT_DIR] [dark]");
  process.exit(2);
}
const token = JSON.parse(readFileSync(`${config}/token.json`, "utf8"));
token.expires = Date.now() + 1800 * 1000;
const garagePath = `${config}/homeo/garage.homeo`;
const garageBefore = readFileSync(garagePath, "utf8");

const browser = await chromium.launch(
  process.env.CHROMIUM ? { executablePath: process.env.CHROMIUM } : {},
);
const context = await browser.newContext({
  viewport: { width: 1400, height: 860 },
  colorScheme: mode === "dark" ? "dark" : "light",
});
await context.addInitScript((t) => localStorage.setItem("hassTokens", JSON.stringify(t)), token);
const page = await context.newPage();

// Errors from our code: exceptions in the panel's module, and failed homeo/ commands.
// (HA's own frontend has unrelated failures, e.g. recorder/info without a recorder.)
const errors = [];
page.on("pageerror", (e) => {
  if (String(e.stack ?? "").includes("homeo")) errors.push(String(e));
});
const sent = {};
page.on("websocket", (ws) => {
  ws.on("framesent", (f) => {
    try {
      for (const m of [].concat(JSON.parse(f.payload))) sent[m.id] = m.type;
    } catch {
      // not JSON
    }
  });
  ws.on("framereceived", (f) => {
    try {
      for (const m of [].concat(JSON.parse(f.payload))) {
        if (m.success === false && sent[m.id]?.startsWith("homeo/")) {
          errors.push(`${sent[m.id]}: ${JSON.stringify(m.error)}`);
        }
      }
    } catch {
      // not JSON
    }
  });
});

let failed = 0;
const check = (name, ok, detail = "") => {
  if (!ok) failed++;
  console.log(`${ok ? "ok  " : "FAIL"} ${name}${ok ? "" : ` ${detail}`}`);
};
const shot = (name) => shots && page.screenshot({ path: `${shots}/${name}-${mode}.png` });

try {
  await page.goto("http://127.0.0.1:8123/homeo");
  const panel = page.locator("homeo-panel");
  await panel.waitFor({ timeout: 30000 });
  await panel.locator(".cm-content").filter({ hasText: "state bath_mode" }).waitFor();

  const files = await panel.locator("nav.files li .name").allTextContents();
  check("file list", files.join() === "bathroom.homeo,garage.homeo", files.join());
  check("highlighting", (await panel.locator(".cm-content span").count()) > 30);
  await shot("panel");

  const editorText = () => panel.locator(".cm-content").innerText();
  const explanation = () => panel.locator(".explanation-body").innerText();
  const until = async (what, predicate, timeout = 8000) => {
    const deadline = Date.now() + timeout;
    while (Date.now() < deadline) {
      if (await predicate()) return true;
      await page.waitForTimeout(100);
    }
    check(what, false, "timed out");
    return false;
  };

  // Typing must not trigger HA's keyboard shortcuts (`e`, `c`, `a`, `m` open dialogs).
  await panel.locator(".cm-content").click();
  await page.keyboard.press("Control+End");
  await page.keyboard.type("\nlight.kitchen_lights := sensor.outside_temperatur > 30\n");
  check("no HA dialog while typing", (await page.locator("ha-quick-bar, ha-voice-command-dialog").count()) === 0);
  await panel.locator(".status").filter({ hasText: "1 error" }).waitFor({ timeout: 5000 });
  const status = await panel.locator(".status").innerText();
  check("inline error", status.includes("unknown entity `sensor.outside_temperatur`"), status);
  check("error underlined", (await panel.locator(".cm-lintRange-error").count()) > 0);
  check("unsaved", await panel.locator("button.save").isEnabled());
  await shot("panel-error");

  await page.keyboard.type("light.bed");
  await panel.locator(".cm-tooltip-autocomplete").waitFor({ timeout: 5000 });
  let options = await panel.locator(".cm-tooltip-autocomplete li").allTextContents();
  check("entity completion", options.some((o) => o.includes("light.bed_light")), options.join());
  await page.keyboard.press("Escape");

  // completing a domain inserts `domain.` and lists its entities right away
  await page.keyboard.press("Enter");
  await page.keyboard.type("input_butt");
  await panel.locator(".cm-tooltip-autocomplete li", { hasText: "input_button" }).first().waitFor({ timeout: 5000 });
  await page.keyboard.press("Enter");
  await page.waitForTimeout(400);
  options = await panel.locator(".cm-tooltip-autocomplete li").allTextContents();
  check("domain completion continues", options.some((o) => o.includes("input_button.boost")), options.join());
  await shot("panel-complete");
  await page.keyboard.press("Escape");

  // Explain the fan's rule (the running version), then follow the cursor
  await panel.locator(".cm-line", { hasText: "fan.ceiling_fan :=" }).click();
  await panel.locator("button.explain").click();
  await until("explain", async () => (await explanation()).includes("fan.ceiling_fan should be"));
  check("explain notes unsaved changes", (await explanation()).includes("without your unsaved changes"));
  await shot("panel-explain");
  await panel.locator(".cm-line", { hasText: "kelvin: 2700" }).click();
  await until("explain follows the cursor", async () => (await explanation()).includes("light.bed_light should be"));
  await panel.locator(".cm-line", { hasText: "let night" }).click();
  await until("explain a let", async () => /night\s*let/.test(await explanation()));
  const nightText = await explanation();
  check("let parts", nightText.includes("now") && nightText.includes("definition"), nightText.slice(0, 300));

  // unsaved edits survive visiting another page
  await page.locator("ha-sidebar").getByText("Overview").click();
  await page.waitForURL(/lovelace|dashboard/, { timeout: 10000 }).catch(() => {});
  await page.locator("ha-sidebar").getByText("Homeo").click();
  await until("draft restored", async () => (await editorText()).includes("sensor.outside_temperatur"));
  check("restore message", (await panel.locator(".status").innerText()).includes("Restored your unsaved changes"));
  check("explanation still open", await panel.locator(".explanation").isVisible());

  // switch files, discarding the edits; the explanation stays open and follows
  page.once("dialog", (d) => d.accept());
  await panel.locator("nav.files li", { hasText: "garage.homeo" }).click();
  await panel.locator(".cm-content").filter({ hasText: "switch.ac" }).waitFor({ timeout: 5000 });
  await until("explanation follows to the new file", async () => (await explanation()).includes("switch.ac"));

  // an unedited file changed on disk reloads itself
  writeFileSync(garagePath, `${garageBefore}# edited elsewhere 1\n`);
  await until("outside change reloads", async () => (await editorText()).includes("edited elsewhere 1"));

  // with unsaved edits, it shows a notice; "Keep mine" then saves over it
  await panel.locator(".cm-content").click();
  await page.keyboard.press("Control+End");
  await page.keyboard.type("# saved from the browser test\n");
  writeFileSync(garagePath, `${garageBefore}# edited elsewhere 2\n`);
  await until("conflict notice", async () => (await panel.locator(".status .conflict").count()) > 0);
  await shot("panel-conflict");
  check("edits kept", (await editorText()).includes("saved from the browser test"));
  await panel.locator(".status .keep-mine").click();
  await panel.locator("button.save").click();
  await panel.locator(".status").filter({ hasText: "Saved" }).waitFor({ timeout: 5000 });
  check("saved and running", (await panel.locator(".status").innerText()).includes("Saved and running"));
  const disk = readFileSync(garagePath, "utf8");
  check("written to disk", disk.includes("# saved from the browser test") && !disk.includes("edited elsewhere 2"), disk);

  await page.setViewportSize({ width: 390, height: 800 });
  await page.waitForTimeout(500);
  check("narrow layout: file picker", await panel.locator(".file-select").isVisible());
  await shot("panel-narrow");
} catch (err) {
  check("ran to the end", false, String(err).split("\n")[0]);
} finally {
  writeFileSync(garagePath, garageBefore);
  check("no errors from the panel", errors.length === 0, errors.join("; "));
  await browser.close();
}
process.exit(failed ? 1 : 0);
