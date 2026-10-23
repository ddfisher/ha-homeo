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
  const options = await panel.locator(".cm-tooltip-autocomplete li").allTextContents();
  check("entity completion", options.some((o) => o.includes("light.bed_light")), options.join());
  await page.keyboard.press("Escape");

  // explain the fan's rule (the running version)
  await panel.locator(".cm-line", { hasText: "fan.ceiling_fan :=" }).click();
  await panel.locator("button.explain").click();
  await panel.locator(".explanation-body section").waitFor({ timeout: 5000 });
  const expl = await panel.locator(".explanation-body").innerText();
  check("explain", expl.includes("fan.ceiling_fan should be"), expl.slice(0, 200));
  check("explain notes unsaved changes", expl.includes("without your unsaved changes"));
  await shot("panel-explain");

  // switch files, discarding the edits; then edit and save with Ctrl+S
  page.once("dialog", (d) => d.accept());
  await panel.locator("nav.files li", { hasText: "garage.homeo" }).click();
  await panel.locator(".cm-content").filter({ hasText: "switch.ac" }).waitFor({ timeout: 5000 });
  check("explanation closed on switching files", await panel.locator(".explanation").isHidden());
  await panel.locator(".cm-content").click();
  await page.keyboard.press("Control+End");
  await page.keyboard.type("# saved from the browser test\n");
  await page.keyboard.press("Control+s");
  await panel.locator(".status").filter({ hasText: "Saved" }).waitFor({ timeout: 5000 });
  check("saved and running", (await panel.locator(".status").innerText()).includes("Saved and running"));
  check("written to disk", readFileSync(garagePath, "utf8").includes("# saved from the browser test"));

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
