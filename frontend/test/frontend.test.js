import assert from "node:assert/strict";
import { test } from "node:test";

import { StringStream } from "@codemirror/language";
import { Text } from "@codemirror/state";

import { diagnosticsToCodeMirror } from "../src/diagnostics.js";
import { explanationHtml, prose, statementTarget } from "../src/explain.js";
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

test("explain prose shows backtick spans as code, escaped", () => {
  assert.equal(
    prose("no binding named `a<b`; did you mean `fan.x`"),
    "no binding named <code>a&#60;b</code>; did you mean <code>fan.x</code>",
  );
});

test("explanation html", () => {
  const html = explanationHtml({
    binding: "fan.bathroom",
    rule: "test.homeo:5",
    result: "on",
    summary: "fan.bathroom should be on, because `x > 1` (line 7)",
    device: "off",
    device_matches: false,
    arms: [
      { arm: "x > 1 -> on", line: 7, condition: true, active: true, values: { x: 2 } },
      { arm: "else -> off", line: 8 },
    ],
  });
  assert.match(html, /<span class="where">test\.homeo:5<\/span>/);
  assert.doesNotMatch(html, /class="summary"/); // the arms say the same
  assert.match(html, /<span class="mark no" title="differs from the result"/);
  assert.match(html, /<li class="arm active">/);
  assert.match(html, /<li class="arm idle">/);
  assert.ok(html.includes(`x &#62; 1 <span class="arrow">-&gt;</span>\u00a0on`)); // wraps before `->` only
  assert.match(html, /<code class="expr">x<\/code><span class="val">2<\/span>/);
  assert.match(html, /checked top to bottom/);
  assert.doesNotMatch(html, /implicit/); // it has an `else`
});

test("explanation html: unknown binding keeps its summary, with code", () => {
  const html = explanationHtml({ binding: "switch.pump", result: "unknown", summary: "left as it is (`keep`)" });
  assert.match(html, /<p class="summary">left as it is \(<code>keep<\/code>\)<\/p>/);
});

test("explanation html: implicit else when no arm applies", () => {
  const groups = [
    { guard: "a", line: 3, condition: true, arms: [{ arm: "b -> 1", line: 4, condition: false }] },
    { arm: "c -> 2", line: 5, condition: "unknown" },
  ];
  const machine = explanationHtml({ machine: "mode", state: "x", summary: "s", arms: groups });
  assert.match(machine, /<li class="arm active implicit">/);
  assert.ok(machine.includes(`else <span class="arrow">-&gt;</span>\u00a0keep`));
  assert.doesNotMatch(machine, /class="summary"/); // the arms say the same
  const let_ = explanationHtml({ let: "v", file: "f", value: "unknown", arms: groups });
  assert.ok(let_.includes(`else <span class="arrow">-&gt;</span>\u00a0unknown`));
  // an arm inside a group decides: no implicit else
  groups[0].arms[0].active = true;
  assert.doesNotMatch(explanationHtml({ machine: "mode", arms: groups }), /implicit/);
});

test("explanation html: an implicit arm from the server has no line", () => {
  const html = explanationHtml({
    binding: "light.desk",
    result: "keep",
    latest: [
      { arm: "input_button.desk_on -> on", line: 2, happened: "never" },
      { arm: "else -> keep", implicit: true, active: true },
    ],
  });
  assert.match(html, /<li class="arm active implicit">/);
  assert.match(html, /<span class="line">implicit<\/span>/);
  assert.doesNotMatch(html, /line undefined/);
  assert.equal(html.match(/implicit<\/span>/g).length, 1); // not added a second time
});

test("explanation html: code may wrap after an opening bracket", () => {
  const html = explanationHtml({ let: "v", file: "f", value: 1, values: { "age(manual(fan.x)) + f()": 1 } });
  assert.ok(html.includes("age(<wbr>manual(<wbr>fan.x)) + f()"));
});

test("a range bound before `..` isn't an entity", () => {
  assert.deepEqual(tokens("let w = today in mon..fri"), [
    ["let", "keyword"],
    ["w", "def"],
    ["=", "operator"],
    ["today", "builtin"],
    ["in", "keyword"],
    ["mon", "builtin"],
    ["..", "operator"],
    ["fri", "builtin"],
  ]);
  assert.deepEqual(tokens("let e = now in sunset..23:00").slice(5, 7), [
    ["sunset", "builtin"],
    ["..", "operator"],
  ]);
});
