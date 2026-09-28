"""
test_raster_zscore_msv4.py
============================
MSv4Backend mirroring for Axis.Z_SCORE as a raster quantity
(test_raster_zscore.py's own MSv2-only coverage), plus the one
MSv4-specific wrinkle MSv2Backend has no equivalent for: single-dish
mode, where there are no baselines at all and the Z-Score's reference
population becomes "this antenna's own samples" instead (self._baseline_dim
resolves to "antenna_name" rather than "baseline_id" -- see
MSv4Backend._baseline_dim's own docstring).

Location in repository:
    cubevis/tests/manual/visplot/test_raster_zscore_msv4.py

Run:
    pytest cubevis/tests/manual/visplot/test_raster_zscore_msv4.py -v

All synthetic -- no real MS/PS needed for any test in this file.
"""
from __future__ import annotations

import numpy as np
import pytest
import xarray as xr

from cubevis.toolbox.visplot.axes import Axis
from cubevis.toolbox.visplot.data.msv4_backend import MSv4Backend
from cubevis.toolbox.visplot.selection import SelectionSpec


def _bare_backend(mode="interferometer"):
    b = MSv4Backend.__new__(MSv4Backend)
    b._datatree = object()
    b._resolved_mode = mode
    return b


def _interferometer_dataset_with_outlier(
    n_time=10, n_baseline=5, n_freq=8, pols=("XX",),
    outlier_time=3, outlier_baseline=2, outlier_freq=4,
    outlier_value=complex(50.0, 50.0), seed=7, flag_outlier=False,
):
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


def _single_dish_dataset_with_outlier(
    n_time=10, n_antenna=5, n_freq=8, pols=("XX",),
    outlier_time=3, outlier_antenna=2, outlier_freq=4,
    outlier_value=complex(50.0, 50.0), seed=7,
):
    rng = np.random.default_rng(seed)
    shape = (n_time, n_antenna, n_freq, len(pols))
    vis = rng.normal(5.0, 0.05, shape) + 1j * rng.normal(0.0, 0.05, shape)
    vis[outlier_time, outlier_antenna, outlier_freq, 0] = outlier_value
    flag = np.zeros(shape, dtype=bool)
    return xr.Dataset(
        data_vars={"VISIBILITY": (("time", "antenna_name", "frequency", "polarization"), vis),
                   "FLAG": (("time", "antenna_name", "frequency", "polarization"), flag)},
        coords={"time": np.arange(n_time, dtype=np.float64),
                "antenna_name": [f"DA{i}" for i in range(n_antenna)],
                "frequency": np.linspace(1e9, 1.1e9, n_freq),
                "polarization": list(pols)},
    )


def _backend_with_dataset(ds, mode="interferometer"):
    backend = _bare_backend(mode)
    backend._iter_visibility_partitions = lambda selection: iter([ds])
    backend._apply_selection = lambda raw_ds, selection: raw_ds
    return backend


# ---------------------------------------------------------------------------
# 1. Interferometer mode -- mirrors MSv2Backend's own coverage
# ---------------------------------------------------------------------------

class TestZScoreRasterMSv4Interferometer:
    def test_time_by_baseline_outlier_dominates_its_own_pixel(self):
        ds = _interferometer_dataset_with_outlier()
        backend = _backend_with_dataset(ds)
        arr, _, _, _ = backend.query_raster(
            Axis.TIME, Axis.BASELINE, Axis.Z_SCORE, SelectionSpec(), polarization="XX",
        )
        vals = arr.values
        idx = (3, 2) if arr.dims == ("time", "baseline_id") else (2, 3)
        outlier_val = vals[idx]
        others = vals.copy(); others[idx] = np.nan
        assert outlier_val > 10 * np.nanmax(others)

    def test_flagging_the_outlier_removes_its_influence(self):
        ds = _interferometer_dataset_with_outlier(flag_outlier=True)
        backend = _backend_with_dataset(ds)
        arr, _, _, _ = backend.query_raster(
            Axis.TIME, Axis.BASELINE, Axis.Z_SCORE, SelectionSpec(), polarization="XX",
        )
        assert np.nanmax(arr.values) < 10

    def test_matches_msv2backend_exactly_on_identical_synthetic_data(self):
        """Same principle Slice 1's own MSv4 mirroring established for
        the scatter-side Z-Score: identical input must give identical
        output across backends."""
        from cubevis.toolbox.visplot.data.msv2_backend import MSv2Backend
        ds = _interferometer_dataset_with_outlier()

        msv4_backend = _backend_with_dataset(ds)
        arr4, _, _, _ = msv4_backend.query_raster(
            Axis.TIME, Axis.BASELINE, Axis.Z_SCORE, SelectionSpec(), polarization="XX",
        )

        msv2_backend = MSv2Backend.__new__(MSv2Backend)
        msv2_backend._datatree = object()
        msv2_backend._path = "synthetic"
        msv2_backend._iter_visibility_partitions = lambda selection: iter([ds])
        msv2_backend._apply_selection = lambda raw_ds, selection: raw_ds
        arr2, _, _, _ = msv2_backend.query_raster(
            Axis.TIME, Axis.BASELINE, Axis.Z_SCORE, SelectionSpec(), polarization="XX",
        )

        np.testing.assert_array_equal(arr2.values, arr4.values)

    def test_amplitude_still_uses_mean_not_max(self):
        ds = _interferometer_dataset_with_outlier()
        backend = _backend_with_dataset(ds)
        arr, _, _, _ = backend.query_raster(
            Axis.TIME, Axis.BASELINE, Axis.AMPLITUDE, SelectionSpec(), polarization="XX",
        )
        vals = arr.values
        idx = (3, 2) if arr.dims == ("time", "baseline_id") else (2, 3)
        outlier_amp = abs(complex(50.0, 50.0))
        assert vals[idx] < outlier_amp / 2


# ---------------------------------------------------------------------------
# 2. Single-dish mode -- MSv4-specific, no MSv2 equivalent
# ---------------------------------------------------------------------------

class TestZScoreRasterMSv4SingleDish:
    def test_baseline_dim_resolves_to_antenna_name(self):
        backend = _bare_backend("single_dish")
        assert backend.is_single_dish
        assert backend._baseline_dim == "antenna_name"

    def test_per_antenna_reference_population_preserves_the_outlier(self):
        """No baselines exist in single-dish mode -- the reference
        population for each antenna's own Z-Score is that antenna's own
        samples, the natural substitution Axis.BASELINE itself already
        makes for every other purpose in this mode."""
        ds = _single_dish_dataset_with_outlier()
        backend = _backend_with_dataset(ds, mode="single_dish")
        arr, _, _, _ = backend.query_raster(
            Axis.TIME, Axis.BASELINE, Axis.Z_SCORE, SelectionSpec(), polarization="XX",
        )
        vals = arr.values
        idx = (3, 2) if arr.dims[0] == "time" else (2, 3)
        outlier_val = vals[idx]
        others = vals.copy(); others[idx] = np.nan
        assert outlier_val > 10 * np.nanmax(others)


# ---------------------------------------------------------------------------
# All-NaN-slice warning suppression (2026-09) -- mirrors
# test_raster_zscore.py's own coverage for MSv2Backend. See that file's
# own comment block for the full rationale (dask/xarray laziness means
# the warning fires at .compute(), inside query_raster, not inside
# _raster_2d -- confirmed from a live traceback).
# ---------------------------------------------------------------------------

def _dask_interferometer_dataset_with_fully_flagged_baseline(
    n_time=10, n_baseline=5, n_freq=8, flagged_baseline=2, seed=3,
):
    import dask.array as da
    rng = np.random.default_rng(seed)
    shape = (n_time, n_baseline, n_freq, 1)
    vis_np = rng.normal(5.0, 0.05, shape) + 1j * rng.normal(0.0, 0.05, shape)
    flag_np = np.zeros(shape, dtype=bool)
    flag_np[:, flagged_baseline, :, :] = True
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


class TestAllNanSliceWarningSuppressionMSv4:
    def test_fully_flagged_baseline_raises_no_warning(self):
        import warnings
        ds = _dask_interferometer_dataset_with_fully_flagged_baseline()
        backend = _backend_with_dataset(ds)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            backend.query_raster(
                Axis.TIME, Axis.BASELINE, Axis.Z_SCORE, SelectionSpec(), polarization="XX",
            )
        allnan = [w for w in caught if "All-NaN slice" in str(w.message)]
        assert allnan == [], f"expected no 'All-NaN slice' warnings, got {len(allnan)}"

    def test_the_fully_flagged_baseline_is_still_correctly_all_nan(self):
        ds = _dask_interferometer_dataset_with_fully_flagged_baseline(flagged_baseline=2)
        backend = _backend_with_dataset(ds)
        arr, _, _, _ = backend.query_raster(
            Axis.TIME, Axis.BASELINE, Axis.Z_SCORE, SelectionSpec(), polarization="XX",
        )
        vals = arr.values
        flagged_slice = vals[:, 2] if arr.dims == ("time", "baseline_id") else vals[2, :]
        assert np.all(np.isnan(flagged_slice))

    def test_fused_opt_b_path_also_suppresses_the_warning(self):
        """The OPT-B fused dask.compute() branch (len(lazy_arrs) > 1) is
        a separate code path from the single-partition fallback --
        confirmed this one is covered too, not just the fallback."""
        import warnings
        ds1 = _dask_interferometer_dataset_with_fully_flagged_baseline(seed=3)
        ds2 = _dask_interferometer_dataset_with_fully_flagged_baseline(seed=11)
        backend = _bare_backend()
        backend._iter_visibility_partitions = lambda selection: iter([ds1, ds2])
        backend._apply_selection = lambda raw_ds, selection: raw_ds
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            backend.query_raster(
                Axis.TIME, Axis.BASELINE, Axis.Z_SCORE, SelectionSpec(), polarization="XX",
            )
        allnan = [w for w in caught if "All-NaN slice" in str(w.message)]
        assert allnan == [], f"expected no 'All-NaN slice' warnings from the fused path, got {len(allnan)}"

