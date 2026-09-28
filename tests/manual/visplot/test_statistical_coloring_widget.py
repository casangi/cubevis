"""
test_statistical_coloring_widget.py
=====================================
Tests for the widget-layer "statistical" coloring fixes in
visibility_scatter.py, found while investigating a test failure caused
by adding "statistical" as a third ScatterLayerSpec.coloring value
(Part 6, 2026-09; see test_colorize_by_axis_render.py's own
test_invalid_coloring_value_rejected, updated alongside this file).

Two real bugs were found and fixed here, beyond the reader.py-side
validation update that test_statistical_coloring.py already covers:

1. ScatterLayer.__post_init__ (the widget-layer counterpart of
   ScatterLayerSpec) still rejected anything but "continuous"/
   "categorical" -- meaning "statistical" was accepted by the backend
   but UNCONSTRUCTABLE from the widget layer at all.
2. VisibilityScatter.update_colorize()'s own coloring value check had
   the same gap, AND its internal branching ("if new_coloring ==
   'continuous': no axis, else: resolve a categorical colorize_axis")
   assumed a strict binary -- switching a layer straight to
   "statistical" would have tried to resolve a colorize_axis for it
   (raising "no colorizable axes available", or worse, silently
   attaching one that ScatterLayerSpec would then reject downstream).
   Its cmap-restoration logic had the same "continuous is the only way
   out of categorical" assumption, so switching categorical->statistical
   directly would have kept the discrete category palette instead of
   restoring a gradient one.

Location in repository:
    cubevis/tests/manual/visplot/test_statistical_coloring_widget.py

Run:
    pytest cubevis/tests/manual/visplot/test_statistical_coloring_widget.py -v

No real MS/PS needed: update_colorize's own coloring/colorize_axis/cmap
logic runs entirely before any backend call, so a bare VisibilityScatter
instance (constructed via __new__, with only the handful of attributes
that logic actually touches) is enough -- _rerender() and
_update_state_source() are stubbed out since they are unrelated to what
these tests check (a live render / bokeh state sync, not the
coloring-mode bookkeeping itself).
"""
from __future__ import annotations

import pytest

from cubevis.toolbox.visplot.axes import Axis
from cubevis.toolbox.visplot.visibility_scatter import ScatterLayer, VisibilityScatter


# ---------------------------------------------------------------------------
# 1. ScatterLayer itself
# ---------------------------------------------------------------------------

class TestScatterLayerStatistical:
    def test_accepts_statistical(self):
        layer = ScatterLayer(y_axis=Axis.AMPLITUDE, polarization="XX", coloring="statistical")
        assert layer.coloring == "statistical"

    def test_still_rejects_unknown_value(self):
        with pytest.raises(ValueError, match="coloring must be"):
            ScatterLayer(y_axis=Axis.AMPLITUDE, polarization="XX", coloring="bogus")

    def test_colorize_axis_rejected_on_statistical(self):
        """Falls through the same "not categorical" branch as
        "continuous" -- same contract as ScatterLayerSpec's own."""
        with pytest.raises(ValueError):
            ScatterLayer(y_axis=Axis.AMPLITUDE, polarization="XX",
                         coloring="statistical", colorize_axis=Axis.SCAN)


# ---------------------------------------------------------------------------
# 2. VisibilityScatter.update_colorize -- the actual regression
# ---------------------------------------------------------------------------

def _bare_scatter(layers, cmap_backup=None, layer_cmaps=(("#000000", "#ffffff"),)):
    """A VisibilityScatter with only what update_colorize's own
    coloring/colorize_axis/cmap logic touches -- no real backend, no
    bokeh figure, no open MS/PS. _rerender/_update_state_source are
    stubbed since they belong to the live-render path, not the
    bookkeeping this test exercises."""
    vs = VisibilityScatter.__new__(VisibilityScatter)
    vs._layers = list(layers)
    vs._layer_continuous_cmap_backup = dict(cmap_backup or {})
    vs._layer_cmaps = list(layer_cmaps)
    vs._theme_hint = lambda: "light"
    vs._rerender = lambda: None
    vs._state_source = None
    return vs


class TestUpdateColorizeStatistical:
    def test_categorical_to_statistical_drops_axis_and_restores_gradient_cmap(self):
        """The core regression: this used to crash trying to resolve a
        colorize_axis for a statistical layer (the branching assumed
        "not continuous" meant "categorical"). Confirmed directly, not
        just by reading the fixed code."""
        vs = _bare_scatter(
            [ScatterLayer(y_axis=Axis.AMPLITUDE, polarization="XX", coloring="categorical",
                         colorize_axis=Axis.SCAN, cmap=("#111", "#222", "#333"))],
            cmap_backup={0: ("#aaa", "#bbb", "#ccc")},
        )
        vs.update_colorize(0, coloring="statistical")
        layer = vs._layers[0]
        assert layer.coloring == "statistical"
        assert layer.colorize_axis is None
        assert layer.cmap == ("#aaa", "#bbb", "#ccc")

    def test_statistical_to_categorical_resolves_axis_normally(self):
        vs = _bare_scatter(
            [ScatterLayer(y_axis=Axis.AMPLITUDE, polarization="XX", coloring="statistical")],
        )
        vs.update_colorize(0, coloring="categorical", colorize_axis=Axis.SCAN)
        layer = vs._layers[0]
        assert layer.coloring == "categorical"
        assert layer.colorize_axis == Axis.SCAN

    def test_repeated_statistical_is_idempotent(self):
        vs = _bare_scatter(
            [ScatterLayer(y_axis=Axis.AMPLITUDE, polarization="XX", coloring="categorical",
                         colorize_axis=Axis.SCAN, cmap=("#111", "#222", "#333"))],
        )
        vs.update_colorize(0, coloring="statistical")
        vs.update_colorize(0, coloring="statistical")
        assert vs._layers[0].coloring == "statistical"
        assert vs._layers[0].colorize_axis is None

    def test_continuous_to_statistical_keeps_its_own_cmap(self):
        """No categorical history to restore FROM -- neither
        cmap-adjustment branch should fire, so the layer just keeps
        whatever cmap it already had."""
        vs = _bare_scatter(
            [ScatterLayer(y_axis=Axis.AMPLITUDE, polarization="XX", coloring="continuous",
                         cmap=("#000000", "#ffffff"))],
        )
        vs.update_colorize(0, coloring="statistical")
        layer = vs._layers[0]
        assert layer.coloring == "statistical"
        assert layer.colorize_axis is None
        assert layer.cmap == ("#000000", "#ffffff")

    def test_still_rejects_unknown_coloring_value(self):
        vs = _bare_scatter(
            [ScatterLayer(y_axis=Axis.AMPLITUDE, polarization="XX", coloring="continuous")],
        )
        with pytest.raises(ValueError, match="coloring must be"):
            vs.update_colorize(0, coloring="bogus")
