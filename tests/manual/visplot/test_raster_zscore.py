"""
test_raster_zscore.py
=======================
Tests for Axis.Z_SCORE as a raster quantity (Part 6, 2026-09;
visplot-colorize-by-axis-design.md §7.4/§7.6): raster's own version of
Slice 1's per-baseline Z-Score, computed natively over MSv2Backend's
xarray/dask ND arrays (raster never builds the flat pandas DataFrame
scatter's own compute_baseline_zscore expects).

Location in repository:
    cubevis/tests/manual/visplot/test_raster_zscore.py

Run:
    pytest cubevis/tests/manual/visplot/test_raster_zscore.py -v

Design note this file exists to verify, not just assume: every raster
axis combination MSv2Backend.query_raster documents already guarantees
an unambiguous, single-baseline reference population per pixel --
TIME x BASELINE and FREQUENCY x BASELINE both have baseline_id as an
explicit axis (one baseline per row/column); TIME x FREQUENCY already
requires selection.baselines to narrow to a single baseline first.
There is no "many baselines folded into one pixel, neither axis is
baseline" configuration in the existing architecture, so this file does
not test for one.

What IS a real design decision, tested directly here: the reduction
over whatever gets averaged out (frequency for TIME x BASELINE, time
for FREQUENCY x BASELINE) uses MAX for Z_SCORE, not the MEAN every
other raster quantity uses -- deliberately, since Z-Score is already
per-baseline normalized (comparable across samples the way a raw
physical quantity is not) and the whole point of a Z-Score raster is
spotting an outlier at a glance, which mean aggregation would dilute.

All synthetic -- no real MS/PS needed for any test in this file.
"""
from __future__ import annotations

import numpy as np
import pytest
import xarray as xr

from cubevis.toolbox.visplot.axes import Axis
from cubevis.toolbox.visplot.data.msv2_backend import MSv2Backend
from cubevis.toolbox.visplot.selection import SelectionSpec


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _bare_backend():
    b = MSv2Backend.__new__(MSv2Backend)
    b._datatree = object()
    b._path = "synthetic"
    return b


def _dataset_with_outlier(n_time=10, n_baseline=5, n_freq=8, pols=("XX",),
                           outlier_time=3, outlier_baseline=2, outlier_freq=4,
                           outlier_value=complex(50.0, 50.0), seed=7,
                           flag_outlier=False):
    rng = np.random.default_rng(seed)
    shape = (n_time, n_baseline, n_freq, len(pols))
    vis = rng.normal(5.0, 0.05, shape) + 1j * rng.normal(0.0, 0.05, shape)
    vis[outlier_time, outlier_baseline, outlier_freq, 0] = outlier_value
    flag = np.zeros(shape, dtype=bool)
    if flag_outlier:
        flag[outlier_time, outlier_baseline, outlier_freq, 0] = True
    return xr.Dataset(
        data_vars={"VISIBILITY": (("time", "baseline_id", "frequency", "polarization"), vis),
                   "FLAG": (("time", "baseline_id", "frequency", "polarization"), flag)},
        coords={"time": np.arange(n_time, dtype=np.float64),
                "baseline_id": np.arange(n_baseline),
                "frequency": np.linspace(1e9, 1.1e9, n_freq),
                "polarization": list(pols)},
    )


def _backend_with_dataset(ds):
    backend = _bare_backend()
    backend._iter_visibility_partitions = lambda selection: iter([ds])
    backend._apply_selection = lambda raw_ds, selection: raw_ds
    return backend


# ---------------------------------------------------------------------------
# 1. Core: max aggregation preserves the outlier signal
# ---------------------------------------------------------------------------

class TestZScoreRasterMaxAggregation:
    def test_time_by_baseline_outlier_dominates_its_own_pixel(self):
        ds = _dataset_with_outlier()
        backend = _backend_with_dataset(ds)
        arr, _, _, _ = backend.query_raster(
            Axis.TIME, Axis.BASELINE, Axis.Z_SCORE, SelectionSpec(), polarization="XX",
        )
        vals = arr.values
        idx = (3, 2) if arr.dims == ("time", "baseline_id") else (2, 3)
        outlier_val = vals[idx]
        others = vals.copy()
        others[idx] = np.nan
        assert outlier_val > 10 * np.nanmax(others), (
            "the outlier's own pixel must dominate every other pixel; "
            "a value close to the others would mean mean aggregation "
            "diluted it"
        )

    def test_frequency_by_baseline_also_preserves_the_outlier(self):
        ds = _dataset_with_outlier()
        backend = _backend_with_dataset(ds)
        arr, _, _, _ = backend.query_raster(
            Axis.FREQUENCY, Axis.BASELINE, Axis.Z_SCORE, SelectionSpec(), polarization="XX",
        )
        assert np.nanmax(arr.values) > 100

    def test_flagging_the_outlier_removes_its_influence_entirely(self):
        ds = _dataset_with_outlier(flag_outlier=True)
        backend = _backend_with_dataset(ds)
        arr, _, _, _ = backend.query_raster(
            Axis.TIME, Axis.BASELINE, Axis.Z_SCORE, SelectionSpec(), polarization="XX",
        )
        assert np.nanmax(arr.values) < 10

    def test_time_by_frequency_single_baseline_shows_the_outlier(self):
        """TIME x FREQUENCY requires selection.baselines to narrow to one
        baseline first -- simulated here directly via a pre-filtered
        single-baseline dataset, matching what _apply_selection would
        hand to _raster_2d for that combination."""
        ds = _dataset_with_outlier()
        single = ds.isel(baseline_id=[2])   # the baseline carrying the outlier
        backend = _backend_with_dataset(single)
        arr, _, _, _ = backend.query_raster(
            Axis.TIME, Axis.FREQUENCY, Axis.Z_SCORE,
            SelectionSpec(baselines=[("A1", "A2")]), polarization="XX",
        )
        assert np.nanmax(arr.values) > 100


# ---------------------------------------------------------------------------
# 2. Regression: every other quantity still uses mean, unaffected
# ---------------------------------------------------------------------------

class TestOtherQuantitiesUnaffected:
    def test_amplitude_still_uses_mean_not_max(self):
        """The same outlier, same dataset -- Amplitude's own pixel value
        must reflect an AVERAGE across frequency (a middling number, not
        a value anywhere near the raw outlier's own huge amplitude),
        confirming the new Z_SCORE-specific branch didn't change every
        other quantity's own reduction."""
        ds = _dataset_with_outlier()
        backend = _backend_with_dataset(ds)
        arr, _, _, _ = backend.query_raster(
            Axis.TIME, Axis.BASELINE, Axis.AMPLITUDE, SelectionSpec(), polarization="XX",
        )
        vals = arr.values
        idx = (3, 2) if arr.dims == ("time", "baseline_id") else (2, 3)
        outlier_amp = abs(complex(50.0, 50.0))
        # Mean over 8 frequency samples (7 normal ~5.0, 1 outlier ~70.7)
        # lands well below the raw outlier amplitude itself.
        assert vals[idx] < outlier_amp / 2

    def test_phase_and_real_and_imaginary_and_flag_all_still_work(self):
        ds = _dataset_with_outlier()
        for qty in (Axis.PHASE, Axis.REAL, Axis.IMAGINARY, Axis.FLAG):
            backend = _backend_with_dataset(ds)
            arr, _, _, _ = backend.query_raster(
                Axis.TIME, Axis.BASELINE, qty, SelectionSpec(), polarization="XX",
            )
            assert arr.shape == (10, 5) or arr.shape == (5, 10)

    def test_unsupported_quantity_still_raises(self):
        ds = _dataset_with_outlier()
        backend = _backend_with_dataset(ds)
        with pytest.raises(NotImplementedError):
            backend._raster_2d(ds, Axis.TIME, Axis.BASELINE, Axis.SCAN, "XX")


# ---------------------------------------------------------------------------
# 3. All-NaN-slice warning suppression (2026-09, found from a live run)
# ---------------------------------------------------------------------------
# dask/xarray operations are lazy: _raster_2d's own .median()/.max() calls
# only BUILD the computation graph; the actual nanmedian/nanmax execution
# -- and any RuntimeWarning it raises -- happens at .compute(), inside
# query_raster, not inside _raster_2d itself. Confirmed directly against a
# live traceback pointing at numpy/dask's own reduction internals, not at
# anything in this file. The dataset helpers above use plain numpy arrays
# (eager, not lazy), so they could never have caught this -- these tests
# use genuinely dask-backed data specifically to exercise that distinction.

def _dask_dataset_with_fully_flagged_baseline(
    n_time=10, n_baseline=5, n_freq=8, flagged_baseline=2, seed=3,
):
    import dask.array as da
    rng = np.random.default_rng(seed)
    shape = (n_time, n_baseline, n_freq, 1)
    vis_np = rng.normal(5.0, 0.05, shape) + 1j * rng.normal(0.0, 0.05, shape)
    flag_np = np.zeros(shape, dtype=bool)
    flag_np[:, flagged_baseline, :, :] = True   # an entirely-flagged baseline
    return xr.Dataset(
        data_vars={
            "VISIBILITY": (("time", "baseline_id", "frequency", "polarization"),
                            da.from_array(vis_np, chunks="auto")),
            "FLAG": (("time", "baseline_id", "frequency", "polarization"),
                      da.from_array(flag_np, chunks="auto")),
        },
        coords={"time": np.arange(n_time, dtype=np.float64),
                "baseline_id": np.arange(n_baseline),
                "frequency": np.linspace(1e9, 1.1e9, n_freq),
                "polarization": ["XX"]},
    )


class TestAllNanSliceWarningSuppression:
    def test_fully_flagged_baseline_raises_no_warning(self):
        import warnings
        ds = _dask_dataset_with_fully_flagged_baseline()
        backend = _backend_with_dataset(ds)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            backend.query_raster(
                Axis.TIME, Axis.BASELINE, Axis.Z_SCORE, SelectionSpec(), polarization="XX",
            )
        allnan = [w for w in caught if "All-NaN slice" in str(w.message)]
        assert allnan == [], f"expected no 'All-NaN slice' warnings, got {len(allnan)}"

    def test_the_same_scenario_really_does_warn_without_suppression(self):
        """Confirms the test above is a meaningful regression guard, not
        vacuously passing: the same lazy computation, .compute()-ed with
        no suppression at all, really does warn."""
        import warnings
        import dask.array as da
        rng = np.random.default_rng(3)
        shape = (10, 5, 8)
        vis_np = rng.normal(5.0, 0.05, shape) + 1j * rng.normal(0.0, 0.05, shape)
        flag_np = np.zeros(shape, dtype=bool)
        flag_np[:, 2, :] = True
        real = xr.DataArray(da.from_array(vis_np.real, chunks="auto"),
                             dims=("time", "baseline_id", "frequency")).where(
            ~xr.DataArray(da.from_array(flag_np, chunks="auto"),
                          dims=("time", "baseline_id", "frequency")))
        med = real.median(dim=["time", "frequency"], skipna=True)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            med.compute()
        allnan = [w for w in caught if "All-NaN slice" in str(w.message)]
        assert len(allnan) > 0, (
            "expected this scenario to genuinely warn without suppression "
            "-- otherwise the suppression test above proves nothing"
        )

    def test_the_fully_flagged_baseline_is_still_correctly_all_nan(self):
        """The suppression must not have changed the actual result --
        only quieted a warning about a case already handled correctly."""
        ds = _dask_dataset_with_fully_flagged_baseline(flagged_baseline=2)
        backend = _backend_with_dataset(ds)
        arr, _, _, _ = backend.query_raster(
            Axis.TIME, Axis.BASELINE, Axis.Z_SCORE, SelectionSpec(), polarization="XX",
        )
        vals = arr.values
        flagged_slice = vals[:, 2] if arr.dims == ("time", "baseline_id") else vals[2, :]
        assert np.all(np.isnan(flagged_slice))

    def test_other_quantities_are_not_silently_affected(self):
        """The suppression is scoped to Z_SCORE only -- a non-Z_SCORE
        quantity over the same dataset must behave exactly as before
        (this fix makes no claim about warnings for other quantities,
        so it must not silently swallow them either)."""
        ds = _dask_dataset_with_fully_flagged_baseline()
        backend = _backend_with_dataset(ds)
        # Must not raise, regardless of whatever warnings it does or does
        # not produce -- this is a smoke check that the branch for other
        # quantities is untouched, not a claim about their own warnings.
        arr, _, _, _ = backend.query_raster(
            Axis.TIME, Axis.BASELINE, Axis.AMPLITUDE, SelectionSpec(), polarization="XX",
        )
        assert arr.shape == (10, 5) or arr.shape == (5, 10)

