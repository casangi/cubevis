"""
test_zscore_cell_cutoff.py
============================
Tests for the n-aware Z-Score raster cutoff (Part 6, 2026-09).

Found from a live screenshot: the Z-Score raster (per-cell MAX over the
reduced samples, threshold-scaled at the per-sample cutoff 3.5) was more
than half "flagged".  That is expected statistics, not a bug: under
noise a Z-Score is Rayleigh distributed, P(Z > t) = exp(-t**2/2), so one
sample exceeds 3.5 ~0.2 % of the time, but the max of 384 channels
exceeds it ~58 % of the time.  ``zscore_cell_cutoff(n)`` holds the
per-CELL false-alarm rate at the per-sample one (Sidak correction).

1. ``zscore_cell_cutoff`` -- the math, including a Monte Carlo check.
2. The REAL pipeline (``MSv2Backend._raster_2d``) on synthetic pure
   noise: 3.5 flags most cells, the n-aware cutoff almost none.
3. Backends record ``zscore_n_reduced`` (MSv2 and MSv4, single and
   multi partition, only for Z_SCORE).
4. ``VisibilityRaster`` applies it, respects user overrides, and reset
   restores the automatic value.

Location: cubevis/tests/manual/visplot/test_zscore_cell_cutoff.py
All synthetic; no real MS needed.
"""
from __future__ import annotations

import math
import re

import numpy as np
import pytest
import xarray as xr

from cubevis.toolbox.visplot.axes import Axis
from cubevis.toolbox.visplot.data._scatter_render import (
    _DEFAULT_ZSCORE_THRESHOLD, zscore_cell_cutoff,
)
from cubevis.toolbox.visplot.data.msv2_backend import MSv2Backend
from cubevis.toolbox.visplot.data.msv4_backend import MSv4Backend
from cubevis.toolbox.visplot.selection import SelectionSpec
from cubevis.toolbox.visplot.visibility_raster import VisibilityRaster

P0 = math.exp(-0.5 * 3.5 ** 2)   # per-sample false-alarm rate at 3.5


# ---------------------------------------------------------------------------
# 1. The math
# ---------------------------------------------------------------------------

class TestZscoreCellCutoff:
    def test_one_sample_is_exactly_the_per_sample_cutoff(self):
        assert zscore_cell_cutoff(1) == 3.5

    def test_384_channels_is_about_4_9(self):
        assert 4.90 < zscore_cell_cutoff(384) < 4.93

    def test_grows_monotonically_with_n(self):
        vals = [zscore_cell_cutoff(n) for n in (1, 2, 10, 100, 384, 5000, 10 ** 6)]
        assert vals == sorted(vals) and len(set(vals)) == len(vals)

    def test_logarithmic_growth_matches_the_closed_form(self):
        """t**2 ~ c**2 + 2 ln n for small p0 -- the reason a nominal n
        is accurate enough."""
        for n in (10, 384, 10000):
            approx = math.sqrt(3.5 ** 2 + 2 * math.log(n))
            assert zscore_cell_cutoff(n) == pytest.approx(approx, abs=0.01)

    @pytest.mark.parametrize("n", [2, 50, 384, 20000])
    def test_holds_the_per_cell_false_alarm_rate_at_p0_exactly(self, n):
        """The defining identity (Sidak): P(max of n exceeds t) == p0."""
        t = zscore_cell_cutoff(n)
        p_sample = math.exp(-0.5 * t * t)
        assert 1 - (1 - p_sample) ** n == pytest.approx(P0, rel=1e-6)

    def test_custom_per_sample_cutoff(self):
        assert zscore_cell_cutoff(1, 4.0) == 4.0
        assert zscore_cell_cutoff(384, 4.0) > zscore_cell_cutoff(384, 3.5)

    @pytest.mark.parametrize("bad", [None, 0, -5, float("nan"), float("inf"), "x"])
    def test_unusable_counts_fall_back_to_the_per_sample_cutoff(self, bad):
        assert zscore_cell_cutoff(bad) == 3.5

    def test_monte_carlo_agrees(self):
        """Draw the null directly (Rayleigh) and check the empirical
        per-cell false-alarm rate at the returned cutoff."""
        rng = np.random.default_rng(1)
        n, cells = 384, 60000
        z = np.sqrt(-2.0 * np.log(rng.random((cells, n))))   # Rayleigh(1)
        rate_at_cutoff = (z.max(axis=1) > zscore_cell_cutoff(n)).mean()
        rate_at_3p5 = (z.max(axis=1) > 3.5).mean()
        assert rate_at_cutoff == pytest.approx(P0, abs=0.0015)   # ~0.0022
        assert rate_at_3p5 > 0.5                                  # the bug


# ---------------------------------------------------------------------------
# 2. The real pipeline on pure noise
# ---------------------------------------------------------------------------

def _bare_v2(ds):
    b = MSv2Backend.__new__(MSv2Backend)
    b._datatree = object()
    b._path = "synthetic"
    b._iter_visibility_partitions = lambda selection: iter(ds if isinstance(ds, list) else [ds])
    b._apply_selection = lambda raw, selection: raw
    return b


def _bare_v4(ds, mode="interferometer"):
    b = MSv4Backend.__new__(MSv4Backend)
    b._datatree = object()
    b._resolved_mode = mode
    b._iter_visibility_partitions = lambda selection: iter(ds if isinstance(ds, list) else [ds])
    b._apply_selection = lambda raw, selection: raw
    return b


def _noise_ds(n_time=60, n_bl=30, n_freq=384, seed=0, sigma=1.0):
    rng = np.random.default_rng(seed)
    shape = (n_time, n_bl, n_freq, 1)
    vis = sigma * (rng.normal(size=shape) + 1j * rng.normal(size=shape)) + 5.0
    return xr.Dataset(
        data_vars={
            "VISIBILITY": (("time", "baseline_id", "frequency", "polarization"), vis),
            "FLAG": (("time", "baseline_id", "frequency", "polarization"),
                     np.zeros(shape, dtype=bool)),
        },
        coords={"time": np.arange(n_time, dtype=float),
                "baseline_id": np.arange(n_bl),
                "frequency": np.linspace(1e9, 1.1e9, n_freq),
                "polarization": ["XX"]},
    )


class TestRealPipelineOnPureNoise:
    def _agg(self):
        ds = _noise_ds()
        arr, _, _, _ = _bare_v2(ds).query_raster(
            Axis.TIME, Axis.BASELINE, Axis.Z_SCORE, SelectionSpec(), polarization="XX")
        return arr

    def test_per_sample_cutoff_flags_most_noise_cells(self):
        """Documents the problem this module fixes."""
        agg = self._agg()
        assert (agg.values > 3.5).mean() > 0.40

    def test_cell_cutoff_flags_almost_none(self):
        agg = self._agg()
        cutoff = zscore_cell_cutoff(agg.attrs["zscore_n_reduced"])
        assert (agg.values > cutoff).mean() < 0.02

    def test_n_matches_the_channel_count(self):
        assert self._agg().attrs["zscore_n_reduced"] == 384

    def test_a_real_outlier_still_stands_out_above_the_cell_cutoff(self):
        ds = _noise_ds()
        ds["VISIBILITY"].values[10, 7, 100, 0] += 30.0      # one bad sample
        arr, _, _, _ = _bare_v2(ds).query_raster(
            Axis.TIME, Axis.BASELINE, Axis.Z_SCORE, SelectionSpec(), polarization="XX")
        cutoff = zscore_cell_cutoff(arr.attrs["zscore_n_reduced"])
        vals = arr.values
        idx = (10, 7) if arr.dims == ("time", "baseline_id") else (7, 10)
        assert vals[idx] > 3 * cutoff


# ---------------------------------------------------------------------------
# 3. Backends record n
# ---------------------------------------------------------------------------

class TestBackendsRecordN:
    @pytest.mark.parametrize("make", [_bare_v2, _bare_v4])
    def test_z_score_records_the_reduced_channel_count(self, make):
        ds = _noise_ds(n_time=6, n_bl=4, n_freq=32)
        arr, *_ = make(ds).query_raster(
            Axis.TIME, Axis.BASELINE, Axis.Z_SCORE, SelectionSpec(), polarization="XX")
        assert arr.attrs["zscore_n_reduced"] == 32

    @pytest.mark.parametrize("make", [_bare_v2, _bare_v4])
    def test_freq_by_baseline_reduces_over_time(self, make):
        ds = _noise_ds(n_time=6, n_bl=4, n_freq=32)
        arr, *_ = make(ds).query_raster(
            Axis.FREQUENCY, Axis.BASELINE, Axis.Z_SCORE, SelectionSpec(), polarization="XX")
        assert arr.attrs["zscore_n_reduced"] == 6

    @pytest.mark.parametrize("make", [_bare_v2, _bare_v4])
    def test_time_by_frequency_single_baseline_reduces_nothing(self, make):
        ds = _noise_ds(n_time=6, n_bl=4, n_freq=32).isel(baseline_id=[1])
        arr, *_ = make(ds).query_raster(
            Axis.TIME, Axis.FREQUENCY, Axis.Z_SCORE, SelectionSpec(), polarization="XX")
        assert arr.attrs["zscore_n_reduced"] == 1
        assert zscore_cell_cutoff(1) == 3.5

    @pytest.mark.parametrize("make", [_bare_v2, _bare_v4])
    def test_other_quantities_carry_no_count(self, make):
        ds = _noise_ds(n_time=6, n_bl=4, n_freq=32)
        arr, *_ = make(ds).query_raster(
            Axis.TIME, Axis.BASELINE, Axis.AMPLITUDE, SelectionSpec(), polarization="XX")
        assert "zscore_n_reduced" not in arr.attrs

    @pytest.mark.parametrize("make", [_bare_v2, _bare_v4])
    def test_multiple_partitions_use_the_largest_count(self, make):
        a = _noise_ds(n_time=6, n_bl=4, n_freq=16, seed=1)
        b = _noise_ds(n_time=6, n_bl=4, n_freq=64, seed=2)
        b = b.assign_coords(baseline_id=np.arange(4, 8))   # disjoint rows
        arr, *_ = make([a, b]).query_raster(
            Axis.TIME, Axis.BASELINE, Axis.Z_SCORE, SelectionSpec(), polarization="XX")
        assert arr.attrs["zscore_n_reduced"] == 64


# ---------------------------------------------------------------------------
# 4. VisibilityRaster
# ---------------------------------------------------------------------------

def _agg_with_n(n):
    return xr.DataArray(np.zeros((2, 2)), dims=("a", "b"),
                        attrs={"zscore_n_reduced": n} if n is not None else {})


def _raster(quantity=Axis.AMPLITUDE, agg=None):
    r = VisibilityRaster.__new__(VisibilityRaster)
    r._quantity = quantity
    r._polarization = "XX"
    r._y_dim, r._x_dim, r._title = Axis.BASELINE, Axis.TIME, None
    r._scaling, r._scaling_vmin, r._scaling_vmax = "eq_hist", None, None
    r._scaling_alpha = r._scaling_gamma = 1.0
    r._zscore_vmin_auto = False
    r._agg = agg
    r._selection = object()
    r._render = lambda sel: None
    r._notify_axes_changed = lambda: None
    r._x_range = r._y_range = (0.0, 1.0)
    r._image_source = None
    r._update_state_source = lambda: None
    r._shade_viewport = lambda xr_, yr_: np.zeros((1, 1), dtype=np.uint32)
    return r


AUTO = zscore_cell_cutoff(384)


class TestVisibilityRasterAppliesIt:
    def test_switch_to_z_score_is_provisional_then_n_aware_after_render(self):
        r = _raster(Axis.AMPLITUDE, agg=object())
        r.update_axes(quantity=Axis.Z_SCORE)
        assert r._scaling == "threshold" and r._scaling_vmin == 3.5
        assert r._zscore_vmin_auto is True
        r._apply_zscore_cell_cutoff(_agg_with_n(384))     # what _render does
        assert r._scaling_vmin == pytest.approx(AUTO)

    def test_missing_count_leaves_the_provisional_cutoff(self):
        r = _raster(Axis.Z_SCORE)
        r._scaling, r._scaling_vmin, r._zscore_vmin_auto = "threshold", 3.5, True
        r._apply_zscore_cell_cutoff(_agg_with_n(None))
        assert r._scaling_vmin == 3.5

    def test_none_agg_is_a_no_op(self):
        r = _raster(Axis.Z_SCORE)
        r._scaling, r._scaling_vmin, r._zscore_vmin_auto = "threshold", 3.5, True
        r._apply_zscore_cell_cutoff(None)
        assert r._scaling_vmin == 3.5

    def test_other_quantity_is_ignored(self):
        r = _raster(Axis.AMPLITUDE)
        r._scaling, r._scaling_vmin, r._zscore_vmin_auto = "threshold", 3.5, True
        r._apply_zscore_cell_cutoff(_agg_with_n(384))
        assert r._scaling_vmin == 3.5

    def test_recomputed_on_every_render_while_automatic(self):
        r = _raster(Axis.Z_SCORE)
        r._scaling, r._scaling_vmin, r._zscore_vmin_auto = "threshold", 3.5, True
        r._apply_zscore_cell_cutoff(_agg_with_n(384))
        first = r._scaling_vmin
        r._apply_zscore_cell_cutoff(_agg_with_n(6))       # e.g. axes swapped
        assert r._scaling_vmin < first
        assert r._scaling_vmin == pytest.approx(zscore_cell_cutoff(6))


class TestUserIntentIsRespected:
    def _auto_raster(self):
        r = _raster(Axis.Z_SCORE)
        r._scaling, r._scaling_vmin, r._zscore_vmin_auto = "threshold", AUTO, True
        return r

    def test_an_explicit_vmin_stops_the_automatic_cutoff(self):
        r = self._auto_raster()
        r.update_scaling(vmin=6.0)
        assert r._zscore_vmin_auto is False and r._scaling_vmin == 6.0
        r._apply_zscore_cell_cutoff(_agg_with_n(384))
        assert r._scaling_vmin == 6.0                     # not overridden

    def test_switching_to_another_scaling_stops_it(self):
        r = self._auto_raster()
        r.update_scaling(scaling="log")
        assert r._zscore_vmin_auto is False

    def test_choosing_threshold_again_does_not_by_itself_disarm(self):
        r = self._auto_raster()
        r.update_scaling(scaling="threshold")
        assert r._zscore_vmin_auto is True

    def test_vmax_alone_does_not_disarm(self):
        """vmax is unused by threshold scaling."""
        r = self._auto_raster()
        r.update_scaling(vmax=9.0)
        assert r._zscore_vmin_auto is True

    def test_reset_restores_the_automatic_value(self):
        r = self._auto_raster()
        r._agg = _agg_with_n(384)
        r.update_scaling(vmin=6.0)
        r.update_scaling(reset_range=True)
        assert r._zscore_vmin_auto is True
        assert r._scaling_vmin == pytest.approx(AUTO)

    def test_reset_without_data_yet_falls_back_to_the_provisional_cutoff(self):
        r = self._auto_raster()
        r._agg = None
        r.update_scaling(vmin=6.0)
        r.update_scaling(reset_range=True)
        assert r._scaling_vmin == 3.5

    def test_reset_on_a_non_zscore_raster_is_unchanged(self):
        r = _raster(Axis.AMPLITUDE, agg=_agg_with_n(384))
        r._scaling_vmin = 2.0
        r.update_scaling(reset_range=True)
        assert r._scaling_vmin is None and r._zscore_vmin_auto is False


class TestRenderOrdering:
    def test_cutoff_is_applied_before_anything_is_shaded(self):
        """_render must set the cutoff immediately after storing the agg
        and before the shading that reads it (source-order check)."""
        import inspect
        src = inspect.getsource(VisibilityRaster._render)
        i_store = src.index("self._agg          = agg")
        i_apply = src.index("self._apply_zscore_cell_cutoff(agg)")
        shade_calls = [m.start() for m in re.finditer(r"self\._shade\w*\(", src)]
        assert shade_calls, "expected _render to call a _shade* method"
        assert i_store < i_apply < min(shade_calls)
