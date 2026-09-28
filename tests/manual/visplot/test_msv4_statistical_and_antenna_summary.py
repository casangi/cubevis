"""
test_msv4_statistical_and_antenna_summary.py
==============================================
MSv4Backend mirroring for two Part 6 features previously verified on
MSv2Backend only:

- "statistical" coloring mode (test_statistical_coloring.py's own
  MSv2-only coverage)
- Slice 2's per-antenna Z-Score summary (test_antenna_zscore_summary.py's
  own MSv2-only coverage)

Location in repository:
    cubevis/tests/manual/visplot/test_msv4_statistical_and_antenna_summary.py

Run:
    pytest cubevis/tests/manual/visplot/test_msv4_statistical_and_antenna_summary.py -v

Both features are wired identically to MSv2Backend at the
query_columns() level (documented there as mirroring MSv2's own
handling exactly), so this file's job is confirming that mirroring is
actually correct on MSv4Backend's own, structurally different
_query_columns_raw -- in particular through BOTH of its internal paths:
the ordinary per-partition path, and OPT-B (the fused, cross-partition
path multiple partitions crossing _THRESH_FUSED route through) -- since
Part 6 Slice 1's own MSv4 mirroring found real, OPT-B-specific bugs the
single-partition path never exercised, this file does not assume the
single-partition tests below are sufficient on their own.

All synthetic -- no real MS/PS needed for any test in this file.
"""
from __future__ import annotations

import numpy as np
import pytest
import xarray as xr

from cubevis.toolbox.visplot.axes import Axis
from cubevis.toolbox.visplot.data import msv4_backend as msv4_mod
from cubevis.toolbox.visplot.data.msv4_backend import MSv4Backend
from cubevis.toolbox.visplot.data.reader import ScatterLayerSpec
from cubevis.toolbox.visplot.selection import SelectionSpec


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _bare_backend():
    b = MSv4Backend.__new__(MSv4Backend)
    b._datatree = object()
    b._identity_categoricals = lambda *a, **k: {}
    b._partition_spw_ident = lambda ds: (None, None)
    b._antenna_lookup_table = lambda: None
    b._scan_time_index = lambda *a, **k: None
    # MSv4Backend-specific: query_columns reads this directly (used for
    # e.g. single-dish-only axis gating) -- found missing when the bare
    # instance first raised AttributeError here, not assumed up front.
    b._resolved_mode = "interferometer"
    return b


def _synthetic_partition(seed, n_time=20, n_baseline=15, n_freq=8,
                          pols=("XX", "YY"), anomaly_baseline=None,
                          anomaly_times=slice(5, 10), anomaly_offset=(6.0, 6.0)):
    rng = np.random.default_rng(seed)
    shape = (n_time, n_baseline, n_freq, len(pols))
    vis = np.zeros(shape, dtype=np.complex128)
    for b in range(n_baseline):
        vis[:, b, :, :] = (rng.normal(5.0, 0.05, (n_time, n_freq, len(pols)))
                           + 1j * rng.normal(0.0, 0.05, (n_time, n_freq, len(pols))))
    if anomaly_baseline is not None:
        vis[anomaly_times, anomaly_baseline, :, :] += complex(*anomaly_offset)
    flag = np.zeros(shape, dtype=np.uint8)
    return xr.Dataset(
        data_vars={"VISIBILITY": (("time", "baseline_id", "frequency", "polarization"), vis),
                   "FLAG": (("time", "baseline_id", "frequency", "polarization"), flag)},
        coords={"time": np.arange(n_time, dtype=np.float64),
                "baseline_id": np.arange(n_baseline),
                "frequency": np.linspace(1e9, 1.1e9, n_freq),
                "polarization": list(pols)},
    )


def _backend_with_partitions(*partitions):
    backend = _bare_backend()
    backend._iter_visibility_partitions = lambda selection: iter(partitions)
    backend._apply_selection = lambda raw_ds, selection: raw_ds
    backend._scan_lookup_for_partition = lambda raw_ds: None
    return backend


def _distinct_nonzero_colors(image: np.ndarray) -> set:
    return set(np.unique(image[image != 0]).tolist())


STAT_LAYER = ScatterLayerSpec(
    y_axis=Axis.AMPLITUDE, polarization="XX", cmap=("#222222", "#ff3333"),
    coloring="statistical", scaling="threshold", scaling_vmin=5.0,
)
ZSCORE_LAYER = ScatterLayerSpec(
    y_axis=Axis.Z_SCORE, polarization="XX", cmap=("#000000", "#ffffff"), scaling="linear",
)
CONT_LAYER_YY = ScatterLayerSpec(
    y_axis=Axis.AMPLITUDE, polarization="YY",
    cmap=tuple(f"#{i:02x}{i:02x}{i:02x}" for i in range(0, 256, 8)), scaling="linear",
)
CONT_LAYER_XX = ScatterLayerSpec(
    y_axis=Axis.AMPLITUDE, polarization="XX", cmap=("#000000", "#ffffff"), scaling="linear",
)


# ---------------------------------------------------------------------------
# 1. "statistical" coloring mode -- single-partition path
# ---------------------------------------------------------------------------

class TestStatisticalColoringMSv4SinglePartition:
    def test_colored_by_zscore_not_amplitude(self):
        ds = _synthetic_partition(seed=42, anomaly_baseline=3)
        backend = _backend_with_partitions(ds)
        result = backend.query_columns(Axis.TIME, [STAT_LAYER], SelectionSpec(),
                                       width=200, height=200)
        assert len(_distinct_nonzero_colors(result.layers[0].image)) == 2

    def test_continuous_layer_in_same_call_unaffected(self):
        ds = _synthetic_partition(seed=42, anomaly_baseline=3)
        backend = _backend_with_partitions(ds)
        result = backend.query_columns(Axis.TIME, [CONT_LAYER_YY, STAT_LAYER], SelectionSpec(),
                                       width=200, height=200)
        assert result.layers[0].peak_value < 15.0
        assert len(_distinct_nonzero_colors(result.layers[1].image)) == 2

    def test_no_row_duplication_from_the_merge(self):
        ds = _synthetic_partition(seed=42, anomaly_baseline=3)
        backend = _backend_with_partitions(ds)
        result = backend.query_columns(Axis.TIME, [STAT_LAYER], SelectionSpec(),
                                       width=200, height=200)
        plain = backend._query_columns_cached(Axis.TIME, [(Axis.AMPLITUDE, "XX")], SelectionSpec())
        assert result.layers[0].n_in_view == len(plain[(Axis.AMPLITUDE, "XX")])

    def test_continuous_only_query_fetches_nothing_extra(self):
        ds = _synthetic_partition(seed=42)
        backend = _backend_with_partitions(ds)
        reads = []
        real_raw = backend._query_columns_raw
        def spy(xaxis, yaxes, selection):
            reads.append(list(yaxes))
            return real_raw(xaxis, yaxes, selection)
        backend._query_columns_raw = spy
        backend.query_columns(Axis.TIME, [CONT_LAYER_YY], SelectionSpec(), width=200, height=200)
        assert reads == [[(Axis.AMPLITUDE, "YY")]]

    def test_reference_skipped_for_statistical_but_not_continuous(self):
        ds = _synthetic_partition(seed=42, anomaly_baseline=3)
        backend = _backend_with_partitions(ds)
        result = backend.query_columns(
            Axis.TIME, [CONT_LAYER_YY, STAT_LAYER], SelectionSpec(),
            width=200, height=200, ref_scale=2.0,
        )
        assert result.layers[0].reference is not None
        assert result.layers[1].reference is None


# ---------------------------------------------------------------------------
# 2. "statistical" coloring mode -- OPT-B (fused, cross-partition) path
# ---------------------------------------------------------------------------

class TestStatisticalColoringMSv4OptB:
    @pytest.fixture(autouse=True)
    def _force_opt_b(self, monkeypatch):
        # Forces the fused, cross-partition path to trigger with a small
        # synthetic dataset rather than needing >500k real samples.
        monkeypatch.setattr(msv4_mod, "_THRESH_FUSED", 100)

    def test_colored_by_zscore_spanning_both_partitions(self):
        """Anomaly lives only in partition 2 -- if the reference
        population correctly spans BOTH partitions (as Slice 1's own
        MSv4 OPT-B validation confirmed for the Z-Score computation
        itself), the statistical layer must still render as a clean
        binary threshold split, not silently fall back to some
        single-partition-only reference."""
        part1 = _synthetic_partition(seed=1, anomaly_baseline=None)
        part2 = _synthetic_partition(seed=2, anomaly_baseline=3)
        backend = _backend_with_partitions(part1, part2)
        result = backend.query_columns(Axis.TIME, [STAT_LAYER], SelectionSpec(),
                                       width=200, height=200)
        assert len(_distinct_nonzero_colors(result.layers[0].image)) == 2

    def test_antenna_summary_count_spans_both_partitions(self):
        part1 = _synthetic_partition(seed=1)
        part2 = _synthetic_partition(seed=2)
        backend = _backend_with_partitions(part1, part2)
        result = backend.query_columns(
            Axis.TIME, [ZSCORE_LAYER], SelectionSpec(antenna_names=["DA41"]),
            width=200, height=200,
        )
        summary = result.layers[0].antenna_summary
        assert summary is not None
        single_partition_count = 20 * 15 * 8   # n_time * n_baseline * n_freq
        assert summary.count == 2 * single_partition_count


# ---------------------------------------------------------------------------
# 3. Slice 2 antenna summary -- single-partition path
# ---------------------------------------------------------------------------

class TestAntennaSummaryMSv4SinglePartition:
    def test_z_score_direct_layer_gets_a_summary(self):
        ds = _synthetic_partition(seed=42)
        backend = _backend_with_partitions(ds)
        result = backend.query_columns(
            Axis.TIME, [ZSCORE_LAYER], SelectionSpec(antenna_names=["DA41"]),
            width=200, height=200,
        )
        summary = result.layers[0].antenna_summary
        assert summary is not None
        assert summary.antenna_name == "DA41"
        assert summary.threshold == 3.5

    def test_statistical_mode_layer_uses_its_own_threshold(self):
        ds = _synthetic_partition(seed=42)
        backend = _backend_with_partitions(ds)
        result = backend.query_columns(
            Axis.TIME, [STAT_LAYER], SelectionSpec(antenna_names=["DV01"]),
            width=200, height=200,
        )
        summary = result.layers[0].antenna_summary
        assert summary is not None
        assert summary.threshold == 5.0

    def test_continuous_layer_gets_no_summary(self):
        ds = _synthetic_partition(seed=42)
        backend = _backend_with_partitions(ds)
        result = backend.query_columns(
            Axis.TIME, [CONT_LAYER_XX], SelectionSpec(antenna_names=["DA41"]),
            width=200, height=200,
        )
        assert result.layers[0].antenna_summary is None

    def test_no_or_multiple_antennas_gives_no_summary(self):
        ds = _synthetic_partition(seed=42)
        backend = _backend_with_partitions(ds)
        result_none = backend.query_columns(Axis.TIME, [ZSCORE_LAYER], SelectionSpec(),
                                            width=200, height=200)
        assert result_none.layers[0].antenna_summary is None
        result_multi = backend.query_columns(
            Axis.TIME, [ZSCORE_LAYER], SelectionSpec(antenna_names=["DA41", "DV01"]),
            width=200, height=200,
        )
        assert result_multi.layers[0].antenna_summary is None

    def test_coexists_with_two_level_rendering_reference(self):
        ds = _synthetic_partition(seed=42)
        backend = _backend_with_partitions(ds)
        result = backend.query_columns(
            Axis.TIME, [ZSCORE_LAYER], SelectionSpec(antenna_names=["DA41"]),
            width=200, height=200, ref_scale=2.0,
        )
        rendered = result.layers[0]
        assert rendered.reference is not None
        assert rendered.antenna_summary is not None
