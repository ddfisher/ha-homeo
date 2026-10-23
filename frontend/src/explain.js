// The panel's "Explain" pane: which rule is under the cursor, and the homeo/explain result
// rendered as HTML.

const esc = (s) => String(s).replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);

/** The explain target for the statement containing `line` (1-based), or null. */
export function statementTarget(text, line) {
  const lines = text.split("\n");
  for (let i = Math.min(line, lines.length) - 1; i >= 0; i--) {
    const l = lines[i];
    if (!l.trim() || /^\s/.test(l) || l.startsWith("#")) continue; // inside a statement
    let m = l.match(/^([A-Za-z_]\w*\.[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+|\["[^"]*"\])?)\s*:=/);
    if (m) return m[1].replace(/\["([^"]*)"\]$/, ".$1");
    m = l.match(/^(?:state|let)\s+([A-Za-z_]\w*)/);
    if (m) return m[1];
    return null;
  }
  return null;
}

/** Source code as HTML, preferring to wrap just inside an opening bracket. */
function code(s) {
  return esc(s).replace(/[([](?![)\]])/g, "$&<wbr>");
}

/** Text with `backtick` spans shown as code, as in summaries and error messages. */
export function prose(s) {
  return esc(s).replace(/`([^`]+)`/g, "<code>$1</code>");
}

function value(v) {
  if (v === true) return `<span class="v-true">true</span>`;
  if (v === false) return `<span class="v-false">false</span>`;
  if (v === "unknown") return `<span class="v-unknown">unknown</span>`;
  return esc(v);
}

/** Subexpressions and their current values: expression on the left, value on the right. */
function values(vals) {
  if (!vals || !Object.keys(vals).length) return "";
  const rows = Object.entries(vals)
    .map(([k, v]) => `<div class="row"><code class="expr">${code(k)}</code><span class="val">${value(v)}</span></div>`)
    .join("");
  return `<div class="values">${rows}</div>`;
}

/** An arm's source, wrapping before `->` rather than inside it. */
function armText(s) {
  return s.split(" -> ").map(code).join(` <span class="arrow">-&gt;</span>\u00a0`);
}

/** A round true/false/unknown mark, as used for arms and the device check. */
function mark(kind, label) {
  const sym = { yes: "✓", no: "✗", unknown: "?", idle: "·" }[kind];
  return `<span class="mark ${kind}" title="${label}" aria-label="${label}">${sym}</span>`;
}

function arm(a) {
  const isGroup = "guard" in a;
  const cond = "condition" in a ? a.condition : undefined;
  const [kind, state, label] = a.active
    ? ["yes", "active", "decides the result"]
    : cond === false
      ? ["no", "off", "condition is false"]
      : cond === "unknown"
        ? ["unknown", "unknown", "condition is unknown"]
        : ["idle", "idle", "not used"];
  const facts = [];
  if (cond !== undefined) facts.push(["condition", value(cond)]);
  if (a.happened !== undefined) facts.push(["happened", esc(a.happened)]);
  if (a.result !== undefined) facts.push(["result", value(a.result)]);
  const factsHtml = facts.length
    ? `<div class="arm-facts">${facts.map(([k, v]) => `<span class="fact"><span class="k">${k}</span> ${v}</span>`).join("")}</div>`
    : "";
  const sub = isGroup ? `<ul class="arms">${a.arms.map(arm).join("")}</ul>` : "";
  return `<li class="arm ${state}${isGroup ? " group" : ""}">
    <div class="arm-head">${mark(kind, label)}<code>${armText(isGroup ? a.guard : a.arm)}</code><span class="line">line ${a.line}</span></div>
    ${factsHtml}${values(a.values)}${sub}</li>`;
}

const isElse = (a) => !("guard" in a) && !("condition" in a) && !("happened" in a);
const anyActive = (arms) => arms.some((a) => a.active || ("arms" in a && anyActive(a.arms)));

/** When no arm applies and there's no `else`: what happens instead, as an arm of its own.
 * A machine or binding keeps its value; a `let` is unknown. */
function implicitElse(e, arms) {
  if (anyActive(arms) || arms.some(isElse)) return "";
  const result = e.let ? "unknown" : "keep";
  return `<li class="arm active implicit">
    <div class="arm-head">${mark("yes", "no arm applies")}<code>${armText(`else -> ${result}`)}</code><span class="line">implicit</span></div></li>`;
}

/** One explanation (a binding, machine or let) as HTML. */
export function explanationHtml(e) {
  const title = e.binding ?? e.machine ?? e.let;
  const kind = e.binding ? "binding" : e.machine ? "state machine" : "let";
  const where = e.rule ?? e.file;
  // A binding's summary only repeats its result and deciding arm, shown below; keep it when
  // the result is unknown, where it says why.
  const summary = e.binding && e.result !== "unknown" ? null : e.summary;
  const facts = [];
  if ("result" in e) facts.push(["result", `<strong>${value(e.result)}</strong>`]);
  if ("value" in e) facts.push(["value", `<strong>${value(e.value)}</strong>`]);
  if ("definition" in e) facts.push(["definition", `<code>${code(e.definition)}</code>`]);
  if ("changed" in e) facts.push(["changed", esc(e.changed)]);
  if ("state" in e) facts.push(["state", `<strong>${esc(e.state)}</strong>`]);
  if ("since" in e) facts.push(["since", esc(e.since)]);
  if ("previous" in e) facts.push(["previous", esc(e.previous)]);
  if ("device" in e) {
    const match = e.device_matches;
    const check =
      match === true
        ? ` ${mark("yes", "matches the result")}`
        : match === false
          ? ` ${mark("no", "differs from the result")}`
          : "";
    facts.push(["device now", esc(e.device) + check]);
  }
  if ("last_written" in e) facts.push(["last written", esc(e.last_written)]);
  if (e.stopped) facts.push(["stopped", `<span class="error">${esc(e.stopped)}</span>`]);
  const arms = e.arms ?? e.latest;
  const section = (label, html) => `<div class="section-label">${label}</div>${html}`;
  let detail = "";
  if (arms) {
    const how = e.latest ? "the latest to happen wins" : "checked top to bottom";
    const list = arms.map(arm).join("") + implicitElse(e, arms);
    detail = section(`Arms <span class="how">· ${how}</span>`, `<ul class="arms">${list}</ul>`);
  } else if (e.values && Object.keys(e.values).length) {
    detail = section("Values", values(e.values));
  }
  return `<section class="expl">
    <header><code class="name">${esc(title)}</code><span class="kind">${kind}</span>${where ? `<span class="where">${esc(where)}</span>` : ""}</header>
    ${summary ? `<p class="summary">${prose(summary)}</p>` : ""}
    <dl class="facts">${facts.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${v}</dd>`).join("")}</dl>
    ${detail}
  </section>`;
}
