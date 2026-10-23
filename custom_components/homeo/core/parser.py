"""Parser for .homeo files: recursive-descent statements and arm blocks, precedence-climbing
expressions (§20), layout from token indentation (I10).

Layout rules:
- A token that starts a line ends the current statement/arm, unless that line is more indented
  than the line the statement/arm started on and doesn't start with `|` (continuation).
- A line ending in `:=`, `=`, `latest` or a header `:` opens an indented block.
- In arm blocks, arms start at the block's indentation; `|` lines indented further are the
  sub-arms of the guard line above them.
"""

from __future__ import annotations

from . import ast as A
from .errors import CompileError, Diagnostic, DiagnosticSink, Span
from .lexer import Token, tokenize

COMPARISONS = ("==", "!=", "<", ">", "<=", ">=")
VALID_MODES = ("dry_run", "live")

HINT_ON_IN_STATE = "use `fresh(x)` for 'happened since entering this state'"
HINT_ON_IN_EXPR = "use `age(x) < D` for 'happened recently'"


class _Abort(Exception):
    """Internal: abandon the current statement (diagnostic already recorded)."""


class Parser:
    def __init__(self, source: str, file: str = "<rules>"):
        self.file = file
        self.sink = DiagnosticSink(file)
        lexed = tokenize(source, file)
        self.toks: list[Token] = lexed.tokens
        self.directives = lexed.directives
        self.i = 0
        self.line_indent = 0
        self.line_start = 0  # index of the token that starts the current logical line

    # ------------------------------------------------------------------ helpers

    def peek(self, k: int = 0) -> Token:
        return self.toks[min(self.i + k, len(self.toks) - 1)]

    def advance(self) -> Token:
        t = self.toks[self.i]
        if t.kind != "EOF":
            self.i += 1
        return t

    def fail(self, message: str, span: Span, hint: str | None = None):
        self.sink.error(message, span, hint)
        raise _Abort

    def at_line_end(self) -> bool:
        """True if the next token doesn't belong to the current logical line."""
        t = self.peek()
        if t.kind == "EOF":
            return True
        if self.i == self.line_start:
            return False
        if t.first_on_line:
            return t.is_op("|") or t.indent <= self.line_indent
        return False

    def at_physical_line_end(self) -> bool:
        """True if the next token starts a new physical line (or is EOF)."""
        t = self.peek()
        return t.kind == "EOF" or t.first_on_line

    def check_op(self, *ops: str) -> bool:
        return not self.at_line_end() and self.peek().is_op(*ops)

    def check_kw(self, *kws: str) -> bool:
        return not self.at_line_end() and self.peek().is_kw(*kws)

    def expect_op(self, op: str, what: str | None = None) -> Token:
        if self.check_op(op):
            return self.advance()
        t = self.peek()
        self.fail(f"expected `{op}`{' ' + what if what else ''}, found {describe(t)}", t.span)
        raise AssertionError

    def expect_name(self, what: str) -> Token:
        t = self.peek()
        if not self.at_line_end() and t.kind == "NAME":
            return self.advance()
        if t.kind == "KEYWORD":
            self.fail(f"`{t.value}` is a keyword and can't be used as {what}", t.span)
        self.fail(f"expected {what}, found {describe(t)}", t.span)
        raise AssertionError

    # --------------------------------------------------------------- top level

    def parse_file(self) -> A.File:
        stmts: list[A.Stmt] = []
        if "mode" in self.directives:
            mode, span = self.directives["mode"]
            if mode not in VALID_MODES:
                self.sink.error(
                    f"unknown mode `{mode}`", span, "valid modes: " + ", ".join(VALID_MODES)
                )
        while self.peek().kind != "EOF":
            t = self.peek()
            try:
                if t.indent > 0:
                    self.fail("unexpected indentation", t.span)
                self.line_indent = 0
                self.line_start = self.i
                stmts.append(self.parse_statement())
                if not self.at_line_end():
                    t2 = self.peek()
                    if t2.first_on_line:
                        self.fail(
                            "unexpected indentation",
                            t2.span,
                            "a more-indented line continues the line above it",
                        )
                    self.fail(f"unexpected {describe(t2)}", t2.span)
                if self.peek().kind != "EOF" and self.peek().indent > 0:
                    t2 = self.peek()
                    self.fail(
                        "unexpected indentation",
                        t2.span,
                        "only arms of a block (after `:=`, `latest` or `:`) are indented",
                    )
            except _Abort:
                self.recover()
        self.sink.raise_if_errors()
        return A.File(self.file, stmts, dict(self.directives))

    def recover(self) -> None:
        """Skip to the next top-level line."""
        self.advance()
        while self.peek().kind != "EOF" and not (
            self.peek().first_on_line and self.peek().indent == 0
        ):
            self.advance()

    def parse_statement(self) -> A.Stmt:
        t = self.peek()
        if t.is_kw("let"):
            return self.parse_let()
        if t.is_kw("state"):
            return self.parse_state()
        if t.is_kw("on") and not self.peek(1).is_op("("):
            return self.parse_action()
        return self.parse_bind()

    def parse_let(self) -> A.Let:
        start = self.advance()
        name = self.expect_name("a name after `let`")
        if self.check_op(":="):
            self.fail(
                "`let` uses `=`",
                self.peek().span,
                "`:=` is only for driving a device: `light.x := …`",
            )
        self.expect_op("=", "after the name")
        value = self.parse_value_or_block("let")
        return A.Let(start.span.to(value.span), name.value, name.span, value)

    def parse_bind(self) -> A.Bind:
        t = self.peek()
        if t.kind != "NAME":
            self.fail(f"expected a statement, found {describe(t)}", t.span)
        target = self.parse_postfix()
        if self.check_op("="):
            self.fail(
                "use `:=` to drive a device",
                self.peek().span,
                "to name a value, write `let name = …`",
            )
        if not self.check_op(":="):
            t2 = self.peek()
            if self.at_line_end():
                self.fail(
                    "expected `:=` after the target",
                    target.span,
                    "statements are `let`, `state`, `on`, or a binding `entity := …`",
                )
            self.fail(f"expected `:=`, found {describe(t2)}", t2.span)
        self.advance()
        value = self.parse_value_or_block("binding")
        return A.Bind(target.span.to(value.span), target, value)

    def parse_value_or_block(self, context: str) -> A.Expr:
        if self.at_physical_line_end():
            arms, span = self.parse_block(context)
            return A.Cascade(span, arms)
        return self.parse_expr()

    def parse_state(self) -> A.StateDecl:
        start = self.advance()
        name = self.expect_name("a state machine name")
        self.expect_op("=", "after the state machine name")
        initial = self.parse_expr()
        self.expect_op(":", "after the initial value")
        if not self.at_physical_line_end():
            self.fail("expected an indented block of arms on the next line", self.peek().span)
        arms, span = self.parse_block("state")
        return A.StateDecl(start.span.to(span), name.value, name.span, initial, arms)

    def parse_action(self) -> A.Action:
        start = self.advance()
        trigger = self.parse_or()
        guard = None
        if self.check_kw("if"):
            self.advance()
            guard = self.parse_or()
        self.expect_op(":", "after the action header")
        body: list[A.Call] = []
        if self.at_physical_line_end():
            first = self.peek()
            if first.kind == "EOF" or first.indent <= self.line_indent:
                self.fail("expected an indented action body", first.span)
            body_indent = first.indent
            saved = self.line_indent
            while self.peek().kind != "EOF" and self.peek().indent == body_indent:
                self.line_indent = body_indent
                self.line_start = self.i
                body.append(self.parse_service_call())
                if not self.at_line_end():
                    self.fail(f"unexpected {describe(self.peek())}", self.peek().span)
            self.line_indent = saved
            nxt = self.peek()
            if nxt.kind != "EOF" and nxt.indent > self.line_indent:
                self.fail("inconsistent indentation", nxt.span)
        else:
            body.append(self.parse_service_call())
        end = body[-1].span
        return A.Action(start.span.to(end), trigger, guard, body)

    def parse_service_call(self) -> A.Call:
        t = self.peek()
        if t.is_kw("on"):
            self.fail(
                "`on` actions can't be nested",
                t.span,
                "`on x:` is a top-level statement; write it unindented",
            )
        if t.kind != "NAME":
            self.fail(f"expected a service call, found {describe(t)}", t.span)
        e = self.parse_postfix()
        if not isinstance(e, A.Call):
            self.fail(
                "action bodies contain service calls",
                e.span,
                'e.g. `notify.mobile_app_phone(message: "…")` or `light.porch.turn_on()`',
            )
        return e

    # ------------------------------------------------------------------ blocks

    def parse_block(self, context: str) -> tuple[list[A.Arm | A.Group], Span]:
        first = self.peek()
        if first.kind == "EOF" or first.indent <= self.line_indent:
            self.fail(
                "expected an indented block of arms",
                first.span,
                "write one `condition -> result` arm per line, ending with `else -> …`",
            )
        if first.is_op("|"):
            self.fail("a `|` sub-arm must follow a guard line", first.span)
        block_indent = first.indent
        saved = self.line_indent
        arms: list[A.Arm | A.Group] = []
        while True:
            t = self.peek()
            if t.kind == "EOF" or t.indent < block_indent:
                break
            if t.indent > block_indent:
                if t.is_op("|"):
                    self.fail(
                        "a `|` sub-arm must follow a guard line (a condition without `->`)",
                        t.span,
                    )
                self.fail("inconsistent indentation", t.span)
            arms.append(self.parse_arm(block_indent, context))
        self.line_indent = saved
        if not arms:
            self.fail("expected an arm", first.span)
        if t.kind != "EOF" and t.indent > saved and t.indent < block_indent:
            self.fail("inconsistent indentation", t.span)
        self.check_else_last(arms)
        span = arms[0].span.to(arms[-1].span)
        return arms, span

    def check_else_last(self, arms: list[A.Arm | A.Group]) -> None:
        for a in arms[:-1]:
            if isinstance(a, A.Arm) and a.cond is None:
                self.sink.error("`else` must be the last arm", a.span)

    def parse_arm(self, indent: int, context: str) -> A.Arm | A.Group:
        saved = self.line_indent
        self.line_indent = indent
        self.line_start = self.i
        try:
            t = self.peek()
            if t.is_kw("else"):
                self.advance()
                self.expect_op("->", "after `else`")
                result = self.parse_expr()
                return A.Arm(None, result, t.span.to(result.span))
            if t.is_kw("on") and not self.peek(1).is_op("("):
                self.fail(
                    "`on` actions are only allowed at the top level",
                    t.span,
                    HINT_ON_IN_STATE if context == "state" else HINT_ON_IN_EXPR,
                )
            cond = self.parse_expr()
            if self.check_op("->"):
                self.advance()
                result = self.parse_expr()
                return A.Arm(cond, result, cond.span.to(result.span))
            nxt = self.peek()
            if nxt.first_on_line and nxt.is_op("|") and nxt.indent > indent:
                sub_indent = nxt.indent
                subs: list[A.Arm | A.Group] = []
                while (
                    self.peek().first_on_line
                    and self.peek().is_op("|")
                    and self.peek().indent == sub_indent
                ):
                    self.advance()
                    subs.append(self.parse_arm(sub_indent, context))
                    self.line_indent = indent
                nxt = self.peek()
                if nxt.first_on_line and nxt.indent > indent and nxt.kind != "EOF":
                    self.fail("inconsistent indentation", nxt.span)
                self.check_else_last(subs)
                return A.Group(cond, subs, cond.span.to(subs[-1].span))
            if self.at_line_end():
                self.fail(
                    "expected `->` after the condition",
                    nxt.span if nxt.kind != "EOF" else cond.span,
                    "write `condition -> result`, or put indented `| …` sub-arms on the "
                    "following lines",
                )
            self.fail(f"expected `->`, found {describe(nxt)}", nxt.span)
            raise AssertionError
        finally:
            self.line_indent = saved

    # ------------------------------------------------------------- expressions

    def parse_expr(self) -> A.Expr:
        return self.parse_ternary()

    def parse_ternary(self) -> A.Expr:
        then = self.parse_or()
        if self.check_kw("if"):
            self.advance()
            cond = self.parse_or()
            if not self.check_kw("else"):
                self.fail("expected `else` in `x if c else y`", self.peek().span)
            self.advance()
            otherwise = self.parse_ternary()
            return A.Ternary(then.span.to(otherwise.span), then, cond, otherwise)
        return then

    def parse_or(self) -> A.Expr:
        left = self.parse_and()
        while self.check_kw("or"):
            self.advance()
            right = self.parse_and()
            left = A.Binary(left.span.to(right.span), "or", left, right)
        return left

    def parse_and(self) -> A.Expr:
        left = self.parse_not()
        while self.check_kw("and"):
            self.advance()
            right = self.parse_not()
            left = A.Binary(left.span.to(right.span), "and", left, right)
        return left

    def parse_not(self) -> A.Expr:
        if self.check_kw("not"):
            t = self.advance()
            operand = self.parse_not()
            return A.Unary(t.span.to(operand.span), "not", operand)
        return self.parse_for()

    def parse_for(self) -> A.Expr:
        left = self.parse_comparison()
        while self.check_kw("for"):
            self.advance()
            dur = self.parse_coalesce()
            left = A.For(left.span.to(dur.span), left, dur)
        return left

    def comparison_op(self) -> str | None:
        if self.at_line_end():
            return None
        t = self.peek()
        if t.is_op(*COMPARISONS):
            return t.value
        if t.is_kw("in"):
            return "in"
        if t.is_kw("not") and self.peek(1).is_kw("in"):
            return "not in"
        return None

    def parse_comparison(self) -> A.Expr:
        left = self.parse_coalesce()
        op = self.comparison_op()
        if op is None:
            return left
        self.advance()
        if op == "not in":
            self.advance()
        if op in ("in", "not in"):
            right = self.parse_coalesce()
            if self.check_op(".."):
                self.advance()
                hi = self.parse_coalesce()
                right = A.Range(right.span.to(hi.span), right, hi)
        else:
            right = self.parse_coalesce()
        node = A.Binary(left.span.to(right.span), op, left, right)
        if self.comparison_op() is not None:
            self.fail(
                "comparisons can't be chained",
                self.peek().span,
                "combine them with `and`, e.g. `a < b and b < c`",
            )
        if self.check_op(".."):
            self.fail("a range `a..b` can only follow `in`", self.peek().span)
        return node

    def parse_coalesce(self) -> A.Expr:
        left = self.parse_additive()
        while self.check_op("??"):
            self.advance()
            right = self.parse_additive()
            left = A.Binary(left.span.to(right.span), "??", left, right)
        return left

    def parse_additive(self) -> A.Expr:
        left = self.parse_multiplicative()
        while self.check_op("+", "-"):
            op = self.advance().value
            right = self.parse_multiplicative()
            left = A.Binary(left.span.to(right.span), op, left, right)
        return left

    def parse_multiplicative(self) -> A.Expr:
        left = self.parse_unary()
        while self.check_op("*", "/"):
            op = self.advance().value
            right = self.parse_unary()
            left = A.Binary(left.span.to(right.span), op, left, right)
        return left

    def parse_unary(self) -> A.Expr:
        if self.check_op("-"):
            t = self.advance()
            operand = self.parse_unary()
            return A.Unary(t.span.to(operand.span), "-", operand)
        return self.parse_postfix()

    def parse_postfix(self) -> A.Expr:
        e = self.parse_primary()
        while True:
            if self.check_op("."):
                self.advance()
                t = self.peek()
                if t.kind not in ("NAME", "KEYWORD") or self.at_line_end():
                    self.fail("expected an attribute name after `.`", t.span)
                self.advance()
                e = A.Attr(e.span.to(t.span), e, t.value)
            elif self.check_op("[") and self.peek().span.col == e.span.end_col:
                self.advance()
                t = self.peek()
                if t.kind != "STRING":
                    self.fail(
                        'expected a quoted attribute name, e.g. `light.hall["rgb color"]`',
                        t.span,
                    )
                self.advance()
                close = self.expect_op("]")
                e = A.Attr(e.span.to(close.span), e, t.value, quoted=True)
            elif self.check_op("("):
                if not isinstance(e, (A.Name, A.Attr)):
                    self.fail("only names can be called", self.peek().span)
                args, kwargs, close = self.parse_args()
                e = A.Call(e.span.to(close.span), e, args, kwargs)
            else:
                return e

    def parse_args(self) -> tuple[list[A.Expr], list[A.Kwarg], Token]:
        self.expect_op("(")
        args: list[A.Expr] = []
        kwargs: list[A.Kwarg] = []
        while not self.peek().is_op(")"):
            t = self.peek()
            if t.kind in ("NAME", "KEYWORD") and self.peek(1).is_op(":"):
                self.advance()
                self.advance()
                value = self.parse_expr()
                if any(k.name == t.value for k in kwargs):
                    self.sink.error(f"duplicate argument `{t.value}`", t.span)
                kwargs.append(A.Kwarg(t.value, value, t.span.to(value.span)))
            else:
                value = self.parse_expr()
                if kwargs:
                    self.fail("positional arguments must come before named ones", value.span)
                args.append(value)
            if self.peek().is_op(","):
                self.advance()
            elif not self.peek().is_op(")"):
                self.fail(f"expected `,` or `)`, found {describe(self.peek())}", self.peek().span)
        close = self.advance()
        return args, kwargs, close

    def parse_primary(self) -> A.Expr:
        t = self.peek()
        if self.at_line_end():
            self.fail(
                "expected an expression" + ("" if t.kind == "EOF" else f", found {describe(t)}"),
                t.span,
            )
        k = t.kind
        if k == "NAME":
            self.advance()
            return A.Name(t.span, t.value)
        if k == "NUMBER":
            self.advance()
            return A.NumberLit(t.span, t.value)
        if k == "PERCENT":
            self.advance()
            return A.NumberLit(t.span, t.value, percent=True)
        if k == "DURATION":
            self.advance()
            return A.DurationLit(t.span, t.value)
        if k == "TIME":
            self.advance()
            return A.TimeLit(t.span, t.value)
        if k == "STRING":
            self.advance()
            return A.StringLit(t.span, t.value)
        if k == "KEYWORD":
            v = t.value
            if v == "on":
                self.advance()
                if self.check_op("("):
                    args, kwargs, close = self.parse_args()
                    if args:
                        self.fail(
                            "`on(...)` takes named settings, e.g. `on(brightness: 50%)`",
                            args[0].span,
                        )
                    return A.OnCall(t.span.to(close.span), kwargs)
                nxt = self.peek()
                if (
                    not self.at_line_end()
                    and nxt.kind in ("NAME", "STRING")
                    or (not self.at_line_end() and nxt.is_kw("not"))
                ):
                    self.fail(
                        "`on` actions are only allowed at the top level",
                        t.span,
                        HINT_ON_IN_EXPR,
                    )
                return A.BoolLit(t.span, True, "on")
            if v in ("off", "true", "false"):
                self.advance()
                return A.BoolLit(t.span, v == "true", v)
            if v == "keep":
                self.advance()
                return A.KeepLit(t.span)
            if v == "every":
                self.advance()
                period = self.parse_postfix()
                return A.Every(t.span.to(period.span), period)
            if v == "latest":
                self.advance()
                if not self.at_physical_line_end():
                    self.fail(
                        "`latest` must be followed by an indented block of arms",
                        self.peek().span,
                    )
                arms, span = self.parse_block("latest")
                return A.Latest(t.span.to(span), arms)
            if v == "else":
                self.fail(
                    "unexpected `else`", t.span, "`else` arms go in a block, on their own line"
                )
            self.fail(f"unexpected keyword `{v}`", t.span)
        if t.is_op("("):
            self.advance()
            e = self.parse_expr()
            close = self.expect_op(")")
            e.span = t.span.to(close.span) if isinstance(e, A.Binary | A.Unary) else e.span
            return e
        if t.is_op("["):
            self.advance()
            items: list[A.Expr] = []
            while not self.peek().is_op("]"):
                items.append(self.parse_expr())
                if self.peek().is_op(","):
                    self.advance()
                elif not self.peek().is_op("]"):
                    self.fail(
                        f"expected `,` or `]`, found {describe(self.peek())}", self.peek().span
                    )
            close = self.advance()
            return A.ListExpr(t.span.to(close.span), items)
        self.fail(f"expected an expression, found {describe(t)}", t.span)
        raise AssertionError


def describe(t: Token) -> str:
    if t.kind == "EOF":
        return "end of file"
    if t.kind in ("OP", "KEYWORD"):
        return f"`{t.value}`"
    if t.kind == "STRING":
        return "a string"
    if t.kind == "NAME":
        return f"`{t.value}`"
    return f"`{t.text}`"


def parse(source: str, file: str = "<rules>") -> A.File:
    """Parse a file. Raises CompileError with all syntax errors found."""
    return Parser(source, file).parse_file()


__all__ = ["parse", "Parser", "CompileError", "Diagnostic"]
