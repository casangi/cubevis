"""
test_antenna_zscore_summary.py
================================
Tests for Slice 2's per-antenna quantitative readout (Part 6, 2026-09;
visplot-colorize-by-axis-design.md §7.6, §7.10): sample count, median
score, and fraction over threshold -- statistics only, never a
qualitative verdict (§7.2's own explicit requirement) -- shown alongside
a Z-Score-carrying layer's existing colorbar whenever the current
selection narrows to exactly one antenna.

Location in repository:
    cubevis/tests/manual/visplot/test_antenna_zscore_summary.py

Run:
    pytest cubevis/tests/manual/visplot/test_antenna_zscore_summary.py -v

Sections
--------
1. compute_antenna_zscore_summary   the pure statistics function
2. MSv2Backend wiring                query_columns' eligibility check
                                     and attachment, end-to-end
3. colorbar_html rendering           the actual HTML text, with real
                                     ColorBand/ScalarMapping objects

Scope note: MSv4Backend mirroring is not yet done (tracked separately,
matching every other Part 6 piece's own dual-backend sequencing).
Everything in this file is synthetic -- no real MS/PS needed.
"""
from __future__ import annotations

import numpy as np
import pytest
import xarray as xr

from cubevis.toolbox.visplot import colormap_scaling as cms
from cubevis.toolbox.visplot import info_panel as ip
from cubevis.toolbox.visplot.axes import Axis
from cubevis.toolbox.visplot.data import _scatter_render as sr
from cubevis.toolbox.visplot.data.msv2_backend import MSv2Backend
from cubevis.toolbox.visplot.data.reader import AntennaZScoreSummary, ScatterLayerSpec
from cubevis.toolbox.visplot.panel_spec import ColorBand
from cubevis.toolbox.visplot.selection import SelectionSpec


# ---------------------------------------------------------------------------
# 1. compute_antenna_zscore_summary -- pure function
# ---------------------------------------------------------------------------

class TestComputeAntennaZscoreSummary:
    def test_basic_statistics(self):
        scores = np.array([1.0, 2.0, 3.0, 5.0, 10.0])
        result = sr.compute_antenna_zscore_summary(scores, "DA41", threshold=3.5)
        assert result.antenna_name == "DA41"
        assert result.count == 5
        assert result.median_score == 3.0
        assert result.fraction_over_threshold == pytest.approx(2 / 5)
        assert result.threshold == 3.5

    def test_nan_values_excluded_from_count_and_stats(self):
        scores = np.array([1.0, np.nan, 3.0, np.nan, 10.0])
        result = sr.compute_antenna_zscore_summary(scores, "DV01")
        assert result.count == 3
        assert result.median_score == 3.0

    def test_all_nan_gives_zero_count_and_none_stats(self):
        result = sr.compute_antenna_zscore_summary(np.array([np.nan, np.nan]), "DV03")
        assert result.count == 0
        assert result.median_score is None
        assert result.fraction_over_threshold is None
        assert result.threshold == sr._DEFAULT_ZSCORE_THRESHOLD

    def test_empty_array_gives_zero_count(self):
        result = sr.compute_antenna_zscore_summary(np.array([]), "DV03")
        assert result.count == 0

    def test_default_threshold_is_the_literature_value(self):
        result = sr.compute_antenna_zscore_summary(np.array([1.0, 2.0]), "DA42")
        assert result.threshold == 3.5

    def test_threshold_comparison_is_strict_greater_than(self):
        result = sr.compute_antenna_zscore_summary(
            np.array([3.5, 3.5, 4.0]), "DA43", threshold=3.5,
        )
        assert result.fraction_over_threshold == pytest.approx(1 / 3)

    def test_custom_threshold_is_used_exactly_as_given(self):
        result = sr.compute_antenna_zscore_summary(
            np.array([1.0, 6.0, 6.0]), "DA44", threshold=5.0,
        )
        assert result.threshold == 5.0
        assert result.fraction_over_threshold == pytest.approx(2 / 3)


# ---------------------------------------------------------------------------
# 2. MSv2Backend.query_columns -- end-to-end wiring
# ---------------------------------------------------------------------------

def _bare_backend():
    b = MSv2Backend.__new__(MSv2Backend)
    b._datatree = object()
    b._identity_categoricals = lambda *a, **k: {}
    b._partition_spw_ident = lambda ds: (None, None)
    b._antenna_lookup_table = lambda: None
    b._scan_time_index = lambda *a, **k: None
    return b


def _synthetic_dataset(n_time=40, n_baseline=15, n_freq=8, pols=("XX", "YY"), seed=42):
    rng = np.random.default_rng(seed)
    shape = (n_time, n_baseline, n_freq, len(pols))
    vis = np.zeros(shape, dtype=np.complex128)
    for b in range(n_baseline):
        vis[:, b, :, :] = (rng.normal(5.0, 0.05, (n_time, n_freq, len(pols)))
                           + 1j * rng.normal(0.0, 0.05, (n_time, n_freq, len(pols))))
    vis[10:15, 3, :, :] += (6.0 + 6.0j)
    flag = np.zeros(shape, dtype=np.uint8)
    return xr.Dataset(
        data_vars={"VISIBILITY": (("time", "baseline_id", "frequency", "polarization"), vis),
                   "FLAG": (("time", "baseline_id", "frequency", "polarization"), flag)},
        coords={"time": np.arange(n_time, dtype=np.float64),
                "baseline_id": np.arange(n_baseline),
                "frequency": np.linspace(1e9, 1.1e9, n_freq),
                "polarization": list(pols)},
    )


class TestQueryColumnsAntennaSummaryWiring:
    @staticmethod
    def _backend_with_dataset(ds):
        backend = _bare_backend()
        backend._iter_visibility_partitions = lambda selection: iter([ds])
        backend._apply_selection = lambda raw_ds, selection: raw_ds
        backend._scan_lookup_for_partition = lambda raw_ds: None
        return backend

    def test_z_score_direct_layer_gets_a_summary(self):
        backend = self._backend_with_dataset(_synthetic_dataset())
        layer = ScatterLayerSpec(y_axis=Axis.Z_SCORE, polarization="XX",
                                 cmap=("#000000", "#ffffff"), scaling="linear")
        result = backend.query_columns(
            Axis.TIME, [layer], SelectionSpec(antenna_names=["DA41"]),
            width=200, height=200,
        )
        summary = result.layers[0].antenna_summary
        assert summary is not None
        assert summary.antenna_name == "DA41"
        assert summary.count > 0
        assert summary.threshold == 3.5

    def test_statistical_mode_layer_gets_a_summary_with_its_own_threshold(self):
        backend = self._backend_with_dataset(_synthetic_dataset())
        layer = ScatterLayerSpec(
            y_axis=Axis.AMPLITUDE, polarization="XX", cmap=("#222222", "#ff3333"),
            coloring="statistical", scaling="threshold", scaling_vmin=5.0,
        )
        result = backend.query_columns(
            Axis.TIME, [layer], SelectionSpec(antenna_names=["DV01"]),
            width=200, height=200,
        )
        summary = result.layers[0].antenna_summary
        assert summary is not None
        assert summary.antenna_name == "DV01"
        assert summary.threshold == 5.0   # scaling_vmin takes precedence

    def test_continuous_layer_gets_no_summary(self):
        backend = self._backend_with_dataset(_synthetic_dataset())
        layer = ScatterLayerSpec(y_axis=Axis.AMPLITUDE, polarization="XX",
                                 cmap=("#000000", "#ffffff"), scaling="linear")
        result = backend.query_columns(
            Axis.TIME, [layer], SelectionSpec(antenna_names=["DA41"]),
            width=200, height=200,
        )
        assert result.layers[0].antenna_summary is None

    def test_no_antenna_selected_gives_no_summary(self):
        backend = self._backend_with_dataset(_synthetic_dataset())
        layer = ScatterLayerSpec(y_axis=Axis.Z_SCORE, polarization="XX",
                                 cmap=("#000000", "#ffffff"), scaling="linear")
        result = backend.query_columns(
            Axis.TIME, [layer], SelectionSpec(), width=200, height=200,
        )
        assert result.layers[0].antenna_summary is None

    def test_multiple_antennas_selected_gives_no_summary(self):
        backend = self._backend_with_dataset(_synthetic_dataset())
        layer = ScatterLayerSpec(y_axis=Axis.Z_SCORE, polarization="XX",
                                 cmap=("#000000", "#ffffff"), scaling="linear")
        result = backend.query_columns(
            Axis.TIME, [layer], SelectionSpec(antenna_names=["DA41", "DV01"]),
            width=200, height=200,
        )
        assert result.layers[0].antenna_summary is None

    def test_mixed_layers_only_eligible_one_gets_a_summary(self):
        backend = self._backend_with_dataset(_synthetic_dataset())
        layer_cont = ScatterLayerSpec(y_axis=Axis.AMPLITUDE, polarization="XX",
                                      cmap=("#000000", "#ffffff"), scaling="linear")
        layer_zscore = ScatterLayerSpec(y_axis=Axis.Z_SCORE, polarization="XX",
                                        cmap=("#000000", "#ffffff"), scaling="linear")
        result = backend.query_columns(
            Axis.TIME, [layer_cont, layer_zscore], SelectionSpec(antenna_names=["DA42"]),
            width=200, height=200,
        )
        assert result.layers[0].antenna_summary is None
        assert result.layers[1].antenna_summary is not None
        assert result.layers[1].antenna_summary.antenna_name == "DA42"

    def test_coexists_with_two_level_rendering_reference(self):
        """Both antenna_summary and the Level-1 reference are attached
        via separate dataclasses.replace() calls in query_columns --
        confirmed they don't clobber each other."""
        backend = self._backend_with_dataset(_synthetic_dataset())
        layer = ScatterLayerSpec(y_axis=Axis.Z_SCORE, polarization="XX",
                                 cmap=("#000000", "#ffffff"), scaling="linear")
        result = backend.query_columns(
            Axis.TIME, [layer], SelectionSpec(antenna_names=["DA41"]),
            width=200, height=200, ref_scale=2.0,
        )
        rendered = result.layers[0]
        assert rendered.reference is not None
        assert rendered.antenna_summary is not None
        assert rendered.antenna_summary.antenna_name == "DA41"


# ---------------------------------------------------------------------------
# 3. colorbar_html -- the actual rendered HTML
# ---------------------------------------------------------------------------

class TestColorbarHtmlRendering:
    @staticmethod
    def _mapping():
        return cms.ScalarMapping.from_values(np.linspace(0, 10, 1000), "linear")

    def test_band_with_summary_shows_all_four_numbers(self):
        summary = AntennaZScoreSummary(
            antenna_name="DA41", count=4800, median_score=1.1774,
            fraction_over_threshold=0.0234, threshold=3.5,
        )
        band = ColorBand(label="Z-Score XX", cmap=("#000000", "#ffffff"),
                         scaling="linear", mapping=self._mapping(),
                         antenna_summary=summary)
        html = ip.colorbar_html([band])
        assert "DA41" in html
        assert "N=4800" in html
        assert "2.34" in html
        assert "1.18" in html
        assert "3.5" in html

    def test_band_without_summary_is_unaffected(self):
        band = ColorBand(label="Amplitude XX", cmap=("#000000", "#ffffff"),
                         scaling="linear", mapping=self._mapping())
        html = ip.colorbar_html([band])
        assert "N=" not in html

    def test_zero_count_says_no_samples_explicitly(self):
        summary = AntennaZScoreSummary(antenna_name="DV03", count=0,
                                       median_score=None, fraction_over_threshold=None,
                                       threshold=3.5)
        band = ColorBand(label="Z-Score XX", cmap=("#000000", "#ffffff"),
                         scaling="linear", mapping=self._mapping(),
                         antenna_summary=summary)
        html = ip.colorbar_html([band])
        assert "no samples" in html
        assert "DV03" in html

    def test_no_bands_returns_empty_string(self):
        assert ip.colorbar_html([]) == ""

    def test_multi_band_summary_appears_once_under_the_right_band(self):
        summary = AntennaZScoreSummary(antenna_name="DA41", count=100,
                                       median_score=1.2, fraction_over_threshold=0.1,
                                       threshold=3.5)
        band_stat = ColorBand(label="Statistical", cmap=("#000000", "#ffffff"),
                              scaling="linear", mapping=self._mapping(),
                              antenna_summary=summary)
        band_cont = ColorBand(label="Continuous", cmap=("#000000", "#ffffff"),
                              scaling="linear", mapping=self._mapping())
        html = ip.colorbar_html([band_stat, band_cont])
        assert html.count("DA41") == 1
        assert "N=100" in html
