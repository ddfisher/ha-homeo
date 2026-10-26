// Autocompletion: entity ids (from the live HA states), their attributes and event types,
// services and their fields (in `on` action bodies), `on(...)` settings, keywords, builtins,
// and the file's own `let` and `state` names. Builtins and keywords show their docs (docs.js).

import { startCompletion } from "@codemirror/autocomplete";

import { DOCS, docText } from "./docs.js";
import { BUILTINS, FUNCTIONS, KEYWORDS } from "./language.js";

/** The settings `on(...)` takes, per bound domain. Mirrors the writers' `on_params`
 * (LIGHT_PARAMS and FAN_PARAMS in core/writers.py; a Python test keeps them in step). */
export const ON_SETTINGS = {
  light: { brightness: "a percentage, e.g. 50%", kelvin: "colour temperature, e.g. 2700" },
  fan: { percentage: "speed, e.g. 60%" },
};

/** Named arguments of builtin functions. */
const FUNCTION_ARGS = { toggle: { initial: "on or off: the value before the first time" } };

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

/** Builtins and keywords with their hover docs as completion info. */
const withDocs = (option) =>
  Object.hasOwn(DOCS, option.label) ? { ...option, info: docText(DOCS[option.label]) } : option;

const STATIC = [
  ...KEYWORDS.map((label) => ({ label, type: "keyword" })),
  ...BUILTINS.map((label) => ({ label, type: "constant" })),
  ...FUNCTIONS.map((label) => ({ label, type: "function", apply: `${label}(` })),
].map(withDocs);

function localNames(doc) {
  const out = new Set();
  for (const m of doc.matchAll(/^(?:let|state)\s+([A-Za-z_]\w*)/gm)) out.add(m[1]);
  return [...out].map((label) => ({ label, type: "variable", detail: "defined here" }));
}

/** `let` names that are plain aliases of an entity: `let remote = event.bathroom_remote`. */
export function aliases(doc) {
  const out = Object.create(null); // keyed by names from the file: no `constructor` etc.
  for (const m of doc.matchAll(/^let\s+([A-Za-z_]\w*)\s*=\s*([a-z_]\w*\.\w+)\s*(?:#.*)?$/gm)) {
    out[m[1]] = m[2];
  }
  return out;
}

/** The statement the cursor is in, from its first line up to `pos`. A statement starts on a
 * line that isn't indented (continuation lines, arms and action bodies are). */
export function statementBefore(doc, pos) {
  let n = doc.lineAt(pos).number;
  while (n > 1) {
    const text = doc.line(n).text;
    if (/^\S/.test(text) && !text.startsWith("#")) break;
    n--;
  }
  return doc.sliceString(doc.line(n).from, pos);
}

/** The innermost open call or list at the end of `text`: its callee (the dotted name before
 * `(`, or null), whether an argument name goes here (right after `(` or `,`), and which
 * argument this is (0 for the first). Strings and comments are skipped; null inside an
 * unterminated string or outside any brackets. */
export function callContext(text) {
  const stack = [];
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    const top = stack[stack.length - 1];
    if (c === "#") {
      const nl = text.indexOf("\n", i);
      if (nl < 0) return null; // in a comment
      i = nl;
    } else if (c === '"' || c === "'") {
      let j = i + 1;
      while (j < text.length && text[j] !== c && text[j] !== "\n") j += text[j] === "\\" ? 2 : 1;
      if (j >= text.length) return null; // in a string
      i = j;
    } else if (c === "(") {
      const m = /([A-Za-z_][\w.]*)\s*$/.exec(text.slice(0, i));
      stack.push({ callee: m ? m[1] : null, argName: true, index: 0 });
    } else if (c === "[") {
      stack.push({ callee: null, argName: false, index: 0 });
    } else if (c === ")" || c === "]") {
      stack.pop();
    } else if (c === ",") {
      if (top) {
        top.argName = top.callee !== null;
        top.index++;
      }
    } else if (!/[\w\s]/.test(c)) {
      if (top) top.argName = false;
    }
  }
  return stack[stack.length - 1] ?? null;
}

/** Whether the statement is an `on x:` action and the cursor is in its body (after the
 * header's `:`, which isn't a time's `07:00` colon, a `:=`, or inside brackets). */
export function inActionBody(statement) {
  if (!/^on\s/.test(statement)) return false;
  let depth = 0;
  for (let i = 0; i < statement.length; i++) {
    const c = statement[i];
    if (c === '"' || c === "'") {
      const end = statement.indexOf(c, i + 1);
      if (end < 0) return false;
      i = end;
    } else if (c === "(" || c === "[") depth++;
    else if (c === ")" || c === "]") depth--;
    else if (c === ":" && depth === 0 && !/[\d=]/.test(statement[i + 1] ?? "")) return true;
  }
  return false;
}

/** The domain bound by a `domain.object[.attr] := …` statement, if it is one. */
function boundDomain(statement) {
  return /^([a-z_]\w*)\.\w+(?:\.\w+)?\s*:=/.exec(statement)?.[1] ?? null;
}

/** A service's data fields, including those grouped in collapsible sections. */
function serviceFields(service) {
  const out = {};
  for (const [key, field] of Object.entries(service?.fields ?? {})) {
    if (field && typeof field === "object" && field.fields) Object.assign(out, field.fields);
    else out[key] = field;
  }
  return out;
}

function text40(s) {
  return s ? String(s).slice(0, 40) : "";
}

/** Argument names for a call to `callee`: `on(...)` settings, builtin function arguments
 * (after the positional first one), or a service's data fields (`notify.phone(` or
 * `light.hall.turn_on(`, aliases resolved). */
export function argumentOptions({ callee, index }, statement, services, alias) {
  if (callee === "on") {
    const domain = boundDomain(statement);
    const table = ON_SETTINGS[domain] ? { [domain]: ON_SETTINGS[domain] } : ON_SETTINGS;
    return Object.entries(table).flatMap(([d, settings]) =>
      Object.entries(settings).map(([label, info]) => ({
        label,
        type: "property",
        detail: `${d} setting`,
        info,
        apply: `${label}: `,
      })),
    );
  }
  if (FUNCTION_ARGS[callee]) {
    if (index === 0) return []; // `toggle(x, …)`: the first argument is what to toggle
    return Object.entries(FUNCTION_ARGS[callee]).map(([label, info]) => ({
      label,
      type: "property",
      info,
      apply: `${label}: `,
    }));
  }
  const parts = callee.split(".");
  let domain;
  let name;
  let scoped = false;
  if (parts.length === 2 && alias[parts[0]]) {
    [domain, name, scoped] = [alias[parts[0]].split(".")[0], parts[1], true];
  } else if (parts.length === 2) {
    [domain, name] = parts;
  } else if (parts.length === 3) {
    [domain, name, scoped] = [parts[0], parts[2], true];
  } else {
    return [];
  }
  const service = services[domain]?.[name];
  if (!service) return [];
  const fields = serviceFields(service);
  if (!scoped && service.target) fields.entity_id ??= { description: "entities to act on" };
  return Object.entries(fields).map(([label, f]) => ({
    label,
    type: "property",
    detail: text40(f?.name ?? (f?.required ? "required" : "")),
    info: [f?.description, f?.example !== undefined ? `e.g. ${f.example}` : ""]
      .filter(Boolean)
      .join("\n"),
    boost: f?.required ? 1 : 0,
    apply: `${label}: `,
  }));
}

function serviceOptions(domain, services, prefix, boost) {
  return Object.entries(services[domain] ?? {}).map(([name, s]) => ({
    label: `${prefix}${name}`,
    type: "method",
    detail: text40(s?.name ?? "service"),
    info: s?.description ?? undefined,
    apply: `${prefix}${name}(`,
    boost,
  }));
}

/** An entity's attributes (event types for event entities), and in action bodies its
 * domain's services: `switch.pump.toggle()`. */
function entityMembers(entityId, st, services, action, from) {
  const domain = entityId.split(".")[0];
  const names = !st
    ? []
    : domain === "event"
      ? (st.attributes.event_types ?? [])
      : Object.keys(st.attributes).filter((a) => /^[A-Za-z_]\w*$/.test(a));
  const options = names.map((label) => ({
    label,
    type: "property",
    detail: domain === "event" ? "event type" : text40(st.attributes[label]),
  }));
  if (action) options.push(...serviceOptions(domain, services, "", 1));
  return { from, options, validFor: /^\w*$/ };
}

export function homeoCompletions(getHass) {
  return (context) => {
    const hass = getHass();
    const states = hass?.states ?? {};
    const services = hass?.services ?? {};
    const doc = context.state.doc;
    const statement = statementBefore(doc, context.pos);
    const docText = doc.toString();
    const alias = aliases(docText);

    // an argument name: `on(brightness: …)`, `notify.phone(message: …)`, `toggle(x, initial: …)`
    const arg = context.matchBefore(/\w*/);
    const call = callContext(statement.slice(0, statement.length - arg.text.length));
    if (call?.callee && call.argName) {
      const options = argumentOptions(call, statement, services, alias);
      const typed = arg.text !== "" || context.explicit || /[(,]\s*$/.test(statement);
      if (options.length && typed) return { from: arg.from, options, validFor: /^\w*$/ };
    }

    const word = context.matchBefore(/[A-Za-z_][\w.]*/);
    if (!word || (word.from === word.to && !context.explicit)) return null;
    const text = word.text;
    const parts = text.split(".");
    const action = inActionBody(statement);

    if (parts.length === 2 && alias[parts[0]]) {
      // a `let` alias of an entity: its attributes or event types (and services in actions)
      const entityId = alias[parts[0]];
      return entityMembers(entityId, states[entityId], services, action, word.from + parts[0].length + 1);
    }
    if (parts.length >= 3) {
      // after a full entity id: its attributes (or, for event entities, its event types)
      const entityId = `${parts[0]}.${parts[1]}`;
      const st = states[entityId];
      if (!st) return null;
      return entityMembers(entityId, st, services, action, word.from + entityId.length + 1);
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
      if (action) options.push(...serviceOptions(parts[0], services, `${parts[0]}.`, 0));
      return { from: word.from, options, validFor: /^[\w.]*$/ };
    }
    const entityDomains = new Set(Object.keys(states).map((id) => id.split(".")[0]));
    // in an action body, domains that only have services (persistent_notification, tts, …)
    const all = action ? new Set([...entityDomains, ...Object.keys(services)]) : entityDomains;
    const domains = [...all].map((d) => ({
      label: d,
      type: "namespace",
      apply: applyDomain(d),
      boost: entityDomains.has(d) ? -1 : -2,
    }));
    return {
      from: word.from,
      options: [...localNames(docText), ...STATIC, ...domains],
      validFor: /^\w*$/,
    };
  };
}
