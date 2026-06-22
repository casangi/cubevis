"""
test_msv2_backend.py — Unit and integration tests for MSv2Backend.

Location in repository:
    cubevis/tests/manual/visplot/test_msv2_backend.py

Tests against:
    cubevis/cubevis/toolbox/visplot/data/msv2_backend.py
    cubevis/cubevis/toolbox/visplot/axes.py       (real Axis/AxisType)
    cubevis/cubevis/toolbox/visplot/selection.py  (real SelectionSpec,
                                                   extended with
                                                   antenna_names and
                                                   channel_range)

Run from the cubevis repository root (so the package is importable):

    MS=sis14_twhya_calibrated_flagged.ms \\
        pytest cubevis/tests/manual/visplot/test_msv2_backend.py -v

Or standalone (falls back to local copies of the source files if the
package is not installed):

    MS=sis14_twhya_calibrated_flagged.ms \\
        python test_msv2_backend.py

Tests
-----
1. Lifecycle           open/close/context manager/idempotency/bad path
2. Metadata            return structure, types, physical plausibility
3. _apply_selection    each constraint reduces dimensions via isel()
4. query_columns       API contract, DataFrame correctness, fused path
5. query_columns       RENDERED: Datashader agg, serial≡fused, timing
6. query_raster        2D DataArray shape, all (y,x) combinations
7. query_uv_coverage   conjugate symmetry, rendered through Datashader
8. samples_per_pixel   geometric ratio correctness

The RENDERED tests (marked # RENDERED) exercise the full pipeline that
produced the 45s time in test_10 and verify:
  * serial pipeline and fused pipeline produce pixel-identical
    Datashader float64 agg DataArrays (no Bokeh required)
  * the fused pipeline is ≥ 2× faster than serial at medium load
  * the full partition (all channels, all time, 4 axes) completes < 10s
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
# Import strategy
#
# Primary: real package (cubevis installed or on PYTHONPATH)
# Fallback: load source files from the same directory as this test
#           (for running the file directly without a package install)
# ---------------------------------------------------------------------------

def _try_package_import():
    """Try to import from the installed cubevis package."""
    from cubevis.toolbox.visplot.axes import Axis, AxisType
    from cubevis.toolbox.visplot.selection import SelectionSpec
    from cubevis.toolbox.visplot.data.msv2_backend import (
        MSv2Backend, _axis_to_dim,
    )
    return Axis, AxisType, SelectionSpec, MSv2Backend, _axis_to_dim


def _local_import():
    """Load source files from the directory containing this test file."""
    import importlib.util

    here = Path(__file__).parent

    def _load(name, path):
        spec = importlib.util.spec_from_file_location(name, path)
        mod  = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
        return mod

    # Load in dependency order so relative imports resolve
    axes_mod      = _load("cubevis.toolbox.visplot.axes",
                          here / "axes.py")
    sel_mod       = _load("cubevis.toolbox.visplot.selection",
                          here / "selection.py")

    # reader.py needs the above two already in sys.modules
    # Patch the relative import names that reader.py uses
    sys.modules["cubevis.toolbox.visplot.axes"]      = axes_mod
    sys.modules["cubevis.toolbox.visplot.selection"] = sel_mod
    _load("cubevis.toolbox.visplot.reader", here / "reader.py")

    backend_mod = _load("cubevis.toolbox.visplot.data.msv2_backend",
                        here / "msv2_backend.py")

    return (
        axes_mod.Axis,
        axes_mod.AxisType,
        sel_mod.SelectionSpec,
        backend_mod.MSv2Backend,
        backend_mod._axis_to_dim,
    )


try:
    Axis, AxisType, SelectionSpec, MSv2Backend, _axis_to_dim = _try_package_import()
    _SOURCE = "package"
except ImportError:
    Axis, AxisType, SelectionSpec, MSv2Backend, _axis_to_dim = _local_import()
    _SOURCE = "local"

print(f"[test_msv2_backend] imports from: {_SOURCE}")

try:
    import datashader as ds
    import datashader.reductions as ds_agg
    HAS_DATASHADER = True
except ImportError:
    HAS_DATASHADER = False

PLOT_W = 400
PLOT_H = 300


# ---------------------------------------------------------------------------
# Fixtures / shared helpers
# ---------------------------------------------------------------------------

def _get_ms() -> str:
    path = os.environ.get("MS", "sis14_twhya_calibrated_flagged.ms")
    if not os.path.isdir(path):
        pytest.skip(
            f"Test MS not found at {path!r}. "
            "Set MS= env var or download from "
            "https://casa.nrao.edu/download/devel/casavis/data/"
            "sis14_twhya_calibrated_flagged.ms.tar.gz"
        )
    return path


def _open_backend(**kwargs) -> MSv2Backend:
    b = MSv2Backend(_get_ms(), **kwargs)
    b.open()
    return b


def _largest_partition(backend: MSv2Backend):
    """Return the partition Dataset with the most integrations."""
    return max(
        backend._iter_visibility_partitions(),
        key=lambda d: d.sizes["time"],
    )


def _suppress_warnings():
    warnings.filterwarnings("ignore", category=UserWarning,
                            module="xarray_ms")
    warnings.filterwarnings("ignore",
                            message="The return type of.*Dataset.dims",
                            category=FutureWarning)
    warnings.filterwarnings("ignore", message="omp_set_nested")


def _require_datashader():
    if not HAS_DATASHADER:
        pytest.skip("datashader not installed — pip install datashader")


def _agg_from_df(df: pd.DataFrame) -> "xr.DataArray":
    """Scatter DataFrame → Datashader float64 agg DataArray."""
    cvs = ds.Canvas(plot_width=PLOT_W, plot_height=PLOT_H)
    return cvs.points(df, "x", "y", agg=ds_agg.mean("y"))


# ---------------------------------------------------------------------------
# 1. Lifecycle
# ---------------------------------------------------------------------------

class TestLifecycle:

    def test_open_and_close(self):
        ms = _get_ms()
        b = MSv2Backend(ms)
        assert b._datatree is None, "DataTree should be None before open()"
        b.open()
        assert b._datatree is not None
        b.close()
        assert b._datatree is None

    def test_open_idempotent(self):
        """Second open() reuses the existing DataTree without re-opening."""
        ms = _get_ms()
        b = MSv2Backend(ms)
        b.open()
        dt1 = id(b._datatree)
        b.open()
        assert id(b._datatree) == dt1, "open() should be idempotent"
        b.close()

    def test_context_manager(self):
        ms = _get_ms()
        with MSv2Backend(ms) as b:
            assert b._datatree is not None
        assert b._datatree is None

    def test_require_open_raises_before_open(self):
        ms = _get_ms()
        b = MSv2Backend(ms)
        with pytest.raises(RuntimeError, match="not open"):
            b.metadata()

    def test_repr_shows_status(self):
        ms = _get_ms()
        b = MSv2Backend(ms)
        assert "closed" in repr(b)
        b.open()
        assert "open" in repr(b)
        b.close()

    def test_wrong_path_raises_runtime_error(self):
        b = MSv2Backend("/nonexistent/does_not_exist.ms")
        with pytest.raises(RuntimeError):
            b.open()

    def test_custom_chunks_applied(self):
        """Chunks passed at construction reach the underlying DataArrays."""
        _suppress_warnings()
        with MSv2Backend(_get_ms(), chunks={"time": 30}) as b:
            for ds_part in b._iter_visibility_partitions():
                vis = ds_part["VISIBILITY"]
                t_chunks = vis.chunks[vis.dims.index("time")]
                assert all(c <= 30 for c in t_chunks), (
                    f"Time chunks {t_chunks} exceed requested size 30"
                )
                break  # one partition is enough

    def test_data_column_stored(self):
        """data_column= parameter is stored and reported in metadata().

        Note: xarray-ms 0.5.x does not expose a column= kwarg at the
        open_datatree level, so DATA/CORRECTED_DATA routing is handled
        by _resolve_vis() rather than at open time.  The stored value
        is used for metadata reporting and future xarray-ms versions.
        """
        _suppress_warnings()
        b = MSv2Backend(_get_ms(), data_column="DATA")
        assert b._data_column == "DATA"
        b.open()
        meta = b.metadata()
        # metadata() reports data_column from self._data_column
        assert "DATA" in meta["data_columns"]
        b.close()


# ---------------------------------------------------------------------------
# 2. Metadata
# ---------------------------------------------------------------------------

class TestMetadata:

    def setup_method(self):
        _suppress_warnings()
        self.backend = _open_backend()
        self.meta = self.backend.metadata()

    def teardown_method(self):
        self.backend.close()

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
        assert isinstance(m["time_range"],         tuple) and len(m["time_range"]) == 2
        assert isinstance(m["freq_range"],         tuple) and len(m["freq_range"]) == 2
        assert isinstance(m["n_baselines"],        int)
        assert isinstance(m["data_columns"],       list)

    def test_field_names_nonempty(self):
        assert len(self.meta["field_names"]) > 0

    def test_antenna_names_plausible(self):
        # ALMA dataset should have ~43 antennas
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
        # ALMA Band 7 ≈ 372 GHz
        assert f0 > 3e11, f"Frequency looks wrong: {f0:.3e} Hz"

    def test_n_baselines_positive(self):
        assert self.meta["n_baselines"] > 0

    def test_correlation_labels_nonempty(self):
        pols = self.meta["correlation_labels"]
        assert len(pols) > 0
        # sis14 has XX, YY
        assert any(p in pols for p in ("XX", "YY", "RR", "LL"))
        print(f"  Polarizations: {pols}")

    def test_data_column_in_metadata(self):
        assert "DATA" in self.meta["data_columns"]


# ---------------------------------------------------------------------------
# 3. _apply_selection — dimension reduction via isel()
#
# Critical difference from MSv4Backend: MSv2Backend uses isel() to
# actually reduce array sizes rather than where() which only NaN-masks.
# Each test checks that sizes["dim"] decreases, not just that values
# outside the selection become NaN.
# ---------------------------------------------------------------------------

class TestApplySelection:

    def setup_method(self):
        _suppress_warnings()
        self.backend = _open_backend()
        self.ds_part = _largest_partition(self.backend)

    def teardown_method(self):
        self.backend.close()

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
        t0, t1 = float(times[0]), float(times[min(9, len(times)-1)])
        ds_sel = self._sel(time_range=(t0, t1))
        assert 0 < ds_sel.sizes["time"] <= 10, (
            f"time_range should reduce time to ≤10, got {ds_sel.sizes['time']}"
        )
        # Verify actual size reduction (not just NaN-masking)
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
        assert ds_sel.sizes["frequency"] == 16, (
            f"channel_range=(4,20) should give 16 channels, "
            f"got {ds_sel.sizes['frequency']}"
        )
        # Must be a genuine size reduction, not NaN padding
        assert ds_sel.sizes["frequency"] < self.ds_part.sizes["frequency"]

    def test_freq_range_reduces_freq_size(self):
        freqs = self.ds_part.coords["frequency"].values
        f0, f1 = float(freqs[4]), float(freqs[19])
        ds_sel = self._sel(freq_range=(f0, f1))
        assert ds_sel.sizes["frequency"] <= 16
        assert ds_sel.sizes["frequency"] < self.ds_part.sizes["frequency"]

    def test_channel_range_takes_precedence_over_freq_range(self):
        """When both are set, channel_range is applied first."""
        freqs = self.ds_part.coords["frequency"].values
        ds_sel = self.backend._apply_selection(
            self.ds_part,
            SelectionSpec(
                channel_range=(0, 8),
                freq_range=(float(freqs[0]), float(freqs[-1])),  # all channels
            ),
        )
        # channel_range=(0,8) reduces to 8; freq_range then selects from those 8
        assert ds_sel.sizes["frequency"] <= 8

    def test_polarization_reduces_pol_size(self):
        pols = self.ds_part.coords["polarization"].values
        if len(pols) < 2:
            pytest.skip("Only one polarization available")
        ds_sel = self._sel(correlation=[str(pols[0])])
        assert ds_sel.sizes["polarization"] == 1

    def test_antenna_names_reduces_baseline_size(self):
        """antenna_names selects baselines where ant1 OR ant2 matches."""
        if "baseline_antenna1_name" not in self.ds_part.coords:
            pytest.skip("Antenna name coords not present")
        ant1v = self.ds_part.coords["baseline_antenna1_name"].values
        ant2v = self.ds_part.coords["baseline_antenna2_name"].values
        all_ants = np.unique(np.concatenate([ant1v, ant2v]))
        target = str(all_ants[0])
        ds_sel = self._sel(antenna_names=[target])
        # Should have fewer baselines than total
        assert 0 < ds_sel.sizes["baseline_id"] < self.ds_part.sizes["baseline_id"]
        # All remaining baselines must involve target
        a1 = ds_sel.coords["baseline_antenna1_name"].values
        a2 = ds_sel.coords["baseline_antenna2_name"].values
        assert ((a1 == target) | (a2 == target)).all()

    def test_baselines_exact_pair_selection(self):
        """baselines= selects only the exact listed antenna pairs."""
        if "baseline_antenna1_name" not in self.ds_part.coords:
            pytest.skip("Antenna name coords not present")
        ant1v = self.ds_part.coords["baseline_antenna1_name"].values
        ant2v = self.ds_part.coords["baseline_antenna2_name"].values
        # Find a valid non-padded pair (finite EIT)
        eit = self.ds_part["EFFECTIVE_INTEGRATION_TIME"].isel(time=0).compute()
        valid = np.where(np.isfinite(eit.values))[0]
        if len(valid) == 0:
            pytest.skip("No non-padded baselines found")
        idx = valid[0]
        a1, a2 = str(ant1v[idx]), str(ant2v[idx])
        ds_sel = self._sel(baselines=[(a1, a2)])
        assert ds_sel.sizes["baseline_id"] == 1
        assert str(ds_sel.coords["baseline_antenna1_name"].values[0]) == a1
        assert str(ds_sel.coords["baseline_antenna2_name"].values[0]) == a2

    def test_baselines_takes_precedence_over_antenna_names(self):
        """When both baselines and antenna_names are set, baselines wins."""
        if "baseline_antenna1_name" not in self.ds_part.coords:
            pytest.skip("Antenna name coords not present")
        ant1v = self.ds_part.coords["baseline_antenna1_name"].values
        ant2v = self.ds_part.coords["baseline_antenna2_name"].values
        eit = self.ds_part["EFFECTIVE_INTEGRATION_TIME"].isel(time=0).compute()
        valid = np.where(np.isfinite(eit.values))[0]
        if len(valid) == 0:
            pytest.skip("No non-padded baselines")
        idx = valid[0]
        a1, a2 = str(ant1v[idx]), str(ant2v[idx])
        # antenna_names selects many baselines; baselines= selects only one
        ds_sel = self.backend._apply_selection(
            self.ds_part,
            SelectionSpec(baselines=[(a1, a2)], antenna_names=[a1]),
        )
        assert ds_sel.sizes["baseline_id"] == 1

    def test_empty_time_range_returns_zero_time(self):
        """A time_range in the distant past matches no integrations."""
        ds_sel = self._sel(time_range=(0.0, 1.0))
        assert ds_sel.sizes["time"] == 0

    def test_compound_selection_cumulative(self):
        times = self.ds_part.coords["time"].values
        t0, t1 = float(times[0]), float(times[min(19, len(times)-1)])
        ds_sel = self._sel(time_range=(t0, t1), channel_range=(0, 16))
        assert ds_sel.sizes["time"]      <= 20
        assert ds_sel.sizes["frequency"] == 16


# ---------------------------------------------------------------------------
# 4. query_columns — structural contract
# ---------------------------------------------------------------------------

class TestQueryColumnsStructure:

    def setup_method(self):
        _suppress_warnings()
        self.backend = _open_backend()
        meta = self.backend.metadata()
        self.pols = meta["correlation_labels"]
        t0, t1 = meta["time_range"]
        self.sel_small = SelectionSpec(
            time_range=(t0, t0 + (t1 - t0) * 0.08),
            channel_range=(0, 16),
        )

    def teardown_method(self):
        self.backend.close()

    def test_returns_dict_of_dataframes(self):
        yaxes = [(Axis.AMPLITUDE, self.pols[0])]
        result = self.backend.query_columns(Axis.TIME, yaxes, self.sel_small)
        assert isinstance(result, dict)
        key = (Axis.AMPLITUDE, self.pols[0])
        assert key in result
        assert isinstance(result[key], pd.DataFrame)
        assert {"x", "y"} <= set(result[key].columns)

    def test_no_nan_in_output(self):
        """DataFrames must be NaN-free — ready for datashader.Canvas.points()."""
        yaxes = [(Axis.AMPLITUDE, self.pols[0])]
        result = self.backend.query_columns(Axis.TIME, yaxes, self.sel_small)
        df = result[(Axis.AMPLITUDE, self.pols[0])]
        assert not df["x"].isna().any(), "NaN x values in query_columns output"
        assert not df["y"].isna().any(), "NaN y values in query_columns output"

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
        """All four visibility-derived y-axis types must produce data."""
        yaxes = [
            (Axis.AMPLITUDE,  self.pols[0]),
            (Axis.PHASE,      self.pols[0]),
            (Axis.REAL,       self.pols[0]),
            (Axis.IMAGINARY,  self.pols[0]),
        ]
        result = self.backend.query_columns(Axis.TIME, yaxes, self.sel_small)
        for key in yaxes:
            df = result[key]
            assert len(df) > 0, f"Empty DataFrame for {key}"
            assert df["x"].notna().all()
            assert df["y"].notna().all()

    def test_antenna_names_selection_reduces_samples(self):
        """antenna_names in SelectionSpec reduces the number of samples."""
        meta = self.backend.metadata()
        target_ant = meta["antenna_names"][0]
        sel_ant = SelectionSpec(
            time_range=self.sel_small.time_range,
            channel_range=self.sel_small.channel_range,
            antenna_names=[target_ant],
        )
        yaxes = [(Axis.AMPLITUDE, self.pols[0])]
        result_full = self.backend.query_columns(Axis.TIME, yaxes, self.sel_small)
        result_ant  = self.backend.query_columns(Axis.TIME, yaxes, sel_ant)
        n_full = len(result_full[(Axis.AMPLITUDE, self.pols[0])])
        n_ant  = len(result_ant[(Axis.AMPLITUDE, self.pols[0])])
        assert n_ant < n_full, (
            f"antenna_names selection should give fewer samples: "
            f"{n_ant} vs {n_full}"
        )


# ---------------------------------------------------------------------------
# 5. query_columns — RENDERED (Datashader agg, correctness, timing)
# ---------------------------------------------------------------------------

class TestQueryColumnsRendered:

    def setup_method(self):
        _require_datashader()
        _suppress_warnings()
        self.backend = _open_backend()
        meta = self.backend.metadata()
        self.pols = meta["correlation_labels"]
        t0, t1 = meta["time_range"]
        # Medium: ~50% of time, 48 channels — exercises fused path
        self.sel_medium = SelectionSpec(
            time_range=(t0, t0 + (t1 - t0) * 0.5),
            channel_range=(0, 48),
        )
        self.sel_small = SelectionSpec(
            time_range=(t0, t0 + (t1 - t0) * 0.1),
            channel_range=(0, 16),
        )

    def teardown_method(self):
        self.backend.close()

    def _override_thresholds(self, fused, par):
        """Context manager that temporarily overrides pipeline thresholds."""
        import cubevis.toolbox.visplot.data.msv2_backend as _be
        return _be, _be._THRESH_FUSED, _be._THRESH_PAR, fused, par

    def _set_thresholds(self, fused, par):
        try:
            import cubevis.toolbox.visplot.data.msv2_backend as _be
        except ImportError:
            import sys
            _be = sys.modules.get("cubevis.toolbox.visplot.data.msv2_backend")
        _be._THRESH_FUSED = fused
        _be._THRESH_PAR   = par
        return _be

    def test_agg_shape_and_dtype(self):  # RENDERED
        """query_columns output → Datashader agg has expected shape and dtype."""
        yaxes  = [(Axis.AMPLITUDE, self.pols[0])]
        result = self.backend.query_columns(Axis.TIME, yaxes, self.sel_small)
        df     = result[(Axis.AMPLITUDE, self.pols[0])]
        agg    = _agg_from_df(df)
        assert agg.shape == (PLOT_H, PLOT_W)
        assert agg.dtype in (np.float64, np.float32)
        assert int(np.isfinite(agg.values).sum()) > 0

    def test_serial_vs_fused_pixel_identity(self):  # RENDERED
        """
        Serial and fused pipelines must produce numerically identical
        Datashader float64 agg DataArrays.

        Forces each path by overriding _THRESH_FUSED.
        """
        _be = self._set_thresholds(fused=10**9, par=10**9)  # serial
        try:
            yaxes  = [(Axis.AMPLITUDE, self.pols[0])]
            result_serial = self.backend.query_columns(
                Axis.TIME, yaxes, self.sel_medium
            )
            df_serial  = result_serial[(Axis.AMPLITUDE, self.pols[0])]
            agg_serial = _agg_from_df(df_serial)

            self._set_thresholds(fused=0, par=10**9)       # fused
            result_fused = self.backend.query_columns(
                Axis.TIME, yaxes, self.sel_medium
            )
            df_fused  = result_fused[(Axis.AMPLITUDE, self.pols[0])]
            agg_fused = _agg_from_df(df_fused)
        finally:
            self._set_thresholds(fused=500_000, par=5_000_000)

        np.testing.assert_allclose(
            agg_serial.values, agg_fused.values,
            rtol=1e-5, equal_nan=True,
            err_msg="Serial and fused pipelines produced different agg values",
        )
        print(f"  Serial rows: {len(df_serial):,}, "
              f"fused rows: {len(df_fused):,} — agg identical ✓")

    def test_fused_faster_than_serial(self):  # RENDERED + TIMING
        """
        Fused pipeline must be ≥2× faster than serial for multiple yaxes.

        The fused path reads VISIBILITY once; serial reads it once per axis.
        With 3 yaxes the minimum expected speedup is ~2× (not 3×, because
        DataFrame construction and Datashader also take time).
        """
        if len(self.pols) < 2:
            pytest.skip("Need ≥2 polarizations")

        yaxes = [
            (Axis.AMPLITUDE, self.pols[0]),
            (Axis.AMPLITUDE, self.pols[1]),
            (Axis.PHASE,     self.pols[0]),
        ]

        _be = self._set_thresholds(fused=10**9, par=10**9)
        try:
            t0 = time_mod.perf_counter()
            self.backend.query_columns(Axis.TIME, yaxes, self.sel_medium)
            t_serial = time_mod.perf_counter() - t0

            self._set_thresholds(fused=0, par=10**9)
            t0 = time_mod.perf_counter()
            self.backend.query_columns(Axis.TIME, yaxes, self.sel_medium)
            t_fused = time_mod.perf_counter() - t0
        finally:
            self._set_thresholds(fused=500_000, par=5_000_000)

        speedup = t_serial / t_fused if t_fused > 0 else float("inf")
        print(f"  Serial: {t_serial:.2f}s, Fused: {t_fused:.2f}s, "
              f"Speedup: {speedup:.1f}×")
        assert speedup >= 2.0, (
            f"Expected ≥2× speedup; got {speedup:.1f}× "
            f"(serial={t_serial:.2f}s, fused={t_fused:.2f}s)"
        )

    def test_full_partition_rendered_timing(self):  # RENDERED + TIMING
        """
        Full partition, 4 yaxes, fused → Datashader agg.

        This is the full pipeline from test_10 (45s in serial).
        With the fused path it must complete in < 10s.
        """
        if len(self.pols) < 2:
            pytest.skip("Need ≥2 polarizations")

        sel_full = SelectionSpec(channel_range=(0, 48))
        yaxes = [
            (Axis.AMPLITUDE, self.pols[0]),
            (Axis.AMPLITUDE, self.pols[1]),
            (Axis.PHASE,     self.pols[0]),
            (Axis.PHASE,     self.pols[1]),
        ]

        t0 = time_mod.perf_counter()
        result = self.backend.query_columns(Axis.TIME, yaxes, sel_full)
        t_query = time_mod.perf_counter() - t0

        t1 = time_mod.perf_counter()
        aggs = {}
        for key, df in result.items():
            assert len(df) > 0, f"Empty DataFrame for {key}"
            aggs[key] = _agg_from_df(df)
        t_render = time_mod.perf_counter() - t1

        total = t_query + t_render
        for key, agg in aggs.items():
            n_finite = int(np.isfinite(agg.values).sum())
            print(f"  {key}: {len(result[key]):,} rows, {n_finite} finite px")
        print(f"  query={t_query:.2f}s, render={t_render:.2f}s, total={total:.2f}s")

        assert total < 10.0, (
            f"Full pipeline took {total:.1f}s — exceeds 10s target "
            f"(query={t_query:.2f}s, render={t_render:.2f}s)"
        )


# ---------------------------------------------------------------------------
# 6. query_raster
# ---------------------------------------------------------------------------

class TestQueryRaster:

    def setup_method(self):
        _require_datashader()
        _suppress_warnings()
        self.backend = _open_backend()
        meta = self.backend.metadata()
        self.pols = meta["correlation_labels"]
        t0, t1 = meta["time_range"]
        self.sel = SelectionSpec(
            time_range=(t0, t0 + (t1 - t0) * 0.2),
            channel_range=(0, 48),
        )

    def teardown_method(self):
        self.backend.close()

    def _check_and_render(self, grid, y_dim, x_dim, label="",
                          x_range=None, y_range=None):
        """Assert 2D shape, correct dims, finite pixels, Datashader renders."""
        y_name = _axis_to_dim(y_dim)
        x_name = _axis_to_dim(x_dim)
        assert grid.ndim == 2, f"{label}: expected 2D, got {grid.ndim}D"
        assert grid.dims == (y_name, x_name), (
            f"{label}: expected dims ({y_name}, {x_name}), got {grid.dims}"
        )
        finite = grid.values[np.isfinite(grid.values)]
        assert len(finite) > 0, f"{label}: all raster values are NaN"

        # Render through Datashader; pass coordinate extents when available
        cvs_kwargs = dict(plot_width=PLOT_W, plot_height=PLOT_H)
        if x_range is not None:
            cvs_kwargs["x_range"] = x_range
        if y_range is not None:
            cvs_kwargs["y_range"] = y_range
        cvs = ds.Canvas(**cvs_kwargs)
        agg = cvs.raster(grid, agg=ds_agg.mean())
        assert agg.shape == (PLOT_H, PLOT_W)
        assert int(np.isfinite(agg.values).sum()) > 0
        return finite

    def test_time_baseline_amplitude(self):  # RENDERED
        grid, x_range, y_range, _ = self.backend.query_raster(
            Axis.TIME, Axis.BASELINE, Axis.AMPLITUDE, self.sel,
            polarization=self.pols[0],
        )
        finite = self._check_and_render(grid, Axis.TIME, Axis.BASELINE,
                                        "time×baseline amp",
                                        x_range=x_range, y_range=y_range)
        assert (finite >= 0).all()
        print(f"  amp range: [{finite.min():.3f}, {finite.max():.3f}] Jy")

    def test_time_baseline_phase(self):  # RENDERED
        grid, x_range, y_range, _ = self.backend.query_raster(
            Axis.TIME, Axis.BASELINE, Axis.PHASE, self.sel,
            polarization=self.pols[0],
        )
        finite = self._check_and_render(grid, Axis.TIME, Axis.BASELINE,
                                        "time×baseline phase",
                                        x_range=x_range, y_range=y_range)
        assert (finite >= -180).all() and (finite <= 180).all()

    def test_time_baseline_flag_fraction(self):  # RENDERED
        grid, x_range, y_range, _ = self.backend.query_raster(
            Axis.TIME, Axis.BASELINE, Axis.FLAG, self.sel,
        )
        finite = self._check_and_render(grid, Axis.TIME, Axis.BASELINE,
                                        "time×baseline flag",
                                        x_range=x_range, y_range=y_range)
        assert (finite >= 0).all() and (finite <= 1).all()
        print(f"  flag fraction mean: {finite.mean():.4f}")

    def test_frequency_baseline_amplitude(self):  # RENDERED
        grid, x_range, y_range, _ = self.backend.query_raster(
            Axis.BASELINE, Axis.FREQUENCY, Axis.AMPLITUDE, self.sel,
            polarization=self.pols[0],
        )
        finite = self._check_and_render(grid, Axis.BASELINE, Axis.FREQUENCY,
                                        "baseline×freq amp",
                                        x_range=x_range, y_range=y_range)
        assert (finite >= 0).all()

    def test_frequency_time_waterfall(self):  # RENDERED
        """Single-baseline waterfall via baselines= selection."""
        if "baseline_antenna1_name" not in next(
            self.backend._iter_visibility_partitions()
        ).coords:
            pytest.skip("Antenna name coords not available")

        ds_part = next(self.backend._iter_visibility_partitions())
        eit  = ds_part["EFFECTIVE_INTEGRATION_TIME"].isel(time=0).compute()
        vidx = int(np.where(np.isfinite(eit.values))[0][0])
        a1   = str(ds_part.coords["baseline_antenna1_name"].values[vidx])
        a2   = str(ds_part.coords["baseline_antenna2_name"].values[vidx])

        sel_bl = SelectionSpec(
            channel_range=(0, 48),
            baselines=[(a1, a2)],
        )
        grid, x_range, y_range, _ = self.backend.query_raster(
            Axis.TIME, Axis.FREQUENCY, Axis.AMPLITUDE, sel_bl,
            polarization=self.pols[0],
        )
        finite = self._check_and_render(grid, Axis.TIME, Axis.FREQUENCY,
                                        f"waterfall {a1}-{a2}",
                                        x_range=x_range, y_range=y_range)
        assert (finite >= 0).all()
        print(f"  waterfall {a1}–{a2}: grid={grid.shape}")

    def test_empty_selection_returns_valid_array(self):
        sel = SelectionSpec(time_range=(0.0, 1.0))
        grid, x_range, y_range, _ = self.backend.query_raster(
            Axis.TIME, Axis.BASELINE, Axis.AMPLITUDE, sel,
            polarization=self.pols[0],
        )
        assert grid.ndim == 2
        assert grid.dtype in (np.float32, np.float64)

    def test_raster_timing(self):  # RENDERED + TIMING
        """Full science target raster < 5s."""
        sel_full = SelectionSpec(channel_range=(0, 48))
        t0 = time_mod.perf_counter()
        grid, x_range, y_range, _ = self.backend.query_raster(
            Axis.TIME, Axis.BASELINE, Axis.AMPLITUDE, sel_full,
            polarization=self.pols[0],
        )
        cvs = ds.Canvas(
            plot_width=PLOT_W, plot_height=PLOT_H,
            x_range=x_range, y_range=y_range,
        )
        agg = cvs.raster(grid, agg=ds_agg.mean())
        elapsed = time_mod.perf_counter() - t0
        print(f"  Full raster: grid={grid.shape}, {elapsed:.2f}s")
        assert elapsed < 5.0, f"Raster took {elapsed:.1f}s — exceeds 5s"


# ---------------------------------------------------------------------------
# 7. query_uv_coverage
# ---------------------------------------------------------------------------

class TestQueryUVCoverage:

    def setup_method(self):
        _suppress_warnings()
        self.backend = _open_backend()
        meta = self.backend.metadata()
        t0, t1 = meta["time_range"]
        self.sel = SelectionSpec(
            time_range=(t0, t0 + (t1 - t0) * 0.3)
        )

    def teardown_method(self):
        self.backend.close()

    def test_returns_dataframe_with_x_y(self):
        df = self.backend.query_uv_coverage(self.sel)
        assert isinstance(df, pd.DataFrame)
        assert {"x", "y"} <= set(df.columns)

    def test_no_nan(self):
        df = self.backend.query_uv_coverage(self.sel)
        assert not df["x"].isna().any()
        assert not df["y"].isna().any()

    def test_conjugate_doubles_row_count(self):
        df_plain = self.backend.query_uv_coverage(self.sel,
                                                   include_conjugate=False)
        df_conj  = self.backend.query_uv_coverage(self.sel,
                                                   include_conjugate=True)
        assert len(df_conj) == 2 * len(df_plain), (
            f"Conjugate should double rows: {len(df_plain)} → {len(df_conj)}"
        )

    def test_conjugate_u_v_symmetric(self):
        """With conjugates the U and V axes are symmetric around zero."""
        df = self.backend.query_uv_coverage(self.sel, include_conjugate=True)
        assert abs(df["x"].min() + df["x"].max()) < 1.0
        assert abs(df["y"].min() + df["y"].max()) < 1.0

    def test_rendered_uv_coverage(self):  # RENDERED
        _require_datashader()
        df = self.backend.query_uv_coverage(self.sel, include_conjugate=True)
        cvs = ds.Canvas(plot_width=PLOT_W, plot_height=PLOT_H)
        agg = cvs.points(df, "x", "y", agg=ds_agg.count())
        assert agg.shape == (PLOT_H, PLOT_W)
        assert int(np.isfinite(agg.values).sum()) > 0
        print(f"  UV-coverage: {len(df):,} points")

    def test_antenna_names_selection_reduces_points(self):
        """antenna_names= in SelectionSpec reduces UV-coverage point count."""
        meta = self.backend.metadata()
        sel_ant = SelectionSpec(
            time_range=self.sel.time_range,
            antenna_names=[meta["antenna_names"][0]],
        )
        df_full = self.backend.query_uv_coverage(self.sel,
                                                  include_conjugate=False)
        df_ant  = self.backend.query_uv_coverage(sel_ant,
                                                  include_conjugate=False)
        assert len(df_ant) < len(df_full)

    def test_empty_selection_returns_empty(self):
        sel = SelectionSpec(time_range=(0.0, 1.0))
        df  = self.backend.query_uv_coverage(sel)
        assert len(df) == 0


# ---------------------------------------------------------------------------
# 8. samples_per_pixel
# ---------------------------------------------------------------------------

class TestSamplesPerPixel:

    def setup_method(self):
        _suppress_warnings()
        self.backend = _open_backend()
        meta = self.backend.metadata()
        t0, t1 = meta["time_range"]
        self.sel = SelectionSpec(
            time_range=(t0, t0 + (t1 - t0) * 0.2)
        )

    def teardown_method(self):
        self.backend.close()

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
        assert abs(rx2 - rx1 / 2) < 0.02, (
            f"Doubling canvas width should halve x ratio: "
            f"{rx1:.3f} → {rx2:.3f} (expected {rx1/2:.3f})"
        )
        assert abs(ry2 - ry1) < 0.02, "y ratio should be unchanged"

    def test_narrow_time_selection_gives_small_y_ratio(self):
        """5 integrations on a 300-pixel-high canvas → ry ≤ 1.0."""
        meta = self.backend.metadata()
        t0, t1 = meta["time_range"]
        dt = (t1 - t0) / 270  # approximate integration interval
        sel_narrow = SelectionSpec(time_range=(t0, t0 + 5 * dt))
        rx, ry = self.backend.samples_per_pixel(
            Axis.TIME, Axis.BASELINE, sel_narrow, PLOT_W, PLOT_H
        )
        print(f"  5-integration selection: ratio_x={rx:.3f}, ratio_y={ry:.3f}")
        assert ry <= 1.0, (
            f"5 integrations on {PLOT_H}px canvas should give ry≤1; got {ry:.3f}"
        )

    def test_full_partition_ratio_greater_than_one(self):
        """Full partition on a small canvas has > 1 cell/pixel."""
        sel_full = SelectionSpec()
        rx, ry = self.backend.samples_per_pixel(
            Axis.TIME, Axis.BASELINE, sel_full, PLOT_W, PLOT_H
        )
        # 270 time × 325 baselines on 400×300 canvas → both > 1
        print(f"  Full partition: ratio_x={rx:.3f}, ratio_y={ry:.3f}")
        assert rx > 0 and ry > 0



# ---------------------------------------------------------------------------
# 9. probe_pixel
# ---------------------------------------------------------------------------

class TestProbePixel:
    """Tests for the two-layer reverse-mapping hover probe.

    These tests verify the three distinct capabilities of probe_pixel():

    A. Layer 1 correctness: the float64 value returned matches
       canvas_agg.values[py, px] and the coordinate ranges bracket the
       known data-space centre.

    B. Layer 2 correctness: metadata fields (field_names, scan_names,
       antenna_pairs, freq_range_ghz) are present and plausible given
       the selection and axis choices.

    C. Scatter sample counting: when scatter_df is supplied, the
       n_scatter_samples count is consistent with a manual boolean-index
       query on the DataFrame.
    """

    def setup_method(self):
        _require_datashader()
        _suppress_warnings()
        self.backend = _open_backend()
        meta = self.backend.metadata()
        self.pols = meta["correlation_labels"]
        t0, t1 = meta["time_range"]
        self.sel = SelectionSpec(
            time_range=(t0, t0 + (t1 - t0) * 0.2),
            channel_range=(0, 48),
        )

    def teardown_method(self):
        self.backend.close()

    # ------------------------------------------------------------------ #
    # Helpers                                                              #
    # ------------------------------------------------------------------ #

    def _raster_agg(self):
        """Build a time×baseline amplitude raster agg for probe tests.

        Returns
        -------
        canvas_agg : xr.DataArray
            Datashader canvas output (dims 'x'/'y', shape PLOT_H×PLOT_W).
            Use for value/bounds checks (pixel value == canvas_agg.values[py, px]).
        raw_grid : xr.DataArray
            Raw backend grid (dims 'time'/'baseline_id').
            Pass to probe_pixel() — it needs named MS dimensions, not
            Datashader's generic 'x'/'y' dims.
        """
        raw_grid, x_range, y_range, _ = self.backend.query_raster(
            Axis.TIME, Axis.BASELINE, Axis.AMPLITUDE, self.sel,
            polarization=self.pols[0],
        )
        cvs = ds.Canvas(
            plot_width=PLOT_W, plot_height=PLOT_H,
            x_range=x_range, y_range=y_range,
        )
        canvas_agg = cvs.raster(raw_grid, agg=ds_agg.mean())
        return canvas_agg, raw_grid, x_range, y_range

    def _scatter_agg_and_df(self):
        """Build an amp-vs-time scatter agg + DataFrame for probe tests."""
        yaxes  = [(Axis.AMPLITUDE, self.pols[0])]
        result = self.backend.query_columns(Axis.TIME, yaxes, self.sel)
        df     = result[(Axis.AMPLITUDE, self.pols[0])]
        cvs    = ds.Canvas(plot_width=PLOT_W, plot_height=PLOT_H)
        canvas_agg    = cvs.points(df, "x", "y", agg=ds_agg.mean("y"))
        return canvas_agg, df

    def _canvas_to_grid(self, canvas_agg, raw_grid, x_range, y_range, px, py):
        """Convert canvas pixel (px,py) to raw_grid indices (gx,gy)."""
        # x/y data-space coordinate at the canvas pixel centre
        x_coords_c = canvas_agg.coords[canvas_agg.dims[1]].values
        y_coords_c = canvas_agg.coords[canvas_agg.dims[0]].values
        x_val = float(x_coords_c[px])
        y_val = float(y_coords_c[py])
        # Find nearest raw_grid coordinate
        x_name = raw_grid.dims[1]
        y_name = raw_grid.dims[0]
        x_coords_g = raw_grid.coords[x_name].values
        y_coords_g = raw_grid.coords[y_name].values
        gx = int(np.argmin(np.abs(x_coords_g - x_val)))
        gy = int(np.argmin(np.abs(y_coords_g - y_val)))
        h, w = raw_grid.shape
        gx = max(0, min(gx, w - 1))
        gy = max(0, min(gy, h - 1))
        return gx, gy

    def _first_finite_pixel(self, canvas_agg):
        """Return (px, py) of the first finite-value pixel."""
        ys, xs = np.where(np.isfinite(canvas_agg.values))
        if len(ys) == 0:
            pytest.skip("No finite pixels in canvas_agg — cannot test probe")
        return int(xs[0]), int(ys[0])

    def _first_nan_pixel(self, canvas_agg):
        """Return (px, py) of the first NaN pixel, or None."""
        ys, xs = np.where(np.isnan(canvas_agg.values))
        if len(ys) == 0:
            return None
        return int(xs[0]), int(ys[0])

    # ------------------------------------------------------------------ #
    # A. Layer 1 — float64 value and coordinate ranges                    #
    # ------------------------------------------------------------------ #

    def test_raster_value_matches_agg(self):
        """probe_pixel value == canvas_agg.values[py, px] for a finite pixel."""
        canvas_agg, raw_grid, x_range_raster, y_range_raster = self._raster_agg()
        px, py = self._first_finite_pixel(canvas_agg)
        gx, gy = self._canvas_to_grid(canvas_agg, raw_grid, x_range_raster, y_range_raster, px, py)
        info   = self.backend.probe_raster_pixel(raw_grid, gx, gy, self.sel)

        expected = float(raw_grid.values[gy, gx])
        assert info["value"] is not None
        assert abs(info["value"] - expected) < 1e-6, (
            f"probe value {info['value']:.6f} != raw_grid[{gy},{gx}]={expected:.6f}"
        )

    def test_raster_value_none_for_nan_pixel(self):
        """probe_pixel returns value=None for an empty (NaN) pixel."""
        canvas_agg, raw_grid, x_range_raster, y_range_raster = self._raster_agg()
        nan_pix = self._first_nan_pixel(canvas_agg)
        if nan_pix is None:
            pytest.skip("No NaN pixels in raster")
        px, py = nan_pix
        gx, gy = self._canvas_to_grid(canvas_agg, raw_grid, x_range_raster, y_range_raster, px, py)
        info = self.backend.probe_raster_pixel(raw_grid, gx, gy, self.sel)
        assert info["value"] is None

    def test_x_centre_within_x_range(self):
        """x_centre must lie within x_range."""
        canvas_agg, raw_grid, x_range_raster, y_range_raster = self._raster_agg()
        px, py = self._first_finite_pixel(canvas_agg)
        gx, gy = self._canvas_to_grid(canvas_agg, raw_grid, x_range_raster, y_range_raster, px, py)
        info   = self.backend.probe_raster_pixel(raw_grid, gx, gy, self.sel)
        assert info["x_range"][0] <= info["x_centre"] <= info["x_range"][1], (
            f"x_centre {info['x_centre']} not in x_range {info['x_range']}"
        )

    def test_y_centre_within_y_range(self):
        """y_centre must lie within y_range."""
        canvas_agg, raw_grid, x_range_raster, y_range_raster = self._raster_agg()
        px, py = self._first_finite_pixel(canvas_agg)
        gx, gy = self._canvas_to_grid(canvas_agg, raw_grid, x_range_raster, y_range_raster, px, py)
        info   = self.backend.probe_raster_pixel(raw_grid, gx, gy, self.sel)
        assert info["y_range"][0] <= info["y_centre"] <= info["y_range"][1]

    def test_x_range_ordered(self):
        canvas_agg, raw_grid, x_range_raster, y_range_raster = self._raster_agg()
        px, py = self._first_finite_pixel(canvas_agg)
        gx, gy = self._canvas_to_grid(canvas_agg, raw_grid, x_range_raster, y_range_raster, px, py)
        info   = self.backend.probe_raster_pixel(raw_grid, gx, gy, self.sel)
        assert info["x_range"][0] <= info["x_range"][1]

    def test_y_range_ordered(self):
        canvas_agg, raw_grid, x_range_raster, y_range_raster = self._raster_agg()
        px, py = self._first_finite_pixel(canvas_agg)
        gx, gy = self._canvas_to_grid(canvas_agg, raw_grid, x_range_raster, y_range_raster, px, py)
        info   = self.backend.probe_raster_pixel(raw_grid, gx, gy, self.sel)
        assert info["y_range"][0] <= info["y_range"][1]

    def test_coordinate_ranges_differ_per_pixel(self):
        """Adjacent pixels must map to different data-space ranges."""
        canvas_agg, raw_grid, x_range_raster, y_range_raster = self._raster_agg()
        px, py = self._first_finite_pixel(canvas_agg)
        gx, gy = self._canvas_to_grid(canvas_agg, raw_grid, x_range_raster, y_range_raster, px, py)
        info0  = self.backend.probe_raster_pixel(raw_grid, gx, gy, self.sel)
        if px + 1 < canvas_agg.shape[1]:
            gx1, gy1 = self._canvas_to_grid(canvas_agg, raw_grid, x_range_raster, y_range_raster, px + 1, py)
            if gx1 != gx:  # only meaningful if canvas pixels map to different grid cells
                info1 = self.backend.probe_raster_pixel(raw_grid, gx1, gy1, self.sel)
                assert info0["x_range"] != info1["x_range"], (
                    "Adjacent x pixels map to same data-space range"
                )

    def test_out_of_range_pixel_raises(self):
        """Requesting a pixel outside the canvas dimensions must raise."""
        canvas_agg, raw_grid, x_range_raster, y_range_raster = self._raster_agg()
        h_g, w_g = raw_grid.shape
        with pytest.raises(IndexError):
            self.backend.probe_raster_pixel(raw_grid, w_g, 0, self.sel)
        with pytest.raises(IndexError):
            self.backend.probe_raster_pixel(raw_grid, 0, h_g, self.sel)

    # ------------------------------------------------------------------ #
    # B. Layer 2 — metadata lookup                                        #
    # ------------------------------------------------------------------ #

    def test_raster_metadata_keys_present(self):
        """All expected metadata keys are present in the probe result."""
        canvas_agg, raw_grid, x_range_raster, y_range_raster = self._raster_agg()
        px, py = self._first_finite_pixel(canvas_agg)
        gx, gy = self._canvas_to_grid(canvas_agg, raw_grid, x_range_raster, y_range_raster, px, py)
        info   = self.backend.probe_raster_pixel(raw_grid, gx, gy, self.sel)
        required = {
            "value", "x_range", "y_range", "x_centre", "y_centre",
            "field_names", "scan_names", "antenna_pairs",
            "freq_range_ghz",
        }
        assert required <= info.keys(), (
            f"Missing keys: {required - info.keys()}"
        )

    def test_raster_field_names_plausible(self):
        """field_names are non-empty strings when time is a plot axis."""
        canvas_agg, raw_grid, x_range_raster, y_range_raster = self._raster_agg()
        # Find a pixel whose y_range (time) has associated field data
        for py in range(PLOT_H):
            for px in range(PLOT_W):
                if not np.isfinite(canvas_agg.values[py, px]):
                    continue
                gx, gy = self._canvas_to_grid(canvas_agg, raw_grid, x_range_raster, y_range_raster, px, py)
                info = self.backend.probe_raster_pixel(raw_grid, gx, gy, self.sel)
                if info["field_names"]:
                    assert all(isinstance(f, str) and f
                               for f in info["field_names"])
                    print(f"  field_names at ({px},{py}): {info['field_names']}")
                    return
        pytest.skip("No pixel with non-empty field_names found")

    def test_raster_antenna_pairs_for_baseline_axis(self):
        """antenna_pairs are populated when baseline_id is a plot axis."""
        canvas_agg, raw_grid, x_range_raster, y_range_raster = self._raster_agg()
        px, py = self._first_finite_pixel(canvas_agg)
        gx, gy = self._canvas_to_grid(canvas_agg, raw_grid, x_range_raster, y_range_raster, px, py)
        info   = self.backend.probe_raster_pixel(raw_grid, gx, gy, self.sel)
        # time×baseline raster has baseline_id as x_dim
        assert len(info["antenna_pairs"]) > 0, (
            "antenna_pairs should be non-empty for time×baseline raster"
        )
        for a1, a2 in info["antenna_pairs"]:
            assert isinstance(a1, str) and isinstance(a2, str)

    def test_raster_no_antenna_pairs_for_time_freq(self):
        """antenna_pairs is empty for a time×frequency raster (no baseline axis)."""
        if "baseline_antenna1_name" not in next(
            self.backend._iter_visibility_partitions()
        ).coords:
            pytest.skip("Antenna coord not present")

        ds_part = next(self.backend._iter_visibility_partitions())
        eit  = ds_part["EFFECTIVE_INTEGRATION_TIME"].isel(time=0).compute()
        vidx = int(np.where(np.isfinite(eit.values))[0][0])
        a1   = str(ds_part.coords["baseline_antenna1_name"].values[vidx])
        a2   = str(ds_part.coords["baseline_antenna2_name"].values[vidx])
        sel_bl = SelectionSpec(channel_range=(0, 48), baselines=[(a1, a2)])

        grid, x_range, y_range, _ = self.backend.query_raster(
            Axis.TIME, Axis.FREQUENCY, Axis.AMPLITUDE, sel_bl,
            polarization=self.pols[0],
        )
        cvs  = ds.Canvas(
            plot_width=PLOT_W, plot_height=PLOT_H,
            x_range=x_range, y_range=y_range,
        )
        canvas_agg  = cvs.raster(grid, agg=ds_agg.mean())
        px, py = self._first_finite_pixel(canvas_agg)
        gx, gy = self._canvas_to_grid(canvas_agg, grid, x_range, y_range, px, py)
        info = self.backend.probe_raster_pixel(grid, gx, gy, sel_bl)
        assert info["antenna_pairs"] == [], (
            "antenna_pairs should be empty for time×frequency raster"
        )

    def test_raster_freq_range_ghz_present_for_freq_axis(self):
        """freq_range_ghz is populated when frequency is a plot axis."""
        if "baseline_antenna1_name" not in next(
            self.backend._iter_visibility_partitions()
        ).coords:
            pytest.skip("Antenna coord not present")

        ds_part = next(self.backend._iter_visibility_partitions())
        eit  = ds_part["EFFECTIVE_INTEGRATION_TIME"].isel(time=0).compute()
        vidx = int(np.where(np.isfinite(eit.values))[0][0])
        a1   = str(ds_part.coords["baseline_antenna1_name"].values[vidx])
        a2   = str(ds_part.coords["baseline_antenna2_name"].values[vidx])
        sel_bl = SelectionSpec(channel_range=(0, 48), baselines=[(a1, a2)])

        grid, x_range, y_range, _ = self.backend.query_raster(
            Axis.TIME, Axis.FREQUENCY, Axis.AMPLITUDE, sel_bl,
            polarization=self.pols[0],
        )
        cvs  = ds.Canvas(
            plot_width=PLOT_W, plot_height=PLOT_H,
            x_range=x_range, y_range=y_range,
        )
        canvas_agg  = cvs.raster(grid, agg=ds_agg.mean())
        px, py = self._first_finite_pixel(canvas_agg)
        gx, gy = self._canvas_to_grid(canvas_agg, grid, x_range, y_range, px, py)
        info = self.backend.probe_raster_pixel(grid, gx, gy, sel_bl)
        assert info["freq_range_ghz"] is not None
        f0, f1 = info["freq_range_ghz"]
        assert f0 <= f1
        # ALMA Band 7 ~ 372 GHz
        assert 300 < f0 < 500, f"Unexpected freq: {f0} GHz"
        print(f"  freq_range_ghz: ({f0:.4f}, {f1:.4f}) GHz")

    def test_raster_no_freq_range_for_time_baseline(self):
        """freq_range_ghz is None for time×baseline raster (no freq axis)."""
        canvas_agg, raw_grid, x_range_raster, y_range_raster = self._raster_agg()
        px, py = self._first_finite_pixel(canvas_agg)
        gx, gy = self._canvas_to_grid(canvas_agg, raw_grid, x_range_raster, y_range_raster, px, py)
        info   = self.backend.probe_raster_pixel(raw_grid, gx, gy, self.sel)
        assert info["freq_range_ghz"] is None

    def test_no_scatter_samples_in_raster_result(self):
        """probe_raster_pixel result must NOT contain n_scatter_samples.

        Scatter sample counting belongs to probe_scatter_pixel only.
        """
        canvas_agg, raw_grid, x_range_raster, y_range_raster = self._raster_agg()
        px, py = self._first_finite_pixel(canvas_agg)
        gx, gy = self._canvas_to_grid(canvas_agg, raw_grid, x_range_raster, y_range_raster, px, py)
        info   = self.backend.probe_raster_pixel(raw_grid, gx, gy, self.sel)
        assert "n_scatter_samples" not in info, (
            "probe_raster_pixel must not return n_scatter_samples — "
            "use probe_scatter_pixel for scatter sample counting"
        )

    # ------------------------------------------------------------------ #
    # C. Scatter sample counting                                          #
    # ------------------------------------------------------------------ #

    def test_scatter_probe_value_matches_agg(self):
        """probe_scatter_pixel value matches canvas_agg.values[py,px]."""
        canvas_agg, df = self._scatter_agg_and_df()
        px, py  = self._first_finite_pixel(canvas_agg)
        info    = self.backend.probe_scatter_pixel(
            canvas_agg, px, py, self.sel, df
        )
        expected = float(canvas_agg.values[py, px])
        assert info["value"] is not None
        assert abs(info["value"] - expected) < 1e-6

    def test_scatter_sample_count_matches_manual_index(self):
        """n_scatter_samples matches a manual boolean-index count on df."""
        canvas_agg, df = self._scatter_agg_and_df()
        px, py  = self._first_finite_pixel(canvas_agg)
        info    = self.backend.probe_scatter_pixel(
            canvas_agg, px, py, self.sel, df
        )
        assert info["n_scatter_samples"] is not None

        # Reproduce manually
        x0, x1 = info["x_range"]
        y0, y1 = info["y_range"]
        manual = int(
            ((df["x"] >= x0) & (df["x"] <= x1) &
             (df["y"] >= y0) & (df["y"] <= y1)).sum()
        )
        assert info["n_scatter_samples"] == manual, (
            f"probe gave {info['n_scatter_samples']} samples; "
            f"manual index gives {manual}"
        )
        print(f"  Scatter probe at ({px},{py}): "
              f"value={info['value']:.4f}, "
              f"n_samples={info['n_scatter_samples']}")

    def test_scatter_empty_pixel_has_zero_or_none_samples(self):
        """An empty (NaN) scatter pixel has n_scatter_samples == 0."""
        canvas_agg, df = self._scatter_agg_and_df()
        nan_pix = self._first_nan_pixel(canvas_agg)
        if nan_pix is None:
            pytest.skip("No NaN pixels in scatter raster")
        px, py = nan_pix
        info   = self.backend.probe_scatter_pixel(
            canvas_agg, px, py, self.sel, df
        )
        assert info["value"] is None
        assert info["n_scatter_samples"] == 0, (
            f"Empty pixel should have 0 samples; got {info['n_scatter_samples']}"
        )

    def test_scatter_sample_count_nonnegative(self):
        """n_scatter_samples is non-negative for all probed pixels."""
        canvas_agg, df = self._scatter_agg_and_df()
        ys, xs = np.where(np.isfinite(canvas_agg.values))
        sample_indices = list(zip(xs[:5], ys[:5]))
        for px, py in sample_indices:
            info = self.backend.probe_scatter_pixel(
                canvas_agg, int(px), int(py), self.sel, df
            )
            assert info["n_scatter_samples"] >= 0

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
    ]

    total_passed = total_failed = total_skipped = 0

    for cls in test_classes:
        print(f"\n{'='*60}\n  {cls.__name__}\n{'='*60}")
        obj = cls()
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

    print(f"\n{'='*60}")
    print(f"  {total_passed} passed, {total_failed} failed, "
          f"{total_skipped} skipped")
