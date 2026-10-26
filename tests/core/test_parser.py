import re
from pathlib import Path

import pytest

from custom_components.homeo.core import ast as A
from custom_components.homeo.core.errors import CompileError
from custom_components.homeo.core.parser import parse
from custom_components.homeo.core.printer import sexpr, to_source


def expr(src):
    """Parse `let x = <src>` and return the expression as an s-expression."""
    (stmt,) = parse(f"let x = {src}").statements
    return sexpr(stmt.value)


def errors(src):
    with pytest.raises(CompileError) as e:
        parse(src)
    return e.value.diagnostics


def error(src):
    return errors(src)[0]


# ------------------------------------------------------------- precedence (§20)


@pytest.mark.parametrize(
    "src,tree",
    [
        ("a if c else b", "(if c a b)"),
        ("a or b and c", "(or a (and b c))"),
        ("not a and b", "(and (not a) b)"),
        ("not motion for 5min", "(not (for motion 5min))"),
        ("co2 > 800 for 5min or humidity > 60%", "(or (for (> co2 800) 5min) (> humidity 60%))"),
        ("a == b + 1", "(== a (+ b 1))"),
        ("a + b * c", "(+ a (* b c))"),
        ("a - b - c", "(- (- a b) c)"),
        ("-a * b", "(* (- a) b)"),
        ("co2 ?? 0 > 800", "(> (?? co2 0) 800)"),
        ("800 < co2 ?? 0", "(< 800 (?? co2 0))"),
        ("age(x) < 30min", "(< (call age x) 30min)"),
        ("light.hall.brightness", "light.hall.brightness"),
        ('light.hall["rgb color"]', 'light.hall["rgb color"]'),
        ("now in 22:00..06:00", "(in now (.. 22:00 06:00))"),
        ("now in sunset - 30min..23:00", "(in now (.. (- sunset 30min) 23:00))"),
        ("today in mon..fri and not holiday", "(and (in today (.. mon fri)) (not holiday))"),
        ('x in ["a", "b"]', '(in x ["a" "b"])'),
        ('x not in ["a"]', '(not in x ["a"])'),
        ("a and b for 2min", "(and a (for b 2min))"),
        ("not motion within 5min", "(not (within motion 5min))"),
        ("a or b within 2min and c", "(or a (and (within b 2min) c))"),
        ("co2 > 800 within 5min", "(within (> co2 800) 5min)"),
        ("a within 5min for 2min", "(for (within a 5min) 2min)"),
        ("x.within", "x.within"),
        ("every 10min", "(every 10min)"),
        ("on(brightness: 80%, kelvin: 2700)", "(on brightness:80% kelvin:2700)"),
        ("on(brightness: 10% if night else 100%)", "(on brightness:(if night 10% 100%))"),
        ("toggle(x, initial: off)", "(call toggle x initial:off)"),
        (
            'bus.zha_event(device_ieee: "ab", command: "on")',
            '(call bus.zha_event device_ieee:"ab" command:"on")',
        ),
        ("x if a else y if b else z", "(if a x (if b y z))"),
        ("(a or b) and c", "(and (or a b) c)"),
        ("on", "on"),
        ("keep", "keep"),
    ],
)
def test_precedence(src, tree):
    assert expr(src) == tree


def test_comparisons_dont_chain():
    d = error("let x = a < b < c")
    assert "can't be chained" in d.message


def test_range_only_after_in():
    assert "only follow `in`" in error("let x = a == 1..2").message


# --------------------------------------------------------------------- statements


def test_statement_kinds():
    f = parse(
        """
let co2 = sensor.co2
fan.bathroom := co2 > 800
state mode = "auto":
    fresh(remote.press) -> "boost"
on input_button.doorbell:
    notify.phone(message: "ding")
on x if y: notify.phone(message: "inline")
"""
    )
    kinds = [type(s).__name__ for s in f.statements]
    assert kinds == ["Let", "Bind", "StateDecl", "Action", "Action"]
    action = f.statements[4]
    assert to_source(action.trigger) == "x" and to_source(action.guard) == "y"


def test_action_header_if_is_a_guard_not_a_ternary():
    (a,) = parse('on person.ann == "home" if night:\n    light.porch.flash()\n').statements
    assert sexpr(a.trigger) == '(== person.ann "home")'
    assert sexpr(a.guard) == "night"
    assert sexpr(a.body[0]) == "(call light.porch.flash)"


def test_cascade_block_with_groups():
    (b,) = parse(
        """
light.hall :=
    age(manual(light.hall)) < 1h       -> keep
    motion or age(motion) < 5min                      # a guard with sub-arms
        | night                        -> on(brightness: 10%, kelvin: 2200)
        | else                         -> on(brightness: 100%)
    else                               -> off
"""
    ).statements
    assert isinstance(b.value, A.Cascade)
    assert sexpr(b.value) == (
        "(cascade (arm (< (call age (call manual light.hall)) 1h) keep) "
        "(group (or motion (< (call age motion) 5min)) "
        "(arm night (on brightness:10% kelvin:2200)) (arm else (on brightness:100%))) "
        "(arm else off))"
    )


def test_nested_groups():
    (s,) = parse(
        """
state m = "a":
    x
        | y
            | z -> "b"
            | else -> "c"
        | else -> "d"
"""
    ).statements
    assert [sexpr(a) for a in s.arms] == [
        '(group x (group y (arm z "b") (arm else "c")) (arm else "d"))'
    ]


def test_latest_block():
    (s,) = parse(
        """
let blinds_mode = latest
    input_button.blinds_auto   -> "auto"
    manual(cover.blinds)       -> "manual"
    else                       -> "auto"
"""
    ).statements
    assert isinstance(s.value, A.Latest) and len(s.value.arms) == 3


def test_continuation_lines():
    (s,) = parse("let stuffy = co2 > 800 or\n    humidity > 70%\n").statements
    assert sexpr(s.value) == "(or (> co2 800) (> humidity 70%))"
    (b,) = parse(
        """
fan.x :=
    a and
      b                    -> on
    else
      -> off
"""
    ).statements
    assert sexpr(b.value) == "(cascade (arm (and a b) on) (arm else off))"


def test_brackets_span_lines():
    (a,) = parse('on x:\n    notify.phone(\n  message: "hi",\n  title: "t")\n').statements
    assert sexpr(a.body[0]) == '(call notify.phone message:"hi" title:"t")'


def test_mode_directive_kept():
    f = parse("# mode: dry_run\nlet a = 1\n")
    assert f.directives["mode"][0] == "dry_run"


def test_unknown_mode():
    assert "unknown mode" in error("# mode: sometimes\nlet a = 1\n").message


# ------------------------------------------------------------------------- errors


def test_let_with_walrus():
    d = error("let a := 1")
    assert d.message == "`let` uses `=`"


def test_bind_with_equals():
    d = error("fan.x = on")
    assert "use `:=`" in d.message and "let name" in d.hint


def test_missing_arrow():
    d = error("fan.x :=\n    a\n    else -> off\n")
    assert "expected `->`" in d.message


def test_else_must_be_last():
    assert (
        "`else` must be the last arm" in error("fan.x :=\n    else -> on\n    a -> off\n").message
    )


def test_on_inside_state_machine():
    d = error('state m = "a":\n    on x -> "b"\n')
    assert "top level" in d.message and "fresh" in d.hint


def test_on_inside_expression():
    d = error("fan.x := on motion")
    assert "top level" in d.message and "age(x)" in d.hint


def test_nested_on():
    d = error("on x:\n    on y:\n        notify.a()\n")
    assert "can't be nested" in d.message


def test_action_body_must_be_calls():
    assert "service calls" in error("on x:\n    a + b\n").message


def test_unexpected_indentation():
    assert "unexpected indentation" in error("let a = 1\n    \nlet b = 2\n  let c = 3\n").message


def test_inconsistent_indentation():
    d = error("fan.x :=\n    a -> on\n  b -> off\n")
    assert "indentation" in d.message


def test_sub_arm_without_guard():
    d = error("fan.x :=\n    a -> on\n        | b -> off\n    else -> off\n")
    assert "must follow a guard" in d.message


def test_multiple_errors_reported():
    ds = errors("let = 1\nfan.x = on\nlet ok = 2\nlet y := 3\n")
    assert [d.span.line for d in ds] == [1, 2, 4]


def test_error_format_has_caret():
    try:
        parse("let x = a < b < c", "bath.homeo")
    except CompileError as e:
        text = e.diagnostics[0].format("let x = a < b < c")
    assert text.splitlines() == [
        "bath.homeo:1:15: error: comparisons can't be chained",
        "    let x = a < b < c",
        "                  ^",
        "hint: combine them with `and`, e.g. `a < b and b < c`",
    ]


# ----------------------------------------------------------- design examples


DESIGN = Path(__file__).parents[2] / "docs" / "design.md"
# Blocks that are fragments or v2 pseudo-code, not complete v1 files.
FRAGMENTS = {"  let co2", "fresh(07:00) and workday", "def bath_fan"}


def design_blocks():
    blocks = re.findall(r"```python\n(.*?)```", DESIGN.read_text(), re.S)
    return [b for b in blocks if not any(b.lstrip("\n").startswith(f) for f in FRAGMENTS)]


@pytest.mark.parametrize("src", design_blocks())
def test_design_examples_parse(src):
    parse(src)


@pytest.mark.parametrize(
    "src",
    [
        "a or b and not c",
        "(a or b) and c",
        "x if c else y",
        "co2 > 800 for 5min",
        "now in sunset - 30min..23:00",
        "on(brightness: 10% if night else 100%)",
        "age(manual(fan.x)) < 1h",
        "a - (b - c)",
        "not (a for 1h30min)",
        "humidity > 63% within 30min",
        "not motion within 5min",
    ],
)
def test_to_source_round_trips(src):
    (s,) = parse(f"let x = {src}").statements
    text = to_source(s.value)
    (s2,) = parse(f"let x = {text}").statements
    assert sexpr(s2.value) == sexpr(s.value)
    assert text == src.replace("not (a for 1h30min)", "not a for 1h30min")


def test_within_is_a_keyword():
    with pytest.raises(CompileError) as e:
        parse("let within = on\n")
    assert "keyword" in e.value.diagnostics[0].message
