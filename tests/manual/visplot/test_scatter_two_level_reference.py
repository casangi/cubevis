"""
test_scatter_two_level_reference.py
====================================
Tests for the scatter two-level (Level-1/Level-2) rendering machinery
introduced by the scatter two-level rendering handoff notes (2026-09).

Location in repository:
    cubevis/tests/manual/visplot/test_scatter_two_level_reference.py

Tests against:
    cubevis/cubevis/toolbox/visplot/colormap_scaling.py
        (EqualizeCurve, build_equalize_curve, apply_equalize_curve)
    cubevis/cubevis/toolbox/visplot/data/reader.py
        (ScatterLayerReference, ScatterLayerRender.reference,
        ScatterRenderResult.ref_canvas_width/height)
    cubevis/cubevis/toolbox/visplot/data/_scatter_render.py
        (_dilate_bounds, build_layer_reference, needs_level2_requery,
        resample_layer_reference, resample_id_grid, _compute_id_grid,
        _categorical_count_agg)

Run:
    pytest cubevis/tests/manual/visplot/test_scatter_two_level_reference.py -v

Sections
--------
1. EqualizeCurve                    build/apply LUT vs. equalize_histogram
2. Dilation (_dilate_bounds)        synthetic block extrema + conservativeness
3. needs_level2_requery             the OR-gate and its edge cases
4. Continuous Level-1 resample      occupancy agreement, all scalings, eq_hist
5. Categorical Level-1 resample     dim-order/transpose + agg="max" regression
6. Hover id-grid Level-1 resample   conservativeness against a true render
7. Edge cases                       empty/None df, 0-in-view, skip_reason
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("datashader")
pytest.importorskip("scipy")

from cubevis.toolbox.visplot import colormap_scaling as _cms
from cubevis.toolbox.visplot.axes import Axis
from cubevis.toolbox.visplot.data import _scatter_render as sr
from cubevis.toolbox.visplot.data.reader import ScatterLayerSpec, ScatterLayerReference


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _continuous_layer(scaling="eq_hist", **kwargs) -> ScatterLayerSpec:
    return ScatterLayerSpec(
        y_axis=Axis.AMPLITUDE, polarization="XX",
        cmap=("#000000", "#ffffff"), scaling=scaling, **kwargs,
    )


def _categorical_layer(priority="rarest", cmap=None) -> ScatterLayerSpec:
    cmap = cmap or tuple(f"#{i:02x}{i:02x}ff" for i in range(0, 256, 32))
    return ScatterLayerSpec(
        y_axis=Axis.AMPLITUDE, polarization="XX", cmap=cmap,
        coloring="categorical", colorize_axis=Axis.SCAN,
        category_priority=priority,
    )


def _synthetic_df(seed=0, n=200_000, scans=None, scan_probs=None):
    rng = np.random.default_rng(seed)
    data = {
        "x": rng.uniform(0, 1000, n),
        "y": np.abs(rng.normal(5, 3, n)),
        "time": rng.uniform(0, 1000, n),
        "baseline_id": rng.integers(0, 40, n),
        "frequency": rng.uniform(1e9, 2e9, n),
    }
    if scans is not None:
        data["scan_name"] = rng.choice(scans, n, p=scan_probs)
    return pd.DataFrame(data)


def _occupancy_agreement(img_a: np.ndarray, img_b: np.ndarray) -> float:
    return float(np.mean((img_a != 0) == (img_b != 0)))


# ---------------------------------------------------------------------------
# 1. EqualizeCurve
# ---------------------------------------------------------------------------

class TestEqualizeCurve:
    def test_build_curve_matches_equalize_histogram(self):
        """apply_equalize_curve(values, build_equalize_curve(ref)) should
        track colormap_scaling.equalize_histogram(values, reference=ref)
        closely -- not bit-identical (different nbins / uniform-vs-
        native bin edges by design, see build_equalize_curve's
        docstring), but well within one step of a 256-level color LUT.
        """
        rng = np.random.default_rng(1)
        reference = rng.exponential(2.0, 500_000)
        values = rng.exponential(2.0, 2_000)

        exact = _cms.equalize_histogram(values, reference=reference)
        curve = _cms.build_equalize_curve(reference, nbins=4096)
        fast = _cms.apply_equalize_curve(values, curve)

        assert curve is not None
        finite = np.isfinite(exact) & np.isfinite(fast)
        assert finite.mean() > 0.99
        max_dev = np.max(np.abs(exact[finite] - fast[finite]))
        assert max_dev < 0.01, f"max deviation {max_dev} too large for a color LUT"

    def test_degenerate_reference_returns_none(self):
        assert _cms.build_equalize_curve(np.array([])) is None
        assert _cms.build_equalize_curve(np.full(10, np.nan)) is None
        assert _cms.build_equalize_curve(np.full(10, 5.0)) is None  # vmax == vmin

    def test_apply_with_none_curve_is_all_nan(self):
        out = _cms.apply_equalize_curve(np.array([1.0, 2.0, np.nan]), None)
        assert np.all(np.isnan(out))

    def test_apply_clamps_outside_range(self):
        """Values outside [vmin, vmax] clamp to the curve's own end bins
        -- cdf_lut[0]/cdf_lut[-1] -- mirroring equalize_histogram's own
        np.interp(..., left=cdf[0], right=cdf[-1]) convention exactly
        (NOT literal 0.0/1.0, since the first/last bin's cumulative
        value is generally nonzero/sub-1 for a real distribution)."""
        ref = np.linspace(0, 10, 1000)
        curve = _cms.build_equalize_curve(ref, nbins=100)
        below = _cms.apply_equalize_curve(np.array([-5.0]), curve)
        above = _cms.apply_equalize_curve(np.array([50.0]), curve)
        assert below[0] == pytest.approx(curve.cdf_lut[0], abs=1e-9)
        assert above[0] == pytest.approx(curve.cdf_lut[-1], abs=1e-9)

    def test_nan_passes_through(self):
        ref = np.linspace(0, 10, 1000)
        curve = _cms.build_equalize_curve(ref)
        out = _cms.apply_equalize_curve(np.array([np.nan, 5.0]), curve)
        assert np.isnan(out[0])
        assert np.isfinite(out[1])


# ---------------------------------------------------------------------------
# 2. Dilation (_dilate_bounds) -- handoff notes §2.1
# ---------------------------------------------------------------------------

class TestDilateBounds:
    def test_synthetic_4x4_to_2x2_block_extrema(self):
        """Mirrors the handoff notes' own synthetic verification: a 4x4
        grid with block-constant values, block extrema recovered exactly
        by min/max over the raw grid; dilation only WIDENS from there,
        never narrows.
        """
        lo = np.array([
            [0, 0, 20, 20],
            [0, 0, 20, 20],
            [80, 80, 100, 100],
            [80, 80, 100, 100],
        ], dtype=np.float64)
        hi = lo + 5
        dilated_lo, dilated_hi = sr._dilate_bounds(lo, hi, radius=1)
        # Dilation must never be tighter than the original bound.
        assert np.all(dilated_lo <= lo)
        assert np.all(dilated_hi >= hi)

    def test_conservativeness_vs_true_bounds(self):
        """The correctness property the dilation fix exists for: after
        dilating a reference id-grid and resampling it (nearest) to a
        tight zoom, the reported (lo, hi) range must never be NARROWER
        than a true direct query's own (lo, hi) at that same viewport --
        checked here the same way the handoff notes did (a direct
        real-data-shaped test, not just a unit check of the filter
        itself). Regression target: the handoff notes found 3 violations
        per ~2919 cells without dilation; this asserts ZERO with it.
        """
        import datashader as ds
        import datashader.reductions as ds_agg

        rng = np.random.default_rng(3)
        n = 300_000
        df = pd.DataFrame({
            "x": rng.uniform(0, 100, n),
            "y": rng.uniform(0, 100, n),
            "time": rng.uniform(0, 5000, n),
        })
        ref_cvs = ds.Canvas(plot_width=64, plot_height=48,
                            x_range=(0, 100), y_range=(0, 100))
        ref = ref_cvs.points(df, "x", "y",
                             ds_agg.summary(t_lo=ds_agg.min("time"),
                                            t_hi=ds_agg.max("time")))
        lo_d, hi_d = sr._dilate_bounds(ref["t_lo"].values, ref["t_hi"].values)
        ref_lo_d = ref["t_lo"].copy(data=lo_d)
        ref_hi_d = ref["t_hi"].copy(data=hi_d)

        zoom_cvs = ds.Canvas(plot_width=64, plot_height=48,
                             x_range=(30, 45), y_range=(30, 45))
        lo_resampled = zoom_cvs.raster(ref_lo_d, interpolate="nearest").values
        hi_resampled = zoom_cvs.raster(ref_hi_d, interpolate="nearest").values

        truth = zoom_cvs.points(df, "x", "y",
                                ds_agg.summary(t_lo=ds_agg.min("time"),
                                               t_hi=ds_agg.max("time")))
        true_lo, true_hi = truth["t_lo"].values, truth["t_hi"].values

        valid = ~np.isnan(true_lo)
        violations = np.sum(
            valid & ((lo_resampled > true_lo + 1e-9) | (hi_resampled < true_hi - 1e-9))
        )
        assert valid.sum() > 500, "test setup produced too few valid cells"
        assert violations == 0

    def test_all_nan_neighborhood_stays_nan(self):
        lo = np.full((5, 5), np.nan)
        hi = np.full((5, 5), np.nan)
        lo_d, hi_d = sr._dilate_bounds(lo, hi)
        assert np.all(np.isnan(lo_d)) and np.all(np.isnan(hi_d))

    def test_missing_scipy_raises_clear_error(self, monkeypatch):
        monkeypatch.setattr(sr, "HAS_SCIPY", False)
        with pytest.raises(ImportError):
            sr._dilate_bounds(np.zeros((3, 3)), np.ones((3, 3)))


# ---------------------------------------------------------------------------
# 3. needs_level2_requery -- handoff notes §2.3's OR-gate
# ---------------------------------------------------------------------------

class TestNeedsLevel2Requery:
    @staticmethod
    @pytest.fixture(scope="class")
    def ref():
        df = _synthetic_df(seed=1, n=5_000)
        lyr = _continuous_layer()
        full_y = (float(df.y.min()), float(df.y.max()))
        return sr.build_layer_reference(
            df, lyr, 0.0, 100.0, *full_y, 400, 300, 200, 150, "global",
        ), full_y

    def test_coarse_zoom_within_ref_scale_stays_level1(self, ref):
        r, full_y = ref
        assert sr.needs_level2_requery(r, 10, 90, *full_y, 200, 150) is False

    def test_tight_zoom_forces_level2(self, ref):
        r, full_y = ref
        y_mid = (full_y[0] + full_y[1]) / 2
        span = (full_y[1] - full_y[0]) * 0.02
        assert sr.needs_level2_requery(
            r, 48, 52, y_mid - span, y_mid + span, 200, 150) is True

    def test_out_of_bounds_viewport_forces_level2(self, ref):
        r, full_y = ref
        assert sr.needs_level2_requery(r, -10, 90, *full_y, 200, 150) is True

    def test_degenerate_viewport_forces_level2(self, ref):
        r, full_y = ref
        assert sr.needs_level2_requery(r, 50, 50, *full_y, 200, 150) is True

    def test_none_reference_forces_level2(self, ref):
        _, full_y = ref
        assert sr.needs_level2_requery(None, 10, 90, *full_y, 200, 150) is True

    def test_skip_reason_reference_forces_level2(self, ref):
        _, full_y = ref
        empty = ScatterLayerReference(
            ref_x_range=(0, 100), ref_y_range=full_y,
            skip_reason="query returned 0 rows",
        )
        assert sr.needs_level2_requery(empty, 10, 90, *full_y, 200, 150) is True

    def test_ref_scale_controls_resolvable_zoom_depth(self):
        """REF_SCALE=N resolves zooms down to 100/N% of the full extent
        before Level-2 is required (handoff notes §2.3) -- verified here
        for N=2 (50%) and N=4 (25%)."""
        df = _synthetic_df(seed=2, n=5_000)
        full_y = (float(df.y.min()), float(df.y.max()))
        lyr = _continuous_layer()
        for ref_scale, boundary_pct in [(2.0, 0.5), (4.0, 0.25)]:
            ref_w, ref_h = int(200 * ref_scale), int(150 * ref_scale)
            ref = sr.build_layer_reference(
                df, lyr, 0.0, 1000.0, *full_y, ref_w, ref_h, 200, 150, "global",
            )
            just_inside = boundary_pct * 1000 * 1.05
            just_outside = boundary_pct * 1000 * 0.90
            assert sr.needs_level2_requery(
                ref, 0, just_inside, *full_y, 200, 150) is False
            assert sr.needs_level2_requery(
                ref, 0, just_outside, *full_y, 200, 150) is True


# ---------------------------------------------------------------------------
# 4. Continuous Level-1 resample
# ---------------------------------------------------------------------------

class TestContinuousLevel1Resample:
    @staticmethod
    @pytest.fixture(scope="class")
    def df():
        return _synthetic_df(seed=42, n=200_000)

    def test_full_view_resample_matches_direct_render_exactly(self, df):
        lyr = _continuous_layer("eq_hist")
        full_y = (float(df.y.min()), float(df.y.max()))
        ref = sr.build_layer_reference(
            df, lyr, 0.0, 1000.0, *full_y, 400, 300, 200, 150, "global",
        )
        direct = sr.render_layer(df, lyr, 0.0, 1000.0, *full_y, 200, 150,
                                 "global", full_y)
        img, n_in_view = sr.resample_layer_reference(
            ref, lyr, None, 0.0, 1000.0, *full_y, 200, 150,
        )
        assert _occupancy_agreement(img, direct.image) == 1.0
        assert n_in_view == direct.n_in_view

    @pytest.mark.parametrize("x0,x1,frac_y", [
        (200.0, 800.0, 1.0),
        (200.0, 750.0, 0.55),
    ])
    def test_zoom_within_ref_scale_has_high_occupancy_agreement(
        self, df, x0, x1, frac_y,
    ):
        lyr = _continuous_layer("eq_hist")
        full_y = (float(df.y.min()), float(df.y.max()))
        ref = sr.build_layer_reference(
            df, lyr, 0.0, 1000.0, *full_y, 400, 300, 200, 150, "global",
        )
        y1 = full_y[0] + (full_y[1] - full_y[0]) * frac_y
        assert sr.needs_level2_requery(ref, x0, x1, full_y[0], y1, 200, 150) is False
        img, n_in_view = sr.resample_layer_reference(
            ref, lyr, None, x0, x1, full_y[0], y1, 200, 150,
        )
        direct = sr.render_layer(df, lyr, x0, x1, full_y[0], y1, 200, 150,
                                 "global", full_y)
        assert _occupancy_agreement(img, direct.image) > 0.9
        assert n_in_view == direct.n_in_view

    @pytest.mark.parametrize("scaling", [
        "linear", "log", "sqrt", "square", "gamma", "power", "eq_hist",
    ])
    def test_every_scaling_executes_and_paints_pixels(self, df, scaling):
        lyr = _continuous_layer(scaling)
        full_y = (float(df.y.min()), float(df.y.max()))
        ref = sr.build_layer_reference(
            df, lyr, 0.0, 1000.0, *full_y, 400, 300, 200, 150, "global",
        )
        img, n_in_view = sr.resample_layer_reference(
            ref, lyr, None, 200.0, 800.0, *full_y, 200, 150,
        )
        assert img.shape == (150, 200)
        assert img.dtype == np.uint32
        assert np.count_nonzero(img) > 0
        assert n_in_view > 0

    def test_eq_hist_curve_only_built_in_global_mode(self, df):
        lyr = _continuous_layer("eq_hist")
        full_y = (float(df.y.min()), float(df.y.max()))
        ref_global = sr.build_layer_reference(
            df, lyr, 0.0, 1000.0, *full_y, 400, 300, 200, 150, "global",
        )
        ref_local = sr.build_layer_reference(
            df, lyr, 0.0, 1000.0, *full_y, 400, 300, 200, 150, "local",
        )
        assert ref_global.eq_curve is not None
        assert ref_local.eq_curve is None

    def test_manual_vmin_vmax_band_limits_eq_curve(self, df):
        """Mirrors render_layer's own vmin/vmax-band-limited eq_hist
        reference exactly (see build_layer_reference's docstring) --
        the cached curve must reflect the SAME band-limited population a
        Level-2 render would use, not the whole selection.
        """
        full_y = (float(df.y.min()), float(df.y.max()))
        lo, hi = full_y[0] + 1.0, full_y[1] - 1.0
        lyr = _continuous_layer("eq_hist", scaling_vmin=lo, scaling_vmax=hi)
        ref = sr.build_layer_reference(
            df, lyr, 0.0, 1000.0, *full_y, 400, 300, 200, 150, "global",
        )
        assert ref.eq_curve is not None
        assert ref.eq_curve.vmin >= lo - 1e-9
        assert ref.eq_curve.vmax <= hi + 1e-9


# ---------------------------------------------------------------------------
# 5. Categorical Level-1 resample -- dim-order/transpose + agg="max" fix
# ---------------------------------------------------------------------------

class TestCategoricalLevel1Resample:
    SCANS = [f"scan{i}" for i in range(8)]
    PROBS = np.array([30, 20, 15, 10, 8, 7, 6, 4], dtype=float)
    PROBS /= PROBS.sum()

    @staticmethod
    @pytest.fixture(scope="class")
    def df():
        return _synthetic_df(seed=7, n=200_000, scans=TestCategoricalLevel1Resample.SCANS,
                             scan_probs=TestCategoricalLevel1Resample.PROBS)

    @pytest.mark.parametrize("priority", ["rarest", "majority"])
    def test_full_view_resample_matches_direct_render_exactly(self, df, priority):
        lyr = _categorical_layer(priority)
        full_y = (float(df.y.min()), float(df.y.max()))
        full_render = sr.render_layer(df, lyr, 0.0, 1000.0, *full_y, 200, 150,
                                      "global", full_y)
        ref = sr.build_layer_reference(
            df, lyr, 0.0, 1000.0, *full_y, 400, 300, 200, 150, "global",
        )
        assert ref.ref_cube is not None
        assert ref.ref_cube.shape[-1] == len(full_render.categories)
        assert ref.ref_cube.dtype == np.float32

        img, n_in_view = sr.resample_layer_reference(
            ref, lyr, full_render.categories, 0.0, 1000.0, *full_y, 200, 150,
        )
        assert _occupancy_agreement(img, full_render.image) == 1.0
        both = (img != 0) & (full_render.image != 0)
        assert np.array_equal(img[both], full_render.image[both])

    @pytest.mark.parametrize("priority", ["rarest", "majority"])
    def test_downsample_preserves_presence_and_ranking(self, df, priority):
        """Regression test for two related bugs found while building this
        feature, both triggered by the "full view" / zoomed-out case
        (Level-1 downsampling FROM a finer reference back TO the display
        canvas's own, coarser resolution):

        1. Canvas.raster()'s default downsample reduction ("mean")
           floor-truncates a fractional mean of small integer counts to
           0 the instant it lands back in an INTEGER dtype -- silently
           erasing up to 82% of true per-category presence on a
           synthetic case. Fix: the reference cube is float32 (see
           build_layer_reference), so a fractional mean survives.
        2. A candidate fix (agg="max" instead of "mean") restored
           presence but broke "majority" priority's relative ranking
           across categories within a pixel (down to ~80% argmax
           agreement with a true render, from "mean"'s ~91%) -- "max" of
           several source cells is not a per-pixel-uniform rescaling of
           the true sum the way "mean" is, so it does not preserve which
           category is largest. The float32 fix keeps the default
           "mean" reduction, which IS such a rescaling
           (argmax(mean) == argmax(sum) exactly), giving both
           presence AND ranking correctly at once.

        This test would fail on either bug: reduced presence (bug 1)
        drops occupancy agreement, and reduced ranking fidelity (bug 2)
        drops the exact-match-where-both-occupied rate.
        """
        lyr = _categorical_layer(priority)
        full_y = (float(df.y.min()), float(df.y.max()))
        full_render = sr.render_layer(df, lyr, 0.0, 1000.0, *full_y, 200, 150,
                                      "global", full_y)
        ref = sr.build_layer_reference(
            df, lyr, 0.0, 1000.0, *full_y, 400, 300, 200, 150, "global",
        )
        img, _ = sr.resample_layer_reference(
            ref, lyr, full_render.categories, 0.0, 1000.0, *full_y, 200, 150,
        )
        occ = _occupancy_agreement(img, full_render.image)
        assert occ > 0.99, (
            f"only {occ:.1%} occupancy agreement at full view ({priority}) -- "
            "categorical downsample may be losing sparse presence again"
        )
        both = (img != 0) & (full_render.image != 0)
        exact = np.mean(img[both] == full_render.image[both]) if both.sum() else 1.0
        assert exact > 0.99, (
            f"only {exact:.1%} exact-match where both occupied ({priority}) -- "
            "categorical downsample may be losing ranking fidelity again"
        )

    @pytest.mark.parametrize("priority", ["rarest", "majority"])
    def test_zoom_within_ref_scale_has_reasonable_occupancy_agreement(
        self, df, priority,
    ):
        """Occupancy (any color at all) stays well-preserved under
        Level-1 for a zoom within REF_SCALE's valid range; EXACT
        per-pixel color agreement is deliberately not asserted tightly
        here -- a "rarest"/"majority" pick is a discontinuous,
        boundary-sensitive function of exact point membership (unlike a
        continuous mean value), so legitimate small differences from
        binning at a different (finer, then resampled) resolution can
        flip which category wins a given pixel. This is an inherent
        property of resampling a winner-take-all pick, not a bug --
        see this test module's own docstring and the two-level
        rendering handoff notes' discussion of the id-grid extrema for
        the analogous (but provably-fixable, via dilation) concern.
        """
        lyr = _categorical_layer(priority)
        full_y = (float(df.y.min()), float(df.y.max()))
        full_render = sr.render_layer(df, lyr, 0.0, 1000.0, *full_y, 200, 150,
                                      "global", full_y)
        ref = sr.build_layer_reference(
            df, lyr, 0.0, 1000.0, *full_y, 400, 300, 200, 150, "global",
        )
        x0, x1 = 200.0, 800.0
        assert sr.needs_level2_requery(ref, x0, x1, *full_y, 200, 150) is False
        img, n_in_view = sr.resample_layer_reference(
            ref, lyr, full_render.categories, x0, x1, *full_y, 200, 150,
        )
        direct = sr.render_layer(df, lyr, x0, x1, *full_y, 200, 150,
                                 "global", full_y)
        assert _occupancy_agreement(img, direct.image) > 0.9
        assert n_in_view == direct.n_in_view

    def test_high_cardinality_axis_still_binned_and_cube_shape_bounded(self):
        """A colorize axis with more distinct values than CATEGORY_CAP
        still produces a valid, boundedly-sized reference cube (see
        _bin_categories) -- guards against the reference path silently
        assuming an unbounded category count."""
        rng = np.random.default_rng(9)
        n = 50_000
        many_scans = [f"scan{i}" for i in range(60)]
        df = _synthetic_df(seed=9, n=n, scans=many_scans,
                           scan_probs=np.full(60, 1 / 60))
        lyr = _categorical_layer("rarest")
        full_y = (float(df.y.min()), float(df.y.max()))
        ref = sr.build_layer_reference(
            df, lyr, 0.0, 1000.0, *full_y, 400, 300, 200, 150, "global",
        )
        assert ref.ref_cube.shape[-1] <= sr.CATEGORY_CAP


# ---------------------------------------------------------------------------
# 6. Hover id-grid Level-1 resample
# ---------------------------------------------------------------------------

class TestIdGridLevel1Resample:
    @staticmethod
    @pytest.fixture(scope="class")
    def setup():
        df = _synthetic_df(seed=11, n=150_000)
        lyr = _continuous_layer("linear")
        full_y = (float(df.y.min()), float(df.y.max()))
        ref = sr.build_layer_reference(
            df, lyr, 0.0, 1000.0, *full_y, 400, 300, 200, 150, "global",
        )
        return df, lyr, full_y, ref

    def test_id_grid_fields_present_when_columns_available(self, setup):
        _, _, _, ref = setup
        assert ref.ref_id_value is not None
        assert ref.ref_id_t_lo is not None
        assert ref.ref_id_bl_lo is not None
        assert ref.ref_id_freq_lo is not None

    def test_resampled_id_grid_never_narrower_than_true_range(self, setup):
        df, lyr, full_y, ref = setup
        x0, x1 = 200.0, 800.0
        resampled = sr.resample_id_grid(ref, x0, x1, *full_y, 200, 150, 3072)
        truth = sr.render_layer(df, lyr, x0, x1, *full_y, 200, 150,
                                "global", full_y, probe_grid_max_cells=3072)
        valid = ~np.isnan(truth.id_grid_t_lo)
        assert valid.sum() > 100
        violations = np.sum(
            valid & (
                (resampled["t_lo"] > truth.id_grid_t_lo + 1e-6) |
                (resampled["t_hi"] < truth.id_grid_t_hi - 1e-6)
            )
        )
        assert violations == 0

    def test_none_when_reference_has_no_id_grid(self):
        empty = ScatterLayerReference(ref_x_range=(0, 1), ref_y_range=(0, 1))
        assert sr.resample_id_grid(empty, 0, 1, 0, 1, 200, 150, 3072) is None


# ---------------------------------------------------------------------------
# 7. Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_df_none_gives_not_queried_skip_reason(self):
        lyr = _continuous_layer()
        ref = sr.build_layer_reference(None, lyr, 0, 1, 0, 1, 40, 30, 20, 15, "global")
        assert ref.skip_reason == "not queried"
        assert ref.ref_agg is None and ref.ref_cube is None

    def test_empty_df_gives_query_returned_0_rows(self):
        lyr = _continuous_layer()
        empty_df = pd.DataFrame({"x": [], "y": []})
        ref = sr.build_layer_reference(
            empty_df, lyr, 0, 1, 0, 1, 40, 30, 20, 15, "global",
        )
        assert ref.skip_reason == "query returned 0 rows"

    def test_zero_in_view_gives_specific_skip_reason(self):
        lyr = _continuous_layer()
        df = pd.DataFrame({"x": [500.0] * 10, "y": [500.0] * 10})
        ref = sr.build_layer_reference(
            df, lyr, 0, 100, 0, 100, 40, 30, 20, 15, "global",
        )
        assert ref.skip_reason == "0 of 10 samples in viewport"

    def test_skip_reason_reference_resamples_to_empty_image(self):
        lyr = _continuous_layer()
        empty = ScatterLayerReference(
            ref_x_range=(0, 1), ref_y_range=(0, 1), skip_reason="query returned 0 rows",
        )
        img, n_in_view = sr.resample_layer_reference(empty, lyr, None, 0, 1, 0, 1, 20, 15)
        assert np.count_nonzero(img) == 0
        assert n_in_view == 0

    def test_categorical_skip_reason_propagates_from_categorize(self):
        """A column with no non-null values still yields a clean
        skip_reason'd reference, matching render_layer's own contract,
        not an exception."""
        lyr = _categorical_layer()
        df = pd.DataFrame({
            "x": np.random.uniform(0, 10, 100),
            "y": np.random.uniform(0, 10, 100),
            # deliberately no "scan_name" column
        })
        ref = sr.build_layer_reference(df, lyr, 0, 10, 0, 10, 40, 30, 20, 15, "global")
        assert ref.skip_reason is not None
        assert ref.ref_cube is None
