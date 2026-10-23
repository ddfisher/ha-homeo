// Autocompletion: entity ids (from the live HA states), their attributes and event types,
// keywords, builtins, and the file's own `let` and `state` names.

import { startCompletion } from "@codemirror/autocomplete";

import { BUILTINS, FUNCTIONS, KEYWORDS } from "./language.js";

/** Insert `domain.` and list that domain's entities straight away. */
function applyDomain(domain) {
  return (view, _completion, from, to) => {
    const insert = `${domain}.`;
    view.dispatch({
      changes: { from, to, insert },
      selection: { anchor: from + insert.length },
      userEvent: "input.complete",
    });
    startCompletion(view);
  };
}

const STATIC = [
  ...KEYWORDS.map((label) => ({ label, type: "keyword" })),
  ...BUILTINS.map((label) => ({ label, type: "constant" })),
  ...FUNCTIONS.map((label) => ({ label, type: "function", apply: `${label}(` })),
];

function localNames(doc) {
  const out = new Set();
  for (const m of doc.matchAll(/^(?:let|state)\s+([A-Za-z_]\w*)/gm)) out.add(m[1]);
  return [...out].map((label) => ({ label, type: "variable", detail: "defined here" }));
}

export function homeoCompletions(getHass) {
  return (context) => {
    const word = context.matchBefore(/[A-Za-z_][\w.]*/);
    if (!word || (word.from === word.to && !context.explicit)) return null;
    const states = getHass()?.states ?? {};
    const text = word.text;
    const parts = text.split(".");

    if (parts.length >= 3) {
      // after a full entity id: its attributes (or, for event entities, its event types)
      const entityId = `${parts[0]}.${parts[1]}`;
      const st = states[entityId];
      if (!st) return null;
      const names =
        parts[0] === "event"
          ? (st.attributes.event_types ?? [])
          : Object.keys(st.attributes).filter((a) => /^[A-Za-z_]\w*$/.test(a));
      return {
        from: word.from + entityId.length + 1,
        options: names.map((label) => ({
          label,
          type: "property",
          detail: parts[0] === "event" ? "event type" : String(st.attributes[label]).slice(0, 40),
        })),
        validFor: /^\w*$/,
      };
    }
    if (parts.length === 2) {
      const options = Object.keys(states)
        .filter((id) => id.startsWith(`${parts[0]}.`))
        .map((id) => ({
          label: id,
          type: "variable",
          detail: states[id].attributes.friendly_name ?? "",
          info: `state: ${states[id].state}`,
        }));
      return { from: word.from, options, validFor: /^[\w.]*$/ };
    }
    const domains = [...new Set(Object.keys(states).map((id) => id.split(".")[0]))].map((d) => ({
      label: d,
      type: "namespace",
      apply: applyDomain(d),
      boost: -1,
    }));
    return {
      from: word.from,
      options: [...localNames(context.state.doc.toString()), ...STATIC, ...domains],
      validFor: /^\w*$/,
    };
  };
}
