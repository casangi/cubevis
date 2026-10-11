"""UTC / elapsed time labels (HRS A0, 2026-10-11).

Time axes and the cursor readout give UTC time of day by default, with
the date in the axis label and an AIPS-style ``d/`` day prefix when the
data cross a UTC midnight; ``time_labels="elapsed"`` keeps the previous
minutes-and-seconds-from-the-first-sample labels.

The UTC branch of ``tick_format._JS_CORE`` is the browser's; it is run
under node here against the same table the Python ``format_utc`` is
checked against, so the browser and an exported PNG cannot disagree.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from cubevis.toolbox.visplot.tick_format import (
    _JS_CORE, TICK_FORMATTER_JS, format_tick, format_utc, time_day0,
    utc_datetime_text, utc_tick_step, mpl_formatter,
)
from cubevis.toolbox.visplot.panel_spec import PanelSpec

# 2012-11-19 07:37:00 UTC, the first integration of the TW Hya test MS.
T0 = 1353310620.0
DAY = int(T0 // 86400)            # 15663

# (tick, step, day0, expected)
GOLDEN_UTC = (
    (T0,                60,   None, "07:37:00"),
    (T0 + 0.25,         0.5,  None, "07:37:00.3"),
    (T0 + 59.95,        1,    None, "07:38:00"),     # carries into minutes
    (T0 + 59.95,        0.5,  None, "07:38:00.0"),
    (T0 + 3.14159,      0.05, None, "07:37:03.14"),
    (T0,                None, None, "07:37:00"),     # no spacing: whole s
    ((DAY + 1) * 86400 - 0.4, 1, None, "00:00:00"),  # rounds into next day
    ((DAY + 1) * 86400 - 0.4, 1, DAY,  "1/00:00:00"),
    ((DAY + 1) * 86400 + 600, 600, DAY, "1/00:10:00"),
    (DAY * 86400 + 86399,     1,  DAY,  "0/23:59:59"),
    (0.0,               1,    None, "00:00:00"),
)

_HARNESS = """
const cases = %s;
function fmt(tick, step, day0) {
  const is_time = true, t0 = 0, mode = 'utc';
%s
}
process.stdout.write(JSON.stringify(cases.map(c => fmt(c[0], c[1], c[2]))));
"""


class TestPython:

    @pytest.mark.parametrize("tick,step,day0,want", GOLDEN_UTC)
    def test_golden(self, tick, step, day0, want):
        assert format_tick(tick, True, 0.0, time_mode="utc",
                           step=step, day0=day0) == want

    def test_elapsed_is_unchanged(self):
        assert format_tick(T0 + 125, True, T0) == "2m 05s"
        assert format_tick(T0 + 125, True, T0, time_mode="elapsed") == "2m 05s"

    def test_readout_text(self):
        assert utc_datetime_text(T0 + 3.04) == "2012-11-19 07:37:03.0"

    def test_day0(self):
        assert time_day0(T0, T0 + 5000) is None
        assert time_day0(T0, T0 + 86400) == DAY

    @pytest.mark.parametrize("span,want", [
        (5, 1.0), (60, 10.0), (120, 20.0), (600, 120.0),
        (3600, 600.0), (5641, 900.0), (86400, 18000.0)])
    def test_tick_step(self, span, want):
        assert utc_tick_step(T0, T0 + span) == want

    def test_mpl_formatter_uses_locs_for_decimals(self):
        pytest.importorskip("matplotlib")
        f = mpl_formatter(True, T0, time_mode="utc")
        f.set_locs([T0, T0 + 0.5])
        assert f(T0 + 0.5) == "07:37:00.5"
        f.set_locs([T0, T0 + 60])
        assert f(T0 + 60) == "07:38:00"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
class TestJavaScript:

    def test_js_matches_golden(self):
        cases = [list(c[:3]) for c in GOLDEN_UTC]
        script = _HARNESS % (json.dumps(cases), _JS_CORE)
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "h.js"
            path.write_text(script)
            res = subprocess.run(["node", str(path)], capture_output=True,
                                 text=True, timeout=30)
        assert res.returncode == 0, res.stderr
        got = json.loads(res.stdout)
        bad = [(c, g) for c, g in zip(GOLDEN_UTC, got) if g != c[3]]
        assert not bad, bad

    def test_formatter_reads_mode_day_and_spacing(self):
        assert "state.data['time_labels']" in TICK_FORMATTER_JS
        assert "day_key" in TICK_FORMATTER_JS
        assert "ticks[1] - ticks[0]" in TICK_FORMATTER_JS


def _spec(**kw):
    base = dict(kind="raster", title="t", x_label="Channel", y_label="Time",
                x_range=(0.0, 383.0), y_range=(T0, T0 + 120.0),
                x_is_time=False, y_is_time=True, agg_n_x=10, agg_n_y=10,
                color_mode="global")
    base.update(kw)
    return PanelSpec(**base)


class TestPanelSpec:

    def test_default_is_utc_with_date_in_label(self):
        s = _spec()
        assert s.time_labels == "utc"
        assert s.axis_label("y") == "Time [UTC, 2012-11-19]"
        d = s.to_state_data()
        assert d["time_labels"] == ["utc"]
        assert d["y_day0"] == [None] and d["x_day0"] == [None]

    def test_elapsed_label_names_its_origin(self):
        s = _spec(time_labels="elapsed")
        assert s.axis_label("y") == "Time [elapsed from 2012-11-19 07:37:00 UTC]"

    def test_crossing_midnight_sets_day0(self):
        s = _spec(y_range=(T0, T0 + 86400.0))
        assert s.to_state_data()["y_day0"] == [DAY]

    def test_placeholder_extent_has_no_date(self):
        s = _spec(y_range=(0.0, 1.0))
        assert s.axis_label("y") == "Time"

    def test_non_time_axis_untouched(self):
        assert _spec().axis_label("x") == "Channel"
