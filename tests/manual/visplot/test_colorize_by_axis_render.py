"""
test_colorize_by_axis_render.py — Unit and integration tests for the
colorize-by-axis Part 3 rendering pipeline.

Location in repository:
    cubevis/tests/manual/visplot/test_colorize_by_axis_render.py

Tests against:
    cubevis/cubevis/toolbox/visplot/data/reader.py
        (ScatterLayerSpec.coloring/colorize_axis validation,
        COLORIZE_AXIS_COLUMNS, DEGENERATE_COLORIZE_AXES,
        colorizable_axes(), ScatterLayerRender.categories/
        category_colors)
    cubevis/cubevis/toolbox/visplot/data/_scatter_render.py
        (_category_sort_key, _resolve_categories, _shade_categorical,
        render_layer's categorical branch, CATEGORY_CAP)
    cubevis/cubevis/toolbox/visplot/palettes.py
        (categorical_cmap, categorical_names, check_background_contrast)

Companion documents:
    visplot-colorize-by-axis-design.md
    visplot-colorize-by-axis-handoff-part3.md (this suite implements
    its "Suggested first steps for Part 3")
    visplot-colorize-by-axis-handoff-part4.md (this suite's own
    handoff, produced alongside it)

Run from the cubevis repository root (so the package is importable):

    MS=sis14_twhya_calibrated_flagged.ms \\
        pytest cubevis/tests/manual/visplot/test_colorize_by_axis_render.py -v

Or standalone (falls back to local copies of the source files if the
package is not installed):

    MS=sis14_twhya_calibrated_flagged.ms \\
        python test_colorize_by_axis_render.py

Sections
--------
1. ScatterLayerSpec validation      coloring/colorize_axis contract
2. COLORIZE_AXIS_COLUMNS            lookup table + colorizable_axes()
3. _category_sort_key               numeric-aware ordering
4. _resolve_categories              cap, missing column, NaN handling
5. render_layer (synthetic)         full categorical render, no MS needed
6. palettes.categorical_cmap        contrast, cycling, self-check
7. render_layer (real MS)           end-to-end via MSv2Backend, all
                                     colorizable axes, real cardinality
"""
from __future__ import annotations

import os
import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

# ---------------------------------------------------------------------------
# Import strategy -- mirrors test_msv2_backend.py's exactly (primary: real
# package; fallback: load source files from this directory's tree by path,
# in dependency order, patching the relative-import names each module
# expects to find already in sys.modules).
# ---------------------------------------------------------------------------

def _try_package_import():
    from cubevis.toolbox.visplot.axes import Axis
    from cubevis.toolbox.visplot.selection import SelectionSpec
    from cubevis.toolbox.visplot import palettes
    from cubevis.toolbox.visplot.data import _scatter_render as sr
    from cubevis.toolbox.visplot.data.reader import (
        ScatterLayerSpec, ScatterLayerRender, COLORIZE_AXIS_COLUMNS,
        DEGENERATE_COLORIZE_AXES, colorizable_axes,
    )
    from cubevis.toolbox.visplot.data.msv2_backend import MSv2Backend
    from cubevis.toolbox.visplot.data.msv4_backend import MSv4Backend
    return (Axis, SelectionSpec, palettes, sr, ScatterLayerSpec,
            ScatterLayerRender, COLORIZE_AXIS_COLUMNS,
            DEGENERATE_COLORIZE_AXES, colorizable_axes, MSv2Backend,
            MSv4Backend)


def _local_import():
    import importlib.util

    here = Path(__file__).parent

    def _load(name, path):
        spec = importlib.util.spec_from_file_location(name, path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
        return mod

    axes_mod = _load("cubevis.toolbox.visplot.axes", here / "axes.py")
    sel_mod = _load("cubevis.toolbox.visplot.selection", here / "selection.py")
    cms_mod = _load("cubevis.toolbox.visplot.colormap_scaling",
                     here / "colormap_scaling.py")
    palettes_mod = _load("cubevis.toolbox.visplot.palettes", here / "palettes.py")

    sys.modules["cubevis.toolbox.visplot.axes"] = axes_mod
    sys.modules["cubevis.toolbox.visplot.selection"] = sel_mod
    reader_mod = _load("cubevis.toolbox.visplot.reader", here / "reader.py")
    sys.modules["cubevis.toolbox.visplot.data.reader"] = reader_mod

    # _scatter_render.py does `from .. import colormap_scaling as _cms`
    # and `from .reader import ...` -- both already staged above.
    sys.modules["cubevis.toolbox.visplot.colormap_scaling"] = cms_mod
    sr_mod = _load("cubevis.toolbox.visplot.data._scatter_render",
                    here / "_scatter_render.py")

    backend_mod = _load("cubevis.toolbox.visplot.data.msv2_backend",
                         here / "msv2_backend.py")
    backend4_mod = _load("cubevis.toolbox.visplot.data.msv4_backend",
                          here / "msv4_backend.py")

    return (axes_mod.Axis, sel_mod.SelectionSpec, palettes_mod, sr_mod,
            reader_mod.ScatterLayerSpec, reader_mod.ScatterLayerRender,
            reader_mod.COLORIZE_AXIS_COLUMNS,
            reader_mod.DEGENERATE_COLORIZE_AXES,
            reader_mod.colorizable_axes, backend_mod.MSv2Backend,
            backend4_mod.MSv4Backend)


try:
    (Axis, SelectionSpec, palettes, sr, ScatterLayerSpec, ScatterLayerRender,
     COLORIZE_AXIS_COLUMNS, DEGENERATE_COLORIZE_AXES, colorizable_axes,
     MSv2Backend, MSv4Backend) = _try_package_import()
    _SOURCE = "package"
except ImportError:
    (Axis, SelectionSpec, palettes, sr, ScatterLayerSpec, ScatterLayerRender,
     COLORIZE_AXIS_COLUMNS, DEGENERATE_COLORIZE_AXES, colorizable_axes,
     MSv2Backend, MSv4Backend) = _local_import()
    _SOURCE = "local"

print(f"[test_colorize_by_axis_render] imports from: {_SOURCE}")


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


def _open_backend(**kwargs) -> "MSv2Backend":
    b = MSv2Backend(_get_ms(), **kwargs)
    b.open()
    return b


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


def _open_backend_msv4(**kwargs) -> "MSv4Backend":
    b = MSv4Backend(_get_ps(), **kwargs)
    b.open()
    return b


def _suppress_warnings():
    warnings.filterwarnings("ignore", category=UserWarning, module="xarray_ms")
    warnings.filterwarnings("ignore",
                             message="The return type of.*Dataset.dims",
                             category=FutureWarning)
    warnings.filterwarnings("ignore", message="omp_set_nested")


def _require_datashader():
    if not sr.HAS_DATASHADER:
        pytest.skip("datashader not installed — pip install datashader")


def _synthetic_df(n=20000, categorical_col=None, categorical_values=None,
                   nan_fraction=0.0, seed=0):
    """A minimal x/y (+ one categorical column) DataFrame for exercising
    render_layer without any real MS -- mirrors the shape
    ``_query_columns_raw`` actually produces (x/y plus whichever
    identity columns happened to be attached)."""
    rng = np.random.default_rng(seed)
    df = pd.DataFrame({
        "x": rng.uniform(0, 100, n),
        "y": rng.uniform(0, 10, n),
    })
    if categorical_col is not None:
        df[categorical_col] = rng.choice(categorical_values, size=n)
        if nan_fraction > 0:
            gap_idx = rng.choice(n, size=int(n * nan_fraction), replace=False)
            df.loc[gap_idx, categorical_col] = np.nan
    return df


# ---------------------------------------------------------------------------
# 1. ScatterLayerSpec validation
# ---------------------------------------------------------------------------

class TestScatterLayerSpecColoring:
    """``coloring``/``colorize_axis`` contract on ``ScatterLayerSpec`` --
    see that class's docstring in reader.py for the design rationale."""

    def test_continuous_default_unaffected(self):
        """Every pre-Part-3 caller passes no coloring/colorize_axis at
        all -- must keep working exactly as before."""
        spec = ScatterLayerSpec(y_axis=Axis.AMPLITUDE, polarization="XX",
                                 cmap=("#000000", "#ffffff"))
        assert spec.coloring == "continuous"
        assert spec.colorize_axis is None

    def test_categorical_valid_construction(self):
        spec = ScatterLayerSpec(
            y_axis=Axis.AMPLITUDE, polarization="XX",
            cmap=palettes.categorical_cmap(theme="dark"),
            coloring="categorical", colorize_axis=Axis.SCAN,
        )
        assert spec.coloring == "categorical"
        assert spec.colorize_axis is Axis.SCAN

    def test_categorical_requires_colorize_axis(self):
        with pytest.raises(ValueError, match="colorize_axis to be set"):
            ScatterLayerSpec(y_axis=Axis.AMPLITUDE, polarization="XX",
                              cmap=("#000000",), coloring="categorical")

    def test_categorical_requires_nonempty_cmap(self):
        with pytest.raises(ValueError, match="non-empty cmap"):
            ScatterLayerSpec(y_axis=Axis.AMPLITUDE, polarization="XX",
                              cmap=(), coloring="categorical",
                              colorize_axis=Axis.SCAN)

    def test_categorical_rejects_non_colorizable_axis(self):
        with pytest.raises(ValueError, match="not a colorizable axis"):
            ScatterLayerSpec(y_axis=Axis.AMPLITUDE, polarization="XX",
                              cmap=("#000000",), coloring="categorical",
                              colorize_axis=Axis.AMPLITUDE)

    def test_colorize_axis_rejected_outside_categorical_mode(self):
        with pytest.raises(ValueError, match="only valid when"):
            ScatterLayerSpec(y_axis=Axis.AMPLITUDE, polarization="XX",
                              cmap=("#000000",), colorize_axis=Axis.SCAN)

    def test_invalid_coloring_value_rejected(self):
        with pytest.raises(ValueError, match="'continuous' or 'categorical'"):
            ScatterLayerSpec(y_axis=Axis.AMPLITUDE, polarization="XX",
                              cmap=("#000000",), coloring="bogus")

    def test_spec_is_still_frozen(self):
        """Validation must not have broken immutability."""
        spec = ScatterLayerSpec(y_axis=Axis.AMPLITUDE, polarization="XX",
                                 cmap=("#000000",))
        with pytest.raises(Exception):
            spec.coloring = "categorical"


# ---------------------------------------------------------------------------
# 2. COLORIZE_AXIS_COLUMNS / colorizable_axes / DEGENERATE_COLORIZE_AXES
# ---------------------------------------------------------------------------

class TestColorizeAxisColumnsTable:
    """The axis -> column lookup table
    visplot-colorize-by-axis-handoff-part3.md asked Part 3 to build."""

    # Part 5b (2026-09) added FIELD and BASELINE; the other five entries are
    # exactly the Part 2 table.  Order matters -- it is the axis picker's
    # order (see the dedicated test below).
    EXPECTED = {
        Axis.SCAN: "scan_name",
        Axis.FIELD: "field_name",
        Axis.ANTENNA1: "baseline_antenna1_name",
        Axis.ANTENNA2: "baseline_antenna2_name",
        Axis.BASELINE: "baseline_name",
        Axis.CORRELATION: "polarization",
        Axis.SPW: "spw",
    }

    def test_matches_the_part2_what_landed_table(self):
        assert COLORIZE_AXIS_COLUMNS == self.EXPECTED

    def test_the_part2_entries_are_unchanged(self):
        part2 = {Axis.SCAN: "scan_name", Axis.ANTENNA1: "baseline_antenna1_name",
                 Axis.ANTENNA2: "baseline_antenna2_name",
                 Axis.CORRELATION: "polarization", Axis.SPW: "spw"}
        assert {a: COLORIZE_AXIS_COLUMNS[a] for a in part2} == part2

    def test_picker_order_and_default(self):
        """Scan stays first, so it stays the picker's default; Field follows
        it and Baseline follows the two antenna axes."""
        order = list(COLORIZE_AXIS_COLUMNS)
        assert order[0] is Axis.SCAN
        assert order.index(Axis.FIELD) == 1
        assert order.index(Axis.BASELINE) == order.index(Axis.ANTENNA2) + 1

    def test_colorizable_axes_matches_the_table_keys(self):
        assert set(colorizable_axes()) == set(COLORIZE_AXIS_COLUMNS)

    def test_correlation_flagged_degenerate_but_still_colorizable(self):
        """Correlation is degenerate (see the design doc's §4.1
        finding) but still a real, correctly-populated column -- it
        stays IN the lookup table, just flagged separately, per
        reader.py's DEGENERATE_COLORIZE_AXES docstring."""
        assert Axis.CORRELATION in COLORIZE_AXIS_COLUMNS
        assert Axis.CORRELATION in DEGENERATE_COLORIZE_AXES
        assert len(DEGENERATE_COLORIZE_AXES) == 1

    def test_observation_and_intent_not_colorizable(self):
        """Dropped in Part 2 -- see the design doc's §4.1 finding and
        the handoff's "Axes dropped or deferred" section. Confirms
        Part 3 didn't accidentally resurrect them."""
        assert Axis.OBSERVATION not in COLORIZE_AXIS_COLUMNS
        assert Axis.INTENT not in COLORIZE_AXIS_COLUMNS


# ---------------------------------------------------------------------------
# 3. _category_sort_key
# ---------------------------------------------------------------------------

class TestCategorySortKey:
    def test_numeric_strings_sort_numerically_not_lexicographically(self):
        values = ["17", "2", "10", "4"]
        assert sorted(values, key=sr._category_sort_key) == ["2", "4", "10", "17"]

    def test_non_numeric_strings_sort_lexicographically(self):
        values = ["DV22", "DA42", "DV02"]
        assert (sorted(values, key=sr._category_sort_key)
                == sorted(values))

    def test_numeric_values_sort_before_non_numeric(self):
        values = ["DA42", "3", "DV02", "1"]
        result = sorted(values, key=sr._category_sort_key)
        assert result[:2] == ["1", "3"]
        assert set(result[2:]) == {"DA42", "DV02"}


# ---------------------------------------------------------------------------
# 4. _bin_categories
# ---------------------------------------------------------------------------

class TestBinCategories:
    """The auto-binning that replaced "refuse over cap" -- see
    CATEGORY_CAP's docstring for why. Antenna cardinality at ngVLA
    scale (263 antennas) is the motivating case throughout."""

    def test_under_cap_is_untouched_singleton_buckets(self):
        distinct = ["1", "2", "3"]
        members = sr._bin_categories(distinct, cap=20)
        assert members == {"1": ("1",), "2": ("2",), "3": ("3",)}

    def test_exactly_at_cap_is_untouched(self):
        distinct = [str(i) for i in range(20)]
        members = sr._bin_categories(distinct, cap=20)
        assert len(members) == 20
        assert all(len(v) == 1 for v in members.values())

    def test_over_cap_produces_at_most_cap_buckets(self):
        distinct = [f"DA{i:02d}" for i in range(30)]
        members = sr._bin_categories(distinct, cap=20)
        assert len(members) <= 20

    def test_ngvla_scale_263_antennas_produces_exactly_cap_buckets(self):
        """The real motivating case: ngVLA's full 263-antenna array.
        This is what makes colorize-by-axis usable there at all --
        the pre-binning design would have refused this outright (see
        the design doc's original §4.2, since superseded)."""
        distinct = [f"ANT{i:03d}" for i in range(263)]
        members = sr._bin_categories(distinct, cap=20)
        assert len(members) == 20
        total = sum(len(v) for v in members.values())
        assert total == 263
        # near-equal bucket sizes: 263 // 20 = 13, remainder 3
        sizes = sorted(len(v) for v in members.values())
        assert sizes[0] == 13 and sizes[-1] == 14
        assert sizes.count(14) == 3

    def test_every_raw_value_covered_exactly_once(self):
        distinct = [f"DA{i:02d}" for i in range(47)]  # a real ALMA-scale count
        members = sr._bin_categories(distinct, cap=20)
        covered = sorted(v for vals in members.values() for v in vals)
        assert covered == sorted(distinct)

    def test_buckets_are_contiguous_in_sort_order(self):
        """A bucket's members must be a contiguous run of the sorted
        input -- required for the "lo-hi" label to mean an actual
        range rather than an arbitrary pair."""
        distinct = [str(i) for i in range(50)]
        members = sr._bin_categories(distinct, cap=10)
        for vals in members.values():
            nums = [int(v) for v in vals]
            assert nums == list(range(nums[0], nums[0] + len(nums)))

    def test_multi_value_bucket_labeled_with_en_dash_range(self):
        distinct = [str(i) for i in range(10)]
        members = sr._bin_categories(distinct, cap=2)
        assert set(members) == {"0\u20134", "5\u20139"}
        assert members["0\u20134"] == ("0", "1", "2", "3", "4")

    def test_single_value_bucket_labeled_as_the_value_itself(self):
        distinct = [str(i) for i in range(3)]
        members = sr._bin_categories(distinct, cap=10)  # cap > n, no binning
        assert set(members) == {"0", "1", "2"}

    def test_remainder_spread_across_first_buckets_not_last(self):
        # 7 values into 3 buckets: base=2, extra=1 -> sizes [3, 2, 2]
        distinct = [str(i) for i in range(7)]
        members = sr._bin_categories(distinct, cap=3)
        sizes = [len(v) for v in members.values()]
        assert sizes == [3, 2, 2]


# ---------------------------------------------------------------------------
# 5. _resolve_categories
# ---------------------------------------------------------------------------

class TestResolveCategories:
    def test_missing_column_returns_skip_reason(self):
        df = _synthetic_df()
        mask, cats, members, reason = sr._resolve_categories(df, "scan_name", "Scan")
        assert mask is None and cats is None and members is None
        assert reason == "no Scan data for this selection"

    def test_all_nan_column_returns_skip_reason(self):
        df = _synthetic_df()
        df["scan_name"] = np.nan
        mask, cats, members, reason = sr._resolve_categories(df, "scan_name", "Scan")
        assert mask is None and cats is None and members is None
        assert "no Scan data" in reason

    def test_over_cap_no_longer_produces_a_skip_reason(self):
        """The behavior change this whole rewrite is about: exceeding
        the cap used to be a skip reason ("narrow the selection");
        it's now silently absorbed by binning, and the render always
        succeeds as long as there's any data at all."""
        df = _synthetic_df(categorical_col="ant",
                            categorical_values=[f"DA{i}" for i in range(30)])
        mask, cats, members, reason = sr._resolve_categories(
            df, "ant", "Antenna 1", cap=20,
        )
        assert reason is None
        assert len(cats) <= 20
        assert sum(len(v) for v in members.values()) == 30

    def test_cap_exactly_at_limit_produces_singleton_buckets(self):
        df = _synthetic_df(categorical_col="ant",
                            categorical_values=[f"DA{i}" for i in range(20)])
        mask, cats, members, reason = sr._resolve_categories(
            df, "ant", "Antenna 1", cap=20,
        )
        assert reason is None
        assert len(cats) == 20
        assert all(len(v) == 1 for v in members.values())

    def test_nan_rows_excluded_from_categories_and_mask(self):
        df = _synthetic_df(categorical_col="scan_name",
                            categorical_values=["1", "2", "3"],
                            nan_fraction=0.1)
        mask, cats, members, reason = sr._resolve_categories(df, "scan_name", "Scan")
        assert reason is None
        assert mask.sum() == df["scan_name"].notna().sum()
        assert cats == ["1", "2", "3"]
        assert members == {"1": ("1",), "2": ("2",), "3": ("3",)}

    def test_categories_sorted_numerically(self):
        df = _synthetic_df(categorical_col="scan_name",
                            categorical_values=["17", "2", "10"])
        _, cats, _, _ = sr._resolve_categories(df, "scan_name", "Scan")
        assert cats == ["2", "10", "17"]

    def test_mixed_int_and_str_spw_identity_merges_into_one_category(self):
        """The design doc's open question: spw's per-partition identity
        can be an int on one partition and a str on another for the
        SAME real spectral window. String-normalizing before comparison
        (rather than after, or not at all) is what makes them merge
        into one category instead of spuriously splitting into two."""
        df = pd.DataFrame({
            "x": np.arange(6.0), "y": np.arange(6.0),
            "spw": [0, 0, 0, "0", "0", "0"],
        })
        _, cats, members, reason = sr._resolve_categories(df, "spw", "SPW")
        assert reason is None
        assert cats == ["0"]
        assert members == {"0": ("0",)}


# ---------------------------------------------------------------------------
# 5. render_layer -- synthetic data, no MS required
# ---------------------------------------------------------------------------

def _decode_rgb(hex_color: str) -> int:
    """Pack a '#rrggbb' string the SAME way _priority_shade packs pixels
    (r | g<<8 | b<<16), for comparing rendered pixels back to a legend
    color. NOT the same bit order as reading the hex string as one
    big integer -- see this file's own investigation of that mix-up."""
    r, g, b = sr._hex_to_rgb_uint8(hex_color)
    return r | (g << 8) | (b << 16)


def _assert_every_pixel_matches_a_legend_color(result):
    """Winner-take-all guarantee: every non-transparent pixel is
    EXACTLY one of category_colors' values, never a blend. The
    property this whole switch away from tf.shade(color_key=...) was
    for -- see _priority_shade's docstring."""
    valid = {_decode_rgb(c) for c in result.category_colors.values()}
    nonzero = result.image[result.image != 0]
    if nonzero.size == 0:
        return
    rgb_vals = nonzero.astype(np.uint32) & np.uint32(0x00FFFFFF)
    bad = [int(v) for v in np.unique(rgb_vals) if int(v) not in valid]
    assert not bad, f"pixel color(s) not matching any legend swatch: {bad!r}"


class TestRenderLayerCategorical:
    def setup_method(self):
        _require_datashader()

    def _layer(self, axis=Axis.SCAN, cmap=None):
        return ScatterLayerSpec(
            y_axis=Axis.AMPLITUDE, polarization="XX",
            cmap=cmap or palettes.categorical_cmap(theme="dark"),
            coloring="categorical", colorize_axis=axis,
        )

    def test_basic_categorical_render(self):
        df = _synthetic_df(categorical_col="scan_name",
                            categorical_values=["1", "2", "3"])
        result = sr.render_layer(df, self._layer(), 0, 100, 0, 10, 200, 150,
                                  "global", (0, 10))
        assert result.skip_reason is None
        assert result.categories == ("1", "2", "3")
        assert set(result.category_colors) == {"1", "2", "3"}
        assert result.image.shape == (150, 200)
        assert result.image.dtype == np.uint32

    def test_continuous_fields_left_none_for_categorical_layer(self):
        """Design doc §4.3: the two modes are mutually exclusive, not
        layered -- a categorical layer has no colorbar/histogram."""
        df = _synthetic_df(categorical_col="scan_name",
                            categorical_values=["1", "2"])
        result = sr.render_layer(df, self._layer(), 0, 100, 0, 10, 200, 150,
                                  "global", (0, 10))
        assert result.peak_value is None
        assert result.hist_counts is None and result.hist_edges is None
        assert result.mapping_x is None and result.mapping_u is None

    def test_categorical_fields_left_none_for_continuous_layer(self):
        """And the reverse -- a continuous layer must not pick up
        stray categorical fields."""
        df = _synthetic_df()
        cont_layer = ScatterLayerSpec(y_axis=Axis.AMPLITUDE, polarization="XX",
                                       cmap=("#000000", "#ffffff"))
        result = sr.render_layer(df, cont_layer, 0, 100, 0, 10, 200, 150,
                                  "global", (0, 10))
        assert result.categories is None
        assert result.category_colors is None
        assert result.peak_value is not None  # continuous path unaffected

    def test_over_cap_bins_instead_of_refusing(self):
        """The behavior this whole rewrite is about: exceeding the cap
        used to skip with a "narrow the selection" reason; now it
        renders successfully with bucketed categories -- see
        CATEGORY_CAP's docstring for the real-data motivation
        (ngVLA's 263-antenna array)."""
        df = _synthetic_df(categorical_col="baseline_antenna1_name",
                            categorical_values=[f"DA{i}" for i in range(25)])
        result = sr.render_layer(
            df, self._layer(axis=Axis.ANTENNA1), 0, 100, 0, 10, 200, 150,
            "global", (0, 10),
        )
        assert result.skip_reason is None
        assert len(result.categories) <= 20
        assert sum(len(v) for v in result.category_members.values()) == 25
        assert result.n_in_view > 0
        _assert_every_pixel_matches_a_legend_color(result)

    def test_ngvla_scale_263_antennas_renders_successfully(self):
        """The actual motivating case: this used to be an outright
        refusal (263 > the old hard cap, no escape hatch). Also a
        light performance sanity check -- should complete quickly on a
        modest canvas, not hang."""
        import time
        df = _synthetic_df(
            n=200000, categorical_col="baseline_antenna1_name",
            categorical_values=[f"ANT{i:03d}" for i in range(263)],
        )
        t0 = time.perf_counter()
        result = sr.render_layer(
            df, self._layer(axis=Axis.ANTENNA1), 0, 100, 0, 10, 400, 300,
            "global", (0, 10),
        )
        elapsed = time.perf_counter() - t0
        assert result.skip_reason is None
        assert len(result.categories) == 20
        assert sum(len(v) for v in result.category_members.values()) == 263
        _assert_every_pixel_matches_a_legend_color(result)
        assert elapsed < 5.0, f"took {elapsed:.2f}s -- unexpectedly slow"

    def test_category_members_present_and_covers_all_raw_values_when_unbinned(self):
        """category_members must be populated even in the common,
        unbinned case (never None on a successful render) -- see
        ScatterLayerRender.category_members's docstring on why callers
        should not need to branch on "was this binned?"."""
        df = _synthetic_df(categorical_col="scan_name",
                            categorical_values=["1", "2", "3"])
        result = sr.render_layer(df, self._layer(), 0, 100, 0, 10, 200, 150,
                                  "global", (0, 10))
        assert result.category_members == {"1": ("1",), "2": ("2",), "3": ("3",)}

    def test_winner_take_all_every_pixel_matches_exactly_one_legend_color(self):
        """Direct test of the property _priority_shade exists for: no
        pixel may render a blended color that matches no legend
        swatch, even with several categories genuinely co-occupying
        the same pixels."""
        df = _synthetic_df(categorical_col="scan_name",
                            categorical_values=["1", "2", "3", "4", "5"],
                            n=100000)
        result = sr.render_layer(df, self._layer(), 0, 100, 0, 10, 60, 45,
                                  "global", (0, 10))
        assert result.skip_reason is None
        _assert_every_pixel_matches_a_legend_color(result)

    def test_empty_pixels_are_fully_transparent(self):
        df = _synthetic_df(categorical_col="scan_name",
                            categorical_values=["1", "2"])
        result = sr.render_layer(df, self._layer(), 0, 100, 0, 10, 50, 50,
                                  "global", (0, 10))
        # x_range/y_range chosen so nothing lands in-viewport -> everything empty
        empty_result = sr.render_layer(df, self._layer(), 1000, 2000, 0, 10,
                                        50, 50, "global", (0, 10))
        assert np.all(empty_result.image == 0) or empty_result.skip_reason is not None

    def test_categorical_pixels_are_fully_opaque(self):
        """Part 5a (2026-09): replaces the old "alpha respects the
        _MIN_ALPHA floor" check.  A categorical layer used to carry a
        histogram-equalized DENSITY alpha (floored at _MIN_ALPHA), which
        drew the lone sample of a rare category faintest -- exactly the
        thing the "rarest" draw priority exists to show -- and which the
        client then flattened to one dim value anyway.  Every occupied
        pixel is now alpha 255; density is the continuous mode's job.
        See test_colorize_by_axis_part5a_priority.py for the priority
        tests themselves."""
        df = _synthetic_df(categorical_col="scan_name",
                            categorical_values=["1", "2"], n=5000)
        result = sr.render_layer(df, self._layer(), 0, 100, 0, 10, 80, 60,
                                  "global", (0, 10))
        nonzero = result.image[result.image != 0]
        assert nonzero.size > 0
        alphas = (nonzero >> 24) & 0xFF
        assert (alphas == 255).all()

    def test_nan_rows_do_not_corrupt_last_category_color_weight(self):
        """Regression guard for the Datashader ds_agg.by() NaN-folding
        bug documented in _resolve_categories's docstring: render a
        column that's ~50% NaN and confirm the categorical image still
        only shows the two REAL categories' colors exactly -- not some
        third color artificially weighted by the dropped rows landing
        in one of them."""
        df = _synthetic_df(categorical_col="scan_name",
                            categorical_values=["1", "2"], nan_fraction=0.5,
                            n=40000)
        result = sr.render_layer(df, self._layer(), 0, 100, 0, 10, 50, 50,
                                  "global", (0, 10))
        assert result.skip_reason is None
        assert result.categories == ("1", "2")
        _assert_every_pixel_matches_a_legend_color(result)

    def test_category_to_color_assignment_stable_across_viewport_change(self):
        """Panning/zooming (a new x0/x1/y0/y1, same df/selection) must
        not reassign a category to a different color -- see
        ScatterLayerRender.categories's docstring."""
        df = _synthetic_df(categorical_col="scan_name",
                            categorical_values=["5", "1", "9"], n=30000)
        layer = self._layer()
        full = sr.render_layer(df, layer, 0, 100, 0, 10, 200, 150,
                                "global", (0, 10))
        zoomed = sr.render_layer(df, layer, 10, 30, 2, 5, 200, 150,
                                  "global", (0, 10))
        assert full.categories == zoomed.categories
        assert full.category_colors == zoomed.category_colors

    def test_cmap_cycles_when_categories_outnumber_colors(self):
        df = _synthetic_df(categorical_col="scan_name",
                            categorical_values=[str(i) for i in range(5)])
        layer = self._layer(cmap=("#111111", "#222222"))
        result = sr.render_layer(df, layer, 0, 100, 0, 10, 100, 100,
                                  "global", (0, 10))
        assert result.skip_reason is None
        assert result.category_colors["0"] == "#111111"
        assert result.category_colors["1"] == "#222222"
        assert result.category_colors["2"] == "#111111"  # wrapped

    def test_degenerate_correlation_axis_still_renders(self):
        """Correlation is flagged degenerate (DEGENERATE_COLORIZE_AXES)
        but must still render correctly -- a single-category legend is
        a valid, if unexciting, result; it is Part 4's job to decide
        whether to offer it in the UI, not render_layer's job to
        refuse it."""
        df = _synthetic_df(categorical_col="polarization",
                            categorical_values=["XX"])
        result = sr.render_layer(
            df, self._layer(axis=Axis.CORRELATION), 0, 100, 0, 10, 100, 100,
            "global", (0, 10),
        )
        assert result.skip_reason is None
        assert result.categories == ("XX",)


# ---------------------------------------------------------------------------
# 6. palettes.categorical_cmap
# ---------------------------------------------------------------------------

class TestCategoricalPalette:
    def test_default_returns_twenty_colors(self):
        assert len(palettes.categorical_cmap(theme="dark")) == 20

    def test_cycles_modulo_length_when_n_exceeds_base(self):
        cmap = palettes.categorical_cmap(theme="dark", n=25)
        assert len(cmap) == 25
        assert cmap[20] == cmap[0]
        assert cmap[24] == cmap[4]

    def test_n_zero_or_negative_returns_empty(self):
        assert palettes.categorical_cmap(theme="dark", n=0) == ()

    def test_dark_and_light_variants_differ_where_contrast_requires_it(self):
        dark = palettes.categorical_cmap(theme="dark")
        light = palettes.categorical_cmap(theme="light")
        assert dark != light

    def test_unknown_name_falls_back_to_default_rather_than_raising(self):
        cmap = palettes.categorical_cmap(name="not-a-real-palette", theme="dark")
        assert cmap == palettes.categorical_cmap(theme="dark")

    def test_categorical_names_lists_category20(self):
        assert "category20" in palettes.categorical_names()

    def test_check_background_contrast_has_no_complaints(self):
        bad = palettes.check_background_contrast()
        cat_complaints = [b for b in bad if b.startswith("categorical")]
        assert cat_complaints == [], cat_complaints


# ---------------------------------------------------------------------------
# 7. render_layer through the real backend -- real MS, real cardinality
# ---------------------------------------------------------------------------

class TestColorizeByAxisRealData:
    """End-to-end through MSv2Backend.query_columns against
    sis14_twhya_calibrated_flagged -- the same MS Part 2's
    TestColorizeByAxisColumns verified the underlying columns against.
    Confirms Part 3's rendering pipeline actually consumes those real
    columns correctly, not just synthetic stand-ins."""

    def setup_method(self):
        _require_datashader()
        _suppress_warnings()
        self.backend = _open_backend()
        meta = self.backend.metadata()
        self.pol = meta["correlation_labels"][0]
        t0, t1 = meta["time_range"]
        # Same 15%-of-full-selection convention Part 2's tests used --
        # narrow enough to keep this fast, wide enough to span several
        # scans/antennas for a meaningful cardinality check.
        self.sel = SelectionSpec(
            time_range=(t0, t0 + (t1 - t0) * 0.15),
            channel_range=(0, 16),
        )

    def teardown_method(self):
        self.backend.close()

    def _layer(self, axis, cmap=None):
        return ScatterLayerSpec(
            y_axis=Axis.AMPLITUDE, polarization=self.pol,
            cmap=cmap or palettes.categorical_cmap(theme="dark"),
            coloring="categorical", colorize_axis=axis,
        )

    @pytest.mark.parametrize("axis", list(COLORIZE_AXIS_COLUMNS))
    def test_every_colorizable_axis_renders_without_crashing(self, axis):
        result = self.backend.query_columns(
            Axis.TIME, [self._layer(axis)], self.sel, width=300, height=200,
        ).layers[0]
        # Either a clean categorical render or an honest skip reason --
        # never a silent empty image with no explanation, and never a
        # crash.
        if result.skip_reason is None:
            assert result.categories is not None
            assert len(result.categories) >= 1
            assert set(result.category_colors) == set(result.categories)
        else:
            assert isinstance(result.skip_reason, str)

    def test_antenna1_cardinality_matches_the_documented_real_finding(self):
        """visplot-colorize-by-axis-handoff-part3.md's real-cardinality
        finding: Antenna1/Antenna2 sit AT the ~20 cap on this specific
        26-antenna MS within a comparable selection window. Pinning
        this down as a real assertion (not just prose in a handoff doc)
        means a future change to partition selection or the cap itself
        gets caught here if it silently shifts this MS's behavior."""
        result = self.backend.query_columns(
            Axis.TIME, [self._layer(Axis.ANTENNA1)], self.sel,
            width=300, height=200,
        ).layers[0]
        assert result.skip_reason is None, result.skip_reason
        assert len(result.categories) == 20

    def test_correlation_is_a_single_category_on_this_ms(self):
        """Confirms the degenerate-axis finding in practice, not just
        in the frozenset flag."""
        result = self.backend.query_columns(
            Axis.TIME, [self._layer(Axis.CORRELATION)], self.sel,
            width=300, height=200,
        ).layers[0]
        assert result.skip_reason is None
        assert len(result.categories) == 1

    def test_spw_reports_the_real_spectral_window_identity(self):
        result = self.backend.query_columns(
            Axis.TIME, [self._layer(Axis.SPW)], self.sel,
            width=300, height=200,
        ).layers[0]
        assert result.skip_reason is None
        assert len(result.categories) == 1

    def test_scan_categories_are_real_scan_numbers(self):
        result = self.backend.query_columns(
            Axis.TIME, [self._layer(Axis.SCAN)], self.sel,
            width=300, height=200,
        ).layers[0]
        assert result.skip_reason is None
        for cat in result.categories:
            int(cat)  # every scan_name category must parse as an integer

    def test_mixing_categorical_and_continuous_layers_in_one_call(self):
        """Realistic multi-layer usage: one categorical, one
        continuous, in the same query_columns() call -- confirms
        render_layer's mode branch doesn't leak state between layers
        processed in the same list comprehension in
        MSv2Backend.query_columns."""
        cont = ScatterLayerSpec(
            y_axis=Axis.AMPLITUDE, polarization=self.pol,
            cmap=("#000000", "#ffffff"),
        )
        cat = self._layer(Axis.SCAN)
        result = self.backend.query_columns(
            Axis.TIME, [cont, cat], self.sel, width=300, height=200,
        )
        cont_r, cat_r = result.layers
        assert cont_r.categories is None and cont_r.peak_value is not None
        assert cat_r.categories is not None and cat_r.peak_value is None


# ---------------------------------------------------------------------------
# 8. render_layer through MSv4Backend -- parity with MSv2, including OPT-B
# ---------------------------------------------------------------------------

class TestColorizeByAxisRealDataMSv4:
    """Same real MS, converted to a ``.ps.zarr`` Processing Set (see
    ``create_test_msv4.py``) -- confirms Part 3's rendering pipeline is
    genuinely backend-agnostic (it only ever consumes the ``x``/``y``/
    identity columns both backends' ``query_columns`` already produce
    identically, per Part 2), not just coincidentally working for
    MSv2Backend.

    Also exercises ``_query_all_partitions_scatter_fused`` (OPT-B) --
    the one code path Part 2's own handoff flagged as NOT
    automatically kept in sync with the ordinary per-partition path
    (MSv2Backend has no equivalent). Forces it deterministically via
    ``_THRESH_FUSED``, the same technique
    ``test_opt_b_cross_partition_path_carries_the_same_columns`` in
    test_msv4_backend.py already uses.
    """

    def setup_method(self):
        _require_datashader()
        _suppress_warnings()
        self.backend = _open_backend_msv4()
        meta = self.backend.metadata()
        self.pol = meta["correlation_labels"][0]

    def teardown_method(self):
        self.backend.close()

    def _layer(self, axis, cmap=None):
        return ScatterLayerSpec(
            y_axis=Axis.AMPLITUDE, polarization=self.pol,
            cmap=cmap or palettes.categorical_cmap(theme="dark"),
            coloring="categorical", colorize_axis=axis,
        )

    @pytest.mark.parametrize("axis", list(COLORIZE_AXIS_COLUMNS))
    def test_every_colorizable_axis_renders_without_crashing(self, axis):
        t0, t1 = self.backend.metadata()["time_range"]
        sel = SelectionSpec(time_range=(t0, t0 + (t1 - t0) * 0.15),
                             channel_range=(0, 16))
        result = self.backend.query_columns(
            Axis.TIME, [self._layer(axis)], sel, width=300, height=200,
        ).layers[0]
        if result.skip_reason is None:
            assert result.categories is not None
            assert set(result.category_colors) == set(result.categories)
        else:
            assert isinstance(result.skip_reason, str)

    def test_colorize_by_axis_survives_the_opt_b_cross_partition_path(self):
        """The design doc's own warning: 'if Part 3 changes anything
        about how columns are threaded through
        _query_partition_scatter, check this method too -- nothing
        enforces the two stay in sync short of remembering to.' This
        is that check, at the rendering-pipeline layer rather than the
        raw-column layer test_msv4_backend.py's own OPT-B test already
        covers."""
        import cubevis.toolbox.visplot.data.msv4_backend as _be

        sel = SelectionSpec(channel_range=(0, 12))
        layer = self._layer(Axis.SCAN)
        orig_thresh = _be._THRESH_FUSED
        try:
            _be._THRESH_FUSED = 0  # force OPT-B (len(selected) > 1 here)
            result_optb = self.backend.query_columns(
                Axis.TIME, [layer], sel, width=200, height=150,
            ).layers[0]

            _be._THRESH_FUSED = 10 ** 12  # force ordinary per-partition path
            result_perpart = self.backend.query_columns(
                Axis.TIME, [layer], sel, width=200, height=150,
            ).layers[0]
        finally:
            _be._THRESH_FUSED = orig_thresh

        assert result_optb.skip_reason is None
        assert result_perpart.skip_reason is None
        assert result_optb.categories == result_perpart.categories
        assert result_optb.category_colors == result_perpart.category_colors
        assert result_optb.n_in_view == result_perpart.n_in_view


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
