"""
test_statistical_coloring.py
=============================
Tests for the "statistical" coloring mode (Part 6; visplot-colorize-
by-axis-design.md §7.6/§7.10): colors an existing continuous layer
(e.g. Amplitude vs. Time stays Amplitude vs. Time) BY the per-baseline
Z-Score instead of by the plotted Y column itself -- ``y_axis``/
``polarization`` still say what's PLOTTED; only the COLOR is decoupled.

Location in repository:
    cubevis/tests/manual/visplot/test_statistical_coloring.py

Run:
    pytest cubevis/tests/manual/visplot/test_statistical_coloring.py -v

Sections
--------
1. ScatterLayerSpec           accepts "statistical", existing validation
                              (colorize_axis/excluded_categories reject)
                              still applies to it exactly like "continuous"
2. render_layer (synthetic)   the "color"-column aggregation branch, with
                              a hand-built DataFrame -- no backend needed
3. query_columns end-to-end   MSv2Backend's merge logic: the render is
                              genuinely driven by Z-Score, not by the
                              plotted Y value
4. Merge correctness           no row duplication/unexpected drops, a
                              degenerate/missing Z-Score group still
                              plots (uncolored), regression check that a
                              continuous-only query fetches nothing extra
5. Two-level rendering interaction  a statistical layer's reference is
                              deliberately left unbuilt (None), a
                              continuous layer's in the SAME call is not

All synthetic -- no real MS/PS needed for any test in this file.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
import xarray as xr

from cubevis.toolbox.visplot.axes import Axis
from cubevis.toolbox.visplot.data import _scatter_render as sr
from cubevis.toolbox.visplot.data.msv2_backend import MSv2Backend
from cubevis.toolbox.visplot.data.reader import ScatterLayerSpec
from cubevis.toolbox.visplot.selection import SelectionSpec


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _bare_backend():
    """Same convention as test_zscore_colorization.py's own helper."""
    b = MSv2Backend.__new__(MSv2Backend)
    b._datatree = object()
    b._identity_categoricals = lambda *a, **k: {}
    b._partition_spw_ident = lambda ds: (None, None)
    b._antenna_lookup_table = lambda: None
    b._scan_time_index = lambda *a, **k: None
    return b


def _synthetic_flat_amplitude_dataset(n_time=40, n_baseline=15, n_freq=8,
                                       pols=("XX", "YY"), anomaly_baseline=3,
                                       anomaly_times=slice(10, 15),
                                       anomaly_offset=(6.0, 6.0), seed=42):
    """Amplitude is roughly CONSTANT (~5.0) across every baseline/time --
    deliberately, so that any color variation in a "statistical" render
    can only be explained by the Z-Score, never by the (nearly flat)
    plotted amplitude itself. One baseline gets a localized anomaly."""
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


def _distinct_nonzero_colors(image: np.ndarray) -> set:
    return set(np.unique(image[image != 0]).tolist())


# ---------------------------------------------------------------------------
# 1. ScatterLayerSpec
# ---------------------------------------------------------------------------

class TestScatterLayerSpecStatistical:
    def test_accepts_statistical(self):
        spec = ScatterLayerSpec(
            y_axis=Axis.AMPLITUDE, polarization="XX",
            cmap=("#222222", "#ff3333"), coloring="statistical",
        )
        assert spec.coloring == "statistical"

    def test_rejects_unknown_coloring_still(self):
        with pytest.raises(ValueError):
            ScatterLayerSpec(
                y_axis=Axis.AMPLITUDE, polarization="XX",
                cmap=("#000000",), coloring="bogus",
            )

    def test_colorize_axis_rejected_on_statistical_same_as_continuous(self):
        """Falls through the same "not categorical" branch as
        "continuous" -- confirmed directly, not just by reading the
        validation code, since this is the exact mechanism the class's
        own docstring claims needs no new field."""
        with pytest.raises(ValueError):
            ScatterLayerSpec(
                y_axis=Axis.AMPLITUDE, polarization="XX",
                cmap=("#000000",), coloring="statistical",
                colorize_axis=Axis.SCAN,
            )

    def test_excluded_categories_rejected_on_statistical(self):
        with pytest.raises(ValueError):
            ScatterLayerSpec(
                y_axis=Axis.AMPLITUDE, polarization="XX",
                cmap=("#000000",), coloring="statistical",
                excluded_categories=("1",),
            )

    def test_scaling_fields_reused_unchanged(self):
        """No new fields needed for the display-style choice -- scaling/
        scaling_vmin/etc. are the exact same fields continuous mode
        already uses."""
        spec = ScatterLayerSpec(
            y_axis=Axis.AMPLITUDE, polarization="XX",
            cmap=("#222222", "#ff3333"), coloring="statistical",
            scaling="threshold", scaling_vmin=5.0,
        )
        assert spec.scaling == "threshold"
        assert spec.scaling_vmin == 5.0


# ---------------------------------------------------------------------------
# 2. render_layer -- the "color"-column aggregation branch, synthetic df
# ---------------------------------------------------------------------------

class TestRenderLayerStatistical:
    @staticmethod
    def _df():
        rng = np.random.default_rng(0)
        n_normal, n_anomaly = 5000, 200
        x = np.concatenate([rng.uniform(0, 100, n_normal), rng.uniform(0, 100, n_anomaly)])
        y = np.concatenate([rng.uniform(4.9, 5.1, n_normal), rng.uniform(4.9, 5.1, n_anomaly)])
        color = np.concatenate([rng.uniform(0.5, 2.0, n_normal), rng.uniform(10.0, 20.0, n_anomaly)])
        return pd.DataFrame({"x": x, "y": y, "color": color})

    def test_aggregates_by_color_not_by_y(self):
        """y is nearly constant (4.9-5.1); color varies widely (0.5-20).
        A continuous render of this df would show almost no variation;
        a statistical one must show real variation, proving it read
        "color", not "y"."""
        df = self._df()
        layer = ScatterLayerSpec(
            y_axis=Axis.AMPLITUDE, polarization="XX",
            cmap=tuple(f"#{i:02x}{i:02x}{i:02x}" for i in range(0, 256, 8)),
            coloring="statistical", scaling="eq_hist",
        )
        result = sr.render_layer(
            df, layer, x0=0, x1=100, y0=4.9, y1=5.1,
            canvas_w=200, canvas_h=50, color_mode="global", full_y_range=(4.9, 5.1),
        )
        assert result.skip_reason is None
        assert len(_distinct_nonzero_colors(result.image)) > 5
        # peak_value must reflect the COLOR column's own scale (up to
        # ~20), not the y column's (~5) -- a continuous render of the
        # same df would report a peak near 5.1, not near 20.
        assert result.peak_value > 8.0

    def test_threshold_on_color_gives_binary_image(self):
        df = self._df()
        layer = ScatterLayerSpec(
            y_axis=Axis.AMPLITUDE, polarization="XX",
            cmap=("#222222", "#ff3333"),
            coloring="statistical", scaling="threshold", scaling_vmin=5.0,
        )
        result = sr.render_layer(
            df, layer, x0=0, x1=100, y0=4.9, y1=5.1,
            canvas_w=200, canvas_h=50, color_mode="global", full_y_range=(4.9, 5.1),
        )
        assert len(_distinct_nonzero_colors(result.image)) == 2

    def test_all_nan_color_column_does_not_crash(self):
        df = self._df()
        df["color"] = np.nan
        layer = ScatterLayerSpec(
            y_axis=Axis.AMPLITUDE, polarization="XX",
            cmap=("#222222", "#ff3333"), coloring="statistical", scaling="linear",
        )
        result = sr.render_layer(
            df, layer, x0=0, x1=100, y0=4.9, y1=5.1,
            canvas_w=200, canvas_h=50, color_mode="global", full_y_range=(4.9, 5.1),
        )
        assert result.skip_reason is None


# ---------------------------------------------------------------------------
# 3-5. query_columns end-to-end -- MSv2Backend's merge logic
# ---------------------------------------------------------------------------

class TestQueryColumnsStatisticalEndToEnd:
    @staticmethod
    def _backend_with_dataset(ds):
        backend = _bare_backend()
        backend._iter_visibility_partitions = lambda selection: iter([ds])
        backend._apply_selection = lambda raw_ds, selection: raw_ds
        backend._scan_lookup_for_partition = lambda raw_ds: None
        return backend

    def test_statistical_layer_colored_by_zscore_not_amplitude(self):
        """The core proof: amplitude is flat everywhere; the
        statistical layer's rendered colors and peak_value must reflect
        the Z-Score's own scale, not amplitude's."""
        ds = _synthetic_flat_amplitude_dataset()
        backend = self._backend_with_dataset(ds)
        layer_stat = ScatterLayerSpec(
            y_axis=Axis.AMPLITUDE, polarization="XX",
            cmap=("#222222", "#ff3333"), coloring="statistical",
            scaling="threshold", scaling_vmin=5.0,
        )
        result = backend.query_columns(
            Axis.TIME, [layer_stat], SelectionSpec(), width=200, height=200,
        )
        stat_render = result.layers[0]
        assert len(_distinct_nonzero_colors(stat_render.image)) == 2
        assert stat_render.peak_value > 20.0   # a Z-Score magnitude, not ~5-12 amplitude

    def test_continuous_layer_in_same_call_unaffected(self):
        ds = _synthetic_flat_amplitude_dataset()
        backend = self._backend_with_dataset(ds)
        layer_cont = ScatterLayerSpec(
            y_axis=Axis.AMPLITUDE, polarization="YY",
            cmap=tuple(f"#{i:02x}{i:02x}{i:02x}" for i in range(0, 256, 8)),
            scaling="linear",
        )
        layer_stat = ScatterLayerSpec(
            y_axis=Axis.AMPLITUDE, polarization="XX",
            cmap=("#222222", "#ff3333"), coloring="statistical",
            scaling="threshold", scaling_vmin=5.0,
        )
        result = backend.query_columns(
            Axis.TIME, [layer_cont, layer_stat], SelectionSpec(), width=200, height=200,
        )
        cont_render, stat_render = result.layers
        # amplitude is nearly flat everywhere EXCEPT the anomaly region
        # (which is also amplitude-elevated there) -- the continuous
        # layer's own peak should stay in amplitude's own scale, not
        # jump to the Z-Score's.
        assert cont_render.peak_value < 15.0
        assert stat_render.peak_value > 20.0

    def test_no_row_duplication_or_unexpected_drop_from_the_merge(self):
        ds = _synthetic_flat_amplitude_dataset()
        backend = self._backend_with_dataset(ds)
        layer_stat = ScatterLayerSpec(
            y_axis=Axis.AMPLITUDE, polarization="XX",
            cmap=("#222222", "#ff3333"), coloring="statistical", scaling="linear",
        )
        result = backend.query_columns(
            Axis.TIME, [layer_stat], SelectionSpec(), width=200, height=200,
        )
        plain = backend._query_columns_cached(
            Axis.TIME, [(Axis.AMPLITUDE, "XX")], SelectionSpec(),
        )
        assert result.layers[0].n_in_view == len(plain[(Axis.AMPLITUDE, "XX")])

    def test_continuous_only_query_fetches_nothing_extra(self):
        """Regression/overhead check: a query with no statistical layers
        must not request Z_SCORE at all."""
        ds = _synthetic_flat_amplitude_dataset()
        backend = self._backend_with_dataset(ds)
        reads = []
        real_raw = backend._query_columns_raw
        def spy(xaxis, yaxes, selection):
            reads.append(list(yaxes))
            return real_raw(xaxis, yaxes, selection)
        backend._query_columns_raw = spy

        layer_cont = ScatterLayerSpec(
            y_axis=Axis.AMPLITUDE, polarization="YY",
            cmap=("#000000", "#ffffff"), scaling="linear",
        )
        backend.query_columns(Axis.TIME, [layer_cont], SelectionSpec(), width=200, height=200)
        assert reads == [[(Axis.AMPLITUDE, "YY")]]
        assert not any(Axis.Z_SCORE in [k[0] for k in call] for call in reads)

    def test_reference_skipped_for_statistical_but_not_continuous(self):
        """Two-level rendering (ref_scale) is not yet taught about
        statistical coloring -- a reference must be deliberately left
        unbuilt for a statistical layer (forcing a Level-2 requery on
        every pan/zoom, correct if slower) rather than built incorrectly
        against the plotted Y column, while a continuous layer in the
        SAME call still gets its reference as normal."""
        ds = _synthetic_flat_amplitude_dataset()
        backend = self._backend_with_dataset(ds)
        layer_cont = ScatterLayerSpec(
            y_axis=Axis.AMPLITUDE, polarization="YY",
            cmap=("#000000", "#ffffff"), scaling="linear",
        )
        layer_stat = ScatterLayerSpec(
            y_axis=Axis.AMPLITUDE, polarization="XX",
            cmap=("#222222", "#ff3333"), coloring="statistical", scaling="linear",
        )
        result = backend.query_columns(
            Axis.TIME, [layer_cont, layer_stat], SelectionSpec(),
            width=200, height=200, ref_scale=2.0,
        )
        cont_render, stat_render = result.layers
        assert cont_render.reference is not None
        assert stat_render.reference is None
