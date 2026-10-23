"""Diagnostics: compile errors with file, line, column, caret snippet and hint (I19)."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class Span:
    """A source range. Lines and columns are 1-based; `end_col` is exclusive."""

    line: int
    col: int
    end_line: int
    end_col: int

    @staticmethod
    def point(line: int, col: int) -> Span:
        return Span(line, col, line, col + 1)

    def to(self, other: Span) -> Span:
        """The span from the start of self to the end of other."""
        return Span(self.line, self.col, other.end_line, other.end_col)


@dataclass(slots=True)
class Diagnostic:
    message: str
    span: Span
    hint: str | None = None
    severity: str = "error"  # "error" or "warning"
    file: str = "<rules>"

    def format(self, source: str | None = None) -> str:
        """Format as `file:line:col: error: message`, caret snippet, hint."""
        out = [f"{self.file}:{self.span.line}:{self.span.col}: {self.severity}: {self.message}"]
        if source is not None:
            lines = source.splitlines()
            if 1 <= self.span.line <= len(lines):
                text = lines[self.span.line - 1]
                out.append(f"    {text}")
                start = self.span.col - 1
                if self.span.end_line == self.span.line:
                    width = max(1, self.span.end_col - self.span.col)
                else:
                    width = max(1, len(text) - start)
                out.append("    " + " " * start + "^" * width)
        if self.hint:
            out.append(f"hint: {self.hint}")
        return "\n".join(out)


class CompileError(Exception):
    """Raised when a file has errors. Carries all diagnostics found."""

    def __init__(self, diagnostics: list[Diagnostic]):
        self.diagnostics = diagnostics
        super().__init__("\n".join(d.format() for d in diagnostics))


@dataclass(slots=True)
class DiagnosticSink:
    """Collects diagnostics for one file."""

    file: str = "<rules>"
    items: list[Diagnostic] = field(default_factory=list)

    def error(self, message: str, span: Span, hint: str | None = None) -> None:
        self.items.append(Diagnostic(message, span, hint, "error", self.file))

    def warning(self, message: str, span: Span, hint: str | None = None) -> None:
        self.items.append(Diagnostic(message, span, hint, "warning", self.file))

    @property
    def errors(self) -> list[Diagnostic]:
        return [d for d in self.items if d.severity == "error"]

    @property
    def warnings(self) -> list[Diagnostic]:
        return [d for d in self.items if d.severity == "warning"]

    def raise_if_errors(self) -> None:
        if self.errors:
            raise CompileError(self.errors)


def did_you_mean(name: str, candidates: list[str] | set[str], n: int = 1) -> str | None:
    """Return a 'did you mean' hint, or None."""
    import difflib

    matches = difflib.get_close_matches(name, sorted(candidates), n=n, cutoff=0.6)
    if not matches:
        return None
    return "did you mean " + " or ".join(f"`{m}`" for m in matches) + "?"
