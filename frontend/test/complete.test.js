import assert from "node:assert/strict";
import { test } from "node:test";

import { CompletionContext } from "@codemirror/autocomplete";
import { EditorState } from "@codemirror/state";

import { callContext, homeoCompletions, inActionBody } from "../src/complete.js";

const hass = {
  states: {
    "switch.pump": { state: "off", attributes: { friendly_name: "Pump" } },
    "light.hall": { state: "on", attributes: { friendly_name: "Hall", brightness: 128 } },
    "event.bathroom_remote": {
      state: "unknown",
      attributes: { event_types: ["press", "long_press"] },
    },
    "input_button.boost": { state: "unknown", attributes: {} },
  },
  services: {
    switch: { turn_on: { name: "Turn on" }, turn_off: { name: "Turn off" }, toggle: { name: "Toggle" } },
    light: {
      turn_on: {
        name: "Turn on",
        target: {},
        fields: {
          transition: { name: "Transition" },
          advanced_fields: { collapsed: true, fields: { flash: { name: "Flash" } } },
        },
      },
    },
    notify: {
      mobile_app_phone: {
        fields: { message: { name: "Message", required: true }, title: { name: "Title" } },
      },
    },
    persistent_notification: { create: { fields: { message: { required: true } } } },
  },
};

/** Completion at `|` in `text`: { from, labels }, or null. */
function complete(text, explicit = false) {
  const pos = text.indexOf("|");
  const state = EditorState.create({ doc: text.replace("|", "") });
  const result = homeoCompletions(() => hass)(new CompletionContext(state, pos, explicit));
  return result && { from: result.from, labels: result.options.map((o) => o.label) };
}

const ACTION = "on input_button.boost:\n    ";

test("entity services in an action body (issue #9)", () => {
  const r = complete(`${ACTION}switch.pump.t|`);
  assert.ok(r.labels.includes("toggle"));
  assert.ok(r.labels.includes("turn_off"));
  assert.equal(r.from, `${ACTION}switch.pump.`.length);
  assert.ok(complete("on input_button.boost: switch.pump.|").labels.includes("toggle"));
});

test("no services where they can't be called", () => {
  const binding = complete("light.hall := switch.pump.|");
  assert.ok(binding.labels.includes("friendly_name"));
  assert.ok(!binding.labels.includes("toggle"));
  assert.ok(!complete("light.hall := notify.|").labels.some((l) => l.startsWith("notify.")));
  assert.ok(!complete("on input_button.|").labels.includes("input_button.press")); // the header
});

test("domain services and service-only domains in an action body", () => {
  assert.ok(complete(`${ACTION}notify.|`).labels.includes("notify.mobile_app_phone"));
  assert.ok(complete(`${ACTION}pers|`).labels.includes("persistent_notification"));
  assert.ok(!complete("let x = pers|").labels.includes("persistent_notification"));
  // a time's colon isn't the end of the header
  assert.ok(complete("on 07:00:\n    notify.|").labels.includes("notify.mobile_app_phone"));
});

test("on(...) settings follow the bound domain", () => {
  assert.deepEqual(complete("light.hall := on(|").labels, ["brightness", "kelvin"]);
  assert.deepEqual(complete("fan.bathroom := on(perc|").labels, ["percentage"]);
  assert.deepEqual(complete("let x = on(|").labels, ["brightness", "kelvin", "percentage"]);
  assert.deepEqual(
    complete("light.hall :=\n    switch.pump -> on(brightness: 50%, |").labels,
    ["brightness", "kelvin"],
  );
  assert.equal(complete("light.hall := on(brightness: |"), null); // a value goes here
});

test("service data fields", () => {
  assert.deepEqual(complete(`${ACTION}notify.mobile_app_phone(|`).labels, ["message", "title"]);
  const scoped = complete(`${ACTION}light.hall.turn_on(|`).labels;
  assert.deepEqual(scoped, ["transition", "flash"]); // sections flattened; no entity_id
  assert.ok(complete(`${ACTION}light.turn_on(|`).labels.includes("entity_id"));
  assert.equal(complete(`${ACTION}notify.mobile_app_phone(message: "a, |`), null); // a string
});

test("toggle's initial comes after what it toggles", () => {
  assert.ok(!complete("light.hall := toggle(inp|").labels.includes("initial"));
  assert.ok(complete("light.hall := toggle(inp|").labels.includes("input_button"));
  assert.deepEqual(complete("light.hall := toggle(input_button.boost, |").labels, ["initial"]);
});

test("a let alias of an entity completes like the entity", () => {
  const doc = "let remote = event.bathroom_remote\nlet pump = switch.pump\nlet hall = light.hall\n";
  assert.deepEqual(complete(`${doc}light.hall := age(remote.|`).labels, ["press", "long_press"]);
  assert.ok(complete(`${doc}${ACTION}pump.|`).labels.includes("toggle"));
  assert.deepEqual(complete(`${doc}${ACTION}hall.turn_on(|`).labels, ["transition", "flash"]);
});

test("call context and action bodies", () => {
  assert.deepEqual(callContext('notify.x(message: "(", title: '), {
    callee: "notify.x",
    argName: false,
    index: 1,
  });
  // a bracket in a comment doesn't count
  assert.deepEqual(callContext("light.hall := on(  # a ( comment\n"), {
    callee: "on",
    argName: true,
    index: 0,
  });
  assert.equal(inActionBody("on bus.zha_event(command: 1)"), false);
  assert.equal(inActionBody("on bus.zha_event(command: 1):\n    "), true);
  assert.equal(inActionBody("light.hall := on"), false);
});
