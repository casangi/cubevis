"""
test_statistical_palette.py
=============================
Tests for giving "statistical" scatter layers the RASTER's ramp (Part 6,
2026-09), found from a live screenshot where a statistical-colored
scatter did not "pop".

Why: the scatter ramps are density ramps, conditioned so a sparse pixel
survives alpha blending -- which keeps their low end bright.  For a
statistical layer (threshold-scaled: below the cutoff = ramp start at
low alpha, above = ramp end at full alpha) that made the NORMAL class as
loud as the flagged one and left the flagged end a pale, washed-out
tone.  The raster's opaque ramp (plasma: deep blue -> yellow) recedes for
normal samples, makes flagged ones unmistakable, and gives both panels
the same "yellow = flagged" language.  (Checked by rendering the same
synthetic layer with each ramp and looking at the composited result.)

Covered: layer construction, the scatter's own ramp bookkeeping (set /
theme change / programmatic mode switches), and that nothing changes
when no statistical ramp is set.

Location: cubevis/tests/manual/visplot/test_statistical_palette.py
"""
from __future__ import annotations

import pytest

from cubevis.toolbox.visplot.axes import Axis
from cubevis.toolbox.visplot.visibility_scatter import ScatterLayer, VisibilityScatter
from cubevis.toolbox.visplot import visibility_plotter as vp
from cubevis.toolbox.visplot import palettes

FAMILY = (("#100000", "#ff0000"), ("#001000", "#00ff00"))
STAT = ("#0d0887", "#7e03a8", "#f0f921")
STAT_OVERRIDE = {"coloring": "statistical"}


# ---------------------------------------------------------------------------
# _make_scatter_layers
# ---------------------------------------------------------------------------

class TestMakeScatterLayersRamp:
    def test_statistical_layers_get_the_statistical_ramp(self):
        a, b = vp._make_scatter_layers(
            Axis.AMPLITUDE, ["XX", "YY"], cmaps=FAMILY,
            colorize_overrides=[STAT_OVERRIDE, STAT_OVERRIDE], statistical_cmap=STAT)
        assert a.cmap == b.cmap == STAT

    def test_other_modes_keep_the_ordinary_family(self):
        cont, cat = vp._make_scatter_layers(
            Axis.AMPLITUDE, ["XX", "YY"], cmaps=FAMILY,
            colorize_overrides=[None, {"coloring": "categorical", "colorize_axis": "SCAN"}],
            statistical_cmap=STAT)
        assert cont.cmap == FAMILY[0]
        assert cat.cmap != STAT and cat.cmap != FAMILY[1]     # categorical palette

    def test_mixed_layers(self):
        cont, stat = vp._make_scatter_layers(
            Axis.AMPLITUDE, ["XX", "YY"], cmaps=FAMILY,
            colorize_overrides=[None, STAT_OVERRIDE], statistical_cmap=STAT)
        assert cont.cmap == FAMILY[0] and stat.cmap == STAT

    def test_without_a_statistical_ramp_behaviour_is_unchanged(self):
        (lyr,) = vp._make_scatter_layers(
            Axis.AMPLITUDE, ["YY"], cmaps=FAMILY, colorize_overrides=[STAT_OVERRIDE])
        assert lyr.cmap == FAMILY[0]

    def test_ramp_is_stored_as_a_tuple(self):
        (lyr,) = vp._make_scatter_layers(
            Axis.AMPLITUDE, ["XX"], cmaps=FAMILY,
            colorize_overrides=[STAT_OVERRIDE], statistical_cmap=list(STAT))
        assert isinstance(lyr.cmap, tuple)


# ---------------------------------------------------------------------------
# VisibilityScatter bookkeeping
# ---------------------------------------------------------------------------

def _scatter(colorings, stat=None):
    vs = VisibilityScatter.__new__(VisibilityScatter)
    vs._layer_cmaps = list(FAMILY)
    vs._statistical_cmap = stat
    vs._layers = [
        ScatterLayer(y_axis=Axis.AMPLITUDE, polarization=p, cmap=FAMILY[i % 2], coloring=c,
                     **({"colorize_axis": Axis.SCAN} if c == "categorical" else {}))
        for i, (p, c) in enumerate(zip(("XX", "YY", "XX")[:len(colorings)], colorings))
    ]
    vs._layer_continuous_cmap_backup = {}
    vs._theme_hint = lambda: "dark"
    vs._rerender = lambda: None
    vs._reshade = lambda: None
    vs._state_source = None
    return vs


class TestSetLayerCmaps:
    def test_statistical_layers_keep_the_statistical_ramp_on_a_theme_change(self):
        vs = _scatter(["continuous", "statistical"], stat=STAT)
        new_family = (("#200000", "#ee0000"), ("#002000", "#00ee00"))
        vs.set_layer_cmaps(new_family, statistical_cmap=("#111", "#eee"))
        assert vs._layers[0].cmap == new_family[0]
        assert vs._layers[1].cmap == ("#111", "#eee")       # new theme's ramp

    def test_omitting_it_keeps_the_previous_statistical_ramp(self):
        vs = _scatter(["statistical"], stat=STAT)
        vs.set_layer_cmaps(FAMILY)
        assert vs._layers[0].cmap == STAT

    def test_no_statistical_ramp_ever_set_is_unchanged_behaviour(self):
        vs = _scatter(["continuous", "statistical"])
        vs.set_layer_cmaps(FAMILY)
        assert vs._layers[0].cmap == FAMILY[0] and vs._layers[1].cmap == FAMILY[1]

    def test_set_statistical_cmap_only_stores(self):
        vs = _scatter(["continuous"])
        called = []
        vs._reshade = lambda: called.append(1)
        vs.set_statistical_cmap(STAT)
        assert vs._statistical_cmap == STAT and not called
        vs.set_statistical_cmap(None)
        assert vs._statistical_cmap is None


class TestUpdateColorizeSwapsRamps:
    def test_continuous_to_statistical_uses_the_statistical_ramp(self):
        vs = _scatter(["continuous"], stat=STAT)
        vs.update_colorize(0, coloring="statistical")
        assert vs._layers[0].cmap == STAT

    def test_statistical_to_continuous_restores_the_family(self):
        vs = _scatter(["statistical"], stat=STAT)
        vs._layers[0] = ScatterLayer(y_axis=Axis.AMPLITUDE, polarization="XX",
                                     cmap=STAT, coloring="statistical")
        vs.update_colorize(0, coloring="continuous")
        assert vs._layers[0].cmap == FAMILY[0]

    def test_categorical_to_statistical_uses_the_statistical_ramp(self):
        vs = _scatter(["categorical"], stat=STAT)
        vs._layer_continuous_cmap_backup[0] = FAMILY[0]
        vs.update_colorize(0, coloring="statistical")
        assert vs._layers[0].cmap == STAT

    def test_statistical_via_categorical_back_to_continuous_gets_the_family(self):
        """The statistical ramp must not be smuggled back out through the
        categorical detour's backup."""
        vs = _scatter(["statistical"], stat=STAT)
        vs._layers[0] = ScatterLayer(y_axis=Axis.AMPLITUDE, polarization="XX",
                                     cmap=STAT, coloring="statistical")
        vs.update_colorize(0, coloring="categorical", colorize_axis=Axis.SCAN)
        vs.update_colorize(0, coloring="continuous")
        assert vs._layers[0].cmap == FAMILY[0]

    def test_repeated_statistical_is_idempotent(self):
        vs = _scatter(["continuous"], stat=STAT)
        vs.update_colorize(0, coloring="statistical")
        vs.update_colorize(0, coloring="statistical")
        assert vs._layers[0].cmap == STAT

    def test_no_statistical_ramp_leaves_existing_behaviour(self):
        vs = _scatter(["continuous"])
        vs.update_colorize(0, coloring="statistical")
        assert vs._layers[0].cmap == FAMILY[0]


class TestDefaultCmapFill:
    def test_a_cmapless_statistical_layer_gets_the_statistical_ramp(self):
        vs = _scatter(["continuous"], stat=STAT)
        out = vs._with_default_cmaps([
            ScatterLayer(y_axis=Axis.AMPLITUDE, polarization="XX", coloring="statistical"),
            ScatterLayer(y_axis=Axis.AMPLITUDE, polarization="YY"),
        ])
        assert out[0].cmap == STAT
        assert out[1].cmap == FAMILY[1]


# ---------------------------------------------------------------------------
# The real palettes
# ---------------------------------------------------------------------------

class TestRealRamps:
    def test_the_raster_ramp_is_what_the_plotter_hands_out(self):
        """The plotter passes self._raster_ramp; sanity-check it really is
        a different (opaque-oriented) ramp from the scatter family."""
        raster = palettes.raster_cmap(None, "dark")
        scatter = palettes.scatter_cmaps(None, "dark")
        assert tuple(raster) not in {tuple(c) for c in scatter}
        assert len(raster) >= 3


# ---------------------------------------------------------------------------
# Compositor: a statistical layer keeps the shader's per-pixel alpha
# ---------------------------------------------------------------------------
# Found from a live screenshot: _collapse_and_composite overwrote EVERY
# non-categorical layer's per-pixel alpha with a density-derived
# auto_alpha (80..255). For a threshold-scaled statistical layer that
# dimmed flagged pixels to the same ~80 alpha as normal ones, so the
# flagged yellow read as dull olive. A statistical layer now behaves like
# a categorical one (it answers "which", not "how dense"), sits above
# continuous layers, and every layer's flagged pixels are drawn after
# every layer's normal ones so one polarization can't tint another's.

import numpy as np

NORMAL_A, FLAG_A = 90, 255


def _px(a, r, g, b):
    return np.uint32((a << 24) | (b << 16) | (g << 8) | r)


def _img(*cells, shape=(1, 4)):
    arr = np.zeros(shape, dtype=np.uint32)
    for (y, x), val in cells:
        arr[y, x] = val
    return arr


def _composite_scatter(layers, images, n_in_view=10_000_000):
    vs = VisibilityScatter.__new__(VisibilityScatter)
    vs._layers = layers
    vs._layer_images = images
    vs._layer_n_in_view = [n_in_view] * len(layers)
    vs._canvas_width, vs._canvas_height = 4, 1
    vs._effective_skip_reason = lambda i: None
    return vs, vs._collapse_and_composite()


def _lyr(coloring, alpha=1.0, pol="XX"):
    return ScatterLayer(y_axis=Axis.AMPLITUDE, polarization=pol, cmap=FAMILY[0],
                        coloring=coloring, alpha=alpha)


def _alpha(word):
    return int(word) >> 24


class TestCompositorStatisticalAlpha:
    def test_flagged_pixel_stays_fully_opaque_even_when_dense(self):
        normal, flagged = _px(NORMAL_A, 13, 8, 135), _px(FLAG_A, 240, 249, 33)
        _vs, out = _composite_scatter(
            [_lyr("statistical")], [_img(((0, 0), normal), ((0, 1), flagged))])
        assert _alpha(out[0, 0]) == NORMAL_A          # not overwritten
        assert out[0, 1] == flagged                    # exact, opaque yellow
        assert out[0, 2] == 0

    def test_a_continuous_layer_is_still_density_dimmed(self):
        """Regression guard: the old rule still applies to continuous."""
        px = _px(FLAG_A, 240, 249, 33)
        _vs, out = _composite_scatter([_lyr("continuous")], [_img(((0, 0), px))])
        assert _alpha(out[0, 0]) < FLAG_A              # auto_alpha at 10M points

    def test_layer_alpha_scales_the_shaders_own_alpha(self):
        px = _px(FLAG_A, 240, 249, 33)
        _vs, out = _composite_scatter([_lyr("statistical", alpha=0.5)],
                                      [_img(((0, 0), px))])
        assert _alpha(out[0, 0]) == 127

    def test_single_layer_composite_equals_its_image(self):
        img = _img(((0, 0), _px(NORMAL_A, 13, 8, 135)), ((0, 1), _px(FLAG_A, 240, 249, 33)))
        _vs, out = _composite_scatter([_lyr("statistical")], [img.copy()])
        assert (out == img).all()


class TestCompositorFlaggedWins:
    def test_a_later_layers_normal_pixel_does_not_tint_an_earlier_flagged_one(self):
        yellow, blue = _px(FLAG_A, 240, 249, 33), _px(NORMAL_A, 13, 8, 135)
        _vs, out = _composite_scatter(
            [_lyr("statistical", pol="XX"), _lyr("statistical", pol="YY")],
            [_img(((0, 0), yellow)), _img(((0, 0), blue))])
        assert out[0, 0] == yellow          # exact -- not the olive blend

    def test_an_earlier_layers_normal_pixel_does_not_hide_a_later_flagged_one(self):
        yellow, blue = _px(FLAG_A, 240, 249, 33), _px(NORMAL_A, 13, 8, 135)
        _vs, out = _composite_scatter(
            [_lyr("statistical", pol="XX"), _lyr("statistical", pol="YY")],
            [_img(((0, 0), blue)), _img(((0, 0), yellow))])
        assert out[0, 0] == yellow

    def test_two_normal_pixels_still_blend_as_before(self):
        blue = _px(NORMAL_A, 13, 8, 135)
        _vs, out = _composite_scatter(
            [_lyr("statistical", pol="XX"), _lyr("statistical", pol="YY")],
            [_img(((0, 0), blue)), _img(((0, 0), blue))])
        assert NORMAL_A < _alpha(out[0, 0]) < FLAG_A   # Porter-Duff over

    def test_no_overlap_leaves_each_pixel_exact(self):
        yellow, blue = _px(FLAG_A, 240, 249, 33), _px(NORMAL_A, 13, 8, 135)
        _vs, out = _composite_scatter(
            [_lyr("statistical", pol="XX"), _lyr("statistical", pol="YY")],
            [_img(((0, 0), yellow)), _img(((0, 3), blue))])
        assert out[0, 0] == yellow and out[0, 3] == blue


class TestStackOrderWithStatistical:
    def test_continuous_then_statistical_then_categorical(self):
        vs = _scatter(["categorical", "statistical", "continuous"])
        assert vs._stack_order() == [2, 1, 0]

    def test_without_statistical_layers_order_is_unchanged(self):
        vs = _scatter(["categorical", "continuous", "continuous"])
        assert vs._stack_order() == [1, 2, 0]

    def test_a_statistical_layer_is_drawn_over_a_continuous_one(self):
        cont_px, stat_px = _px(200, 10, 10, 10), _px(FLAG_A, 240, 249, 33)
        _vs, out = _composite_scatter(
            [_lyr("statistical", pol="XX"), _lyr("continuous", pol="YY")],
            [_img(((0, 0), stat_px)), _img(((0, 0), cont_px))])
        assert out[0, 0] == stat_px         # opaque flagged pixel on top
