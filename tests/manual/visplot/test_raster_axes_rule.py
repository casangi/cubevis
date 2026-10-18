"""
test_raster_axes_rule.py
========================
Raster axis pairs that cannot be drawn (2026-10-18, from Darrell's test):
Antenna 1 / Antenna 2 with anything but each other, and Y == X.  The
antenna rule used to be enforced only in the backend: choosing Antenna 1
x Time in a gear tab showed the message, but only after the request had
reached the backend (a remote kernel, when one is used) and left a
traceback in the terminal.  Now one rule, ``raster_axes_problem``, is
checked in the browser (gear tab message, Plot refuses to send), in
``_handle_plot`` before anything is read, and on the constructor's
arguments.

The Python and JavaScript versions are compared under node.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import warnings

import pytest

from cubevis.toolbox.visplot import visibility_plotter as vpm

warnings.filterwarnings("ignore")

AXES = ["TIME", "BASELINE", "CHANNEL", "FREQUENCY", "ANTENNA1", "ANTENNA2"]
MS = os.environ.get("MS", "")


def test_python_rule():
    p = vpm.raster_axes_problem
    assert p("TIME", "CHANNEL") is None
    assert p("ANTENNA1", "ANTENNA2") is None and p("ANTENNA2", "ANTENNA1") is None
    assert "must be different" in p("TIME", "TIME")
    for other in ("TIME", "BASELINE", "CHANNEL", "FREQUENCY"):
        assert "Antenna 1 goes with Antenna 2" in p("ANTENNA1", other)
        assert "Antenna 1 goes with Antenna 2" in p(other, "ANTENNA2")
    from cubevis.toolbox.visplot.axes import Axis
    assert p(Axis.ANTENNA1, Axis.TIME) and p(Axis.TIME, Axis.CHANNEL) is None


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_javascript_rule_is_the_same():
    pairs = [(y, x) for y in AXES for x in AXES]
    js = vpm._RASTER_AXES_RULE_JS + (
        "\nconst pairs = " + json.dumps(pairs) + ";\n"
        "console.log(JSON.stringify(pairs.map(p => rasterAxesProblem(p[0], p[1]))));\n")
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, check=True)
    got = json.loads(out.stdout)
    want = [vpm.raster_axes_problem(y, x) for y, x in pairs]
    assert got == want


def test_constructor_refuses():
    if not os.path.isdir(MS):
        pytest.skip("MS not set")
    with pytest.raises(ValueError, match="Antenna 1 goes with Antenna 2"):
        vpm.VisibilityPlotter(ms=MS, field="3c279", headless=True,
                              raster_y="ANTENNA1", raster_x="TIME")
    with pytest.raises(ValueError, match="panel B: Antenna 1"):
        vpm.VisibilityPlotter(ms=MS, field="3c279", headless=True, layout="side",
                              panels=[None, dict(kind="raster", raster_y="CHANNEL",
                                                 raster_x="ANTENNA2")])


def test_plot_refuses_without_reading():
    if not os.path.isdir(MS):
        pytest.skip("MS not set")
    vp = vpm.VisibilityPlotter(ms=MS, field="3c279", layout="side")
    try:
        vp._build_layout()
        W = vp._panel_axis_widgets
        # The gear tab's live message and doPlot's refusal use the rule.
        assert "rasterAxesProblem" in vp._do_plot_js
        assert "rasterConflict(panel0_kind_switch" in vp._do_plot_js
        calls = []
        orig = vp._reader.query_raster

        def spy(*a, **k):
            calls.append(1)
            return orig(*a, **k)
        vp._reader.query_raster = spy
        msg = {"correlation": "XX,YY", "panels": {
            "A": {"kind": "raster", "y": "ANTENNA1", "x": "TIME",
                  "qty": W["A"]["raster"]["q_sel"].value},
            "B": {"kind": "scatter", "x": W["B"]["scatter"]["x_sel"].value,
                  "y": W["B"]["scatter"]["y_sel"].value, "colorize": [None, None]}}}
        rep = asyncio.run(vp._handle_plot(msg))
        assert rep["status"] == "error" and rep["failed_slot"] == "A"
        assert "Antenna 1 goes with Antenna 2" in rep["notify_text"]
        assert calls == []
    finally:
        vp.close()
