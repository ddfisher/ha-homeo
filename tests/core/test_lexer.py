import pytest

from custom_components.homeo.core.errors import CompileError
from custom_components.homeo.core.lexer import tokenize


def kinds(src):
    return [(t.kind, t.value) for t in tokenize(src).tokens[:-1]]


def lex_error(src):
    with pytest.raises(CompileError) as e:
        tokenize(src)
    return e.value.diagnostics[0]


def test_entity_and_operators():
    assert kinds("fan.bathroom := co2 > 800") == [
        ("NAME", "fan"),
        ("OP", "."),
        ("NAME", "bathroom"),
        ("OP", ":="),
        ("NAME", "co2"),
        ("OP", ">"),
        ("NUMBER", 800),
    ]


def test_object_id_may_start_with_digit_after_dot():
    assert kinds("sensor.0x12_temp") == [("NAME", "sensor"), ("OP", "."), ("NAME", "0x12_temp")]


def test_keyword_after_dot_is_a_name():
    assert kinds("light.hall.state")[-1] == ("NAME", "state")


@pytest.mark.parametrize(
    "src,seconds",
    [("30s", 30), ("5min", 300), ("2h", 7200), ("1d", 86400), ("1h30min", 5400), ("1.5h", 5400)],
)
def test_durations(src, seconds):
    assert kinds(src) == [("DURATION", seconds)]


def test_m_is_not_minutes():
    d = lex_error("x for 5m")
    assert "unknown unit `m`" in d.message
    assert "min" in d.hint


def test_duration_units_order():
    assert "largest to smallest" in lex_error("30min1h").message


def test_percent_and_numbers():
    assert kinds("50% 0.5 7") == [("PERCENT", 50), ("NUMBER", 0.5), ("NUMBER", 7)]


@pytest.mark.parametrize("src,secs", [("07:00", 25200), ("7:30", 27000), ("22:30:15", 81015)])
def test_times(src, secs):
    assert kinds(src) == [("TIME", secs)]


def test_invalid_time():
    assert "invalid time" in lex_error("25:00").message


def test_range_operator_after_literals():
    assert kinds("sunset - 30min..23:00") == [
        ("NAME", "sunset"),
        ("OP", "-"),
        ("DURATION", 1800),
        ("OP", ".."),
        ("TIME", 82800),
    ]
    assert kinds("1..5") == [("NUMBER", 1), ("OP", ".."), ("NUMBER", 5)]


def test_strings_and_escapes():
    assert kinds(r'"a\"b" ' + r"'it\'s' " + r'"tab\tnl\n"') == [
        ("STRING", 'a"b'),
        ("STRING", "it's"),
        ("STRING", "tab\tnl\n"),
    ]


def test_unknown_escape():
    assert "unknown escape" in lex_error(r'"\q"').message


def test_unterminated_string():
    assert "unterminated" in lex_error('"abc').message


def test_comments_and_line_info():
    toks = tokenize("let a = 1  # comment\n  # indented comment\n    b\n").tokens
    first = [t for t in toks if t.first_on_line]
    assert [(t.value, t.indent) for t in first[:-1]] == [("let", 0), ("b", 4)]


def test_brackets_suppress_line_starts():
    toks = tokenize("x(\n  a,\n  b)\n").tokens
    assert [t.value for t in toks if t.first_on_line][:-1] == ["x"]


def test_tabs_in_indentation():
    d = lex_error("fan.x :=\n\ta -> on\n")
    assert d.message == "tab in indentation"
    assert d.span.line == 2


def test_unclosed_bracket():
    assert "unclosed `(`" in lex_error("age(x").message


def test_mismatched_bracket():
    assert "doesn't match" in lex_error("age(x]").message


def test_mode_directive():
    lexed = tokenize("# bathroom rules\n# mode: dry_run\nlet a = 1\n# mode: live\n")
    assert lexed.directives["mode"][0] == "dry_run"


def test_positions():
    toks = tokenize("let x =\n    a -> b").tokens
    a = toks[3]
    assert (a.value, a.span.line, a.span.col, a.first_on_line, a.indent) == ("a", 2, 5, True, 4)
