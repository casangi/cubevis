"""Plot summary and the reproducing call (HRS A3, 2026-10-11).

The summary page reports a panel's render state; its ``visplot(...)``
text must rebuild the same view.  The round trip is checked by running
the printed call (with ``visplot`` standing for a headless plotter) and
comparing the call the result prints.
"""

from __future__ import annotations

import os

import pytest

MS = os.environ.get("MS")
pytestmark = pytest.mark.skipif(not (MS and os.path.isdir(MS)),
                                reason="set MS to the TW Hya data")


def _plotter(**kw):
    from cubevis.toolbox.visplot.visibility_plotter import VisibilityPlotter
    return VisibilityPlotter(ms=MS, headless=True, **kw)


def _round_trip(p):
    call = p.reproducing_call()
    built = {}

    def visplot(**kw):
        from cubevis.toolbox.visplot.visibility_plotter import VisibilityPlotter
        built["p"] = VisibilityPlotter(headless=True, **kw)
    exec(call, {"visplot": visplot})
    return call, built["p"].reproducing_call()


@pytest.mark.parametrize("kw", [
    dict(field="3c279", preset="vplot", timerange="08:59:00~09:00:00"),
    dict(field="3c279", preset="spectrum", scan="33", uvrange="<100m"),
    dict(field="3c279", layout="over", antenna="DA44&DV08,DA45&DV05",
         panels=[dict(kind="raster", raster_y="TIME", raster_x="CHANNEL",
                      raster_qty="PHASE", scaling="linear", vmin=-90, vmax=90),
                 dict(kind="scatter", scatter_x="CHANNEL", scatter_y="REAL",
                      scatter_avg_time="scan")]),
])
def test_call_round_trips(kw):
    a, b = _round_trip(_plotter(**kw))
    assert a == b


def test_description_sections_and_page():
    p = _plotter(field="3c279", preset="vplot", uvrange="100~300m")
    d = p.describe("A", viewport=[0, 1, 0, 1])
    titles = [t for t, _ in d["sections"]]
    assert titles[:3] == ["Data set", "Selection", "Plot"] and "Software" in titles
    sel = dict(d["sections"][1][1])
    assert sel["UV range"] == "100~300m"
    soft = dict(d["sections"][-1][1])
    assert "cubevis" in soft and "python" in soft
    html = p.describe("B", html=True)
    assert html.startswith("<!DOCTYPE html>") and "Reproduce this view" in html
    assert "visplot(" in html
