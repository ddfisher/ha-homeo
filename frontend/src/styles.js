// Panel styles, using HA's theme variables so light/dark themes apply.

export const STYLES = `
:host {
  display: flex;
  flex-direction: column;
  height: 100vh;
  height: 100dvh;
  background: var(--primary-background-color);
  color: var(--primary-text-color);
  font-family: var(--ha-font-family-body, Roboto, sans-serif);
  -webkit-font-smoothing: antialiased;
}
button {
  font: inherit;
  font-size: 14px;
  border-radius: 4px;
  border: 1px solid var(--divider-color);
  background: var(--card-background-color);
  color: var(--primary-text-color);
  padding: 6px 14px;
  cursor: pointer;
}
button:hover:not(:disabled) { background: var(--secondary-background-color); }
button:disabled { opacity: 0.5; cursor: default; }
button.primary {
  background: var(--primary-color);
  border-color: var(--primary-color);
  color: var(--text-primary-color, #fff);
}
button.primary:hover:not(:disabled) { filter: brightness(1.08); background: var(--primary-color); }
button.link {
  border: none;
  background: none;
  color: var(--primary-color);
  padding: 2px 6px;
}
button:focus-visible { outline: 2px solid var(--primary-color); outline-offset: 1px; }
code { font-family: var(--ha-font-family-code, var(--code-font-family, monospace)); font-size: 0.92em; }

.toolbar {
  display: flex;
  align-items: center;
  gap: 8px;
  height: var(--header-height, 56px);
  padding: 0 12px 0 4px;
  box-sizing: border-box;
  background: var(--app-header-background-color, var(--primary-color));
  color: var(--app-header-text-color, #fff);
  border-bottom: var(--app-header-border-bottom, none);
  flex: none;
}
.toolbar .title { font-size: 20px; margin-left: 12px; }
.toolbar button { border-color: transparent; }
.toolbar button:not(.primary) {
  background: rgba(255, 255, 255, 0.12);
  color: inherit;
}
.toolbar button.primary {
  background: var(--card-background-color);
  color: var(--primary-color);
}
.toolbar button:not(.primary):hover:not(:disabled) { background: rgba(255, 255, 255, 0.22); }
.file-select { display: none; }
.spacer { flex: 1; }

.body { flex: 1; display: flex; min-height: 0; }
nav.files {
  width: 220px;
  flex: none;
  border-right: 1px solid var(--divider-color);
  background: var(--sidebar-background-color, var(--card-background-color));
  display: flex;
  flex-direction: column;
  overflow: auto;
}
nav.files ul { list-style: none; margin: 0; padding: 8px 0; }
nav.files li {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px 8px 16px;
  cursor: pointer;
  font-size: 14px;
}
nav.files li:hover { background: var(--secondary-background-color); }
nav.files li.selected {
  background: rgba(var(--rgb-primary-color, 3, 169, 244), 0.12);
  color: var(--primary-color);
}
nav.files .name { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
nav.files .badge { font-size: 11px; color: var(--secondary-text-color); }
nav.files .dot {
  width: 8px; height: 8px; border-radius: 50%;
  background: var(--disabled-text-color, #bdbdbd);
  flex: none;
}
nav.files li.ok .dot { background: var(--success-color, #43a047); }
nav.files li.has-errors .dot { background: var(--error-color, #db4437); }
nav.files li.has-errors .badge { color: var(--error-color, #db4437); }
nav.files li.new .dot { background: var(--warning-color, #ffa600); }
nav.files .new { margin: 4px 12px 12px; }

main { flex: 1; display: flex; flex-direction: column; min-width: 0; }
.file-bar {
  display: flex; align-items: center; gap: 8px;
  padding: 6px 12px;
  font-size: 14px;
  border-bottom: 1px solid var(--divider-color);
  background: var(--card-background-color);
}
.file-name { font-weight: 500; }
.dirty {
  font-size: 12px;
  color: var(--warning-color, #ffa600);
  border: 1px solid currentColor;
  border-radius: 10px;
  padding: 0 8px;
}
.editor { flex: 1; min-height: 0; overflow: hidden; }
.editor.empty { visibility: hidden; }
.editor .cm-editor { height: 100%; }
.editor .cm-editor.cm-focused { outline: none; }

.status {
  flex: none;
  max-height: 30%;
  overflow: auto;
  border-top: 1px solid var(--divider-color);
  background: var(--card-background-color);
  font-size: 13px;
}
.status .summary { padding: 6px 12px; color: var(--secondary-text-color); }
.status ul { list-style: none; margin: 0; padding: 0 0 6px; }
.status li { padding: 3px 12px; cursor: pointer; }
.status li:hover { background: var(--secondary-background-color); }
.status li.error::before { content: "●"; color: var(--error-color, #db4437); margin-right: 6px; }
.status li.warning::before { content: "●"; color: var(--warning-color, #ffa600); margin-right: 6px; }
.status .where { color: var(--secondary-text-color); font-variant-numeric: tabular-nums; }
.hint { color: var(--secondary-text-color); }
.error { color: var(--error-color, #db4437); }

aside.explanation {
  width: 380px;
  flex: none;
  border-left: 1px solid var(--divider-color);
  background: var(--card-background-color);
  display: flex;
  flex-direction: column;
  min-height: 0;
}
aside.explanation[hidden] { display: none; }
.aside-bar {
  display: flex; align-items: center; justify-content: space-between;
  padding: 6px 6px 6px 12px;
  border-bottom: 1px solid var(--divider-color);
  font-weight: 500; font-size: 14px;
}
.explanation-body { overflow: auto; padding: 0 12px 12px; font-size: 13px; }
.expl h3 { font-size: 15px; font-weight: 500; margin: 14px 0 6px; }
.expl .kind { font-size: 12px; font-weight: normal; color: var(--secondary-text-color); margin-left: 6px; }
.expl .summary { margin: 4px 0 10px; line-height: 1.4; }
.facts-table, .values { border-collapse: collapse; width: 100%; }
.facts-table th {
  text-align: left; font-weight: normal; color: var(--secondary-text-color);
  padding: 2px 10px 2px 0; white-space: nowrap; vertical-align: top;
}
.facts-table td, .values td { padding: 2px 0; vertical-align: top; overflow-wrap: anywhere; }
.values { margin: 4px 0 2px 22px; width: calc(100% - 22px); }
.values td:first-child { padding-right: 10px; color: var(--secondary-text-color); }
ul.arms { list-style: none; margin: 10px 0 0; padding: 0; }
ul.arms ul.arms { margin: 6px 0 0 18px; }
li.arm { padding: 6px 0; border-top: 1px solid var(--divider-color); }
ul.arms ul.arms li.arm { border-top: none; padding: 4px 0; }
li.arm.off > .arm-head code { opacity: 0.6; }
li.arm.active > .arm-head { color: var(--primary-color); font-weight: 500; }
.arm-head { display: flex; gap: 6px; align-items: baseline; }
.arm-head code { flex: 1; overflow-wrap: anywhere; }
.mark { width: 16px; flex: none; text-align: center; }
.line { font-size: 11px; color: var(--secondary-text-color); white-space: nowrap; }
.facts { margin: 2px 0 0 22px; color: var(--secondary-text-color); }
.v-true { color: var(--success-color, #43a047); }
.v-false { color: var(--secondary-text-color); }
.v-unknown { color: var(--warning-color, #ffa600); }

:host([narrow]) nav.files { display: none; }
:host([narrow]) .file-select {
  display: block;
  max-width: 40vw;
  font: inherit;
  background: rgba(255, 255, 255, 0.12);
  color: inherit;
  border: none;
  border-radius: 4px;
  padding: 4px;
}
:host([narrow]) .file-select option { color: var(--primary-text-color); background: var(--card-background-color); }
:host([narrow]) .toolbar .title { display: none; }
:host([narrow]) aside.explanation {
  position: fixed; inset: var(--header-height, 56px) 0 0 0; width: auto; z-index: 2;
}
`;
