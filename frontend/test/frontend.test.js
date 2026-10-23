import assert from "node:assert/strict";
import { test } from "node:test";

import { StringStream } from "@codemirror/language";
import { Text } from "@codemirror/state";

import { diagnosticsToCodeMirror } from "../src/diagnostics.js";
import { statementTarget } from "../src/explain.js";
import { validFileName } from "../src/files.js";
import { homeoStream } from "../src/language.js";

/** [text, style] pairs for one line (non-blank tokens only). */
function tokens(line, state = homeoStream.startState()) {
  const stream = new StringStream(line, 4, 4);
  const out = [];
  while (!stream.eol()) {
    const style = homeoStream.token(stream, state);
    const text = stream.current();
    if (text.trim()) out.push([text, style]);
    stream.start = stream.pos;
  }
  return out;
}

test("binding line", () => {
  assert.deepEqual(tokens("fan.bathroom := co2 > 800 or humidity > 60% # boost"), [
    ["fan.bathroom", "target"],
    [":=", "operator"],
    ["co2", "variable"],
    [">", "operator"],
    ["800", "number"],
    ["or", "keyword"],
    ["humidity", "variable"],
    [">", "operator"],
    ["60%", "number"],
    ["# boost", "comment"],
  ]);
});

test("literals and builtins", () => {
  assert.deepEqual(tokens('    age(input_button.boost) < 1h30min -> on(brightness: 10%)'), [
    ["age", "builtinFunction"],
    ["(", null],
    ["input_button.boost", "entity"],
    [")", null],
    ["<", "operator"],
    ["1h30min", "number"],
    ["->", "operator"],
    ["on", "atom"],
    ["(", null],
    ["brightness", "variable"],
    [":", null],
    ["10%", "number"],
    [")", null],
  ]);
  assert.deepEqual(tokens('let night = now in 22:00..07:00'), [
    ["let", "keyword"],
    ["night", "def"],
    ["=", "operator"],
    ["now", "builtin"],
    ["in", "keyword"],
    ["22:00", "number"],
    ["..", "operator"],
    ["07:00", "number"],
  ]);
  assert.deepEqual(tokens('on input_button.doorbell:').slice(0, 2), [
    ["on", "keyword"],
    ["input_button.doorbell", "entity"],
  ]);
  assert.deepEqual(tokens('notify.x(message: "say \\"hi\\"")').slice(-2), [
    ['"say \\"hi\\""', "string"],
    [")", null],
  ]);
  assert.deepEqual(tokens("# mode: dry_run"), [["# mode: dry_run", "meta"]]);
});

test("statementTarget finds the statement around the cursor", () => {
  const text = [
    "let co2 = sensor.co2", // 1
    "", // 2
    "fan.bathroom :=", // 3
    "    co2 > 800    -> on", // 4
    "    else         -> off", // 5
    'state mode = "auto":', // 6
    '    fresh(input_button.x) -> "boost"', // 7
    "climate.lr.temperature := 21", // 8
    'light.hall["rgb color"] := 1', // 9
    "on input_button.x:", // 10
    "    notify.x()", // 11
  ].join("\n");
  assert.equal(statementTarget(text, 1), "co2");
  assert.equal(statementTarget(text, 2), "co2");
  assert.equal(statementTarget(text, 3), "fan.bathroom");
  assert.equal(statementTarget(text, 5), "fan.bathroom");
  assert.equal(statementTarget(text, 7), "mode");
  assert.equal(statementTarget(text, 8), "climate.lr.temperature");
  assert.equal(statementTarget(text, 9), "light.hall.rgb color");
  assert.equal(statementTarget(text, 11), null); // actions can't be explained
});

test("diagnostics map to document offsets", () => {
  const doc = Text.of(["let x =", "fan.a := nope"]);
  const [d] = diagnosticsToCodeMirror(doc, [
    { line: 2, col: 10, end_line: 2, end_col: 14, severity: "error", message: "unknown", hint: "did you mean `on`?" },
  ]);
  assert.equal(doc.sliceString(d.from, d.to), "nope");
  assert.equal(d.message, "unknown\nhint: did you mean `on`?");
  // past the end of a line or the file: clamped, never empty
  const [e] = diagnosticsToCodeMirror(doc, [
    { line: 9, col: 99, end_line: 9, end_col: 99, severity: "warning", message: "x", hint: null },
  ]);
  assert.equal(e.severity, "warning");
  assert.ok(e.from <= doc.length && e.to <= doc.length);
});

test("file names: anything the integration loads, nothing outside the folder", () => {
  for (const ok of ["bathroom.homeo", "küche.homeo", "living room.homeo", "bath.v2.homeo"]) {
    assert.ok(validFileName(ok), ok);
  }
  for (const bad of ["../x.homeo", "a/b.homeo", "a\\b.homeo", ".hidden.homeo", ".homeo", "x.txt", "a\0.homeo"]) {
    assert.ok(!validFileName(bad), bad);
  }
});
