"""``panels=[...]``: each panel's configuration as arguments (HRS A2).

Before A2 the constructor and ``visplot()`` described one panel (one
``raster_y``, one ``scatter_x``, ...), so a two-panel view could only be
reached through a preset.  ``panels`` takes one dict per panel; a key
left out takes what the flat arguments / preset give that panel.
"""

from __future__ import annotations

import os

import pytest

from cubevis.toolbox.visplot.axes import Axis

MS = os.environ.get("MS")
pytestmark = pytest.mark.skipif(not (MS and os.path.isdir(MS)),
                                reason="set MS to the TW Hya data")


def _plotter(**kw):
    from cubevis.toolbox.visplot.visibility_plotter import VisibilityPlotter
    kw.setdefault("field", "3c279")
    return VisibilityPlotter(ms=MS, headless=True, **kw)


def test_two_panels_each_configured():
    p = _plotter(layout="over", panels=[
        dict(kind="scatter", scatter_x="FREQUENCY", scatter_y="AMPLITUDE",
             scatter_avg_time="scan", averaging="scalar"),
        dict(kind="raster", raster_y="TIME", raster_x="CHANNEL",
             raster_qty="PHASE", baseline_combine="median",
             scaling="linear", vmin=-90, vmax=90)])
    a, b = p._slots
    assert a.kind == "scatter" and b.kind == "raster"
    assert a.scatter._x_dim == Axis.FREQUENCY
    assert a.scatter.layers[0].y_axis == Axis.AMPLITUDE
    assert a.scatter._avg_time == "scan"
    assert b.raster._quantity == Axis.PHASE
    assert b.raster.baseline_combine == "median"
    assert (b.raster._scaling, b.raster._scaling_vmin,
            b.raster._scaling_vmax) == ("linear", -90.0, 90.0)


def test_unset_keys_follow_flat_arguments_and_preset():
    p = _plotter(preset="spectrum", panels=[None, dict(scatter_y="REAL")])
    a, b = p._slots
    assert a.kind == b.kind == "scatter"
    assert a.scatter._x_dim == Axis.FREQUENCY       # from the preset
    assert a.scatter.layers[0].y_axis == Axis.AMPLITUDE
    assert b.scatter.layers[0].y_axis == Axis.REAL  # from panels
    assert b.scatter._avg_time == "scan"


def test_flat_arguments_unchanged_without_panels():
    p = _plotter(raster_y="BASELINE", raster_x="TIME", scatter_x="TIME")
    a, b = p._slots
    assert (a.kind, b.kind) == ("raster", "scatter")
    assert (a.raster._y_dim, a.raster._x_dim) == (Axis.BASELINE, Axis.TIME)
    assert b.scatter._x_dim == Axis.TIME


@pytest.mark.parametrize("panels,msg", [
    ([{}, {}, {}], "at most 2"),
    ({"kind": "raster"}, "list of dicts"),
    ([{"colour": "red"}], "unknown key"),
    ([{"kind": "image"}], "kind must be"),
    ([{"raster_qty": "BOGUS"}], "not a valid axis"),
    ([{"raster_y": "TIME", "raster_x": "TIME"}], "must differ"),
    ([{"scatter_avg_time": "fortnight"}], "scatter_avg_time"),
    ([{"scaling": "wobbly"}], "scaling must be"),
])
def test_refused(panels, msg):
    with pytest.raises(ValueError, match=msg):
        _plotter(panels=panels)
