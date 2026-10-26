// Hover documentation: what a builtin, keyword or operator means, an entity's current state
// (from the live HA states), a service's description, a call argument, and the current value
// of the file's own `let` and `state` names (from the running version, via homeo/value).

import { StringStream } from "@codemirror/language";
import { hoverTooltip } from "@codemirror/view";

import { aliases, argumentOptions, callContext, statementBefore } from "./complete.js";
import { DOCS, SOLAR_ATTRS } from "./docs.js";
import { prose } from "./explain.js";
import { homeoStream } from "./language.js";

const esc = (s) => String(s).replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);

/** Domains whose state is the time something last happened (§9). */
const TIMESTAMP_DOMAINS = new Set(["event", "input_button", "button", "scene"]);

/** The token of `lineText` covering column `col`: its range, text and highlighter style
 * (the same classification the highlighter uses), or null over whitespace. */
export function tokenAt(lineText, col) {
  const stream = new StringStream(lineText, 4, 4);
  const state = homeoStream.startState();
  while (!stream.eol()) {
    stream.start = stream.pos;
    const style = homeoStream.token(stream, state);
    if (stream.pos === stream.start) stream.next(); // never stall
    if (stream.start <= col && col < stream.pos) {
      const text = stream.current();
      return text.trim() ? { from: stream.start, to: stream.pos, text, style } : null;
    }
  }
  return null;
}

/** A duration in the style of the integration's messages: `3min`, `1h5min`, `45s`. */
export function fmtDuration(seconds) {
  let rest = Math.round(Math.abs(seconds));
  if (rest >= 3600) rest = Math.round(rest / 60) * 60; // minutes are enough past an hour
  const parts = [];
  for (const [unit, size] of [["d", 86400], ["h", 3600], ["min", 60]]) {
    if (rest >= size) {
      parts.push(`${Math.floor(rest / size)}${unit}`);
      rest %= size;
    }
  }
  if (rest || !parts.length) parts.push(`${rest}s`);
  return parts.join("");
}

const pad = (n) => String(n).padStart(2, "0");

/** A point in time as local `HH:MM:SS` (with the date if it isn't today) and how far off. */
export function fmtInstant(iso, now = new Date()) {
  const t = new Date(iso);
  if (Number.isNaN(t.getTime())) return String(iso);
  let text = `${pad(t.getHours())}:${pad(t.getMinutes())}:${pad(t.getSeconds())}`;
  if (t.toDateString() !== now.toDateString()) {
    text = `${t.getFullYear()}-${pad(t.getMonth() + 1)}-${pad(t.getDate())} ${text}`;
  }
  const delta = (now - t) / 1000;
  if (delta >= 0.5) return `${text} (${fmtDuration(delta)} ago)`;
  if (delta <= -0.5) return `${text} (in ${fmtDuration(-delta)})`;
  return `${text} (now)`;
}

function attrText(v) {
  if (v === null || v === undefined) return "none";
  return typeof v === "object" ? JSON.stringify(v) : String(v);
}

/** An entity's state, attribute or event type, from the HA states. */
function entityInfo(parts, states, now) {
  const entityId = `${parts[0]}.${parts[1]}`;
  const domain = parts[0];
  const st = states[entityId];
  const out = { title: entityId, kind: st?.attributes?.friendly_name ?? "entity", rows: [] };
  if (!st) {
    out.kind = "entity";
    out.note = "Not found in Home Assistant.";
    return out;
  }
  const rest = parts.slice(2);
  const isTime = TIMESTAMP_DOMAINS.has(domain) || st.attributes.device_class === "timestamp";
  const unit = st.attributes.unit_of_measurement;
  const shown =
    isTime && !["unknown", "unavailable"].includes(st.state)
      ? fmtInstant(st.state, now)
      : `${st.state}${unit ? (unit === "%" ? "%" : ` ${unit}`) : ""}`;
  if (domain === "event" && rest.length === 1) {
    const type = rest[0];
    out.title = `${entityId}.${type}`;
    out.text = `The time of the last \`${type}\` event.`;
    if (st.attributes.event_type === type) out.rows.push(["last", shown]);
    else if (st.attributes.event_type) {
      out.rows.push(["last event", `${st.attributes.event_type}, ${shown}`]);
    }
    const types = st.attributes.event_types;
    if (Array.isArray(types) && !types.includes(type)) {
      out.note = `\`${type}\` isn't one of this entity's event types: ${types.map((t) => `\`${t}\``).join(", ")}.`;
    }
    return out;
  }
  if (rest.length === 1) {
    const attr = rest[0];
    out.title = `${entityId}.${attr}`;
    if (Object.hasOwn(st.attributes, attr)) out.rows.push([attr, attrText(st.attributes[attr])]);
    else out.note = `\`${entityId}\` has no attribute \`${attr}\` now.`;
    out.rows.push(["state", shown]);
    return out;
  }
  out.rows.push(["state", shown]);
  if (st.last_changed && !isTime) out.rows.push(["changed", fmtInstant(st.last_changed, now)]);
  return out;
}

/** A service, called as `domain.service(` or `entity.service(`. */
function serviceInfo(domain, name, services, entityId) {
  const s = services[domain]?.[name];
  if (!s) return null;
  return {
    title: `${domain}.${name}`,
    kind: entityId ? `service, on ${entityId}` : "service",
    text: [s.name, s.description].filter(Boolean).join(": ") || undefined,
    plainText: true,
  };
}

/** A line without its comment (a `#` inside a string isn't one). */
export function withoutComment(lineText) {
  const stream = new StringStream(lineText, 4, 4);
  const state = homeoStream.startState();
  while (!stream.eol()) {
    stream.start = stream.pos;
    if (homeoStream.token(stream, state) === "comment") return lineText.slice(0, stream.start);
    if (stream.pos === stream.start) stream.next(); // never stall
  }
  return lineText;
}

/** How much a line changes the bracket nesting (brackets in strings and comments don't count). */
function bracketDelta(lineText) {
  const stream = new StringStream(lineText, 4, 4);
  const state = homeoStream.startState();
  let delta = 0;
  while (!stream.eol()) {
    stream.start = stream.pos;
    const style = homeoStream.token(stream, state);
    if (stream.pos === stream.start) stream.next(); // never stall
    if (style === null) {
      const c = stream.current();
      if (c === "(" || c === "[") delta++;
      else if (c === ")" || c === "]") delta--;
    }
  }
  return delta;
}

/** The most lines of a definition a hover shows. */
const MAX_DEFINITION_LINES = 12;

/** The `let` and `state` names this file defines, with each one's text: its first line and
 * the lines that continue it (more-indented ones, or any inside open brackets, as in the
 * lexer), without comments. */
export function definitions(docText) {
  const out = Object.create(null); // keyed by names from the file: no `constructor` etc.
  const lines = docText.split("\n");
  for (let i = 0; i < lines.length; i++) {
    const m = /^(let|state)[ \t]+([A-Za-z_]\w*)/.exec(lines[i]);
    if (!m || out[m[2]]) continue;
    // the first line, without the spaces that align `=` with neighbouring lines
    const body = [withoutComment(lines[i]).trimEnd().replace(/^(\S+\s+\w+)\s+=\s*/, "$1 = ").trimEnd()];
    let depth = Math.max(0, bracketDelta(lines[i]));
    for (let j = i + 1; j < lines.length; j++) {
      const text = withoutComment(lines[j]).trimEnd();
      if (!text.trim()) continue; // blank or only a comment
      if (depth === 0 && !/^\s/.test(text)) break; // the next statement
      body.push(text);
      depth = Math.max(0, depth + bracketDelta(text));
    }
    if (body.length > MAX_DEFINITION_LINES) body.splice(MAX_DEFINITION_LINES, Infinity, "    …");
    out[m[2]] = { kind: m[1], line: body.join("\n") };
  }
  return out;
}

/**
 * What to show for the token at `pos` (the character under the pointer). Returns null, or
 * `{ from, to, info }` where `info` is `{ title, kind, text, rows, example, note }`, or has
 * `lookup` (a `let`/`state` name whose value the server knows).
 */
export function describe(doc, pos, hass, now = new Date()) {
  const line = doc.lineAt(pos);
  const tok = tokenAt(line.text, pos - line.from);
  if (!tok) return null;
  const { text, style } = tok;
  const from = line.from + tok.from;
  const to = line.from + tok.to;
  const after = line.text.slice(tok.to);
  const states = hass?.states ?? {};
  const services = hass?.services ?? {};
  const docText = doc.toString();
  const at = (info) => (info ? { from, to, info } : null);
  const fromDocs = (key) => {
    const d = Object.hasOwn(DOCS, key) ? DOCS[key] : null;
    return d ? at({ title: d.sig, kind: d.kind, text: d.text, example: d.example }) : null;
  };

  if (style === "keyword" || style === "builtin" || style === "builtinFunction") {
    return fromDocs(text);
  }
  if (style === "atom") {
    if (text === "on" && /^\s*\(/.test(after)) return fromDocs("on(");
    return text === "keep" ? fromDocs("keep") : null; // `on`, `off`, `true`, `false`: obvious
  }
  if (style === "operator") {
    if (text === "|" && line.text.slice(0, tok.from).trim()) return null; // not a sub-arm
    return fromDocs(text);
  }
  if (style === "entity" || style === "target") {
    let parts = text.split(".");
    const alias = aliases(docText);
    if (alias[parts[0]]) parts = [...alias[parts[0]].split("."), ...parts.slice(1)];
    if (/^\s*\(/.test(after)) {
      if (parts[0] === "bus") {
        return at({
          title: `bus.${parts[1] ?? ""}(…)`,
          kind: "bus event",
          text:
            "The time of the last Home Assistant bus event of this type whose data match the " +
            "filters in parentheses, for integrations without event entities.",
          example: 'bus.zha_event(device_ieee: "…", command: "on")',
        });
      }
      if (parts.length === 2) return at(serviceInfo(parts[0], parts[1], services));
      if (parts.length === 3) {
        return at(serviceInfo(parts[0], parts[2], services, `${parts[0]}.${parts[1]}`));
      }
      return null;
    }
    if (parts.length < 2) return null;
    const info = entityInfo(parts, states, now);
    if (style === "target") info.text = info.text ?? "Driven by this binding.";
    return at(info);
  }
  if (style === "variable" || style === "def") {
    const defs = definitions(docText);
    if (style === "variable" && /^\s*:(?!=)/.test(after)) {
      // a call's argument name: `on(brightness: …)`, `notify.phone(message: …)`
      const statement = statementBefore(doc, from);
      const call = callContext(statement);
      if (call?.callee && call.argName) {
        const options = argumentOptions(call, statement, services, aliases(docText));
        const opt = options.find((o) => o.label === text);
        if (opt) {
          return at({
            title: text,
            kind: opt.detail || `argument of ${call.callee}`,
            text: opt.info || undefined,
            plainText: true,
          });
        }
        return null;
      }
    }
    const def = defs[text];
    if (!def) return null;
    return at({
      title: text,
      kind: def.kind === "state" ? "state machine" : "let",
      example: def.line,
      lookup: text,
    });
  }
  return null;
}

/** Extra rows for builtin names whose value the HA states give: the next solar time. */
function builtinRows(name, states, now) {
  const attr = SOLAR_ATTRS[name];
  const next = attr && states["sun.sun"]?.attributes?.[attr];
  return next ? [["next", fmtInstant(next, now)]] : [];
}

/** The `homeo/value` result (or error) as rows and a note. `stale` says why the running
 * version may differ from the editor: "unsaved" changes, or saved "errors" (the previous
 * version keeps running). */
export function valueRows(result, { stale } = {}) {
  if (result?.error) {
    let note = `No value: ${result.error}`;
    if (result.code === "not_found") {
      note =
        stale === "errors"
          ? "No value: it isn't in the version that's running, because this file has errors."
          : "No value yet: it isn't in the running version of this file. Save to see it.";
    }
    return { rows: [], note };
  }
  const rows = [["value", result.value]];
  if (result.type) rows.push(["type", result.type]);
  if (result.changed) rows.push(["changed", result.changed]);
  const note = {
    unsaved: "From the running version, without your unsaved changes.",
    errors: "From the previous version, which keeps running while this file has errors.",
  }[stale];
  return { rows, note };
}

/** The tooltip's HTML. */
export function hoverHtml(info) {
  const text = info.text ? (info.plainText ? esc(info.text) : prose(info.text)) : "";
  const rows = (info.rows ?? [])
    .map(([k, v]) => `<div class="hover-row"><span class="hover-key">${esc(k)}</span><span class="hover-val">${esc(v)}</span></div>`)
    .join("");
  return [
    `<div class="hover-title"><code>${esc(info.title)}</code>`,
    info.kind ? ` <span class="hover-kind">${esc(info.kind)}</span>` : "",
    "</div>",
    text ? `<div class="hover-text">${text}</div>` : "",
    rows ? `<div class="hover-rows">${rows}</div>` : "",
    info.example ? `<pre class="hover-example"><code>${esc(info.example)}</code></pre>` : "",
    info.note ? `<div class="hover-note">${prose(info.note)}</div>` : "",
  ].join("");
}

/**
 * The hover extension. `getHass()` gives the live HA object; `lookup(name)` resolves to the
 * `homeo/value` result for a `let`/`state` name (or `{ error, code }`); `staleness()` says
 * why its values may not match the editor (see `valueRows`), or null.
 */
export function homeoHover({ getHass, lookup, staleness }) {
  return hoverTooltip(async (view, pos, side) => {
    const ch = side < 0 ? pos - 1 : pos;
    if (ch < 0 || ch >= view.state.doc.length) return null;
    const hass = getHass();
    const found = describe(view.state.doc, ch, hass);
    if (!found) return null;
    const info = { ...found.info };
    if (Object.hasOwn(SOLAR_ATTRS, info.title)) info.rows = builtinRows(info.title, hass?.states ?? {}, new Date());
    if (info.lookup) Object.assign(info, valueRows(await lookup(info.lookup), { stale: staleness() }));
    return {
      pos: found.from,
      end: found.to,
      above: true,
      create() {
        const dom = document.createElement("div");
        dom.className = "cm-homeo-hover";
        dom.innerHTML = hoverHtml(info);
        return { dom };
      },
    };
  }, { hideOnChange: true }); // its content describes the text as it was
}
