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
    .map(([k, v]) => `<div class="row"><code class="expr">${esc(k)}</code><span class="val">${value(v)}</span></div>`)
    .join("");
  return `<div class="values">${rows}</div>`;
}

/** An arm's source, wrapping before `->` rather than inside it. */
function armText(s) {
  return s.split(" -> ").map(esc).join(` <span class="arrow">-&gt;</span>\u00a0`);
}

function arm(a) {
  const isGroup = "guard" in a;
  const cond = "condition" in a ? a.condition : undefined;
  const [mark, state, label] = a.active
    ? ["✓", "active", "decides the result"]
    : cond === false
      ? ["✗", "off", "condition is false"]
      : cond === "unknown"
        ? ["?", "unknown", "condition is unknown"]
        : ["·", "idle", "not used"];
  const facts = [];
  if (cond !== undefined) facts.push(["condition", value(cond)]);
  if (a.happened !== undefined) facts.push(["happened", esc(a.happened)]);
  if (a.result !== undefined) facts.push(["result", value(a.result)]);
  const factsHtml = facts.length
    ? `<div class="arm-facts">${facts.map(([k, v]) => `<span class="fact"><span class="k">${k}</span> ${v}</span>`).join("")}</div>`
    : "";
  const sub = isGroup ? `<ul class="arms">${a.arms.map(arm).join("")}</ul>` : "";
  return `<li class="arm ${state}${isGroup ? " group" : ""}">
    <div class="arm-head"><span class="mark" title="${label}">${mark}</span><code>${armText(isGroup ? a.guard : a.arm)}</code><span class="line">line ${a.line}</span></div>
    ${factsHtml}${values(a.values)}${sub}</li>`;
}

/** One explanation (a binding, machine or let) as HTML. */
export function explanationHtml(e) {
  const title = e.binding ?? e.machine ?? e.let;
  const kind = e.binding ? "binding" : e.machine ? "state machine" : "let";
  const where = e.rule ?? e.file;
  const facts = [];
  if ("result" in e) facts.push(["result", `<strong>${value(e.result)}</strong>`]);
  if ("value" in e) facts.push(["value", `<strong>${value(e.value)}</strong>`]);
  if ("definition" in e) facts.push(["definition", `<code>${esc(e.definition)}</code>`]);
  if ("changed" in e) facts.push(["changed", esc(e.changed)]);
  if ("state" in e) facts.push(["state", `<strong>${esc(e.state)}</strong>`]);
  if ("since" in e) facts.push(["since", esc(e.since)]);
  if ("previous" in e) facts.push(["previous", esc(e.previous)]);
  if ("device" in e) {
    const match = e.device_matches;
    const badge =
      match === true
        ? ` <span class="badge ok">matches</span>`
        : match === false
          ? ` <span class="badge warn">differs</span>`
          : "";
    facts.push(["device now", esc(e.device) + badge]);
  }
  if ("last_written" in e) facts.push(["last written", esc(e.last_written)]);
  if (e.stopped) facts.push(["stopped", `<span class="error">${esc(e.stopped)}</span>`]);
  const arms = e.arms ?? e.latest;
  const section = (label, html) => `<div class="section-label">${label}</div>${html}`;
  let detail = "";
  if (arms) {
    const how = e.latest ? "the latest to happen wins" : "the first that holds wins";
    detail = section(`Arms <span class="how">· ${how}</span>`, `<ul class="arms">${arms.map(arm).join("")}</ul>`);
  } else if (e.values && Object.keys(e.values).length) {
    detail = section("Values", values(e.values));
  }
  return `<section class="expl">
    <header><code class="name">${esc(title)}</code><span class="kind">${kind}</span>${where ? `<span class="where">${esc(where)}</span>` : ""}</header>
    ${e.summary ? `<p class="summary">${prose(e.summary)}</p>` : ""}
    <dl class="facts">${facts.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${v}</dd>`).join("")}</dl>
    ${detail}
  </section>`;
}
