// The Homeo sidebar panel (§19): edit .homeo files with highlighting, inline errors (compiled by
// the integration as you type, without loading), save (which reloads), and explanations.

import { autocompletion, closeBrackets, closeBracketsKeymap, completionKeymap } from "@codemirror/autocomplete";
import { defaultKeymap, history, historyKeymap, indentWithTab } from "@codemirror/commands";
import { bracketMatching, indentOnInput, indentUnit } from "@codemirror/language";
import { lintGutter, linter, lintKeymap, setDiagnostics } from "@codemirror/lint";
import { highlightSelectionMatches, searchKeymap } from "@codemirror/search";
import { Compartment, EditorState } from "@codemirror/state";
import {
  EditorView,
  drawSelection,
  highlightActiveLine,
  highlightActiveLineGutter,
  keymap,
  lineNumbers,
} from "@codemirror/view";

import { homeoCompletions } from "./complete.js";
import { diagnosticsToCodeMirror } from "./diagnostics.js";
import { explanationHtml, statementTarget } from "./explain.js";
import { homeoHighlight, homeoLanguage } from "./language.js";
import { STYLES } from "./styles.js";

const FILE_NAME = /^[A-Za-z0-9_][A-Za-z0-9_-]*\.homeo$/;
const LAST_FILE_KEY = "homeo-panel-last-file";
const NEW_FILE = "# Homeo rules: see the language guide in docs/design.md\n\n";

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);
}

class HomeoPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._hass = null;
    this._narrow = false;
    this.files = [];
    this.current = null; // name of the open file
    this.saved = ""; // its text as last read or saved
    this.isNew = false; // not on disk yet
    this.diagnostics = [];
    this.message = "";
    this.themeCompartment = new Compartment();
    this.view = null;
    this.checkSeq = 0;
  }

  // ------------------------------------------------------------ HA properties

  set hass(hass) {
    const first = !this._hass;
    const darkChanged = this._hass?.themes?.darkMode !== hass?.themes?.darkMode;
    this._hass = hass;
    if (first) this.init();
    else if (darkChanged && this.view) {
      this.view.dispatch({ effects: this.themeCompartment.reconfigure(this.theme()) });
    }
    const menu = this.shadowRoot.querySelector("ha-menu-button");
    if (menu) menu.hass = hass;
  }

  get hass() {
    return this._hass;
  }

  set narrow(narrow) {
    this._narrow = narrow;
    this.toggleAttribute("narrow", !!narrow);
    const menu = this.shadowRoot.querySelector("ha-menu-button");
    if (menu) menu.narrow = narrow;
  }

  get narrow() {
    return this._narrow;
  }

  // ------------------------------------------------------------------ set-up

  init() {
    this.shadowRoot.innerHTML = `
      <style>${STYLES}</style>
      <div class="toolbar">
        <ha-menu-button></ha-menu-button>
        <div class="title">Homeo</div>
        <select class="file-select" aria-label="File"></select>
        <div class="spacer"></div>
        <button class="explain" title="Why does the rule under the cursor do what it does?">Explain</button>
        <button class="save primary" title="Save (Ctrl+S)" disabled>Save</button>
      </div>
      <div class="body">
        <nav class="files">
          <ul></ul>
          <button class="new">New file</button>
        </nav>
        <main>
          <div class="file-bar">
            <span class="file-name"></span>
            <span class="dirty" hidden>unsaved</span>
            <span class="spacer"></span>
            <button class="delete link" hidden>Delete file</button>
          </div>
          <div class="editor"></div>
          <div class="status" role="status"></div>
        </main>
        <aside class="explanation" hidden>
          <div class="aside-bar"><span>Explanation</span><button class="close link" aria-label="Close">✕</button></div>
          <div class="explanation-body"></div>
        </aside>
      </div>`;
    const $ = (sel) => this.shadowRoot.querySelector(sel);
    const menu = $("ha-menu-button");
    menu.hass = this._hass;
    menu.narrow = this._narrow;
    $(".save").addEventListener("click", () => this.save());
    $(".new").addEventListener("click", () => this.newFile());
    $(".delete").addEventListener("click", () => this.deleteFile());
    $(".explain").addEventListener("click", () => this.explain());
    $(".close").addEventListener("click", () => ($(".explanation").hidden = true));
    $(".file-select").addEventListener("change", (e) => {
      const value = e.target.value;
      if (value === "__new__") this.newFile();
      else this.open(value);
    });
    this.createEditor();
    this.refreshFiles().then(() => {
      let last = null;
      try {
        last = localStorage.getItem(LAST_FILE_KEY);
      } catch (_e) {
        // storage may be unavailable
      }
      const name = this.files.some((f) => f.name === last) ? last : this.files[0]?.name;
      if (name) this.open(name);
      else this.render();
    });
  }

  theme() {
    return EditorView.theme(
      {
        "&": {
          height: "100%",
          color: "var(--primary-text-color)",
          backgroundColor: "var(--code-editor-background-color, var(--card-background-color))",
        },
        ".cm-scroller": {
          fontFamily: "var(--ha-font-family-code, var(--code-font-family, monospace))",
          lineHeight: "1.5",
        },
        ".cm-content": { caretColor: "var(--primary-text-color)" },
        ".cm-cursor, .cm-dropCursor": { borderLeftColor: "var(--primary-text-color)" },
        ".cm-gutters": {
          backgroundColor: "var(--code-editor-gutter-color, var(--secondary-background-color, whitesmoke))",
          color: "var(--secondary-text-color)",
          border: "none",
        },
        ".cm-activeLine": { backgroundColor: "rgba(var(--rgb-primary-color, 3, 169, 244), 0.06)" },
        ".cm-activeLineGutter": { backgroundColor: "rgba(var(--rgb-primary-color, 3, 169, 244), 0.12)" },
        "&.cm-focused .cm-selectionBackground, .cm-selectionBackground, ::selection": {
          backgroundColor: "rgba(var(--rgb-primary-color, 3, 169, 244), 0.25)",
        },
        ".cm-tooltip": {
          backgroundColor: "var(--card-background-color)",
          color: "var(--primary-text-color)",
          border: "1px solid var(--divider-color)",
        },
        ".cm-diagnostic": { whiteSpace: "pre-wrap" },
      },
      { dark: !!this._hass?.themes?.darkMode },
    );
  }

  createEditor() {
    const check = linter((view) => this.check(view), { delay: 400 });
    const state = EditorState.create({
      doc: "",
      extensions: [
        lineNumbers(),
        highlightActiveLineGutter(),
        history(),
        drawSelection(),
        indentOnInput(),
        indentUnit.of("    "),
        EditorState.tabSize.of(4),
        bracketMatching(),
        closeBrackets(),
        highlightActiveLine(),
        highlightSelectionMatches(),
        homeoLanguage,
        homeoHighlight,
        autocompletion({ override: [homeoCompletions(() => this._hass)] }),
        check,
        lintGutter(),
        this.themeCompartment.of(this.theme()),
        keymap.of([
          { key: "Mod-s", preventDefault: true, run: () => (this.save(), true) },
          ...closeBracketsKeymap,
          ...defaultKeymap,
          ...searchKeymap,
          ...historyKeymap,
          ...completionKeymap,
          ...lintKeymap,
          indentWithTab,
        ]),
        EditorView.updateListener.of((u) => {
          if (u.docChanged && !u.transactions.some((tr) => tr.isUserEvent("homeo.load"))) {
            this.message = ""; // a save's outcome no longer applies
          }
          if (u.docChanged) this.renderDirty();
        }),
      ],
    });
    this.view = new EditorView({
      state,
      parent: this.shadowRoot.querySelector(".editor"),
      root: this.shadowRoot,
    });
    // HA's global keyboard shortcuts (`e`, `c`, `a`, `m`, …) don't recognise an editable area
    // inside a shadow root as a text field, so keep typing from reaching them.
    for (const type of ["keydown", "keypress", "keyup"]) {
      this.view.dom.addEventListener(type, (e) => e.stopPropagation());
    }
  }

  // ---------------------------------------------------------------- server

  ws(msg) {
    return this._hass.callWS(msg);
  }

  async refreshFiles() {
    try {
      const result = await this.ws({ type: "homeo/files" });
      this.files = result.files;
      this.directory = result.directory;
    } catch (err) {
      this.message = `Couldn't list the rule files: ${err.message ?? err}`;
    }
    this.render();
  }

  async check(view) {
    if (!this.current) return [];
    const text = view.state.doc.toString();
    const seq = ++this.checkSeq;
    let result;
    try {
      result = await this.ws({ type: "homeo/file/check", name: this.current, text });
    } catch (_err) {
      return [];
    }
    if (seq !== this.checkSeq) return []; // a newer check is on its way
    this.diagnostics = result.diagnostics;
    this.renderStatus();
    return diagnosticsToCodeMirror(view.state.doc, result.diagnostics);
  }

  // ----------------------------------------------------------------- files

  get dirty() {
    return this.isNew || (this.view && this.view.state.doc.toString() !== this.saved);
  }

  confirmDiscard() {
    return !this.dirty || confirm(`Discard the unsaved changes to ${this.current}?`);
  }

  async open(name) {
    if (name === this.current) return;
    if (!this.confirmDiscard()) {
      this.render();
      return;
    }
    let text;
    try {
      text = (await this.ws({ type: "homeo/file/read", name })).text;
    } catch (err) {
      this.message = `Couldn't open ${name}: ${err.message ?? err}`;
      this.render();
      return;
    }
    this.load(name, text, false);
  }

  load(name, text, isNew) {
    this.current = name;
    this.saved = isNew ? "" : text;
    this.isNew = isNew;
    this.diagnostics = [];
    this.message = "";
    this.checkSeq++;
    this.view.dispatch({
      changes: { from: 0, to: this.view.state.doc.length, insert: text },
      selection: { anchor: 0 },
      scrollIntoView: true,
      userEvent: "homeo.load",
    });
    this.view.dispatch(setDiagnostics(this.view.state, []));
    this.shadowRoot.querySelector(".explanation").hidden = true; // it was about another file
    try {
      localStorage.setItem(LAST_FILE_KEY, name);
    } catch (_e) {
      // storage may be unavailable
    }
    this.render();
    this.view.focus();
  }

  newFile() {
    if (!this.confirmDiscard()) {
      this.render();
      return;
    }
    let name = prompt("Name of the new rule file (e.g. bathroom):", "");
    if (!name) {
      this.render();
      return;
    }
    name = name.trim();
    if (!name.endsWith(".homeo")) name += ".homeo";
    if (!FILE_NAME.test(name)) {
      alert("Use letters, digits, _ and - only, e.g. bathroom.homeo");
      this.render();
      return;
    }
    if (this.files.some((f) => f.name === name)) {
      this.open(name);
      return;
    }
    this.load(name, NEW_FILE, true);
  }

  async save() {
    if (!this.current || !this.dirty) return;
    const text = this.view.state.doc.toString();
    const name = this.current;
    this.message = "Saving…";
    this.renderStatus();
    let result;
    try {
      result = await this.ws({ type: "homeo/file/save", name, text });
    } catch (err) {
      this.message = `Couldn't save: ${err.message ?? err}`;
      this.renderStatus();
      return;
    }
    if (name !== this.current) return;
    this.saved = text;
    this.isNew = false;
    this.diagnostics = result.diagnostics;
    this.message = result.loaded
      ? "Saved and running."
      : "Saved, but the file has errors: its previous version (if any) keeps running.";
    if (!result.loaded) {
      this.view.dispatch(
        setDiagnostics(this.view.state, diagnosticsToCodeMirror(this.view.state.doc, result.diagnostics)),
      );
    }
    await this.refreshFiles();
  }

  async deleteFile() {
    const name = this.current;
    if (!name) return;
    if (!confirm(`Delete ${name}? Its rules stop running.`)) return;
    if (!this.isNew) {
      try {
        await this.ws({ type: "homeo/file/delete", name });
      } catch (err) {
        this.message = `Couldn't delete: ${err.message ?? err}`;
        this.renderStatus();
        return;
      }
    }
    this.current = null;
    this.isNew = false;
    this.saved = "";
    this.view.dispatch({ changes: { from: 0, to: this.view.state.doc.length, insert: "" } });
    await this.refreshFiles();
    if (this.files.length) this.open(this.files[0].name);
  }

  // --------------------------------------------------------------- explain

  async explain() {
    const aside = this.shadowRoot.querySelector(".explanation");
    const body = this.shadowRoot.querySelector(".explanation-body");
    aside.hidden = false;
    const state = this.view.state;
    const line = state.doc.lineAt(state.selection.main.head).number;
    const target = statementTarget(state.doc.toString(), line);
    if (!target) {
      body.innerHTML = `<p class="hint">Put the cursor on a binding (<code>x := …</code>), a <code>state</code> or a <code>let</code>, then press Explain.</p>`;
      return;
    }
    body.innerHTML = `<p class="hint">Explaining <code>${escapeHtml(target)}</code>…</p>`;
    try {
      const result = await this.ws({ type: "homeo/explain", target });
      const note = this.dirty
        ? `<p class="hint">This explains the running version of the file, without your unsaved changes.</p>`
        : "";
      body.innerHTML = note + result.explanations.map(explanationHtml).join("");
    } catch (err) {
      body.innerHTML = `<p class="error">${escapeHtml(err.message ?? err)}</p>`;
    }
  }

  // ---------------------------------------------------------------- render

  render() {
    const $ = (sel) => this.shadowRoot.querySelector(sel);
    const status = (f) =>
      f.errors ? `${f.errors} error${f.errors > 1 ? "s" : ""}` : f.loaded ? "running" : "not loaded";
    const names = this.files.map((f) => f.name);
    if (this.current && !names.includes(this.current)) names.push(this.current);
    names.sort();
    $(".files ul").innerHTML = names
      .map((name) => {
        const f = this.files.find((x) => x.name === name);
        const cls = !f ? "new" : f.errors ? "has-errors" : f.loaded ? "ok" : "";
        const label = f ? status(f) : "new";
        return `<li class="${cls}${name === this.current ? " selected" : ""}" data-name="${escapeHtml(name)}">
            <span class="dot"></span><span class="name">${escapeHtml(name)}</span>
            <span class="badge">${escapeHtml(label)}</span></li>`;
      })
      .join("");
    for (const li of this.shadowRoot.querySelectorAll(".files li")) {
      li.addEventListener("click", () => this.open(li.dataset.name));
    }
    $(".file-select").innerHTML =
      names
        .map(
          (name) =>
            `<option value="${escapeHtml(name)}"${name === this.current ? " selected" : ""}>${escapeHtml(name)}</option>`,
        )
        .join("") + `<option value="__new__">New file…</option>`;
    $(".file-name").textContent = this.current ?? "";
    $(".delete").hidden = !this.current;
    this.shadowRoot.querySelector(".editor").classList.toggle("empty", !this.current);
    if (!this.current && !this.message) {
      this.message = this.files.length
        ? "Choose a file."
        : `No rule files yet in ${this.directory ?? "the homeo folder"}. Create one with "New file".`;
    }
    this.renderDirty();
    this.renderStatus();
  }

  renderDirty() {
    const dirty = !!this.current && this.dirty;
    this.shadowRoot.querySelector(".dirty").hidden = !dirty;
    this.shadowRoot.querySelector(".save").disabled = !dirty;
  }

  renderStatus() {
    const el = this.shadowRoot.querySelector(".status");
    const errors = this.diagnostics.filter((d) => d.severity === "error");
    const warnings = this.diagnostics.filter((d) => d.severity !== "error");
    const items = this.diagnostics
      .map(
        (d, i) =>
          `<li class="${d.severity}" data-i="${i}"><span class="where">${d.line}:${d.col}</span> ${escapeHtml(d.message)}${
            d.hint ? ` <span class="hint">(${escapeHtml(d.hint)})</span>` : ""
          }</li>`,
      )
      .join("");
    let summary = this.message;
    if (this.current && !summary) {
      summary = errors.length
        ? `${errors.length} error${errors.length > 1 ? "s" : ""}`
        : warnings.length
          ? `${warnings.length} warning${warnings.length > 1 ? "s" : ""}`
          : "No problems";
    }
    el.innerHTML = `<div class="summary">${escapeHtml(summary)}</div>${items ? `<ul>${items}</ul>` : ""}`;
    for (const li of el.querySelectorAll("li")) {
      li.addEventListener("click", () => {
        const d = this.diagnostics[Number(li.dataset.i)];
        const [cm] = diagnosticsToCodeMirror(this.view.state.doc, [d]);
        this.view.dispatch({ selection: { anchor: cm.from }, scrollIntoView: true });
        this.view.focus();
      });
    }
  }
}

if (!customElements.get("homeo-panel")) customElements.define("homeo-panel", HomeoPanel);
