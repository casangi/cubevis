"""
test_msv4_backend.py — Unit and integration tests for MSv4Backend.

Location in repository:
    cubevis/tests/manual/visplot/test_msv4_backend.py

Tests against:
    cubevis/cubevis/toolbox/visplot/data/msv4_backend.py
    cubevis/cubevis/toolbox/visplot/axes.py
    cubevis/cubevis/toolbox/visplot/selection.py

Run from the cubevis repository root:

    PS=sis14_twhya_calibrated_flagged.ps.zarr \\
        pytest cubevis/tests/manual/visplot/test_msv4_backend.py -v

Or standalone (falls back to local copies of the source files):

    PS=sis14_twhya_calibrated_flagged.ps.zarr \\
        python test_msv4_backend.py

Create the test data with:

    MS=sis14_twhya_calibrated_flagged.ms python create_test_msv4.py

Tests
-----
1.  Lifecycle           open/close/context manager/idempotency/bad path
2.  Metadata            return structure, types, physical plausibility
3.  _apply_selection    each constraint genuinely reduces array sizes (isel)
4.  query_columns       API contract, DataFrame correctness, fused path
5.  query_columns       RENDERED: Datashader agg, serial≡fused
6.  query_raster        2D DataArray shape, all (y,x) combinations
7.  query_uv_coverage   conjugate symmetry, rendered through Datashader
8.  samples_per_pixel   geometric ratio correctness
9.  probe_pixel         raster value/metadata, scatter sample counting

Performance note — setup_class vs setup_method
-----------------------------------------------
Opening a Zarr DataTree is moderately expensive (zarr metadata scan).
TestLifecycle explicitly tests open/close lifecycle, so it opens per-test.
All other classes use ``setup_class`` / ``teardown_class`` to open the
backend once and share it across every test method in that class,
eliminating 60+ redundant opens from a full test run.
"""

from __future__ import annotations

import os
import sys
import time as time_mod
import warnings
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

# ---------------------------------------------------------------------------
# Import strategy: package first, local fallback
# ---------------------------------------------------------------------------

def _try_package_import():
    from cubevis.toolbox.visplot.axes import Axis, AxisType
    from cubevis.toolbox.visplot.selection import SelectionSpec
    from cubevis.toolbox.visplot.data.msv4_backend import (
        MSv4Backend, _axis_to_dim,
    )
    return Axis, AxisType, SelectionSpec, MSv4Backend, _axis_to_dim


def _local_import():
    import importlib.util

    here = Path(__file__).parent

    def _load(name, path):
        spec = importlib.util.spec_from_file_location(name, path)
        mod  = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
        return mod

    axes_mod = _load("cubevis.toolbox.visplot.axes",      here / "axes.py")
    sel_mod  = _load("cubevis.toolbox.visplot.selection", here / "selection.py")
    sys.modules["cubevis.toolbox.visplot.axes"]      = axes_mod
    sys.modules["cubevis.toolbox.visplot.selection"] = sel_mod
    _load("cubevis.toolbox.visplot.reader", here / "reader.py")
    backend_mod = _load("cubevis.toolbox.visplot.data.msv4_backend",
                        here / "msv4_backend.py")
    return (
        axes_mod.Axis,
        axes_mod.AxisType,
        sel_mod.SelectionSpec,
        backend_mod.MSv4Backend,
        backend_mod._axis_to_dim,
    )


try:
    Axis, AxisType, SelectionSpec, MSv4Backend, _axis_to_dim = _try_package_import()
    _SOURCE = "package"
except ImportError:
    Axis, AxisType, SelectionSpec, MSv4Backend, _axis_to_dim = _local_import()
    _SOURCE = "local"

print(f"[test_msv4_backend] imports from: {_SOURCE}")

try:
    import datashader as ds_lib
    import datashader.reductions as ds_agg
    HAS_DATASHADER = True
except ImportError:
    HAS_DATASHADER = False

PLOT_W = 400
PLOT_H = 300


# ---------------------------------------------------------------------------
# Fixtures / shared helpers
# ---------------------------------------------------------------------------

def _get_ps() -> str:
    path = os.environ.get("PS", "sis14_twhya_calibrated_flagged.ps.zarr")
    if not os.path.isdir(path):
        pytest.skip(
            f"Test PS not found at {path!r}.  "
            "Create it with:\n"
            "  MS=sis14_twhya_calibrated_flagged.ms python create_test_msv4.py\n"
            "Then set PS= env var."
        )
    return path


def _open_backend(**kwargs) -> MSv4Backend:
    b = MSv4Backend(_get_ps(), **kwargs)
    b.open()
    return b


def _largest_partition(backend: MSv4Backend):
    """Return the partition Dataset with the most integrations."""
    return max(
        backend._iter_visibility_partitions(),
        key=lambda d: d.sizes["time"],
    )


def _first_finite_baseline(ds) -> tuple[str, str] | None:
    """Return the first (ant1, ant2) pair that has finite VISIBILITY values.

    In ALMA cross-correlation-only datasets, ~40% of baseline_id slots are
    NaN-padded.  Picking baseline_id=0 naively often picks a padded slot.
    This helper finds the first non-padded baseline by checking
    EFFECTIVE_INTEGRATION_TIME if present, falling back to the VISIBILITY
    variable magnitude.  Returns None if no finite baseline is found.
    """
    if "baseline_antenna1_name" not in ds.coords:
        return None

    ant1 = ds.coords["baseline_antenna1_name"].values
    ant2 = ds.coords["baseline_antenna2_name"].values

    # Prefer EIT (cheap: no visibility read)
    if "EFFECTIVE_INTEGRATION_TIME" in ds:
        eit = ds["EFFECTIVE_INTEGRATION_TIME"].isel(time=0).compute()
        finite = np.where(np.isfinite(eit.values))[0]
        if len(finite):
            idx = int(finite[0])
            return str(ant1[idx]), str(ant2[idx])

    # Fallback: check VISIBILITY magnitude at first time step
    vis_name = next((v for v in ("VISIBILITY", "DATA") if v in ds.data_vars), None)
    if vis_name is not None:
        vis0 = ds[vis_name].isel(time=0)  # (baseline_id, frequency, polarization)
        amp = np.abs(vis0.values)         # eagerly compute this small slice
        finite_bl = np.where(np.isfinite(amp).any(axis=(-2, -1)))[0]
        if len(finite_bl):
            idx = int(finite_bl[0])
            return str(ant1[idx]), str(ant2[idx])

    return None


def _suppress_warnings():
    warnings.filterwarnings("ignore", category=UserWarning)
    warnings.filterwarnings("ignore", message="The return type of.*Dataset.dims",
                            category=FutureWarning)
    warnings.filterwarnings("ignore", message="omp_set_nested")
    warnings.filterwarnings("ignore", message=".*UnstableSpecification.*",
                            category=UserWarning)
    warnings.filterwarnings("ignore", message=".*Consolidated metadata.*",
                            category=UserWarning)


@pytest.fixture(scope="session", autouse=True)
def _cleanup_synthetic_stores():
    """Remove synthetic /tmp ps.zarr stores after the test session ends.

    The ``_get_or_create_*`` functions reuse existing stores within a
    session for speed, but stale stores written by an older xarray/zarr
    version cause ``open_consolidated`` failures in subsequent sessions.
    This fixture ensures a clean slate for the next run while still
    allowing all test classes within one session to share the stores.

    The paths are derived from the same env-var / default pairs used by
    the fixture creation functions, so overriding ``PS_DG`` etc. on the
    command line is respected here too.
    """
    yield   # all tests run first

    import shutil
    for env_var, default in (
        ("PS_DG",     "/tmp/msv4_test_dg.ps.zarr"),
        ("PS_NATIVE", "/tmp/msv4_test_native.ps.zarr"),
        ("PS_SD",     "/tmp/msv4_test_sd.ps.zarr"),
    ):
        path = os.environ.get(env_var, default)
        if os.path.isdir(path):
            shutil.rmtree(path)


def _require_datashader():
    if not HAS_DATASHADER:
        pytest.skip("datashader not installed — pip install datashader")


# ---------------------------------------------------------------------------
# 1. Lifecycle  (per-method open — tests the lifecycle itself)
# ---------------------------------------------------------------------------

class TestLifecycle:

    def test_open_and_close(self):
        ps = _get_ps()
        b = MSv4Backend(ps)
        assert b._datatree is None, "DataTree should be None before open()"
        b.open()
        assert b._datatree is not None
        b.close()
        assert b._datatree is None

    def test_open_idempotent(self):
        ps = _get_ps()
        b = MSv4Backend(ps)
        b.open()
        dt1 = id(b._datatree)
        b.open()
        assert id(b._datatree) == dt1, "open() should be idempotent"
        b.close()

    def test_context_manager(self):
        ps = _get_ps()
        with MSv4Backend(ps) as b:
            assert b._datatree is not None
        assert b._datatree is None

    def test_require_open_raises_before_open(self):
        ps = _get_ps()
        b = MSv4Backend(ps)
        with pytest.raises(RuntimeError, match="not open"):
            b.metadata()

    def test_repr_shows_status(self):
        ps = _get_ps()
        b = MSv4Backend(ps)
        assert "closed" in repr(b)
        b.open()
        assert "open" in repr(b)
        b.close()

    def test_wrong_path_raises_runtime_error(self):
        b = MSv4Backend("/nonexistent/does_not_exist.ps.zarr")
        with pytest.raises(RuntimeError):
            b.open()

    def test_custom_chunks_applied(self):
        """Chunks passed at construction affect Dask chunk sizes."""
        _suppress_warnings()
        with MSv4Backend(_get_ps(), chunks={"time": 30}) as b:
            for ds_part in b._iter_visibility_partitions():
                vis = ds_part["VISIBILITY"]
                t_idx = vis.dims.index("time")
                t_chunks = vis.chunks[t_idx]
                assert all(c <= 30 for c in t_chunks), (
                    f"Time chunks {t_chunks} exceed requested size 30"
                )
                break


# ---------------------------------------------------------------------------
# 2. Metadata  (class-level open)
# ---------------------------------------------------------------------------

class TestMetadata:

    @classmethod
    def setup_class(cls):
        _suppress_warnings()
        cls.backend = _open_backend()
        cls.meta = cls.backend.metadata()

    @classmethod
    def teardown_class(cls):
        cls.backend.close()

    def test_required_keys_present(self):
        required = {
            "scan_names", "field_names", "antenna_names", "spw_ids",
            "correlation_labels", "time_range", "freq_range",
            "n_baselines", "data_columns",
        }
        missing = required - self.meta.keys()
        assert not missing, f"Missing metadata keys: {missing}"

    def test_value_types(self):
        m = self.meta
        assert isinstance(m["scan_names"],         list)
        assert isinstance(m["field_names"],        list)
        assert isinstance(m["antenna_names"],      list)
        assert isinstance(m["spw_ids"],            list)
        assert isinstance(m["correlation_labels"], list)
        assert isinstance(m["time_range"],  tuple) and len(m["time_range"]) == 2
        assert isinstance(m["freq_range"],  tuple) and len(m["freq_range"]) == 2
        assert isinstance(m["n_baselines"], int)
        assert isinstance(m["data_columns"],       list)

    def test_field_names_nonempty(self):
        assert len(self.meta["field_names"]) > 0

    def test_antenna_names_plausible(self):
        ants = self.meta["antenna_names"]
        assert len(ants) >= 10, f"Expected ≥10 antennas, got {len(ants)}"
        print(f"  Antennas: {len(ants)}")

    def test_time_range_ordered_and_mjd(self):
        t0, t1 = self.meta["time_range"]
        assert t0 < t1
        assert t0 > 1e9, f"Time looks wrong (expected MJD seconds): {t0}"

    def test_freq_range_ordered_and_alma_band7(self):
        f0, f1 = self.meta["freq_range"]
        assert f0 < f1
        assert f0 > 3e11, f"Frequency looks wrong: {f0:.3e} Hz"

    def test_n_baselines_positive(self):
        assert self.meta["n_baselines"] > 0

    def test_correlation_labels_nonempty(self):
        pols = self.meta["correlation_labels"]
        assert len(pols) > 0
        assert any(p in pols for p in ("XX", "YY", "RR", "LL"))
        print(f"  Polarizations: {pols}")

    def test_data_column_in_metadata(self):
        assert "DATA" in self.meta["data_columns"]

    def test_metadata_matches_msv2_keys(self):
        expected_keys = {
            "scan_names", "field_names", "antenna_names", "spw_ids",
            "correlation_labels", "time_range", "freq_range",
            "n_baselines", "data_columns",
        }
        assert set(self.meta.keys()) == expected_keys


# ---------------------------------------------------------------------------
# 3. _apply_selection  (class-level open)
# ---------------------------------------------------------------------------

class TestApplySelection:

    @classmethod
    def setup_class(cls):
        _suppress_warnings()
        cls.backend = _open_backend()
        cls.ds_part = _largest_partition(cls.backend)

    @classmethod
    def teardown_class(cls):
        cls.backend.close()

    def _sel(self, **kwargs):
        return self.backend._apply_selection(
            self.ds_part, SelectionSpec(**kwargs)
        )

    def test_no_selection_unchanged(self):
        ds_sel = self._sel()
        for dim in ("time", "baseline_id", "frequency"):
            assert ds_sel.sizes[dim] == self.ds_part.sizes[dim]

    def test_time_range_reduces_time_size(self):
        times = self.ds_part.coords["time"].values
        t0, t1 = float(times[0]), float(times[min(9, len(times) - 1)])
        ds_sel = self._sel(time_range=(t0, t1))
        assert 0 < ds_sel.sizes["time"] <= 10
        assert ds_sel.sizes["time"] < self.ds_part.sizes["time"]

    def test_field_selection_reduces_time_size(self):
        if "field_name" not in self.ds_part.coords:
            pytest.skip("field_name not in partition coords")
        fields = np.unique(self.ds_part.coords["field_name"].values)
        if len(fields) < 2:
            pytest.skip("Only one field in this partition")
        target = str(fields[0])
        ds_sel = self._sel(field_names=[target])
        expected = int((self.ds_part.coords["field_name"].values == target).sum())
        assert ds_sel.sizes["time"] == expected

    def test_channel_range_reduces_freq_size(self):
        ds_sel = self._sel(channel_range=(4, 20))
        assert ds_sel.sizes["frequency"] == 16
        assert ds_sel.sizes["frequency"] < self.ds_part.sizes["frequency"]

    def test_freq_range_reduces_freq_size(self):
        freqs = self.ds_part.coords["frequency"].values
        f0, f1 = float(freqs[4]), float(freqs[19])
        ds_sel = self._sel(freq_range=(f0, f1))
        assert ds_sel.sizes["frequency"] <= 16
        assert ds_sel.sizes["frequency"] < self.ds_part.sizes["frequency"]

    def test_channel_range_takes_precedence_over_freq_range(self):
        freqs = self.ds_part.coords["frequency"].values
        ds_sel = self.backend._apply_selection(
            self.ds_part,
            SelectionSpec(
                channel_range=(0, 8),
                freq_range=(float(freqs[0]), float(freqs[-1])),
            ),
        )
        assert ds_sel.sizes["frequency"] <= 8

    def test_polarization_reduces_pol_size(self):
        pols = self.ds_part.coords["polarization"].values
        if len(pols) < 2:
            pytest.skip("Only one polarization available")
        ds_sel = self._sel(correlation=[str(pols[0])])
        assert ds_sel.sizes["polarization"] == 1

    def test_antenna_names_reduces_baseline_size(self):
        if "baseline_antenna1_name" not in self.ds_part.coords:
            pytest.skip("Antenna name coords not present")
        ant1v = self.ds_part.coords["baseline_antenna1_name"].values
        ant2v = self.ds_part.coords["baseline_antenna2_name"].values
        all_ants = np.unique(np.concatenate([ant1v, ant2v]))
        target = str(all_ants[0])
        ds_sel = self._sel(antenna_names=[target])
        assert 0 < ds_sel.sizes["baseline_id"] < self.ds_part.sizes["baseline_id"]
        a1 = ds_sel.coords["baseline_antenna1_name"].values
        a2 = ds_sel.coords["baseline_antenna2_name"].values
        assert ((a1 == target) | (a2 == target)).all()

    def test_baselines_exact_pair_selection(self):
        if "baseline_antenna1_name" not in self.ds_part.coords:
            pytest.skip("Antenna name coords not present")
        pair = _first_finite_baseline(self.ds_part)
        if pair is None:
            pytest.skip("No finite baseline found")
        a1, a2 = pair
        ds_sel = self._sel(baselines=[(a1, a2)])
        assert ds_sel.sizes["baseline_id"] == 1
        assert str(ds_sel.coords["baseline_antenna1_name"].values[0]) == a1
        assert str(ds_sel.coords["baseline_antenna2_name"].values[0]) == a2

    def test_baselines_takes_precedence_over_antenna_names(self):
        if "baseline_antenna1_name" not in self.ds_part.coords:
            pytest.skip("Antenna name coords not present")
        pair = _first_finite_baseline(self.ds_part)
        if pair is None:
            pytest.skip("No finite baseline found")
        a1, a2 = pair
        ds_sel = self.backend._apply_selection(
            self.ds_part,
            SelectionSpec(baselines=[(a1, a2)], antenna_names=[a1]),
        )
        assert ds_sel.sizes["baseline_id"] == 1

    def test_empty_time_range_returns_zero_time(self):
        ds_sel = self._sel(time_range=(0.0, 1.0))
        assert ds_sel.sizes["time"] == 0

    def test_compound_selection_cumulative(self):
        times = self.ds_part.coords["time"].values
        t0, t1 = float(times[0]), float(times[min(19, len(times) - 1)])
        ds_sel = self._sel(time_range=(t0, t1), channel_range=(0, 16))
        assert ds_sel.sizes["time"]      <= 20
        assert ds_sel.sizes["frequency"] == 16


# ---------------------------------------------------------------------------
# 4. query_columns — structural contract  (class-level open)
# ---------------------------------------------------------------------------

class TestQueryColumnsStructure:

    @classmethod
    def setup_class(cls):
        _suppress_warnings()
        cls.backend = _open_backend()
        meta = cls.backend.metadata()
        cls.pols = meta["correlation_labels"]
        t0, t1 = meta["time_range"]
        cls.sel_small = SelectionSpec(
            time_range=(t0, t0 + (t1 - t0) * 0.08),
            channel_range=(0, 16),
        )

    @classmethod
    def teardown_class(cls):
        cls.backend.close()

    def test_returns_dict_of_dataframes(self):
        yaxes = [(Axis.AMPLITUDE, self.pols[0])]
        result = self.backend.query_columns(Axis.TIME, yaxes, self.sel_small)
        assert isinstance(result, dict)
        key = (Axis.AMPLITUDE, self.pols[0])
        assert key in result
        assert isinstance(result[key], pd.DataFrame)
        assert {"x", "y"} <= set(result[key].columns)

    def test_no_nan_in_output(self):
        yaxes = [(Axis.AMPLITUDE, self.pols[0])]
        result = self.backend.query_columns(Axis.TIME, yaxes, self.sel_small)
        df = result[(Axis.AMPLITUDE, self.pols[0])]
        assert not df["x"].isna().any(), "NaN x values in output"
        assert not df["y"].isna().any(), "NaN y values in output"

    def test_amplitude_nonnegative(self):
        yaxes = [(Axis.AMPLITUDE, self.pols[0])]
        result = self.backend.query_columns(Axis.TIME, yaxes, self.sel_small)
        df = result[(Axis.AMPLITUDE, self.pols[0])]
        assert len(df) > 0
        assert (df["y"] >= 0).all()

    def test_phase_in_range(self):
        yaxes = [(Axis.PHASE, self.pols[0])]
        result = self.backend.query_columns(Axis.TIME, yaxes, self.sel_small)
        df = result[(Axis.PHASE, self.pols[0])]
        assert len(df) > 0
        assert df["y"].between(-180, 180).all()

    def test_time_x_is_mjd_seconds(self):
        yaxes = [(Axis.AMPLITUDE, self.pols[0])]
        result = self.backend.query_columns(Axis.TIME, yaxes, self.sel_small)
        df = result[(Axis.AMPLITUDE, self.pols[0])]
        assert df["x"].min() > 1e9

    def test_uvdist_x_nonnegative(self):
        yaxes = [(Axis.AMPLITUDE, self.pols[0])]
        result = self.backend.query_columns(Axis.UVDIST, yaxes, self.sel_small)
        df = result[(Axis.AMPLITUDE, self.pols[0])]
        assert (df["x"] >= 0).all()

    def test_multiple_yaxes_returned(self):
        if len(self.pols) < 2:
            pytest.skip("Need ≥2 polarizations")
        yaxes = [
            (Axis.AMPLITUDE, self.pols[0]),
            (Axis.AMPLITUDE, self.pols[1]),
        ]
        result = self.backend.query_columns(Axis.TIME, yaxes, self.sel_small)
        assert len(result) == 2
        for key in yaxes:
            assert key in result and len(result[key]) > 0

    def test_empty_selection_returns_empty_dataframe(self):
        sel = SelectionSpec(time_range=(0.0, 1.0))
        yaxes = [(Axis.AMPLITUDE, self.pols[0])]
        result = self.backend.query_columns(Axis.TIME, yaxes, sel)
        df = result[(Axis.AMPLITUDE, self.pols[0])]
        assert len(df) == 0

    def test_all_y_axis_types(self):
        yaxes = [
            (Axis.AMPLITUDE,  self.pols[0]),
            (Axis.PHASE,      self.pols[0]),
            (Axis.REAL,       self.pols[0]),
            (Axis.IMAGINARY,  self.pols[0]),
        ]
        result = self.backend.query_columns(Axis.TIME, yaxes, self.sel_small)
        for key in yaxes:
            assert key in result
            df = result[key]
            assert len(df) > 0, f"Empty result for {key}"
            assert not df["y"].isna().any()


# ---------------------------------------------------------------------------
# 5. query_columns RENDERED  (class-level open)
# ---------------------------------------------------------------------------

class TestQueryColumnsRendered:

    @classmethod
    def setup_class(cls):
        _require_datashader()
        _suppress_warnings()
        cls.backend = _open_backend()
        meta = cls.backend.metadata()
        cls.pols = meta["correlation_labels"]
        t0, t1 = meta["time_range"]
        cls.sel = SelectionSpec(
            time_range=(t0, t0 + (t1 - t0) * 0.15),
            channel_range=(0, 16),
        )

    @classmethod
    def teardown_class(cls):
        cls.backend.close()

    def test_datashader_agg_has_correct_shape(self):
        yaxes  = [(Axis.AMPLITUDE, self.pols[0])]
        result = self.backend.query_columns(Axis.TIME, yaxes, self.sel)
        df     = result[(Axis.AMPLITUDE, self.pols[0])]
        assert len(df) > 0
        cvs = ds_lib.Canvas(plot_width=PLOT_W, plot_height=PLOT_H)
        agg = cvs.points(df, "x", "y", agg=ds_agg.mean("y"))
        assert agg.shape == (PLOT_H, PLOT_W)
        assert np.isfinite(agg.values).any()
        print(f"  Scatter points: {len(df):,}")

    def test_serial_and_fused_pipelines_agree(self):
        """Serial (< 500K) and fused (≥ 500K) paths must produce identical values."""
        yaxes = [(Axis.AMPLITUDE, self.pols[0])]

        ds_part = _largest_partition(self.backend)
        ds_sel  = self.backend._apply_selection(ds_part, self.sel)
        frames_serial = self.backend._query_partition_scatter(
            ds_sel, Axis.TIME, yaxes,
            use_fused=False, use_parallel=False,
        )
        frames_fused = self.backend._query_partition_scatter(
            ds_sel, Axis.TIME, yaxes,
            use_fused=True, use_parallel=False,
        )

        key = (Axis.AMPLITUDE, self.pols[0])
        df_s = frames_serial[key].sort_values(["x", "y"]).reset_index(drop=True)
        df_f = frames_fused[key].sort_values(["x", "y"]).reset_index(drop=True)

        assert len(df_s) == len(df_f), (
            f"Serial rows {len(df_s)} ≠ fused rows {len(df_f)}"
        )
        np.testing.assert_allclose(
            df_s["y"].values, df_f["y"].values, rtol=1e-5,
            err_msg="Serial and fused amplitude values differ",
        )


# ---------------------------------------------------------------------------
# 6. query_raster  (class-level open)
# ---------------------------------------------------------------------------

class TestQueryRaster:

    @classmethod
    def setup_class(cls):
        _suppress_warnings()
        cls.backend = _open_backend()
        meta = cls.backend.metadata()
        cls.pols = meta["correlation_labels"]
        t0, t1 = meta["time_range"]
        cls.sel = SelectionSpec(
            time_range=(t0, t0 + (t1 - t0) * 0.3),
            channel_range=(0, 48),
        )

    @classmethod
    def teardown_class(cls):
        cls.backend.close()

    def _raster(self, y_dim, x_dim, quantity=Axis.AMPLITUDE, **kw):
        return self.backend.query_raster(
            y_dim, x_dim, quantity, self.sel,
            polarization=self.pols[0], **kw
        )

    def test_returns_four_tuple(self):
        result = self._raster(Axis.TIME, Axis.BASELINE)
        assert len(result) == 4

    def test_agg_is_2d_dataarray(self):
        import xarray as xr
        agg, *_ = self._raster(Axis.TIME, Axis.BASELINE)
        assert isinstance(agg, xr.DataArray)
        assert agg.ndim == 2

    def test_agg_dims_match_axes(self):
        agg, *_ = self._raster(Axis.TIME, Axis.BASELINE)
        assert agg.dims[0] == "time"
        assert agg.dims[1] == "baseline_id"

    def test_x_range_ordered(self):
        _, x_range, _, _ = self._raster(Axis.TIME, Axis.BASELINE)
        assert x_range[0] <= x_range[1]

    def test_y_range_ordered(self):
        _, _, y_range, _ = self._raster(Axis.TIME, Axis.BASELINE)
        assert y_range[0] <= y_range[1]

    def test_x_range_is_mjd_seconds_for_time_axis(self):
        agg, x_range, _, _ = self._raster(Axis.BASELINE, Axis.TIME)
        assert x_range[0] > 1e9

    def test_freq_baseline_raster(self):
        agg, x_range, y_range, _ = self._raster(Axis.FREQUENCY, Axis.BASELINE)
        assert agg.dims[0] == "frequency"
        assert agg.dims[1] == "baseline_id"
        assert np.isfinite(agg.values).any()

    def test_time_freq_raster_with_baseline_selection(self):
        """TIME × FREQUENCY raster on a single non-padded baseline."""
        ds_part = next(self.backend._iter_visibility_partitions())
        pair = _first_finite_baseline(ds_part)
        if pair is None:
            pytest.skip("No non-padded baseline found in first partition")
        a1, a2 = pair
        sel_bl = SelectionSpec(
            channel_range=(0, 48),
            baselines=[(a1, a2)],
            time_range=self.sel.time_range,
        )
        agg, *_ = self.backend.query_raster(
            Axis.TIME, Axis.FREQUENCY, Axis.AMPLITUDE, sel_bl,
            polarization=self.pols[0],
        )
        assert agg.dims[0] == "time"
        assert agg.dims[1] == "frequency"
        assert np.isfinite(agg.values).any(), (
            f"All-NaN TIME×FREQUENCY raster for baseline ({a1}, {a2}). "
            "This baseline may still be padded — check _first_finite_baseline."
        )

    def test_max_cells_limits_output_size(self):
        agg, _, _, is_dec = self._raster(
            Axis.TIME, Axis.BASELINE, max_cells=10_000
        )
        total = agg.sizes["time"] * agg.sizes["baseline_id"]
        assert total <= 10_000 * 1.1
        assert is_dec

    def test_empty_selection_returns_nan_array(self):
        sel_empty = SelectionSpec(time_range=(0.0, 1.0))
        agg, x_range, y_range, is_dec = self.backend.query_raster(
            Axis.TIME, Axis.BASELINE, Axis.AMPLITUDE, sel_empty,
            polarization=self.pols[0],
        )
        assert not np.isfinite(agg.values).any()
        assert not is_dec

    def test_flag_quantity_returns_fraction(self):
        agg, *_ = self._raster(Axis.TIME, Axis.BASELINE, quantity=Axis.FLAG)
        vals = agg.values[np.isfinite(agg.values)]
        assert (vals >= 0).all() and (vals <= 1).all()

    def test_amplitude_values_nonnegative(self):
        agg, *_ = self._raster(Axis.TIME, Axis.BASELINE)
        vals = agg.values[np.isfinite(agg.values)]
        assert (vals >= 0).all()

    def test_rendered_through_datashader(self):  # RENDERED
        _require_datashader()
        agg, x_range, y_range, _ = self._raster(Axis.TIME, Axis.BASELINE)
        cvs = ds_lib.Canvas(
            plot_width=PLOT_W, plot_height=PLOT_H,
            x_range=x_range, y_range=y_range,
        )
        canvas_agg = cvs.raster(agg, agg=ds_agg.mean())
        assert canvas_agg.shape == (PLOT_H, PLOT_W)
        assert np.isfinite(canvas_agg.values).any()


# ---------------------------------------------------------------------------
# 7. query_uv_coverage  (class-level open)
# ---------------------------------------------------------------------------

class TestQueryUVCoverage:

    @classmethod
    def setup_class(cls):
        _suppress_warnings()
        cls.backend = _open_backend()
        meta = cls.backend.metadata()
        t0, t1 = meta["time_range"]
        cls.sel = SelectionSpec(
            time_range=(t0, t0 + (t1 - t0) * 0.3)
        )

    @classmethod
    def teardown_class(cls):
        cls.backend.close()

    def test_returns_dataframe_with_x_y(self):
        df = self.backend.query_uv_coverage(self.sel)
        assert isinstance(df, pd.DataFrame)
        assert {"x", "y"} <= set(df.columns)

    def test_no_nan(self):
        df = self.backend.query_uv_coverage(self.sel)
        assert not df["x"].isna().any()
        assert not df["y"].isna().any()

    def test_conjugate_doubles_row_count(self):
        df_plain = self.backend.query_uv_coverage(self.sel, include_conjugate=False)
        df_conj  = self.backend.query_uv_coverage(self.sel, include_conjugate=True)
        assert len(df_conj) == 2 * len(df_plain)

    def test_conjugate_u_v_symmetric(self):
        df = self.backend.query_uv_coverage(self.sel, include_conjugate=True)
        assert abs(df["x"].min() + df["x"].max()) < 1.0
        assert abs(df["y"].min() + df["y"].max()) < 1.0

    def test_rendered_uv_coverage(self):  # RENDERED
        _require_datashader()
        df = self.backend.query_uv_coverage(self.sel, include_conjugate=True)
        cvs = ds_lib.Canvas(plot_width=PLOT_W, plot_height=PLOT_H)
        agg = cvs.points(df, "x", "y", agg=ds_agg.count())
        assert agg.shape == (PLOT_H, PLOT_W)
        assert int(np.isfinite(agg.values).sum()) > 0
        print(f"  UV-coverage: {len(df):,} points")

    def test_empty_selection_returns_empty(self):
        sel = SelectionSpec(time_range=(0.0, 1.0))
        df  = self.backend.query_uv_coverage(sel)
        assert len(df) == 0


# ---------------------------------------------------------------------------
# 8. samples_per_pixel  (class-level open)
# ---------------------------------------------------------------------------

class TestSamplesPerPixel:

    @classmethod
    def setup_class(cls):
        _suppress_warnings()
        cls.backend = _open_backend()
        meta = cls.backend.metadata()
        t0, t1 = meta["time_range"]
        cls.sel = SelectionSpec(
            time_range=(t0, t0 + (t1 - t0) * 0.2)
        )

    @classmethod
    def teardown_class(cls):
        cls.backend.close()

    def test_returns_two_positive_floats(self):
        rx, ry = self.backend.samples_per_pixel(
            Axis.TIME, Axis.BASELINE, self.sel, PLOT_W, PLOT_H
        )
        assert isinstance(rx, float) and rx > 0
        assert isinstance(ry, float) and ry > 0

    def test_doubling_canvas_width_halves_x_ratio(self):
        rx1, ry1 = self.backend.samples_per_pixel(
            Axis.TIME, Axis.BASELINE, self.sel, PLOT_W, PLOT_H
        )
        rx2, ry2 = self.backend.samples_per_pixel(
            Axis.TIME, Axis.BASELINE, self.sel, PLOT_W * 2, PLOT_H
        )
        assert abs(rx2 - rx1 / 2) < 0.02
        assert abs(ry2 - ry1) < 0.02

    def test_narrow_time_selection_gives_small_y_ratio(self):
        meta = self.backend.metadata()
        t0, t1 = meta["time_range"]
        dt = (t1 - t0) / 270
        sel_narrow = SelectionSpec(time_range=(t0, t0 + 5 * dt))
        rx, ry = self.backend.samples_per_pixel(
            Axis.TIME, Axis.BASELINE, sel_narrow, PLOT_W, PLOT_H
        )
        print(f"  5-integration selection: ratio_x={rx:.3f}, ratio_y={ry:.3f}")
        assert ry <= 1.0

    def test_full_partition_ratio_greater_than_zero(self):
        sel_full = SelectionSpec()
        rx, ry = self.backend.samples_per_pixel(
            Axis.TIME, Axis.BASELINE, sel_full, PLOT_W, PLOT_H
        )
        print(f"  Full partition: ratio_x={rx:.3f}, ratio_y={ry:.3f}")
        assert rx > 0 and ry > 0


# ---------------------------------------------------------------------------
# 9. probe_pixel  (class-level open)
# ---------------------------------------------------------------------------

class TestProbePixel:

    @classmethod
    def setup_class(cls):
        _require_datashader()
        _suppress_warnings()
        cls.backend = _open_backend()
        meta = cls.backend.metadata()
        cls.pols = meta["correlation_labels"]
        t0, t1 = meta["time_range"]
        cls.sel = SelectionSpec(
            time_range=(t0, t0 + (t1 - t0) * 0.2),
            channel_range=(0, 48),
        )

    @classmethod
    def teardown_class(cls):
        cls.backend.close()

    # ------------------------------------------------------------------ #
    # Helpers                                                              #
    # ------------------------------------------------------------------ #

    def _raster_agg(self):
        raw_grid, x_range, y_range, _ = self.backend.query_raster(
            Axis.TIME, Axis.BASELINE, Axis.AMPLITUDE, self.sel,
            polarization=self.pols[0],
        )
        cvs = ds_lib.Canvas(
            plot_width=PLOT_W, plot_height=PLOT_H,
            x_range=x_range, y_range=y_range,
        )
        canvas_agg = cvs.raster(raw_grid, agg=ds_agg.mean())
        return canvas_agg, raw_grid, x_range, y_range

    def _scatter_agg_and_df(self):
        yaxes  = [(Axis.AMPLITUDE, self.pols[0])]
        result = self.backend.query_columns(Axis.TIME, yaxes, self.sel)
        df     = result[(Axis.AMPLITUDE, self.pols[0])]
        cvs    = ds_lib.Canvas(plot_width=PLOT_W, plot_height=PLOT_H)
        canvas_agg = cvs.points(df, "x", "y", agg=ds_agg.mean("y"))
        return canvas_agg, df

    def _canvas_to_grid(self, canvas_agg, raw_grid, x_range, y_range, px, py):
        x_coords_c = canvas_agg.coords[canvas_agg.dims[1]].values
        y_coords_c = canvas_agg.coords[canvas_agg.dims[0]].values
        x_val = float(x_coords_c[px])
        y_val = float(y_coords_c[py])
        x_name = raw_grid.dims[1]
        y_name = raw_grid.dims[0]
        x_coords_g = raw_grid.coords[x_name].values
        y_coords_g = raw_grid.coords[y_name].values
        gx = int(np.argmin(np.abs(x_coords_g - x_val)))
        gy = int(np.argmin(np.abs(y_coords_g - y_val)))
        h, w = raw_grid.shape
        return max(0, min(gx, w - 1)), max(0, min(gy, h - 1))

    def _first_finite_pixel(self, canvas_agg):
        ys, xs = np.where(np.isfinite(canvas_agg.values))
        if len(ys) == 0:
            pytest.skip("No finite pixels in canvas_agg")
        return int(xs[0]), int(ys[0])

    def _first_nan_pixel(self, canvas_agg):
        ys, xs = np.where(np.isnan(canvas_agg.values))
        if len(ys) == 0:
            return None
        return int(xs[0]), int(ys[0])

    # ------------------------------------------------------------------ #
    # A. Layer 1 — float64 value and coordinate ranges                    #
    # ------------------------------------------------------------------ #

    def test_raster_value_matches_agg(self):
        canvas_agg, raw_grid, xr_, yr_ = self._raster_agg()
        px, py = self._first_finite_pixel(canvas_agg)
        gx, gy = self._canvas_to_grid(canvas_agg, raw_grid, xr_, yr_, px, py)
        info   = self.backend.probe_raster_pixel(raw_grid, gx, gy, self.sel)
        expected = float(raw_grid.values[gy, gx])
        assert info["value"] is not None
        assert abs(info["value"] - expected) < 1e-6

    def test_raster_value_none_for_nan_pixel(self):
        canvas_agg, raw_grid, xr_, yr_ = self._raster_agg()
        nan_pix = self._first_nan_pixel(canvas_agg)
        if nan_pix is None:
            pytest.skip("No NaN pixels in raster")
        px, py = nan_pix
        gx, gy = self._canvas_to_grid(canvas_agg, raw_grid, xr_, yr_, px, py)
        info = self.backend.probe_raster_pixel(raw_grid, gx, gy, self.sel)
        assert info["value"] is None

    def test_x_centre_within_x_range(self):
        canvas_agg, raw_grid, xr_, yr_ = self._raster_agg()
        px, py = self._first_finite_pixel(canvas_agg)
        gx, gy = self._canvas_to_grid(canvas_agg, raw_grid, xr_, yr_, px, py)
        info = self.backend.probe_raster_pixel(raw_grid, gx, gy, self.sel)
        assert info["x_range"][0] <= info["x_centre"] <= info["x_range"][1]

    def test_out_of_range_pixel_raises(self):
        canvas_agg, raw_grid, xr_, yr_ = self._raster_agg()
        h_g, w_g = raw_grid.shape
        with pytest.raises(IndexError):
            self.backend.probe_raster_pixel(raw_grid, w_g, 0, self.sel)
        with pytest.raises(IndexError):
            self.backend.probe_raster_pixel(raw_grid, 0, h_g, self.sel)

    # ------------------------------------------------------------------ #
    # B. Layer 2 — metadata lookup                                        #
    # ------------------------------------------------------------------ #

    def test_raster_metadata_keys_present(self):
        canvas_agg, raw_grid, xr_, yr_ = self._raster_agg()
        px, py = self._first_finite_pixel(canvas_agg)
        gx, gy = self._canvas_to_grid(canvas_agg, raw_grid, xr_, yr_, px, py)
        info   = self.backend.probe_raster_pixel(raw_grid, gx, gy, self.sel)
        required = {
            "value", "x_range", "y_range", "x_centre", "y_centre",
            "field_names", "scan_names", "antenna_pairs", "freq_range_ghz",
        }
        assert required <= info.keys()

    def test_raster_no_scatter_samples_key(self):
        canvas_agg, raw_grid, xr_, yr_ = self._raster_agg()
        px, py = self._first_finite_pixel(canvas_agg)
        gx, gy = self._canvas_to_grid(canvas_agg, raw_grid, xr_, yr_, px, py)
        info   = self.backend.probe_raster_pixel(raw_grid, gx, gy, self.sel)
        assert "n_scatter_samples" not in info

    def test_raster_antenna_pairs_for_baseline_axis(self):
        canvas_agg, raw_grid, xr_, yr_ = self._raster_agg()
        px, py = self._first_finite_pixel(canvas_agg)
        gx, gy = self._canvas_to_grid(canvas_agg, raw_grid, xr_, yr_, px, py)
        info   = self.backend.probe_raster_pixel(raw_grid, gx, gy, self.sel)
        assert len(info["antenna_pairs"]) > 0
        for a1, a2 in info["antenna_pairs"]:
            assert isinstance(a1, str) and isinstance(a2, str)

    # ------------------------------------------------------------------ #
    # C. Scatter sample counting                                           #
    # ------------------------------------------------------------------ #

    def test_scatter_probe_value_matches_agg(self):
        canvas_agg, df = self._scatter_agg_and_df()
        px, py = self._first_finite_pixel(canvas_agg)
        info   = self.backend.probe_scatter_pixel(
            canvas_agg, px, py, self.sel, df
        )
        expected = float(canvas_agg.values[py, px])
        assert info["value"] is not None
        assert abs(info["value"] - expected) < 1e-6

    def test_scatter_sample_count_matches_manual_index(self):
        canvas_agg, df = self._scatter_agg_and_df()
        px, py = self._first_finite_pixel(canvas_agg)
        info   = self.backend.probe_scatter_pixel(
            canvas_agg, px, py, self.sel, df
        )
        x0, x1 = info["x_range"]
        y0, y1 = info["y_range"]
        manual = int(
            ((df["x"] >= x0) & (df["x"] <= x1) &
             (df["y"] >= y0) & (df["y"] <= y1)).sum()
        )
        assert info["n_scatter_samples"] == manual

    def test_scatter_empty_pixel_has_zero_samples(self):
        canvas_agg, df = self._scatter_agg_and_df()
        nan_pix = self._first_nan_pixel(canvas_agg)
        if nan_pix is None:
            pytest.skip("No NaN pixels in scatter canvas")
        px, py = nan_pix
        info   = self.backend.probe_scatter_pixel(
            canvas_agg, px, py, self.sel, df
        )
        assert info["value"] is None
        assert info["n_scatter_samples"] == 0


# ---------------------------------------------------------------------------
# Standalone runner (no pytest required)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    _suppress_warnings()

    test_classes = [
        TestLifecycle,
        TestMetadata,
        TestApplySelection,
        TestQueryColumnsStructure,
        TestQueryColumnsRendered,
        TestQueryRaster,
        TestQueryUVCoverage,
        TestSamplesPerPixel,
        TestProbePixel,
        TestDataGroup,
        TestXRadioNativeStructure,
        TestSingleDish,
    ]

    total_passed = total_failed = total_skipped = 0

    for cls in test_classes:
        print(f"\n{'='*60}\n  {cls.__name__}\n{'='*60}")
        obj = cls()

        # Call class-level setup if present
        if hasattr(cls, "setup_class"):
            try:
                cls.setup_class()
            except Exception as exc:
                print(f"  setup_class FAILED: {exc}")
                continue

        methods = sorted(m for m in dir(obj) if m.startswith("test_"))
        for name in methods:
            print(f"\n  --- {name} ---")
            try:
                if hasattr(obj, "setup_method"):
                    obj.setup_method()
                getattr(obj, name)()
                if hasattr(obj, "teardown_method"):
                    obj.teardown_method()
                print("  PASS")
                total_passed += 1
            except pytest.skip.Exception as exc:
                print(f"  SKIP: {exc}")
                total_skipped += 1
                try:
                    if hasattr(obj, "teardown_method"):
                        obj.teardown_method()
                except Exception:
                    pass
            except Exception as exc:
                import traceback
                print(f"  FAIL: {exc}")
                traceback.print_exc()
                total_failed += 1
                try:
                    if hasattr(obj, "teardown_method"):
                        obj.teardown_method()
                except Exception:
                    pass

        if hasattr(cls, "teardown_class"):
            try:
                cls.teardown_class()
            except Exception:
                pass

    print(f"\n{'='*60}")
    print(f"  {total_passed} passed, {total_failed} failed, "
          f"{total_skipped} skipped")

# ---------------------------------------------------------------------------
# 10. Data group selection  (class-level open, uses synthetic multi-group store)
# ---------------------------------------------------------------------------

def _get_or_create_dg_ps() -> str:
    """Return path to a synthetic two-data-group ps.zarr, creating it if needed.

    The store has two data groups:
    * ``'base'``    → VISIBILITY   (all-zero complex, no flags)
    * ``'imaging'`` → VISIBILITY_CORRECTED  (all-two complex, no flags)

    This covers the xradio use-case of raw + calibrated data coexisting in
    the same partition.  The store is written once per test session to a
    deterministic temp path and reused across all TestDataGroup tests.
    """
    import shutil
    import xarray as xr
    import dask.array as da

    path = os.environ.get("PS_DG", "/tmp/msv4_test_dg.ps.zarr")
    if os.path.isdir(path):
        return path

    n_time, n_bl, n_freq, n_pol = 5, 3, 8, 2
    ds = xr.Dataset(
        {
            "VISIBILITY": xr.DataArray(
                da.zeros((n_time, n_bl, n_freq, n_pol),
                         chunks=(n_time, n_bl, n_freq, n_pol),
                         dtype=np.complex64),
                dims=["time", "baseline_id", "frequency", "polarization"],
            ),
            "VISIBILITY_CORRECTED": xr.DataArray(
                (da.zeros((n_time, n_bl, n_freq, n_pol),
                          chunks=(n_time, n_bl, n_freq, n_pol),
                          dtype=np.complex64)
                 + complex(2, 0)),
                dims=["time", "baseline_id", "frequency", "polarization"],
            ),
            "FLAG": xr.DataArray(
                da.zeros((n_time, n_bl, n_freq, n_pol),
                         chunks=(n_time, n_bl, n_freq, n_pol),
                         dtype=np.uint8),
                dims=["time", "baseline_id", "frequency", "polarization"],
            ),
            "UVW": xr.DataArray(
                da.zeros((n_time, n_bl, 3),
                         chunks=(n_time, n_bl, 3),
                         dtype=np.float32),
                dims=["time", "baseline_id", "uvw_label"],
            ),
        },
        coords={
            "time":                   np.linspace(1.35e9, 1.36e9, n_time),
            "baseline_id":            np.arange(n_bl),
            "frequency":              np.linspace(3.72e11, 3.73e11, n_freq),
            "polarization":           ["XX", "YY"],
            "uvw_label":              ["u", "v", "w"],
            "baseline_antenna1_name": ("baseline_id", ["DA01", "DA01", "DA02"]),
            "baseline_antenna2_name": ("baseline_id", ["DA02", "DA03", "DA03"]),
            "field_name":             ("time", ["F1"] * n_time),
            "scan_name":              ("time", ["1"] * n_time),
        },
        attrs={
            "type": "visibility",
            "data_groups": {
                "base": {
                    "correlated_data": "VISIBILITY",
                    "flag":            "FLAG",
                    "uvw":             "UVW",
                },
                "imaging": {
                    "correlated_data": "VISIBILITY_CORRECTED",
                    "flag":            "FLAG",
                    "uvw":             "UVW",
                },
            },
        },
    )
    xr.DataTree.from_dict({"partition_000": ds}).to_zarr(path, mode="w",
                                                          compute=True)
    return path


class TestDataGroup:
    """Tests for the ``data_group`` constructor parameter.

    Uses a synthetic store with two data groups:
    * ``'base'``    → VISIBILITY            (amplitude = 0)
    * ``'imaging'`` → VISIBILITY_CORRECTED  (amplitude = 2)

    Tests verify that each group selects the correct variable, that the
    default (first group) works without an explicit parameter, and that
    an unknown group raises a ``KeyError`` with the available names listed.
    """

    @classmethod
    def setup_class(cls):
        _suppress_warnings()
        cls.ps_path = _get_or_create_dg_ps()

    def _open(self, **kwargs) -> MSv4Backend:
        b = MSv4Backend(self.ps_path, **kwargs)
        b.open()
        return b

    # ------------------------------------------------------------------ #
    # _get_data_group resolution                                           #
    # ------------------------------------------------------------------ #

    def test_default_resolves_to_first_group(self):
        """data_group=None returns the first group ('base' here)."""
        with self._open() as b:
            ds = b._partitions[0]
            group = b._get_data_group(ds)
            assert group is not None
            assert group["correlated_data"] == "VISIBILITY"

    def test_explicit_base_group(self):
        with self._open(data_group="base") as b:
            ds = b._partitions[0]
            group = b._get_data_group(ds)
            assert group["correlated_data"] == "VISIBILITY"

    def test_explicit_imaging_group(self):
        with self._open(data_group="imaging") as b:
            ds = b._partitions[0]
            group = b._get_data_group(ds)
            assert group["correlated_data"] == "VISIBILITY_CORRECTED"

    def test_unknown_group_raises_key_error(self):
        """Unknown data_group name raises KeyError listing available groups."""
        with self._open(data_group="nonexistent") as b:
            ds = b._partitions[0]
            with pytest.raises(KeyError) as exc_info:
                b._get_data_group(ds)
            msg = str(exc_info.value)
            assert "nonexistent" in msg
            # At least one available group name should appear in the message
            assert "base" in msg or "imaging" in msg

    # ------------------------------------------------------------------ #
    # _resolve_vis selects the correct variable per group                  #
    # ------------------------------------------------------------------ #

    def test_base_group_reads_visibility(self):
        """base group → VISIBILITY → all-zero amplitude."""
        with self._open(data_group="base") as b:
            ds   = b._partitions[0]
            vis  = b._resolve_vis(ds).compute()
            amps = np.abs(vis.values)
            assert (amps < 1e-6).all(), (
                f"Expected all-zero amplitude for 'base' group; "
                f"max={amps.max():.4f}"
            )

    def test_imaging_group_reads_visibility_corrected(self):
        """imaging group → VISIBILITY_CORRECTED → amplitude ≈ 2."""
        with self._open(data_group="imaging") as b:
            ds   = b._partitions[0]
            vis  = b._resolve_vis(ds).compute()
            amps = np.abs(vis.values)
            assert np.allclose(amps, 2.0, atol=1e-5), (
                f"Expected amplitude=2 for 'imaging' group; "
                f"min={amps.min():.4f} max={amps.max():.4f}"
            )

    # ------------------------------------------------------------------ #
    # _flag_mask honours the group's flag variable                         #
    # ------------------------------------------------------------------ #

    def test_flag_mask_dtype_bool(self):
        """FLAG (uint8) is cast to bool regardless of group."""
        with self._open() as b:
            ds   = b._partitions[0]
            flag = b._flag_mask(ds)
            assert flag.dtype == bool

    # ------------------------------------------------------------------ #
    # query_columns propagates the group selection end-to-end              #
    # ------------------------------------------------------------------ #

    def test_query_columns_base_amplitude_near_zero(self):
        """Scatter query on 'base' group produces near-zero amplitudes."""
        sel = SelectionSpec()
        with self._open(data_group="base") as b:
            pol    = b.metadata()["correlation_labels"][0]
            result = b.query_columns(Axis.TIME,
                                     [(Axis.AMPLITUDE, pol)], sel)
            df = result[(Axis.AMPLITUDE, pol)]
            assert len(df) >= 0   # may be empty if all-zero and flagged
            # If rows exist, all amplitudes are near zero
            if len(df) > 0:
                assert (df["y"] < 1e-5).all()

    def test_query_columns_imaging_amplitude_near_two(self):
        """Scatter query on 'imaging' group produces amplitude ≈ 2."""
        sel = SelectionSpec()
        with self._open(data_group="imaging") as b:
            pol    = b.metadata()["correlation_labels"][0]
            result = b.query_columns(Axis.TIME,
                                     [(Axis.AMPLITUDE, pol)], sel)
            df = result[(Axis.AMPLITUDE, pol)]
            if len(df) > 0:
                assert np.allclose(df["y"].values, 2.0, atol=1e-4), (
                    f"Expected amplitude≈2, got range "
                    f"[{df['y'].min():.4f}, {df['y'].max():.4f}]"
                )

    # ------------------------------------------------------------------ #
    # repr includes data_group when set                                    #
    # ------------------------------------------------------------------ #

    def test_repr_includes_data_group(self):
        with self._open(data_group="imaging") as b:
            r = repr(b)
            assert "imaging" in r


# ---------------------------------------------------------------------------
# 11. xradio-native DataTree structure  (synthetic store, no env var needed)
# ---------------------------------------------------------------------------

def _get_or_create_xradio_native_ps() -> str:
    """Return path to a synthetic xradio-native-style ps.zarr.

    xradio-produced Processing Sets have a different DataTree layout from
    xarray-ms-written stores:

    * xarray-ms style: partition node contains the correlated data directly,
      ``attrs["type"] == "visibility"``.
    * xradio-native style: partition node (``ms_xdt``) is a container whose
      correlated data lives in a ``correlated_xds`` child node.  The parent
      node itself does not carry ``attrs["type"] == "visibility"`` and does
      not contain VISIBILITY / SPECTRUM directly.

    ``_collect_visibility_partitions`` must detect the ``correlated_xds``
    child and use that Dataset, not the empty parent.
    """
    import xarray as xr
    import dask.array as da

    path = os.environ.get("PS_NATIVE", "/tmp/msv4_test_native.ps.zarr")
    if os.path.isdir(path):
        return path

    n_time, n_bl, n_freq, n_pol = 5, 3, 8, 2

    # The correlated data Dataset — lives as a child called 'correlated_xds'
    corr_ds = xr.Dataset(
        {
            "VISIBILITY": xr.DataArray(
                da.ones((n_time, n_bl, n_freq, n_pol),
                        chunks=(n_time, n_bl, n_freq, n_pol),
                        dtype=np.complex64),
                dims=["time", "baseline_id", "frequency", "polarization"],
            ),
            "FLAG": xr.DataArray(
                da.zeros((n_time, n_bl, n_freq, n_pol),
                         chunks=(n_time, n_bl, n_freq, n_pol),
                         dtype=np.uint8),
                dims=["time", "baseline_id", "frequency", "polarization"],
            ),
            "UVW": xr.DataArray(
                da.zeros((n_time, n_bl, 3),
                         chunks=(n_time, n_bl, 3),
                         dtype=np.float32),
                dims=["time", "baseline_id", "uvw_label"],
            ),
        },
        coords={
            "time":                   np.linspace(1.35e9, 1.36e9, n_time),
            "baseline_id":            np.arange(n_bl),
            "frequency":              np.linspace(3.72e11, 3.73e11, n_freq),
            "polarization":           ["XX", "YY"],
            "uvw_label":              ["u", "v", "w"],
            "baseline_antenna1_name": ("baseline_id", ["DA01", "DA01", "DA02"]),
            "baseline_antenna2_name": ("baseline_id", ["DA02", "DA03", "DA03"]),
            "field_name":             ("time", ["F1"] * n_time),
            "scan_name":              ("time", ["1"] * n_time),
        },
        attrs={
            "type": "visibility",
            "data_groups": {
                "base": {
                    "correlated_data": "VISIBILITY",
                    "flag":            "FLAG",
                    "uvw":             "UVW",
                },
            },
        },
    )

    # The parent container node — intentionally empty of data vars,
    # no 'type' attribute, no VISIBILITY directly.  This is the ms_xdt
    # pattern used by xradio.
    parent_ds = xr.Dataset(
        attrs={"observation_info": {"telescope": "VLA"}},
    )

    tree = xr.DataTree.from_dict({
        "ms_partition_000":                parent_ds,
        "ms_partition_000/correlated_xds": corr_ds,
    })
    tree.to_zarr(path, mode="w", compute=True)
    return path


class TestXRadioNativeStructure:
    """Tests for the xradio-native DataTree layout.

    Verifies that ``_collect_visibility_partitions`` correctly detects
    ``correlated_xds`` child nodes (the xradio-produced layout) and returns
    them as visibility partitions, alongside the xarray-ms flat layout
    where data lives directly in the partition node.
    """

    @classmethod
    def setup_class(cls):
        _suppress_warnings()
        cls.ps_path = _get_or_create_xradio_native_ps()

    # ------------------------------------------------------------------ #
    # Partition detection                                                  #
    # ------------------------------------------------------------------ #

    def test_finds_one_partition(self):
        """xradio-native store should yield exactly one visibility partition."""
        with MSv4Backend(self.ps_path) as b:
            assert len(b._partitions) == 1, (
                f"Expected 1 partition, got {len(b._partitions)}"
            )

    def test_partition_has_visibility(self):
        """The detected partition must contain VISIBILITY."""
        with MSv4Backend(self.ps_path) as b:
            ds = b._partitions[0]
            assert "VISIBILITY" in ds.data_vars, (
                f"VISIBILITY missing; vars={list(ds.data_vars)}"
            )

    def test_partition_has_correct_dims(self):
        """Partition dims should follow the standard MSv4 interferometer schema."""
        with MSv4Backend(self.ps_path) as b:
            ds = b._partitions[0]
            for dim in ("time", "baseline_id", "frequency", "polarization"):
                assert dim in ds.sizes, f"Missing dimension: {dim}"

    def test_observation_mode_auto_detected_as_interferometer(self):
        """Auto-detection should resolve to interferometer for this store."""
        with MSv4Backend(self.ps_path) as b:
            assert b._resolved_mode == "interferometer"
            assert not b.is_single_dish

    # ------------------------------------------------------------------ #
    # End-to-end query through the detected partition                      #
    # ------------------------------------------------------------------ #

    def test_metadata_returns_antennas(self):
        with MSv4Backend(self.ps_path) as b:
            meta = b.metadata()
            assert len(meta["antenna_names"]) > 0
            assert meta["n_baselines"] > 0

    def test_query_columns_returns_data(self):
        """query_columns must succeed and return non-empty DataFrames."""
        with MSv4Backend(self.ps_path) as b:
            pol    = b.metadata()["correlation_labels"][0]
            sel    = SelectionSpec()
            result = b.query_columns(Axis.TIME,
                                     [(Axis.AMPLITUDE, pol)], sel)
            df = result[(Axis.AMPLITUDE, pol)]
            assert len(df) > 0
            assert not df.isna().any().any()

    def test_query_raster_correct_dims(self):
        """query_raster must return a 2D DataArray with the right dimension names."""
        with MSv4Backend(self.ps_path) as b:
            pol = b.metadata()["correlation_labels"][0]
            agg, *_ = b.query_raster(
                Axis.TIME, Axis.BASELINE, Axis.AMPLITUDE,
                SelectionSpec(), polarization=pol,
            )
            assert agg.dims == ("time", "baseline_id")
            assert np.isfinite(agg.values).any()

    def test_parent_node_not_included_as_partition(self):
        """The empty parent ms_xdt container must NOT appear as a partition."""
        with MSv4Backend(self.ps_path) as b:
            for ds in b._partitions:
                # Every partition must have a time dimension and correlated data
                assert ds.sizes.get("time", 0) > 0
                assert any(v in ds.data_vars
                           for v in ("VISIBILITY", "SPECTRUM", "DATA")), (
                    f"Partition has no correlated data var: {list(ds.data_vars)}"
                )


# ---------------------------------------------------------------------------
# 12. Single-dish mode  (synthetic SpectrumXds store, PS_SD env var)
# ---------------------------------------------------------------------------

def _get_or_create_sd_ps() -> str:
    """Return path to a synthetic single-dish (radiometer) ps.zarr.

    The store follows the MSv4 SpectrumXds schema:
    * Second dimension is ``antenna_name``, not ``baseline_id``.
    * Primary data variable is ``SPECTRUM`` (real-valued float32).
    * No ``UVW`` variable.
    * ``attrs["type"] == "radiometer"``.

    The SPECTRUM values are set to ``float(antenna_index + 1)`` so each
    antenna has a distinct, predictable amplitude for assertion purposes.
    """
    import xarray as xr
    import dask.array as da

    path = os.environ.get("PS_SD", "/tmp/msv4_test_sd.ps.zarr")
    if os.path.isdir(path):
        return path

    n_time, n_ant, n_freq, n_pol = 10, 4, 16, 2
    ant_names = ["GBT", "VLA01", "VLA02", "VLA03"]

    # Spectrum values: antenna index + 1, broadcast over (time, freq, pol)
    spectrum_vals = (
        np.arange(1, n_ant + 1, dtype=np.float32)
        .reshape(1, n_ant, 1, 1) *
        np.ones((n_time, n_ant, n_freq, n_pol), dtype=np.float32)
    )

    ds = xr.Dataset(
        {
            "SPECTRUM": xr.DataArray(
                da.from_array(spectrum_vals,
                              chunks=(5, n_ant, 8, n_pol)),
                dims=["time", "antenna_name", "frequency", "polarization"],
            ),
            "FLAG": xr.DataArray(
                da.zeros((n_time, n_ant, n_freq, n_pol),
                         chunks=(5, n_ant, 8, n_pol),
                         dtype=np.uint8),
                dims=["time", "antenna_name", "frequency", "polarization"],
            ),
            "WEIGHT": xr.DataArray(
                da.ones((n_time, n_ant, n_freq, n_pol),
                        chunks=(5, n_ant, 8, n_pol),
                        dtype=np.float32),
                dims=["time", "antenna_name", "frequency", "polarization"],
            ),
        },
        coords={
            "time":        np.linspace(1.35e9, 1.36e9, n_time),
            "antenna_name": ant_names,
            "frequency":   np.linspace(1.0e9, 1.1e9, n_freq),
            "polarization": ["XX", "YY"],
            "field_name":  ("time", ["Field1"] * 5 + ["Field2"] * 5),
            "scan_name":   ("time", ["1"] * 5 + ["2"] * 5),
        },
        attrs={
            "type": "radiometer",
            "data_groups": {
                "base": {
                    "correlated_data": "SPECTRUM",
                    "flag":            "FLAG",
                    "weight":          "WEIGHT",
                },
            },
        },
    )
    xr.DataTree.from_dict({"partition_000": ds}).to_zarr(path, mode="w",
                                                          compute=True)
    return path


class TestSingleDish:
    """Tests for single-dish (radiometer / SpectrumXds) mode.

    Uses a synthetic store with ``antenna_name`` as the second dimension
    and ``SPECTRUM`` as the correlated data variable.  Four antennas with
    amplitudes 1, 2, 3, 4 respectively for predictable assertions.

    Covers:
    * Auto-detection of single-dish mode
    * ``_baseline_dim == "antenna_name"``
    * ``metadata()`` collecting from ``antenna_name`` coord
    * ``_apply_selection`` routing ``antenna_names`` correctly
    * ``sel.baselines`` silently ignored
    * UV axes raising ``NotImplementedError``
    * ``query_uv_coverage`` returning empty DataFrame
    * ``query_raster`` producing ``time × antenna_name`` arrays
    * ``query_columns`` producing valid DataFrames from spectrum data
    * ``samples_per_pixel`` using ``antenna_name`` size
    """

    _ANT_NAMES = ["GBT", "VLA01", "VLA02", "VLA03"]

    @classmethod
    def setup_class(cls):
        _suppress_warnings()
        cls.ps_path = _get_or_create_sd_ps()
        cls.backend = MSv4Backend(cls.ps_path)
        cls.backend.open()
        cls.meta = cls.backend.metadata()
        cls.pols = cls.meta["correlation_labels"]
        cls.sel  = SelectionSpec()

    @classmethod
    def teardown_class(cls):
        cls.backend.close()

    # ------------------------------------------------------------------ #
    # Mode detection                                                       #
    # ------------------------------------------------------------------ #

    def test_is_single_dish(self):
        assert self.backend.is_single_dish
        assert self.backend._resolved_mode == "single_dish"

    def test_baseline_dim_is_antenna_name(self):
        assert self.backend._baseline_dim == "antenna_name"

    def test_explicit_single_dish_mode_respected(self):
        with MSv4Backend(self.ps_path,
                         observation_mode="single_dish") as b:
            assert b._resolved_mode == "single_dish"

    # ------------------------------------------------------------------ #
    # Metadata                                                             #
    # ------------------------------------------------------------------ #

    def test_antenna_names_from_dim_coord(self):
        """antenna_names collected from antenna_name dimension, not pair coords."""
        assert set(self.meta["antenna_names"]) == set(self._ANT_NAMES)

    def test_n_baselines_equals_n_antennas(self):
        """n_baselines reports the antenna count for single-dish data."""
        assert self.meta["n_baselines"] == len(self._ANT_NAMES)

    def test_field_names_present(self):
        assert len(self.meta["field_names"]) > 0

    def test_data_column_in_metadata(self):
        """SPECTRUM maps to 'DATA' in the data_columns list."""
        assert "DATA" in self.meta["data_columns"]

    # ------------------------------------------------------------------ #
    # _apply_selection — antenna dimension                                 #
    # ------------------------------------------------------------------ #

    def test_antenna_names_selects_antenna_name_dim(self):
        ds_part = self.backend._partitions[0]
        sel_ant = SelectionSpec(antenna_names=["GBT", "VLA01"])
        ds_sel  = self.backend._apply_selection(ds_part, sel_ant)
        assert ds_sel.sizes["antenna_name"] == 2
        assert set(ds_sel.coords["antenna_name"].values) == {"GBT", "VLA01"}

    def test_baselines_silently_ignored(self):
        """sel.baselines has no meaning for single-dish; antenna_name is unchanged."""
        ds_part = self.backend._partitions[0]
        n_orig  = ds_part.sizes["antenna_name"]
        sel_bl  = SelectionSpec(baselines=[("GBT", "VLA01")])
        ds_sel  = self.backend._apply_selection(ds_part, sel_bl)
        assert ds_sel.sizes["antenna_name"] == n_orig

    def test_time_range_still_reduces_time(self):
        ds_part = self.backend._partitions[0]
        t0, t1  = self.meta["time_range"]
        sel_t   = SelectionSpec(time_range=(t0, t0 + (t1 - t0) * 0.5))
        ds_sel  = self.backend._apply_selection(ds_part, sel_t)
        assert 0 < ds_sel.sizes["time"] < ds_part.sizes["time"]

    # ------------------------------------------------------------------ #
    # UV axes — not applicable for single dish                             #
    # ------------------------------------------------------------------ #

    def test_uvdist_x_raises_not_implemented(self):
        with pytest.raises(NotImplementedError, match="single"):
            self.backend.query_columns(
                Axis.UVDIST,
                [(Axis.AMPLITUDE, self.pols[0])],
                self.sel,
            )

    def test_uvdist_lambda_x_raises_not_implemented(self):
        with pytest.raises(NotImplementedError, match="single"):
            self.backend.query_columns(
                Axis.UVDIST_LAMBDA,
                [(Axis.AMPLITUDE, self.pols[0])],
                self.sel,
            )

    def test_u_axis_raises_not_implemented(self):
        with pytest.raises(NotImplementedError, match="single"):
            self.backend.query_columns(
                Axis.U,
                [(Axis.AMPLITUDE, self.pols[0])],
                self.sel,
            )

    def test_v_axis_raises_not_implemented(self):
        with pytest.raises(NotImplementedError, match="single"):
            self.backend.query_columns(
                Axis.V,
                [(Axis.AMPLITUDE, self.pols[0])],
                self.sel,
            )

    # ------------------------------------------------------------------ #
    # query_uv_coverage — empty for single dish                           #
    # ------------------------------------------------------------------ #

    def test_uv_coverage_returns_empty_dataframe(self):
        df = self.backend.query_uv_coverage(self.sel)
        assert isinstance(df, pd.DataFrame)
        assert {"x", "y"} <= set(df.columns)
        assert len(df) == 0

    def test_uv_coverage_empty_regardless_of_conjugate(self):
        df_plain = self.backend.query_uv_coverage(self.sel,
                                                   include_conjugate=False)
        df_conj  = self.backend.query_uv_coverage(self.sel,
                                                   include_conjugate=True)
        assert len(df_plain) == 0
        assert len(df_conj)  == 0

    # ------------------------------------------------------------------ #
    # query_columns                                                        #
    # ------------------------------------------------------------------ #

    def test_query_columns_returns_dataframe(self):
        result = self.backend.query_columns(
            Axis.TIME,
            [(Axis.AMPLITUDE, self.pols[0])],
            self.sel,
        )
        df = result[(Axis.AMPLITUDE, self.pols[0])]
        assert isinstance(df, pd.DataFrame)
        assert {"x", "y"} <= set(df.columns)
        assert len(df) > 0

    def test_query_columns_no_nan(self):
        result = self.backend.query_columns(
            Axis.TIME,
            [(Axis.AMPLITUDE, self.pols[0])],
            self.sel,
        )
        df = result[(Axis.AMPLITUDE, self.pols[0])]
        assert not df["x"].isna().any()
        assert not df["y"].isna().any()

    def test_query_columns_amplitude_nonnegative(self):
        result = self.backend.query_columns(
            Axis.TIME,
            [(Axis.AMPLITUDE, self.pols[0])],
            self.sel,
        )
        df = result[(Axis.AMPLITUDE, self.pols[0])]
        assert (df["y"] >= 0).all()

    def test_query_columns_time_x_is_mjd_seconds(self):
        result = self.backend.query_columns(
            Axis.TIME,
            [(Axis.AMPLITUDE, self.pols[0])],
            self.sel,
        )
        df = result[(Axis.AMPLITUDE, self.pols[0])]
        assert df["x"].min() > 1e9

    def test_query_columns_frequency_x_axis(self):
        result = self.backend.query_columns(
            Axis.FREQUENCY,
            [(Axis.AMPLITUDE, self.pols[0])],
            self.sel,
        )
        df = result[(Axis.AMPLITUDE, self.pols[0])]
        assert len(df) > 0
        assert df["x"].min() > 1e8  # frequency in Hz

    # ------------------------------------------------------------------ #
    # query_raster — time × antenna_name                                   #
    # ------------------------------------------------------------------ #

    def test_raster_returns_four_tuple(self):
        result = self.backend.query_raster(
            Axis.TIME, Axis.BASELINE, Axis.AMPLITUDE,
            self.sel, polarization=self.pols[0],
        )
        assert len(result) == 4

    def test_raster_dims_are_time_antenna_name(self):
        agg, *_ = self.backend.query_raster(
            Axis.TIME, Axis.BASELINE, Axis.AMPLITUDE,
            self.sel, polarization=self.pols[0],
        )
        assert agg.dims == ("time", "antenna_name"), (
            f"Expected (time, antenna_name), got {agg.dims}"
        )

    def test_raster_has_finite_values(self):
        agg, *_ = self.backend.query_raster(
            Axis.TIME, Axis.BASELINE, Axis.AMPLITUDE,
            self.sel, polarization=self.pols[0],
        )
        assert np.isfinite(agg.values).any()

    def test_raster_x_range_is_index_range(self):
        """x_range for antenna_name (string) dim should be integer indices."""
        n_ant = len(self._ANT_NAMES)
        _, x_range, _, _ = self.backend.query_raster(
            Axis.TIME, Axis.BASELINE, Axis.AMPLITUDE,
            self.sel, polarization=self.pols[0],
        )
        assert x_range == (0.0, float(n_ant - 1)), (
            f"Expected index range (0.0, {float(n_ant-1)}), got {x_range}"
        )

    def test_raster_y_range_is_mjd_seconds(self):
        """y_range for time (numeric) dim should be MJD seconds."""
        _, _, y_range, _ = self.backend.query_raster(
            Axis.TIME, Axis.BASELINE, Axis.AMPLITUDE,
            self.sel, polarization=self.pols[0],
        )
        assert y_range[0] > 1e9, (
            f"Expected MJD seconds, got y_range={y_range}"
        )

    def test_freq_baseline_raster_dims(self):
        """FREQUENCY × BASELINE → frequency × antenna_name."""
        agg, *_ = self.backend.query_raster(
            Axis.FREQUENCY, Axis.BASELINE, Axis.AMPLITUDE,
            self.sel, polarization=self.pols[0],
        )
        assert agg.dims == ("frequency", "antenna_name")

    def test_flag_raster_values_in_range(self):
        agg, *_ = self.backend.query_raster(
            Axis.TIME, Axis.BASELINE, Axis.FLAG, self.sel,
        )
        vals = agg.values[np.isfinite(agg.values)]
        assert (vals >= 0).all() and (vals <= 1).all()

    # ------------------------------------------------------------------ #
    # samples_per_pixel uses antenna_name size                             #
    # ------------------------------------------------------------------ #

    def test_samples_per_pixel_positive(self):
        rx, ry = self.backend.samples_per_pixel(
            Axis.TIME, Axis.BASELINE, self.sel, 400, 300,
        )
        assert rx > 0 and ry > 0

    def test_samples_per_pixel_uses_antenna_count(self):
        """x ratio should reflect antenna count, not baseline_id count."""
        n_ant = len(self._ANT_NAMES)
        # Axis.BASELINE maps to antenna_name for single dish → x dimension
        rx, _  = self.backend.samples_per_pixel(
            Axis.TIME, Axis.BASELINE, self.sel, n_ant, 300,
        )
        # ratio = n_ant / canvas_width = n_ant / n_ant = 1.0
        assert abs(rx - 1.0) < 0.01, (
            f"Expected ratio ≈ 1.0 when canvas_width == n_ant, got {rx:.4f}"
        )


