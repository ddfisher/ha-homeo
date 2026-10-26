import ast
import math
import subprocess
import sys
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from custom_components.homeo.core import timeutil
from custom_components.homeo.core.values import INF, UNKNOWN, DailyTime, Num, compare, snap

CORE = Path(__file__).parents[2] / "custom_components" / "homeo" / "core"


def test_core_does_not_import_home_assistant():
    for path in CORE.glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            assert not any(n.split(".")[0] == "homeassistant" for n in names), path.name


def test_core_imports_without_home_assistant():
    # The integration package's __init__ must not pull Home Assistant in either.
    code = (
        "import sys; sys.modules['homeassistant'] = None\n"
        "import custom_components.homeo.core.runtime, custom_components.homeo.core.checker"
    )
    root = Path(__file__).parents[2]
    subprocess.run([sys.executable, "-c", code], cwd=root, check=True)


# ------------------------------------------------------------------- intervals


def moving(v: float) -> Num:
    return Num(v, v, 1.0, 1.0)


def test_exact_comparisons():
    assert compare("<", Num.of(1), Num.of(2), 0) == (True, INF)
    assert compare("<", Num.of(2), Num.of(2), 0)[0] is False
    assert compare("<=", Num.of(2), Num.of(2), 0)[0] is True
    assert compare("==", Num.of(2), Num.of(2), 0)[0] is True
    assert compare("!=", Num.of(2), Num.of(3), 0)[0] is True


def test_moving_comparison_reports_crossing():
    # age = 100 s now, growing; age < 300 flips at now + 200
    assert compare("<", moving(100), Num.of(300), 1000.0) == (True, 1200.0)
    # at the crossing instant the new result already shows (right limit)
    assert compare("<", moving(300), Num.of(300), 0)[0] is False
    assert compare(">", moving(300), Num.of(300), 0)[0] is True


NOW = 1.77e9  # a 2026 timestamp: one clock tick is about 2.4e-7 s


def test_snap_counts_a_crossing_within_a_clock_tick_as_reached():
    assert snap(5e-10, -0.009, 0.0, NOW) == 0.0  # 5.6e-8 s to go: rounds to now
    assert snap(-5e-10, -0.009, 0.0, NOW) == 0.0  # 5.6e-8 s ago
    assert snap(5e-6, -0.009, 0.0, NOW) == 5e-6  # 0.6 ms to go: the clock can tell
    assert snap(5e-10, 0.0, 0.0, NOW) == 5e-10  # not moving
    assert snap(INF, -1.0, 0.0, NOW) == INF


def test_comparison_a_hair_short_of_its_crossing_has_crossed():
    # Rounding left the line 5e-10 above 0 at its crossing instant. A wake-up 5.6e-8 s on
    # would be lost (it rounds to now), so the comparison would never flip.
    falling = Num(5e-10, 5e-10, -0.009, -0.009)
    assert compare("<", falling, Num.of(0), NOW) == (True, INF)


def test_lower_bound_interval_is_unknown_until_settled():
    age = Num(60, INF, 1.0, 1.0)  # at least 60 s
    result, wake = compare(">", age, Num.of(3600), 0.0)
    assert result is UNKNOWN and wake == 3540.0
    assert compare(">", Num(3600, INF, 1, 1), Num.of(3600), 0)[0] is True
    assert compare("<", Num(3600, INF, 1, 1), Num.of(600), 0)[0] is False


def test_interval_arithmetic():
    a = Num(1, 2)
    b = Num(10, 20)
    assert b - a == Num(8, 19)
    assert (moving(5) - Num.of(5)) == Num(0, 0, 1, 1)
    assert -Num(1, 2, 0, 1) == Num(-2, -1, -1, 0)
    assert math.isinf((Num(0, 0, 1, 1) - Num(-INF, 5)).hi)


# ------------------------------------------------------------------------- time


def solar(event, day):
    return None


def test_wall_time_dst_gap_and_overlap():
    tz = ZoneInfo("Europe/Amsterdam")
    gap = timeutil.wall_to_utc(date(2026, 3, 29), 2 * 3600 + 1800, tz)
    assert timeutil.datetime.fromtimestamp(gap, tz).strftime("%H:%M") == "03:00"
    fold = timeutil.wall_to_utc(date(2026, 10, 25), 2 * 3600 + 1800, tz)
    dt = timeutil.datetime.fromtimestamp(fold, tz)
    assert dt.strftime("%H:%M") == "02:30" and dt.utcoffset().total_seconds() == 7200


@pytest.mark.parametrize(
    "hour,expected",
    [(21, False), (22, True), (23, True), (3, True), (6, False), (12, False)],
)
def test_range_wraps_midnight(hour, expected):
    tz = ZoneInfo("UTC")
    now = timeutil.wall_to_utc(date(2026, 1, 6), hour * 3600, tz)
    inside, _ = timeutil.in_range(DailyTime(22 * 3600), DailyTime(6 * 3600), now, tz, solar)
    assert inside is expected


def test_empty_range():
    tz = ZoneInfo("UTC")
    now = timeutil.wall_to_utc(date(2026, 1, 6), 7 * 3600, tz)
    assert timeutil.in_range(DailyTime(7 * 3600), DailyTime(7 * 3600), now, tz, solar)[0] is False


def test_solar_without_occurrence():
    tz = ZoneInfo("UTC")
    now = timeutil.wall_to_utc(date(2026, 6, 21), 12 * 3600, tz)
    assert timeutil.last_occurrence(DailyTime(solar="sunset"), now, tz, solar) is None


def test_every_ticks():
    tz = ZoneInfo("UTC")
    now = timeutil.wall_to_utc(date(2026, 1, 6), 8 * 3600 + 7 * 60, tz)
    last, nxt = timeutil.every_last_tick(900, now, tz)
    assert (nxt - last, (now - last) / 60) == (900, 7)
