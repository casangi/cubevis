"""
test_cursor_readout_reset.py
==============================
A replotted panel's cursor readout is reset (2026-10-05).

Location in repository:
    cubevis/tests/manual/visplot/test_cursor_readout_reset.py

Run:
    pytest cubevis/tests/manual/visplot/test_cursor_readout_reset.py -v

Background: after switching a raster from Amplitude to Phase RMS and
pressing Plot, the readout under it still said "Amplitude: empty | ..."
from the previous plot until the mouse next moved over it.  The plot
response handler now puts the readout of the panel it just replotted back
to its placeholder.

The reset itself runs in the browser, so what can be checked here without
one is the wiring: the placeholder is a single shared constant, a fresh
panel shows it, and the response handler's JavaScript resets the right
Div for each slot, guarded on the response carrying a title (which is how
the handler knows the panel was really re-queried).  No real MS needed.
"""
from __future__ import annotations

import inspect
import re

import numpy as np
import pytest
import xarray as xr

from cubevis.toolbox.visplot import visibility_plot, visibility_plotter
from cubevis.toolbox.visplot.axes import Axis
from cubevis.toolbox.visplot.selection import SelectionSpec


class _Reader:
    def query_raster(self, y_dim, x_dim, quantity, selection,
                     polarization=None, max_cells=2_000_000, **kw):
        agg = xr.DataArray(
            np.ones((4, 3)), dims=("time", "baseline_id"),
            coords={"time": np.arange(4.0), "baseline_id": np.arange(3)})
        return agg, (0.0, 2.0), (0.0, 3.0), False

    def identity_tables(self, *a, **kw):
        return {}


def test_placeholder_is_one_constant():
    assert visibility_plotter.CURSOR_PLACEHOLDER_HTML is \
        visibility_plot.CURSOR_PLACEHOLDER_HTML
    assert "Hover" in visibility_plot.CURSOR_PLACEHOLDER_HTML


def test_fresh_panel_shows_the_placeholder():
    pytest.importorskip("datashader")
    from cubevis.toolbox.visplot.visibility_raster import VisibilityRaster
    vr = VisibilityRaster(_Reader(), SelectionSpec(), Axis.TIME,
                          Axis.BASELINE, Axis.AMPLITUDE)
    vr.layout                         # the info block is built with the layout
    if vr._info_div is None:
        pytest.skip("info block not built without a full plotter")
    assert vr._info_div.text == visibility_plot.CURSOR_PLACEHOLDER_HTML


@pytest.mark.parametrize("n", [0, 1])
def test_response_handler_resets_the_replotted_panel(n):
    src = inspect.getsource(visibility_plotter)
    # The Divs and the placeholder are handed to the handler...
    assert f'"panel{n}_raster_cursor":' in src
    assert f'"panel{n}_scatter_cursor":' in src
    assert '"cursor_placeholder":' in src
    # ...and the reset picks the Div for the kind that rendered, only when
    # the response has a title for this panel.
    pat = (rf"if \(p{n} && p{n}\.title != null\) \{{\s*"
           rf"const cur{n} = \(p{n}_kind === 'raster'\) \? panel{n}_raster_cursor\s*"
           rf": panel{n}_scatter_cursor;\s*"
           rf"if \(cur{n}\) cur{n}\.text = cursor_placeholder;")
    assert re.search(pat, src), "reset block missing or changed"
