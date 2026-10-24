"""The editor panel mirrors a few tables of the core; keep them in step."""

import re
from pathlib import Path

from custom_components.homeo.core.writers import WRITERS

COMPLETE_JS = Path(__file__).parents[2] / "frontend" / "src" / "complete.js"


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
