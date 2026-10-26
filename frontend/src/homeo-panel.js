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
import { explanationHtml, prose, statementTarget } from "./explain.js";
import { validFileName } from "./files.js";
import { homeoHover } from "./hover.js";
import { homeoHighlight, homeoLanguage } from "./language.js";
import { STYLES } from "./styles.js";

const LAST_FILE_KEY = "homeo-panel-last-file";
const EXPLAIN_OPEN_KEY = "homeo-panel-explain-open";
const DRAFT_KEY = "homeo-panel-draft"; // unsaved edits, kept while you visit other pages
const NEW_FILE = "# Homeo rules: see the language guide in docs/design.md\n\n";

function storage(kind) {
  try {
    return kind === "session" ? sessionStorage : localStorage;
  } catch (_e) {
    return null; // storage may be unavailable
  }
}

function readStored(kind, key) {
  try {
    return JSON.parse(storage(kind)?.getItem(key) ?? "null");
  } catch (_e) {
    return null;
  }
}

function writeStored(kind, key, value) {
  try {
    if (value === null) storage(kind)?.removeItem(key);
    else storage(kind)?.setItem(key, JSON.stringify(value));
  } catch (_e) {
    // storage may be unavailable or full
  }
}

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
    this.version = null; // version of the file on disk the edits started from
    this.saving = false;
    this.conflict = null; // {message, fromSave}: the file changed on disk under unsaved edits
    this.diagnostics = [];
    this.message = "";
    this.themeCompartment = new Compartment();
    this.view = null;
    this.checkSeq = 0;
    this.explainOpen = !!readStored("local", EXPLAIN_OPEN_KEY);
    this.explainedTarget = undefined;
    this.explainSeq = 0;
    this.unsubFiles = null;
    this.onBeforeUnload = (e) => {
      if (this.current && this.dirty) {
        e.preventDefault();
        e.returnValue = "";
      }
    };
  }

  connectedCallback() {
    window.addEventListener("beforeunload", this.onBeforeUnload);
    if (this._hass && !this.unsubFiles) this.subscribeFiles();
  }

  disconnectedCallback() {
    window.removeEventListener("beforeunload", this.onBeforeUnload);
    this.unsubFiles?.then((unsub) => unsub()).catch(() => {});
    this.unsubFiles = null;
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
    $(".explain").addEventListener("click", () => this.setExplainOpen(true, true));
    $(".close").addEventListener("click", () => this.setExplainOpen(false));
    $(".file-select").addEventListener("change", (e) => {
      const value = e.target.value;
      if (value === "__new__") this.newFile();
      else this.open(value);
    });
    this.createEditor();
    this.subscribeFiles();
    this.refreshFiles().then(() => {
      const draft = readStored("session", DRAFT_KEY);
      if (draft?.name) {
        this.restoreDraft(draft);
        return;
      }
      const last = readStored("local", LAST_FILE_KEY);
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
        ".cm-homeo-hover": {
          maxWidth: "min(440px, 90vw)",
          padding: "6px 10px",
          fontFamily: "var(--ha-font-family-body, var(--paper-font-body1_-_font-family, sans-serif))",
          fontSize: "13px",
          lineHeight: "1.45",
        },
        ".cm-homeo-hover code": {
          fontFamily: "var(--ha-font-family-code, var(--code-font-family, monospace))",
          fontSize: "0.95em",
        },
        ".cm-homeo-hover .hover-title code": { fontWeight: "600" },
        ".cm-homeo-hover .hover-kind, .cm-homeo-hover .hover-note, .cm-homeo-hover .hover-key": {
          color: "var(--secondary-text-color)",
        },
        ".cm-homeo-hover .hover-text, .cm-homeo-hover .hover-rows, .cm-homeo-hover .hover-note": {
          marginTop: "4px",
        },
        ".cm-homeo-hover .hover-row": { display: "flex", gap: "1ch" },
        ".cm-homeo-hover .hover-key": { minWidth: "7ch" },
        ".cm-homeo-hover .hover-val": { overflowWrap: "anywhere" },
        ".cm-homeo-hover .hover-example": {
          margin: "6px 0 0",
          padding: "4px 6px",
          whiteSpace: "pre-wrap",
          backgroundColor: "var(--secondary-background-color, rgba(127, 127, 127, 0.1))",
          borderRadius: "4px",
        },
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
        homeoHover({
          getHass: () => this._hass,
          lookup: (name) => this.lookupValue(name),
          staleness: () => this.hoverStaleness(),
        }),
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
          const loading = u.transactions.some((tr) => tr.isUserEvent("homeo.load"));
          if (u.docChanged && !loading) {
            this.message = ""; // a save's outcome no longer applies
            this.saveDraft();
          }
          if (u.docChanged) this.renderDirty();
          if ((u.selectionSet || u.docChanged) && this.explainOpen) this.followCursor();
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

  /** Why the running version of the open file may differ from the editor, for hovers. */
  hoverStaleness() {
    if (this.dirty) return "unsaved";
    return this.files.find((f) => f.name === this.current)?.errors ? "errors" : null;
  }

  /** A `let` or `state` name's value in the running version of the open file, for hovers. */
  async lookupValue(symbol) {
    if (!this.current || this.isNew) return { error: "the file isn't saved yet", code: "not_found" };
    try {
      return await this.ws({ type: "homeo/value", name: this.current, symbol });
    } catch (e) {
      return { error: e?.message ?? String(e), code: e?.code };
    }
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

  /** The integration sends the file list again after every reload, including ones caused by
   * edits made outside the panel. */
  subscribeFiles() {
    this.unsubFiles = this._hass.connection.subscribeMessage(
      (event) => {
        this.files = event.files;
        this.directory = event.directory;
        this.noticeOutsideChange();
        this.render();
      },
      { type: "homeo/files/subscribe" },
    );
    this.unsubFiles.catch(() => {
      this.unsubFiles = null;
    });
  }

  /** The open file changed (or vanished) on disk, not through this panel. */
  async noticeOutsideChange() {
    if (!this.current || this.isNew || this.saving) return;
    const onDisk = this.files.find((f) => f.name === this.current);
    const diskVersion = onDisk?.version ?? null;
    if (diskVersion === this.version) return;
    if (!onDisk) {
      this.isNew = true;
      this.version = null;
      this.message = `${this.current} was deleted outside the editor. Saving creates it again.`;
      this.saveDraft();
      this.render();
      return;
    }
    if (!this.dirty) {
      await this.reloadFromDisk(`Reloaded: ${this.current} was changed outside the editor.`);
      return;
    }
    this.conflict = {
      message: `${this.current} was changed outside the editor, and you have unsaved changes.`,
      fromSave: false,
    };
    this.renderStatus();
  }

  async reloadFromDisk(message) {
    const name = this.current;
    let file;
    try {
      file = await this.ws({ type: "homeo/file/read", name });
    } catch (err) {
      this.message = `Couldn't read ${name}: ${err.message ?? err}`;
      this.renderStatus();
      return;
    }
    if (name !== this.current) return;
    const head = this.view.state.selection.main.head;
    this.load(name, file.text, false, file.version, Math.min(head, file.text.length));
    this.message = message;
    this.renderStatus();
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
    let file;
    try {
      file = await this.ws({ type: "homeo/file/read", name });
    } catch (err) {
      this.message = `Couldn't open ${name}: ${err.message ?? err}`;
      this.render();
      return;
    }
    writeStored("session", DRAFT_KEY, null); // any unsaved edits were discarded above
    this.load(name, file.text, false, file.version);
  }

  load(name, text, isNew, version = null, cursor = 0) {
    this.current = name;
    this.saved = isNew ? "" : text;
    this.isNew = isNew;
    this.version = version;
    this.conflict = null;
    this.diagnostics = [];
    this.message = "";
    this.checkSeq++;
    this.view.dispatch({
      changes: { from: 0, to: this.view.state.doc.length, insert: text },
      selection: { anchor: cursor },
      scrollIntoView: true,
      userEvent: "homeo.load",
    });
    this.view.dispatch(setDiagnostics(this.view.state, []));
    writeStored("local", LAST_FILE_KEY, name);
    this.render();
    this.view.focus();
    if (this.explainOpen) this.explain(true); // the explanation was about another file
  }

  /** Back to the panel with unsaved edits from before (another page, or a reload). */
  async restoreDraft(draft) {
    const onDisk = this.files.find((f) => f.name === draft.name);
    if (draft.isNew || !onDisk) {
      this.load(draft.name, draft.text, true);
    } else {
      let file;
      try {
        file = await this.ws({ type: "homeo/file/read", name: draft.name });
      } catch (_err) {
        writeStored("session", DRAFT_KEY, null);
        this.render();
        return;
      }
      this.load(draft.name, file.text, false, draft.version);
      if (file.text !== draft.text) {
        this.view.dispatch({
          changes: { from: 0, to: this.view.state.doc.length, insert: draft.text },
          userEvent: "homeo.load",
        });
      }
      if (file.version !== draft.version) {
        this.conflict = {
          message: `${draft.name} was changed outside the editor since you started editing it.`,
          fromSave: false,
        };
      }
    }
    this.message = "Restored your unsaved changes.";
    this.render();
  }

  saveDraft() {
    if (!this.current) return;
    writeStored(
      "session",
      DRAFT_KEY,
      this.dirty
        ? { name: this.current, text: this.view.state.doc.toString(), version: this.version, isNew: this.isNew }
        : null,
    );
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
    if (!validFileName(name)) {
      alert("A file name can't contain / or \\ or start with a dot, e.g. bathroom.homeo");
      this.render();
      return;
    }
    if (this.files.some((f) => f.name === name)) {
      this.open(name);
      return;
    }
    this.load(name, NEW_FILE, true);
  }

  async save(force = false) {
    if (!this.current || (!this.dirty && !force)) return;
    const text = this.view.state.doc.toString();
    const name = this.current;
    this.message = "Saving…";
    this.conflict = null;
    this.renderStatus();
    let result;
    this.saving = true;
    try {
      result = await this.ws({ type: "homeo/file/save", name, text, version: this.version, force });
    } catch (err) {
      if (err.code === "conflict") {
        this.message = "";
        this.conflict = { message: `Not saved: ${err.message}.`, fromSave: true };
      } else {
        this.message = `Couldn't save: ${err.message ?? err}`;
      }
      this.renderStatus();
      return;
    } finally {
      this.saving = false;
    }
    if (name !== this.current) return;
    this.saved = text;
    this.isNew = false;
    this.version = result.version;
    writeStored("session", DRAFT_KEY, null);
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
    if (this.explainOpen) this.explain(true); // the running version changed
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
    this.version = null;
    this.conflict = null;
    writeStored("session", DRAFT_KEY, null);
    this.view.dispatch({ changes: { from: 0, to: this.view.state.doc.length, insert: "" }, userEvent: "homeo.load" });
    await this.refreshFiles();
    if (this.files.length) this.open(this.files[0].name);
  }

  // --------------------------------------------------------------- explain

  setExplainOpen(open, refresh = false) {
    this.explainOpen = open;
    writeStored("local", EXPLAIN_OPEN_KEY, open);
    this.shadowRoot.querySelector(".explanation").hidden = !open;
    if (open) this.explain(refresh);
  }

  /** While the pane is open, explain whatever statement the cursor moves to. */
  followCursor() {
    clearTimeout(this.followTimer);
    this.followTimer = setTimeout(() => this.explain(false), 250);
  }

  cursorTarget() {
    const state = this.view.state;
    const line = state.doc.lineAt(state.selection.main.head).number;
    return statementTarget(state.doc.toString(), line);
  }

  async explain(force) {
    const body = this.shadowRoot.querySelector(".explanation-body");
    this.shadowRoot.querySelector(".explanation").hidden = false;
    const target = this.current ? this.cursorTarget() : null;
    if (!force && target === this.explainedTarget) return;
    this.explainedTarget = target;
    const seq = ++this.explainSeq;
    if (!target) {
      body.innerHTML = `<p class="hint">Put the cursor on a binding (<code>x := …</code>), a <code>state</code> or a <code>let</code> to see why it has its value.</p>`;
      return;
    }
    let html;
    try {
      const result = await this.ws({ type: "homeo/explain", target });
      const note = this.dirty
        ? `<p class="hint">This explains the running version of the file, without your unsaved changes.</p>`
        : "";
      html = note + result.explanations.map(explanationHtml).join("");
    } catch (err) {
      html = `<p class="error">${prose(err.message ?? err)}</p>`;
      if (this.dirty || this.isNew) html += `<p class="hint">Save the file to explain your changes.</p>`;
    }
    if (seq === this.explainSeq) body.innerHTML = html;
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
    $(".explanation").hidden = !this.explainOpen;
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
    const conflict = this.conflict
      ? `<div class="conflict"><span>${escapeHtml(this.conflict.message)}</span>
          <button class="load-disk">Load the file from disk</button>
          <button class="keep-mine">${this.conflict.fromSave ? "Overwrite it with mine" : "Keep mine"}</button></div>`
      : "";
    el.innerHTML = `${conflict}<div class="summary">${escapeHtml(summary)}</div>${items ? `<ul>${items}</ul>` : ""}`;
    el.querySelector(".load-disk")?.addEventListener("click", () => {
      if (confirm("Discard your unsaved changes and load the file from disk?")) {
        writeStored("session", DRAFT_KEY, null);
        this.reloadFromDisk("Loaded the version on disk.");
      }
    });
    el.querySelector(".keep-mine")?.addEventListener("click", () => {
      const fromSave = this.conflict.fromSave;
      this.conflict = null;
      if (fromSave) {
        this.save(true);
      } else {
        // Saving will replace the version on disk: take it as the base.
        this.version = this.files.find((f) => f.name === this.current)?.version ?? null;
        this.isNew = this.version === null;
        this.saveDraft();
        this.renderStatus();
      }
    });
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
