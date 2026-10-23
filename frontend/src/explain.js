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

function value(v) {
  if (v === true) return `<span class="v-true">true</span>`;
  if (v === false) return `<span class="v-false">false</span>`;
  if (v === "unknown") return `<span class="v-unknown">unknown</span>`;
  return esc(v);
}

function values(vals) {
  if (!vals || !Object.keys(vals).length) return "";
  const rows = Object.entries(vals)
    .map(([k, v]) => `<tr><td><code>${esc(k)}</code></td><td>${value(v)}</td></tr>`)
    .join("");
  return `<table class="values">${rows}</table>`;
}

function arm(a) {
  const isGroup = "guard" in a;
  const cond = "condition" in a ? a.condition : undefined;
  const mark = a.active ? "✓" : cond === false ? "✗" : cond === "unknown" ? "?" : "·";
  const cls = ["arm", a.active ? "active" : "", cond === false ? "off" : ""].join(" ");
  const head = `<code>${esc(isGroup ? a.guard : a.arm)}</code>`;
  const facts = [];
  if (cond !== undefined) facts.push(`condition: ${value(cond)}`);
  if (a.happened !== undefined) facts.push(`happened: ${esc(a.happened)}`);
  if (a.result !== undefined) facts.push(`result: ${value(a.result)}`);
  const sub = isGroup ? `<ul class="arms">${a.arms.map(arm).join("")}</ul>` : "";
  return `<li class="${cls}"><div class="arm-head"><span class="mark">${mark}</span>${head}<span class="line">line ${a.line}</span></div>
    ${facts.length ? `<div class="facts">${facts.join(" · ")}</div>` : ""}${values(a.values)}${sub}</li>`;
}

/** One explanation (a binding, machine or let) as HTML. */
export function explanationHtml(e) {
  const title = e.binding ?? e.machine ?? e.let;
  const kind = e.binding ? "binding" : e.machine ? "state machine" : "let";
  const facts = [];
  if (e.rule) facts.push(["rule", e.rule]);
  if (e.file) facts.push(["file", e.file]);
  if ("result" in e) facts.push(["result", e.result]);
  if ("value" in e) facts.push(["value", e.value]);
  if ("definition" in e) facts.push(["definition", e.definition]);
  if ("changed" in e) facts.push(["changed", e.changed]);
  if ("state" in e) facts.push(["state", e.state]);
  if ("since" in e) facts.push(["since", e.since]);
  if ("previous" in e) facts.push(["previous", e.previous]);
  if ("device" in e) facts.push(["device", e.device]);
  if ("device_matches" in e) facts.push(["device matches", e.device_matches]);
  if ("last_written" in e) facts.push(["last written", e.last_written]);
  if (e.stopped) facts.push(["stopped", e.stopped]);
  const arms = e.arms ?? e.latest;
  return `<section class="expl">
    <h3><code>${esc(title)}</code> <span class="kind">${kind}</span></h3>
    ${e.summary ? `<p class="summary">${esc(e.summary)}</p>` : ""}
    <table class="facts-table">${facts.map(([k, v]) => `<tr><th>${esc(k)}</th><td>${value(v)}</td></tr>`).join("")}</table>
    ${arms ? `<ul class="arms">${arms.map(arm).join("")}</ul>` : values(e.values)}
  </section>`;
}
