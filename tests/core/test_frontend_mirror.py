"""The editor panel mirrors a few tables of the core; keep them in step."""

import re
from pathlib import Path

from custom_components.homeo.core.lexer import KEYWORDS
from custom_components.homeo.core.writers import WRITERS

COMPLETE_JS = Path(__file__).parents[2] / "frontend" / "src" / "complete.js"
LANGUAGE_JS = Path(__file__).parents[2] / "frontend" / "src" / "language.js"


def test_keywords_match_the_lexer():
    js = LANGUAGE_JS.read_text()
    block = re.search(r"export const KEYWORDS = \[(.*?)\];", js, re.S).group(1)
    assert set(re.findall(r'"(\w+)"', block)) == set(KEYWORDS)
    # and every keyword is highlighted as one (control word) or as a value (`on`, `keep`, …)
    styled: set[str] = set()
    for name in ("CONTROL", "ATOMS"):
        styled |= set(
            re.findall(
                r'"(\w+)"', re.search(rf"const {name} = new Set\(\[(.*?)\]\)", js, re.S).group(1)
            )
        )
    assert set(KEYWORDS) <= styled


def test_on_settings_match_the_writers():
    js = COMPLETE_JS.read_text()
    block = re.search(r"export const ON_SETTINGS = \{(.*?)\n\};", js, re.S).group(1)
    mirrored = {
        domain: set(re.findall(r"(\w+): \"", settings))
        for domain, settings in re.findall(r"(\w+): \{(.*?)\}", block)
    }
    actual = {
        d: set(w.on_params) for (d, attr), w in WRITERS.items() if attr is None and w.on_params
    }
    assert mirrored == actual
