"""Lexer for .homeo files (I9).

Produces a flat token list. There are no NEWLINE/INDENT tokens: instead, the first token of
each physical line is marked `first_on_line` and carries the line's indentation. The parser uses
that to implement layout (blocks, arms, continuation lines; I10). Inside brackets, line breaks
don't count, so tokens there are never `first_on_line`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .errors import CompileError, Diagnostic, Span

KEYWORDS = frozenset(
    {
        "let",
        "state",
        "on",
        "off",
        "true",
        "false",
        "and",
        "or",
        "not",
        "if",
        "else",
        "in",
        "for",
        "within",
        "keep",
        "latest",
        "every",
    }
)

# Longest first, so `:=` wins over `:` and `->` over `-`.
OPERATORS = (
    ":=",
    "->",
    "..",
    "??",
    "==",
    "!=",
    "<=",
    ">=",
    "<",
    ">",
    "+",
    "-",
    "*",
    "/",
    "(",
    ")",
    "[",
    "]",
    ",",
    ":",
    ".",
    "|",
    "=",
)

DURATION_UNITS = {"s": 1.0, "min": 60.0, "h": 3600.0, "d": 86400.0}

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_SEGMENT = re.compile(r"[A-Za-z0-9_]+")  # after a dot: object ids may start with a digit
_NUMBER = re.compile(r"\d+(?:\.\d+)?")
_TIME = re.compile(r"(\d{1,2}):(\d{2})(?::(\d{2}))?")
_UNIT = re.compile(r"[A-Za-z]+")
_DIRECTIVE = re.compile(r"#\s*mode\s*:\s*([A-Za-z_]+)\s*$")


@dataclass(slots=True)
class Token:
    kind: str  # NAME, KEYWORD, NUMBER, PERCENT, DURATION, TIME, STRING, OP, EOF
    value: Any
    span: Span
    first_on_line: bool = False
    indent: int = 0
    text: str = ""

    def is_op(self, *ops: str) -> bool:
        return self.kind == "OP" and self.value in ops

    def is_kw(self, *kws: str) -> bool:
        return self.kind == "KEYWORD" and self.value in kws

    def __repr__(self) -> str:  # compact, for test failure output
        return f"{self.kind}({self.value!r})@{self.span.line}:{self.span.col}"


@dataclass(slots=True)
class LexResult:
    tokens: list[Token]
    directives: dict[str, tuple[str, Span]]


class Lexer:
    def __init__(self, source: str, file: str = "<rules>"):
        self.src = source
        self.file = file
        self.tokens: list[Token] = []
        self.directives: dict[str, tuple[str, Span]] = {}
        self.depth = 0  # bracket nesting
        self.brackets: list[Token] = []

    def error(self, message: str, line: int, col: int, width: int = 1, hint: str | None = None):
        raise CompileError(
            [Diagnostic(message, Span(line, col, line, col + width), hint, "error", self.file)]
        )

    def run(self) -> LexResult:
        lines = self.src.split("\n")
        seen_statement = False
        for lineno, raw in enumerate(lines, start=1):
            line = raw.rstrip("\r")
            stripped = line.lstrip(" \t")
            indent_text = line[: len(line) - len(stripped)]
            if not stripped or stripped.startswith("#"):
                if stripped.startswith("#") and not seen_statement and self.depth == 0:
                    m = _DIRECTIVE.match(stripped)
                    if m:
                        col = len(indent_text) + 1
                        self.directives["mode"] = (
                            m.group(1),
                            Span(lineno, col, lineno, col + len(stripped)),
                        )
                continue
            if "\t" in indent_text and self.depth == 0:
                self.error(
                    "tab in indentation",
                    lineno,
                    indent_text.index("\t") + 1,
                    hint="indent with spaces",
                )
            seen_statement = True
            self.lex_line(line, lineno, len(indent_text))
        if self.brackets:
            open_tok = self.brackets[-1]
            raise CompileError(
                [
                    Diagnostic(
                        f"unclosed `{open_tok.value}`", open_tok.span, None, "error", self.file
                    )
                ]
            )
        end = Span(len(lines) + 1, 1, len(lines) + 1, 2)
        self.tokens.append(Token("EOF", None, end, True, 0))
        return LexResult(self.tokens, self.directives)

    def lex_line(self, line: str, lineno: int, indent: int) -> None:
        i = indent
        first = self.depth == 0
        n = len(line)
        while i < n:
            c = line[i]
            if c in " \t":
                i += 1
                continue
            if c == "#":
                break
            col = i + 1
            start = i
            prev = self.tokens[-1] if self.tokens else None
            after_dot = (
                prev is not None
                and prev.is_op(".")
                and prev.span.line == lineno
                and prev.span.end_col == col
            )
            if after_dot and (m := _SEGMENT.match(line, i)):
                text = m.group(0)
                tok = Token("NAME", text, Span(lineno, col, lineno, col + len(text)))
                i = m.end()
            elif c.isalpha() or c == "_":
                m = _IDENT.match(line, i)
                assert m
                text = m.group(0)
                kind = "KEYWORD" if text in KEYWORDS else "NAME"
                tok = Token(kind, text, Span(lineno, col, lineno, col + len(text)))
                i = m.end()
            elif c.isdigit():
                tok, i = self.lex_number(line, i, lineno)
            elif c in "\"'":
                tok, i = self.lex_string(line, i, lineno)
            else:
                for op in OPERATORS:
                    if line.startswith(op, i):
                        break
                else:
                    self.error(f"unexpected character `{c}`", lineno, col)
                tok = Token("OP", op, Span(lineno, col, lineno, col + len(op)))
                i += len(op)
                if op in "([":
                    self.depth += 1
                    self.brackets.append(tok)
                elif op in ")]":
                    if not self.brackets:
                        self.error(f"unmatched `{op}`", lineno, col)
                    opener = self.brackets.pop()
                    if (opener.value, op) not in (("(", ")"), ("[", "]")):
                        self.error(f"`{op}` doesn't match `{opener.value}`", lineno, col)
                    self.depth -= 1
            tok.text = line[start:i]
            if first:
                tok.first_on_line = True
                tok.indent = indent
                first = False
            self.tokens.append(tok)

    def lex_number(self, line: str, i: int, lineno: int) -> tuple[Token, int]:
        col = i + 1
        m = _TIME.match(line, i)
        if m and not (m.end() < len(line) and (line[m.end()].isalnum())):
            h, mi, s = int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)
            if h > 23 or mi > 59 or s > 59:
                self.error("invalid time of day", lineno, col, m.end() - i)
            return (
                Token("TIME", h * 3600 + mi * 60 + s, Span(lineno, col, lineno, m.end() + 1)),
                m.end(),
            )
        m = _NUMBER.match(line, i)
        assert m
        j = m.end()
        num_text = m.group(0)
        value: float = float(num_text) if "." in num_text else int(num_text)
        if j < len(line) and line[j] == "%":
            return Token("PERCENT", value, Span(lineno, col, lineno, j + 2)), j + 1
        if j < len(line) and line[j].isalpha():
            # Duration, possibly compound: 1h30min
            total = 0.0
            last_unit_rank = -1
            k = i
            ranks = {"d": 0, "h": 1, "min": 2, "s": 3}
            while True:
                m = _NUMBER.match(line, k)
                if not m:
                    break
                u = _UNIT.match(line, m.end())
                if not u:
                    if k == i:
                        break
                    self.error("expected a unit after the number", lineno, m.end() + 1)
                unit = u.group(0)
                if unit not in DURATION_UNITS:
                    hint = (
                        "use `min` for minutes (`m` is not a unit)"
                        if unit in ("m", "mins", "minute", "minutes")
                        else "duration units are s, min, h, d"
                    )
                    self.error(
                        f"unknown unit `{unit}`", lineno, u.start() + 1, len(unit), hint=hint
                    )
                rank = ranks[unit]
                if rank <= last_unit_rank:
                    self.error(
                        "duration units must go from largest to smallest",
                        lineno,
                        u.start() + 1,
                        len(unit),
                    )
                last_unit_rank = rank
                total += float(m.group(0)) * DURATION_UNITS[unit]
                k = u.end()
                if not (k < len(line) and line[k].isdigit()):
                    break
            if k < len(line) and (line[k].isalnum() or line[k] == "_"):
                self.error("unexpected characters after duration", lineno, k + 1)
            return Token("DURATION", total, Span(lineno, col, lineno, k + 1)), k
        if j < len(line) and line[j] == "_":
            self.error("unexpected `_` after number", lineno, j + 1)
        return Token("NUMBER", value, Span(lineno, col, lineno, j + 1)), j

    def lex_string(self, line: str, i: int, lineno: int) -> tuple[Token, int]:
        quote = line[i]
        col = i + 1
        j = i + 1
        out: list[str] = []
        escapes = {"\\": "\\", '"': '"', "'": "'", "n": "\n", "t": "\t"}
        while j < len(line):
            c = line[j]
            if c == quote:
                return Token("STRING", "".join(out), Span(lineno, col, lineno, j + 2)), j + 1
            if c == "\\":
                if j + 1 >= len(line):
                    break
                e = line[j + 1]
                if e not in escapes:
                    self.error(
                        f"unknown escape `\\{e}`",
                        lineno,
                        j + 1,
                        2,
                        hint="supported escapes: \\\\ \\\" \\' \\n \\t",
                    )
                out.append(escapes[e])
                j += 2
                continue
            out.append(c)
            j += 1
        self.error("unterminated string", lineno, col, len(line) - i)
        raise AssertionError  # unreachable


def tokenize(source: str, file: str = "<rules>") -> LexResult:
    return Lexer(source, file).run()
