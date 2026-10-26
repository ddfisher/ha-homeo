// Syntax highlighting for .homeo files. Mirrors the token rules of core/lexer.py (I9).

import { HighlightStyle, StreamLanguage, syntaxHighlighting } from "@codemirror/language";
import { tags as t } from "@lezer/highlight";

export const KEYWORDS = [
  "let", "state", "on", "off", "true", "false", "and", "or", "not", "if", "else", "in", "for",
  "keep", "latest", "every",
];
const CONTROL = new Set(["let", "state", "if", "else", "for", "latest", "every", "and", "or", "not", "in"]);
const ATOMS = new Set(["on", "off", "true", "false", "keep"]);
export const FUNCTIONS = ["age", "fresh", "previous", "toggle", "count", "manual", "available"];
export const BUILTINS = [
  "now", "today", "sunrise", "sunset", "dawn", "dusk",
  "mon", "tue", "wed", "thu", "fri", "sat", "sun", "weekday", "weekend",
];
const FUNCTION_SET = new Set(FUNCTIONS);
const BUILTIN_SET = new Set(BUILTINS);

const TIME = /^\d{1,2}:\d{2}(?::\d{2})?(?![\d:])/;
const DURATION = /^(?:\d+(?:\.\d+)?(?:min|s|h|d)(?![A-Za-z_]))+/;
const PERCENT = /^\d+(?:\.\d+)?%/;
const NUMBER = /^\d+(?:\.\d+)?/;
const OPERATORS = /^(?::=|->|\.\.|\?\?|==|!=|<=|>=|[<>+\-*/=|])/;

export const homeoStream = {
  name: "homeo",
  startState: () => ({ afterDefKeyword: false, lineStart: true }),
  token(stream, state) {
    if (stream.sol()) state.lineStart = true;
    if (stream.eatSpace()) return null;
    const lineStart = state.lineStart;
    state.lineStart = false;

    if (stream.peek() === "#") {
      stream.skipToEnd();
      return /^#\s*mode\s*:/.test(stream.current()) ? "meta" : "comment";
    }
    const quote = stream.peek();
    if (quote === '"' || quote === "'") {
      stream.next();
      let escaped = false;
      let ch;
      while ((ch = stream.next()) != null) {
        if (ch === quote && !escaped) break;
        escaped = !escaped && ch === "\\";
      }
      return "string";
    }
    if (stream.match(TIME) || stream.match(DURATION) || stream.match(PERCENT)) return "number";
    if (stream.match(NUMBER)) return "number";

    const ident = stream.match(/^[A-Za-z_][A-Za-z0-9_]*/);
    if (ident) {
      const word = ident[0];
      if (state.afterDefKeyword) {
        state.afterDefKeyword = false;
        return "def";
      }
      if (word === "let" || word === "state") {
        state.afterDefKeyword = true;
        return "keyword";
      }
      // `on x:` at the start of a line is an action; `on`/`on(...)` elsewhere is a value.
      if (word === "on" && lineStart && !/^\s*\(/.test(stream.string.slice(stream.pos))) {
        return "keyword";
      }
      if (CONTROL.has(word)) return "keyword";
      if (ATOMS.has(word)) return "atom";
      // A dotted name: an entity id (`sensor.co2`), maybe with an attribute or event type.
      // (`mon..fri` and `sunset..23:00` are ranges, not dotted names)
      if (stream.peek() === "." && stream.match(/^(?:\.[A-Za-z0-9_]+)+/)) {
        return lineStart && /^\s*:=/.test(stream.string.slice(stream.pos)) ? "target" : "entity";
      }
      if (FUNCTION_SET.has(word) && stream.peek() === "(") return "builtinFunction";
      if (BUILTIN_SET.has(word)) return "builtin";
      return "variable";
    }
    if (stream.match(OPERATORS)) return "operator";
    stream.next();
    return null;
  },
  languageData: {
    commentTokens: { line: "#" },
    indentOnInput: /^\s*\|$/,
  },
  tokenTable: {
    def: t.definition(t.variableName),
    entity: t.special(t.variableName),
    target: t.definition(t.special(t.variableName)),
    builtinFunction: t.function(t.standard(t.variableName)),
    builtin: t.standard(t.variableName),
    atom: t.atom,
    meta: t.meta,
  },
};

export const homeoLanguage = StreamLanguage.define(homeoStream);

const v = (name, fallback) => `var(--codemirror-${name}, ${fallback})`;

// Colours from HA's own code editor: its theme variables (set in dark mode) with the same
// light-mode fallbacks it uses, so themes and dark mode apply.
export const homeoHighlight = syntaxHighlighting(
  HighlightStyle.define([
    { tag: t.comment, color: v("comment", "#777"), fontStyle: "italic" },
    { tag: t.meta, color: v("meta", "#555") },
    { tag: t.keyword, color: v("keyword", "#6262FF") },
    { tag: t.atom, color: v("atom", "#F90") },
    { tag: t.number, color: v("number", "#ca7841") },
    { tag: t.string, color: v("string", "#07a") },
    { tag: t.operator, color: v("operator", "#cda869") },
    { tag: t.definition(t.variableName), color: v("def", "#2a5db0") },
    { tag: t.special(t.variableName), color: v("property", "#905") },
    { tag: t.definition(t.special(t.variableName)), color: v("property", "#905"), fontWeight: "bold" },
    { tag: t.function(t.standard(t.variableName)), color: v("qualifier", "#690") },
    { tag: t.standard(t.variableName), color: v("qualifier", "#690") },
  ]),
);
