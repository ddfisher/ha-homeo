import assert from "node:assert/strict";
import { test } from "node:test";

import { Text } from "@codemirror/state";

import { DOCS, docText } from "../src/docs.js";
import { describe, fmtDuration, fmtInstant, hoverHtml, tokenAt, valueRows } from "../src/hover.js";
import { BUILTINS, FUNCTIONS, KEYWORDS } from "../src/language.js";

const NOW = new Date(2026, 0, 5, 8, 10, 0); // local time, so the tests don't depend on the zone

const HASS = {
  states: {
    "sensor.co2": {
      state: "812",
      attributes: { friendly_name: "Bathroom CO2", unit_of_measurement: "ppm" },
      last_changed: new Date(2026, 0, 5, 8, 7, 0).toISOString(),
    },
    "light.hall": {
      state: "on",
      attributes: { friendly_name: "Hall", brightness: 128 },
      last_changed: new Date(2026, 0, 5, 7, 0, 0).toISOString(),
    },
    "event.remote": {
      state: new Date(2026, 0, 5, 8, 9, 30).toISOString(),
      attributes: { event_type: "single", event_types: ["single", "double"] },
    },
    "sun.sun": { state: "below_horizon", attributes: { next_rising: "2026-01-05T08:30:00Z" } },
  },
  services: {
    notify: { phone: { name: "Send to phone", description: "Sends a notification.", fields: { message: { name: "Message", required: true, description: "The text." } } } },
    light: { turn_on: { name: "Turn on", description: "Turns on lights." } },
  },
};

const SRC = [
  "let co2 = sensor.co2",
  "let remote = event.remote",
  'state mode = "auto":',
  '    fresh(remote.double) -> "boost"',
  "light.hall := latest",
  "    co2 > 800 for 5min  -> on(brightness: 50%)",
  "    else                -> keep",
  "on co2 > 900 if now in 22:00..06:00:",
  '    notify.phone(message: "co2")',
  "    light.hall.turn_on()",
  "",
].join("\n");
const DOC = Text.of(SRC.split("\n"));

/** describe() for the first occurrence of `needle` (offset `k` into it) in line `n`. */
function at(n, needle, k = 0) {
  const line = DOC.line(n);
  const i = line.text.indexOf(needle);
  assert.ok(i >= 0, `${needle} not in line ${n}`);
  return describe(DOC, line.from + i + k, HASS, NOW);
}

test("tokens under the pointer", () => {
  assert.deepEqual(tokenAt("light.hall := on(brightness: 50%)", 3), { from: 0, to: 10, text: "light.hall", style: "target" });
  assert.equal(tokenAt("let x = 1", 3), null); // whitespace
  assert.equal(tokenAt("on x:", 0).style, "keyword");
  assert.equal(tokenAt("x := on", 6).style, "atom");
});

test("builtin functions, names and keywords", () => {
  const age = describe(Text.of(["light.x := age(sensor.co2) < 1h"]), 11, HASS, NOW);
  assert.equal(age.info.title, "age(x)");
  assert.equal(age.info.kind, "function");
  assert.equal(at(4, "fresh").info.title, "fresh(x)");
  assert.equal(at(5, "latest").info.title, "latest");
  assert.equal(at(6, "for").info.title, "x for duration");
  assert.equal(at(7, "keep").info.title, "keep");
  assert.equal(at(8, "on").info.kind, "action");
  assert.equal(at(8, "if").info.kind, "keyword");
  assert.equal(at(8, "now").info.kind, "timestamp");
  assert.equal(at(8, "in").info.title, "x in a..b");
  assert.equal(at(8, "..").info.kind, "range");
  // a range bound written right before `..` is a day or solar time, not an entity
  const range = Text.of(["let w = today in mon..fri and now in sunset..23:00"]);
  assert.equal(describe(range, range.toString().indexOf("mon"), HASS, NOW).info.title, "mon");
  assert.equal(describe(range, range.toString().indexOf("sunset"), HASS, NOW).info.title, "sunset");
  assert.equal(at(1, "let").info.kind, "keyword");
  assert.equal(at(3, "state").info.kind, "keyword");
  assert.equal(at(6, "on(").info.title, "on(setting: value, …)");
  assert.equal(at(6, "->").info.kind, "arm");
});

test("common words have no hover", () => {
  const doc = Text.of(["let x = a and not b or true"]);
  for (const word of ["and", "not", "or", "true"]) {
    assert.equal(describe(doc, doc.toString().indexOf(word), HASS, NOW), null, word);
  }
});

test("every builtin and keyword the editor knows has docs", () => {
  for (const name of [...FUNCTIONS, ...BUILTINS]) assert.ok(DOCS[name], name);
  // every keyword but the common words, which have no hover (above)
  const common = ["and", "or", "not", "true", "false", "off"];
  for (const name of KEYWORDS.filter((k) => !common.includes(k))) assert.ok(DOCS[name], name);
  assert.match(docText(DOCS.age), /^age\(x\)\n\nHow long since x last changed/);
});

test("entities: state, attribute, event type", () => {
  const co2 = at(1, "sensor.co2").info;
  assert.equal(co2.title, "sensor.co2");
  assert.equal(co2.kind, "Bathroom CO2");
  assert.deepEqual(co2.rows, [["state", "812 ppm"], ["changed", "08:07:00 (3min ago)"]]);

  const hall = describe(Text.of(["let b = light.hall.brightness"]), 9, HASS, NOW).info;
  assert.deepEqual(hall.rows, [["brightness", "128"], ["state", "on"]]);

  const target = at(5, "light.hall").info;
  assert.equal(target.text, "Driven by this binding.");

  // through the alias `remote`, an event type
  const dbl = at(4, "remote.double").info;
  assert.equal(dbl.title, "event.remote.double");
  assert.deepEqual(dbl.rows, [["last event", "single, 08:09:30 (30s ago)"]]);
  const typo = describe(Text.of(["let remote = event.remote", "let x = remote.tripple"]), 34, HASS, NOW);
  assert.match(typo.info.note, /isn't one of this entity's event types: `single`, `double`/);

  const missing = describe(Text.of(["let x = sensor.nope"]), 10, HASS, NOW).info;
  assert.equal(missing.note, "Not found in Home Assistant.");
});

test("services and their arguments", () => {
  const notify = at(9, "notify.phone").info;
  assert.equal(notify.title, "notify.phone");
  assert.equal(notify.text, "Send to phone: Sends a notification.");
  const turnOn = at(10, "light.hall.turn_on").info;
  assert.equal(turnOn.title, "light.turn_on");
  assert.equal(turnOn.kind, "service, on light.hall");
  const message = at(9, "message").info;
  assert.equal(message.title, "message");
  assert.equal(message.kind, "Message");
  assert.equal(message.text, "The text.");
  const brightness = at(6, "brightness").info;
  assert.equal(brightness.kind, "light setting");
});

test("let and state names look up their value", () => {
  for (const [n, needle] of [[1, "co2"], [6, "co2"], [8, "co2"]]) {
    const d = at(n, needle);
    assert.equal(d.info.lookup, "co2");
    assert.equal(d.info.kind, "let");
    assert.equal(d.info.example, "let co2 = sensor.co2");
  }
  const mode = at(3, "mode").info;
  assert.equal(mode.kind, "state machine");
  assert.equal(mode.lookup, "mode");
  assert.equal(describe(Text.of(["let x = y"]), 8, HASS, NOW), null); // not defined here
  const aligned = describe(Text.of(["let co2    = sensor.co2  # ppm", "let y = co2"]), 40, HASS, NOW);
  assert.equal(aligned.info.example, "let co2 = sensor.co2");
});

test("value rows", () => {
  assert.deepEqual(valueRows({ kind: "let", type: "number", value: "812", changed: "08:07:00 (3min ago)" }), {
    rows: [["value", "812"], ["type", "number"], ["changed", "08:07:00 (3min ago)"]],
    note: undefined,
  });
  assert.match(valueRows({ value: "true" }, { stale: "unsaved" }).note, /without your unsaved changes/);
  assert.match(valueRows({ value: "true" }, { stale: "errors" }).note, /previous version, which keeps running/);
  assert.match(valueRows({ error: "nope", code: "not_found" }).note, /isn't in the running version.*Save/);
  const broken = valueRows({ error: "nope", code: "not_found" }, { stale: "errors" }).note;
  assert.match(broken, /because this file has errors/);
  assert.doesNotMatch(broken, /Save/);
});

test("formatting", () => {
  assert.equal(fmtDuration(45), "45s");
  assert.equal(fmtDuration(3 * 60), "3min");
  assert.equal(fmtDuration(3600 + 5 * 60 + 20), "1h5min");
  assert.equal(fmtDuration(2 * 86400), "2d");
  assert.equal(fmtInstant(new Date(2026, 0, 5, 9, 0, 0).toISOString(), NOW), "09:00:00 (in 50min)");
  assert.equal(fmtInstant(new Date(2026, 0, 4, 9, 0, 0).toISOString(), NOW), "2026-01-04 09:00:00 (23h10min ago)");
  const html = hoverHtml({ title: "age(x)", kind: "function", text: "Use `x` <here>", rows: [["a", "<b>"]], example: "age(x)" });
  assert.match(html, /<code>age\(x\)<\/code> <span class="hover-kind">function<\/span>/);
  assert.match(html, /Use <code>x<\/code> &#60;here&#62;/);
  assert.match(html, /<span class="hover-val">&#60;b&#62;<\/span>/);
});

test("names from the file never match object prototype keys", () => {
  for (const name of ["constructor", "toString", "__proto__"]) {
    const doc = Text.of([`let x = ${name}`]);
    assert.equal(describe(doc, 9, HASS, NOW), null, name); // not defined here: no hover
    const dotted = Text.of([`let x = ${name}.y`]);
    assert.doesNotThrow(() => describe(dotted, 9, HASS, NOW), name);
  }
  const attr = describe(Text.of(["let b = light.hall.constructor"]), 9, HASS, NOW).info;
  assert.match(attr.note, /has no attribute `constructor`/);
});

test("a `#` inside a string isn't a comment in a definition line", () => {
  const doc = Text.of(['let msg = "room #1"  # the sign', "let y = msg"]);
  const d = describe(doc, doc.toString().lastIndexOf("msg"), HASS, NOW);
  assert.equal(d.info.example, 'let msg = "room #1"');
});

test("now is a timestamp", () => {
  assert.equal(describe(Text.of(["let n = now"]), 8, HASS, NOW).info.kind, "timestamp");
});
