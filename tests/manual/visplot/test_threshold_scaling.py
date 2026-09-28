"""
test_threshold_scaling.py
==========================
Tests for the "threshold" scaling function (Part 6; visplot-colorize-
by-axis-design.md §7.6/§7.10): everything under a cutoff renders
neutrally, everything at/above it in one unmissable color -- a first-
class alternative to a continuous gradient, motivated by rflag's own
default being threshold-based.

Location in repository:
    cubevis/tests/manual/visplot/test_threshold_scaling.py

Run:
    pytest cubevis/tests/manual/visplot/test_threshold_scaling.py -v

Sections
--------
1. apply_explicit_scaling    the core binary classification, in isolation
2. Registration              ALL_SCALINGS / EXPLICIT_SCALINGS / DATASHADER_HOW
                              / scaling_equation_label
3. ScalarMapping              the colorbar-curve construction, including
                              the near-step fix and its degenerate
                              (cutoff outside/at the reference range) cases
4. render_layer end-to-end    the real rendering pipeline produces a
                              genuinely binary image, not a gradient
5. resample_layer_reference   the two-level rendering Level-1 fast path
                              agrees exactly with the full render_layer path
6. span computation            scaling_vmin ALONE (no scaling_vmax) is
                              sufficient to set the threshold cutoff,
                              at both call sites in _scatter_render.py

All synthetic -- no real MS/PS needed for any test in this file.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from cubevis.toolbox.visplot import colormap_scaling as cms
from cubevis.toolbox.visplot.axes import Axis
from cubevis.toolbox.visplot.data import _scatter_render as sr
from cubevis.toolbox.visplot.data.reader import ScatterLayerSpec


# ---------------------------------------------------------------------------
# 1. apply_explicit_scaling -- the core binary classification
# ---------------------------------------------------------------------------

class TestApplyExplicitScalingThreshold:
    def test_basic_classification_and_boundary(self):
        values = np.array([0.0, 1.0, 4.9, 5.0, 5.1, 10.0, -3.0])
        out = cms.apply_explicit_scaling(values, "threshold", vmin=5.0)
        assert list(out) == [0.0, 0.0, 0.0, 1.0, 1.0, 1.0, 0.0]

    def test_nan_preserved(self):
        values = np.array([1.0, np.nan, 10.0])
        out = cms.apply_explicit_scaling(values, "threshold", vmin=5.0)
        assert out[0] == 0.0
        assert np.isnan(out[1])
        assert out[2] == 1.0

    def test_default_cutoff_uses_array_own_minimum(self):
        """vmin=None -- every value is, by construction, >= the array's
        own minimum, so everything classifies as 1.0. Not a very useful
        default in practice (the whole point of a threshold is picking
        a MEANINGFUL cutoff), but it must not crash or misbehave."""
        out = cms.apply_explicit_scaling(np.array([1.0, 2.0, 3.0]), "threshold", vmin=None)
        assert (out == 1.0).all()

    def test_all_nan_input_does_not_crash(self):
        out = cms.apply_explicit_scaling(np.array([np.nan, np.nan]), "threshold", vmin=5.0)
        assert np.isnan(out).all()

    def test_vmax_is_ignored(self):
        """Documented behavior, confirmed directly: threshold has one
        meaningful boundary, not a range -- vmax must not affect the
        classification at all."""
        values = np.array([3.0, 7.0])
        out_no_vmax = cms.apply_explicit_scaling(values, "threshold", vmin=5.0, vmax=None)
        out_with_vmax = cms.apply_explicit_scaling(values, "threshold", vmin=5.0, vmax=6.0)
        assert list(out_no_vmax) == list(out_with_vmax)


# ---------------------------------------------------------------------------
# 2. Registration
# ---------------------------------------------------------------------------

class TestRegistration:
    def test_in_all_scalings(self):
        assert "threshold" in cms.ALL_SCALINGS

    def test_in_explicit_scalings_not_datashader_how(self):
        assert "threshold" in cms.EXPLICIT_SCALINGS
        assert "threshold" not in cms.DATASHADER_HOW

    def test_equation_label(self):
        label = cms.scaling_equation_label("threshold")
        assert "cutoff" in label

    def test_scatter_layer_spec_accepts_it(self):
        spec = ScatterLayerSpec(
            y_axis=Axis.Z_SCORE, polarization="XX",
            cmap=("#222222", "#ff3333"), scaling="threshold", scaling_vmin=5.0,
        )
        assert spec.scaling == "threshold"
        assert spec.scaling_vmin == 5.0


# ---------------------------------------------------------------------------
# 3. ScalarMapping -- the colorbar-curve construction
# ---------------------------------------------------------------------------

class TestScalarMappingThreshold:
    def test_sharp_transition_not_a_linear_ramp(self):
        """Regression test: a first version of this curve sampled a
        uniform grid through apply_explicit_scaling directly, which
        __init__'s monotonicity filter collapses to just two surviving
        points -- turning the step into a shallow linear ramp across the
        WHOLE [grid_lo, cutoff] range. forward(4.0) for a cutoff of 5.0
        wrongly returned ~0.8 (mostly "toward highlighted") instead of
        ~0.0 -- found by testing this exact case directly, not by
        inspection. Must never regress to that.
        """
        mapping = cms.ScalarMapping.from_values(np.linspace(0, 10, 1000), "threshold", vmin=5.0)
        assert mapping.forward(4.0) < 0.01
        assert mapping.forward(6.0) > 0.99
        assert mapping.forward(0.0) == pytest.approx(0.0, abs=1e-6)
        assert mapping.forward(10.0) == pytest.approx(1.0, abs=1e-6)

    def test_cutoff_below_range_is_constant_highlighted(self):
        mapping = cms.ScalarMapping.from_values(np.linspace(0, 10, 1000), "threshold", vmin=-5.0)
        assert mapping.forward(0.0) == 1.0
        assert mapping.forward(10.0) == 1.0

    def test_cutoff_above_range_is_constant_neutral(self):
        mapping = cms.ScalarMapping.from_values(np.linspace(0, 10, 1000), "threshold", vmin=15.0)
        assert mapping.forward(0.0) == 0.0
        assert mapping.forward(10.0) == 0.0

    def test_cutoff_at_grid_lo(self):
        """Everything in range is >= grid_lo, so everything is >= the
        cutoff -- constant highlighted, matching the cutoff-below case."""
        mapping = cms.ScalarMapping.from_values(np.linspace(0, 10, 1000), "threshold", vmin=0.0)
        assert mapping.forward(0.0) == 1.0
        assert mapping.forward(5.0) == 1.0

    def test_default_cutoff_no_crash(self):
        mapping = cms.ScalarMapping.from_values(np.linspace(0, 10, 1000), "threshold", vmin=None)
        assert mapping is not None

    def test_tiny_span_no_crash(self):
        mapping = cms.ScalarMapping.from_values(
            np.array([5.0, 5.0000001]), "threshold", vmin=5.0,
        )
        assert mapping is not None

    def test_all_nan_reference_returns_none(self):
        mapping = cms.ScalarMapping.from_values(np.array([np.nan, np.nan]), "threshold", vmin=5.0)
        assert mapping is None

    def test_rendering_is_exact_at_boundary_even_where_colorbar_curve_is_not(self):
        """Documented, accepted narrow gap: the degenerate colorbar-curve
        fallback (cutoff exactly at grid_hi) reads the single boundary
        point as "not highlighted", while the actual per-pixel rendering
        (apply_explicit_scaling, which is what matters for correctness)
        is exact there -- confirmed directly so a future change doesn't
        assume the colorbar curve and the renderer must always agree to
        the last ULP, when only the renderer's exactness is the real
        requirement.
        """
        values = np.array([9.9999, 10.0, 10.0001])
        out = cms.apply_explicit_scaling(values, "threshold", vmin=10.0)
        assert list(out) == [0.0, 1.0, 1.0]


# ---------------------------------------------------------------------------
# Shared synthetic scatter data for sections 4-6
# ---------------------------------------------------------------------------

def _synthetic_scatter_df(seed=0, n_normal=5000, n_anomaly=200):
    rng = np.random.default_rng(seed)
    x = np.concatenate([rng.uniform(0, 100, n_normal), rng.uniform(0, 100, n_anomaly)])
    y = np.concatenate([rng.uniform(0.5, 2.0, n_normal), rng.uniform(10.0, 20.0, n_anomaly)])
    return pd.DataFrame({"x": x, "y": y})


def _threshold_layer(vmin=5.0, cmap=("#222222", "#ff3333")):
    return ScatterLayerSpec(
        y_axis=Axis.Z_SCORE, polarization="XX",
        cmap=cmap, scaling="threshold", scaling_vmin=vmin,
    )


def _distinct_nonzero_colors(image: np.ndarray) -> set:
    return set(np.unique(image[image != 0]).tolist())


# ---------------------------------------------------------------------------
# 4. render_layer end-to-end
# ---------------------------------------------------------------------------

class TestRenderLayerThreshold:
    def test_produces_a_genuinely_binary_image(self):
        df = _synthetic_scatter_df()
        layer = _threshold_layer()
        result = sr.render_layer(
            df, layer, x0=0, x1=100, y0=0, y1=100,
            canvas_w=200, canvas_h=200, color_mode="global", full_y_range=(0.0, 20.0),
        )
        assert result.skip_reason is None
        colors = _distinct_nonzero_colors(result.image)
        assert len(colors) == 2

    def test_ordinary_scaling_still_produces_a_gradient(self):
        """Sanity check that this file's tests would actually catch a
        regression: an ordinary continuous scaling on the same data
        should show noticeably more than 2 colors."""
        df = _synthetic_scatter_df()
        layer = ScatterLayerSpec(
            y_axis=Axis.Z_SCORE, polarization="XX",
            cmap=tuple(f"#{i:02x}{i:02x}{i:02x}" for i in range(0, 256, 8)),
            scaling="eq_hist",
        )
        result = sr.render_layer(
            df, layer, x0=0, x1=100, y0=0, y1=100,
            canvas_w=200, canvas_h=200, color_mode="global", full_y_range=(0.0, 20.0),
        )
        colors = _distinct_nonzero_colors(result.image)
        assert len(colors) > 2


# ---------------------------------------------------------------------------
# 5. resample_layer_reference -- Level-1 fast path parity
# ---------------------------------------------------------------------------

class TestResampleLayerReferenceThreshold:
    def test_matches_full_render_layer_path_exactly(self):
        df = _synthetic_scatter_df()
        layer = _threshold_layer()
        canvas_w, canvas_h = 200, 200

        ref = sr.build_layer_reference(
            df, layer, x0=0, x1=100, y0=0, y1=100,
            ref_w=canvas_w * 2, ref_h=canvas_h * 2,
            canvas_w=canvas_w, canvas_h=canvas_h, color_mode="global",
        )
        assert ref.ref_agg is not None

        img_arr, _n_in_view = sr.resample_layer_reference(
            ref, layer, None, x0=0, x1=100, y0=0, y1=100,
            canvas_w=canvas_w, canvas_h=canvas_h,
        )
        full_result = sr.render_layer(
            df, layer, x0=0, x1=100, y0=0, y1=100,
            canvas_w=canvas_w, canvas_h=canvas_h, color_mode="global",
            full_y_range=(0.0, 20.0),
        )
        assert _distinct_nonzero_colors(img_arr) == _distinct_nonzero_colors(full_result.image)
        assert len(_distinct_nonzero_colors(img_arr)) == 2


# ---------------------------------------------------------------------------
# 6. span computation -- scaling_vmin alone is sufficient
# ---------------------------------------------------------------------------

class TestSpanComputationVminAlone:
    def test_render_layer_vmin_alone_changes_the_cutoff(self):
        """A direct behavioral test: raising scaling_vmin should shrink
        the highlighted population, confirming the span-computation fix
        (vmin alone reaches apply_explicit_scaling as the cutoff) is
        actually wired through render_layer, not just correct in
        isolation."""
        df = _synthetic_scatter_df()
        low_cutoff = _threshold_layer(vmin=1.0)
        high_cutoff = _threshold_layer(vmin=15.0)

        result_low = sr.render_layer(
            df, low_cutoff, x0=0, x1=100, y0=0, y1=100,
            canvas_w=200, canvas_h=200, color_mode="global", full_y_range=(0.0, 20.0),
        )
        result_high = sr.render_layer(
            df, high_cutoff, x0=0, x1=100, y0=0, y1=100,
            canvas_w=200, canvas_h=200, color_mode="global", full_y_range=(0.0, 20.0),
        )
        highlight_color = 0xFF3333FF  # matches the (#222222, #ff3333) cmap's second entry, packed
        low_highlighted = int((result_low.image == highlight_color).sum())
        high_highlighted = int((result_high.image == highlight_color).sum())
        assert low_highlighted > high_highlighted > 0

    def test_ordinary_scalings_still_require_both_vmin_and_vmax(self):
        """Regression guard: the threshold-specific branch must not
        accidentally let vmin alone override span for any OTHER
        scaling -- confirmed by checking that an eq_hist layer with only
        scaling_vmin set behaves identically to one with neither set
        (i.e. the override is correctly skipped, exactly as before this
        feature existed)."""
        df = _synthetic_scatter_df()
        layer_no_override = ScatterLayerSpec(
            y_axis=Axis.Z_SCORE, polarization="XX",
            cmap=("#000000", "#ffffff"), scaling="log",
        )
        layer_vmin_only = ScatterLayerSpec(
            y_axis=Axis.Z_SCORE, polarization="XX",
            cmap=("#000000", "#ffffff"), scaling="log", scaling_vmin=5.0,
        )
        r1 = sr.render_layer(
            df, layer_no_override, x0=0, x1=100, y0=0, y1=100,
            canvas_w=200, canvas_h=200, color_mode="global", full_y_range=(0.0, 20.0),
        )
        r2 = sr.render_layer(
            df, layer_vmin_only, x0=0, x1=100, y0=0, y1=100,
            canvas_w=200, canvas_h=200, color_mode="global", full_y_range=(0.0, 20.0),
        )
        assert np.array_equal(r1.image, r2.image)
