"""
test_colorize_by_axis_part5a_priority.py -- Tests for the Part 5a
categorical-rendering pass: vectorized categorization, the selectable draw
priority ("rarest" / "majority"), opaque categorical layers, the legend's
layer labelling + priority caption, and the export caption.

Location in repository:
    cubevis/tests/manual/visplot/test_colorize_by_axis_part5a_priority.py

Tests against:
    cubevis/cubevis/toolbox/visplot/data/reader.py
        (CATEGORY_PRIORITIES, DEFAULT_CATEGORY_PRIORITY,
        ScatterLayerSpec.category_priority)
    cubevis/cubevis/toolbox/visplot/data/_scatter_render.py
        (_categorize, _resolve_categories, _priority_shade,
        _shade_categorical, render_layer's categorical branch)
    cubevis/cubevis/toolbox/visplot/visibility_scatter.py
        (ScatterLayer.category_priority, update_colorize,
        _collapse_and_composite, _full_legend_html, _panel_spec,
        colorize_controls, _handle_update_axes_scatter)
    cubevis/cubevis/toolbox/visplot/visibility_plotter.py
        (_make_scatter_layers, _colorize_key_from_override,
        _colorize_key_from_layer)
    cubevis/cubevis/toolbox/visplot/panel_spec.py
        (ColorBand.category_priority / priority_caption,
        CATEGORY_PRIORITY_CAPTIONS)
    cubevis/cubevis/toolbox/visplot/png_export.py
        (_legend_handles / _legend_handle_count / _band_key caption)

Run from the cubevis repository root:

    MS=sis14_twhya_calibrated_flagged.ms \\
        pytest cubevis/tests/manual/visplot/test_colorize_by_axis_part5a_priority.py -v

Sections
--------
1. Spec validation                  reader.ScatterLayerSpec / ScatterLayer
2. _categorize                      codes, population, exclusion, binning
3. Draw priority (render level)     independent NumPy reference for BOTH
                                    modes, global-vs-per-pixel semantics,
                                    zoom stability, ties, opacity
4. Export caption                   ColorBand / png_export
5. Widget level (real MS)           the first executed coverage of
                                    VisibilityScatter's colorize pieces --
                                    test_colorize_by_axis_part4_export.py's
                                    header lists them as reviewed-but-
                                    unverified
6. Plotter helper                   _make_scatter_layers
7. Change detection                 the "did the staged colorize state
                                    change?" comparison behind Plot,
                                    including the round-trip invariant
                                    that stops no-op presses re-rendering
"""
from __future__ import annotations

import asyncio
import os

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("datashader")

from cubevis.toolbox.visplot.axes import Axis
from cubevis.toolbox.visplot.selection import SelectionSpec
from cubevis.toolbox.visplot import palettes
from cubevis.toolbox.visplot import png_export as pe
from cubevis.toolbox.visplot.data import _scatter_render as sr
from cubevis.toolbox.visplot.data.reader import (
    CATEGORY_PRIORITIES, DEFAULT_CATEGORY_PRIORITY, ScatterLayerSpec,
)
from cubevis.toolbox.visplot.panel_spec import (
    CATEGORY_PRIORITY_CAPTIONS, ColorBand, PanelSpec, RenderedPanel,
)

CMAP = tuple(palettes.categorical_cmap(theme="dark"))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _spec(axis=Axis.SCAN, priority=DEFAULT_CATEGORY_PRIORITY, excluded=(),
          cmap=CMAP) -> ScatterLayerSpec:
    return ScatterLayerSpec(
        y_axis=Axis.AMPLITUDE, polarization="XX", cmap=cmap,
        coloring="categorical", colorize_axis=axis,
        excluded_categories=tuple(excluded), category_priority=priority,
    )


def _pixel_df(groups, seed=0) -> pd.DataFrame:
    """DataFrame from ``[(x, y, category, count), ...]``, rows shuffled so a
    result cannot depend on row order (draw order must be a property of the
    data's populations, never of how the rows happened to be laid out)."""
    parts = [
        pd.DataFrame({"x": [x] * n, "y": [y] * n, "scan_name": [c] * n})
        for x, y, c, n in groups
    ]
    df = pd.concat(parts, ignore_index=True)
    return df.sample(frac=1.0, random_state=seed).reset_index(drop=True)


def _render(df, priority, *, xr=(0.0, 11.0), yr=(0.0, 11.0), size=(11, 11),
            excluded=(), axis=Axis.SCAN):
    """render_layer on a canvas of *size* = (width, height)."""
    return sr.render_layer(
        df, _spec(axis, priority, excluded), xr[0], xr[1], yr[0], yr[1],
        size[0], size[1], "global", yr,
    )


def _rgb_of(result, category) -> int:
    r, g, b = sr._hex_to_rgb_uint8(result.category_colors[category])
    return r | (g << 8) | (b << 16)


def _pixel(result, row, col) -> int:
    return int(result.image[row, col]) & 0x00FFFFFF


# ---------------------------------------------------------------------------
# 1. Spec validation
# ---------------------------------------------------------------------------

class TestCategoryPrioritySpec:
    def test_default_is_rarest(self):
        assert DEFAULT_CATEGORY_PRIORITY == "rarest"
        assert _spec().category_priority == "rarest"

    def test_the_two_documented_values(self):
        assert CATEGORY_PRIORITIES == ("rarest", "majority")

    @pytest.mark.parametrize("value", CATEGORY_PRIORITIES)
    def test_both_values_accepted_on_a_categorical_layer(self, value):
        assert _spec(priority=value).category_priority == value

    def test_unknown_value_rejected(self):
        with pytest.raises(ValueError, match="category_priority"):
            _spec(priority="loudest")

    def test_accepted_on_a_continuous_layer_where_it_is_unused(self):
        # Unlike excluded_categories it has a default in BOTH modes, so a
        # non-default value on a continuous layer is not a distinguishable
        # mistake -- see ScatterLayerSpec's docstring.
        s = ScatterLayerSpec(y_axis=Axis.AMPLITUDE, polarization="XX",
                             cmap=("#000000", "#ffffff"),
                             category_priority="majority")
        assert s.coloring == "continuous"

    def test_unknown_value_rejected_on_a_continuous_layer_too(self):
        with pytest.raises(ValueError, match="category_priority"):
            ScatterLayerSpec(y_axis=Axis.AMPLITUDE, polarization="XX",
                             cmap=("#000000", "#ffffff"),
                             category_priority="nope")

    def test_spec_remains_frozen(self):
        s = _spec()
        with pytest.raises(Exception):
            s.category_priority = "majority"


# ---------------------------------------------------------------------------
# 2. _categorize
# ---------------------------------------------------------------------------

class TestCategorize:
    def test_missing_and_excluded_rows_get_bucket_minus_one(self):
        df = pd.DataFrame({"scan_name": ["1", "2", None, "2", "3", "1"]})
        cat = sr._categorize(df, "scan_name", "Scan", excluded=frozenset({"3"}))
        assert cat.skip_reason is None
        assert cat.categories == ["1", "2"]
        assert cat.bucket.tolist() == [0, 1, -1, 1, -1, 0]
        assert cat.bucket.dtype == np.int32

    def test_population_counts_only_drawn_rows(self):
        df = pd.DataFrame({"scan_name": ["1", "2", None, "2", "3", "1", "1"]})
        cat = sr._categorize(df, "scan_name", "Scan", excluded=frozenset({"3"}))
        assert cat.population.tolist() == [3, 2]
        assert int(cat.population.sum()) == int((cat.bucket >= 0).sum())

    def test_population_is_aligned_with_categories(self):
        df = pd.DataFrame({"scan_name": ["10"] * 5 + ["2"] * 7})
        cat = sr._categorize(df, "scan_name", "Scan")
        assert cat.categories == ["2", "10"]          # numeric, not lexicographic
        assert cat.population.tolist() == [7, 5]

    def test_binned_population_sums_each_buckets_members(self):
        counts = {f"DA{i:02d}": i + 1 for i in range(40)}
        df = pd.DataFrame({"a": [k for k, n in counts.items() for _ in range(n)]})
        cat = sr._categorize(df, "a", "Antenna 1")
        assert len(cat.categories) <= sr.CATEGORY_CAP
        for label, pop in zip(cat.categories, cat.population):
            assert pop == sum(counts[m] for m in cat.members[label])

    def test_mixed_int_and_str_values_merge(self):
        df = pd.DataFrame({"spw": [0, "0", 1, "1", 1]}, dtype=object)
        cat = sr._categorize(df, "spw", "SPW")
        assert cat.categories == ["0", "1"]
        assert cat.population.tolist() == [2, 3]

    def test_resolve_categories_wrapper_agrees_with_bucket(self):
        df = pd.DataFrame({"scan_name": ["1", None, "2", "2", "3"]})
        cat = sr._categorize(df, "scan_name", "Scan", excluded=frozenset({"1"}))
        mask, cats, members, reason = sr._resolve_categories(
            df, "scan_name", "Scan", excluded=frozenset({"1"}))
        assert reason is None
        assert mask.tolist() == (cat.bucket >= 0).tolist()
        assert cats == cat.categories and members == cat.members

    @pytest.mark.parametrize("df,excluded,fragment", [
        (pd.DataFrame({"other": [1]}), (), "no Scan data"),
        (pd.DataFrame({"scan_name": [None, None]}), (), "no Scan data"),
        (pd.DataFrame({"scan_name": ["1", "2"]}), ("1", "2"), "all Scan categories excluded"),
    ])
    def test_skip_reasons_keep_their_wording(self, df, excluded, fragment):
        cat = sr._categorize(df, "scan_name", "Scan", excluded=frozenset(excluded) or None)
        assert cat.categories is None and fragment in cat.skip_reason


# ---------------------------------------------------------------------------
# 3. Draw priority, at render level
# ---------------------------------------------------------------------------

class TestDrawPriority:
    """Every pixel is exactly one legend color; *which* one is what the
    priority decides.  Points sit on pixel centres of an 11x11 canvas over
    [0, 11)^2 so each named pixel is unambiguous; the centre pixel (5, 5) is
    the same in either image orientation."""

    def test_rarest_shows_the_rare_category_in_a_shared_pixel(self):
        df = _pixel_df([(5.5, 5.5, "A", 50), (5.5, 5.5, "B", 3)])
        rare = _render(df, "rarest")
        assert _pixel(rare, 5, 5) == _rgb_of(rare, "B")
        major = _render(df, "majority")
        assert _pixel(major, 5, 5) == _rgb_of(major, "A")

    def test_rarest_is_global_not_per_pixel(self):
        """In the shared pixel B outnumbers A 50:2, but B is the common
        category overall (another 5000 samples elsewhere).  'rarest' must
        follow the whole-selection population -- A -- not the local counts."""
        df = _pixel_df([(5.5, 5.5, "A", 2), (5.5, 5.5, "B", 50),
                        (1.5, 1.5, "B", 5000)])
        rare = _render(df, "rarest")
        assert _pixel(rare, 5, 5) == _rgb_of(rare, "A")
        major = _render(df, "majority")
        assert _pixel(major, 5, 5) == _rgb_of(major, "B")

    def test_rarest_order_does_not_change_when_zooming(self):
        """Inside the zoom window A has 500 samples and B only 5, so a
        population taken from the VIEWPORT would call B rarer and flip the
        pixel.  Globally B has another 1000 elsewhere, so A is the rare one
        and must stay on top at every zoom level."""
        df = _pixel_df([(5.5, 5.5, "A", 500), (5.5, 5.5, "B", 5),
                        (90.5, 90.5, "B", 1000)])
        spec = _spec(priority="rarest")
        zoom = sr.render_layer(df, spec, 5.0, 6.0, 5.0, 6.0, 4, 4, "global", (5.0, 6.0))
        occupied = np.argwhere(zoom.image != 0)
        assert len(occupied) == 1
        r, c = occupied[0]
        assert _pixel(zoom, r, c) == _rgb_of(zoom, "A")

    def test_ties_go_to_the_lower_sorted_category_regardless_of_row_order(self):
        for seed in (0, 1, 2):
            df = _pixel_df([(5.5, 5.5, "A", 10), (5.5, 5.5, "B", 10)], seed=seed)
            rare = _render(df, "rarest")
            assert _pixel(rare, 5, 5) == _rgb_of(rare, "A")

    def test_an_excluded_rare_category_is_not_drawn(self):
        df = _pixel_df([(5.5, 5.5, "A", 50), (5.5, 5.5, "B", 3)])
        res = _render(df, "rarest", excluded=("B",))
        assert res.categories == ("A",)
        assert _pixel(res, 5, 5) == _rgb_of(res, "A")

    @pytest.mark.parametrize("priority", CATEGORY_PRIORITIES)
    def test_every_occupied_pixel_is_fully_opaque_and_empty_ones_are_zero(self, priority):
        rng = np.random.default_rng(3)
        df = pd.DataFrame({
            "x": rng.uniform(0, 10, 4000), "y": rng.uniform(0, 10, 4000),
            "scan_name": rng.choice(list("ABCDE"), 4000),
        })
        res = _render(df, priority, xr=(0, 10), yr=(0, 10), size=(40, 30))
        occupied = res.image != 0
        assert occupied.any()
        assert ((res.image[occupied] >> 24) == 255).all()
        assert (res.image[~occupied] == 0).all()

    def test_priority_changes_which_color_never_which_pixels(self):
        rng = np.random.default_rng(4)
        df = pd.DataFrame({
            "x": rng.uniform(0, 10, 6000), "y": rng.uniform(0, 10, 6000),
            "scan_name": rng.choice(list("ABCD"), 6000, p=[.6, .25, .1, .05]),
        })
        kw = dict(xr=(0, 10), yr=(0, 10), size=(30, 20))
        rare, major = _render(df, "rarest", **kw), _render(df, "majority", **kw)
        assert np.array_equal(rare.image != 0, major.image != 0)
        assert rare.categories == major.categories
        assert rare.category_colors == major.category_colors
        legend = {_rgb_of(rare, c) for c in rare.categories}
        for res in (rare, major):
            px = res.image[res.image != 0] & 0x00FFFFFF
            assert set(np.unique(px).tolist()) <= legend
        # ...and the two modes really do differ on data like this.
        assert not np.array_equal(rare.image, major.image)

    @pytest.mark.parametrize("priority", CATEGORY_PRIORITIES)
    def test_matches_an_independent_numpy_reference(self, priority):
        """No shared code with the implementation: per-category 2-D
        histograms with NumPy, then the documented rule applied directly."""
        rng = np.random.default_rng(11)
        n, w, h, K = 30000, 25, 18, 5
        cats = np.array(list("ABCDE"))
        probs = np.array([.5, .3, .12, .06, .02])
        df = pd.DataFrame({
            "x": rng.uniform(0, 10, n), "y": rng.uniform(0, 10, n),
            "scan_name": cats[rng.choice(K, n, p=probs)],
        })
        res = _render(df, priority, xr=(0, 10), yr=(0, 10), size=(w, h))

        counts = np.zeros((h, w, K), dtype=np.int64)
        for k, c in enumerate(cats):
            sub = df[df.scan_name == c]
            hist, _, _ = np.histogram2d(sub.y, sub.x, bins=[h, w], range=[[0, 10], [0, 10]])
            counts[..., k] = hist
        population = counts.sum(axis=(0, 1))
        present = counts > 0
        if priority == "majority":
            expected = counts.argmax(-1)
        else:
            # rarest present; population ties -> lower index
            key = np.where(present, population[None, None, :] * (K + 1) + np.arange(K), np.iinfo(np.int64).max)
            expected = key.argmin(-1)
        want = np.zeros((h, w), dtype=np.int64)
        for k, c in enumerate(cats):
            want[expected == k] = _rgb_of(res, c)
        occ = present.any(-1)
        got = (res.image.astype(np.int64)) & 0x00FFFFFF
        assert np.array_equal(occ, res.image != 0)
        assert np.array_equal(got[occ], want[occ])

    def test_default_render_is_rarest(self):
        df = _pixel_df([(5.5, 5.5, "A", 50), (5.5, 5.5, "B", 3)])
        res = sr.render_layer(df, _spec(), 0.0, 11.0, 0.0, 11.0, 11, 11, "global", (0.0, 11.0))
        assert _pixel(res, 5, 5) == _rgb_of(res, "B")


# ---------------------------------------------------------------------------
# 4. Export caption
# ---------------------------------------------------------------------------

def _band(kind="categorical", priority=None, label="Amplitude XX", n=3):
    cats = tuple(f"S{i}" for i in range(n)) if kind == "categorical" else None
    return ColorBand(
        label=label, cmap=("#111111", "#222222"), scaling="linear", kind=kind,
        categories=cats,
        category_colors={c: "#123456" for c in cats} if cats else None,
        category_members={c: (c,) for c in cats} if cats else None,
        category_priority=priority,
    )


def _panel_spec(bands):
    return PanelSpec(
        kind="scatter", title="t", x_label="x", y_label="y",
        x_range=(0.0, 1.0), y_range=(0.0, 1.0), x_is_time=False,
        y_is_time=False, agg_n_x=10, agg_n_y=10, color_mode="global",
        bands=tuple(bands), theme="dark", status="ok",
    )


class TestExportCaption:
    def test_captions_exist_for_every_priority(self):
        assert set(CATEGORY_PRIORITY_CAPTIONS) == set(CATEGORY_PRIORITIES)

    @pytest.mark.parametrize("priority", CATEGORY_PRIORITIES)
    def test_priority_caption_text(self, priority):
        assert _band(priority=priority).priority_caption() == CATEGORY_PRIORITY_CAPTIONS[priority]

    @pytest.mark.parametrize("band", [
        _band(priority=None), _band(kind="density", priority="rarest"),
        _band(priority="bogus"),
    ])
    def test_no_caption_when_not_applicable(self, band):
        assert band.priority_caption() == ""

    def test_legend_gets_one_caption_entry_after_the_categories(self):
        handles = pe._legend_handles([_band(priority="rarest", n=3)], theme=None)
        labels = [h.get_label() for h in handles]
        assert labels == ["S0", "S1", "S2", CATEGORY_PRIORITY_CAPTIONS["rarest"]]

    def test_caption_is_prefixed_with_the_layer_when_several_bands_are_visible(self):
        bands = [_band(kind="density", label="Amplitude YY"),
                 _band(priority="majority", label="Amplitude XX", n=2)]
        labels = [h.get_label() for h in pe._legend_handles(bands, theme=None)]
        assert labels[-1] == "Amplitude XX: " + CATEGORY_PRIORITY_CAPTIONS["majority"]

    @pytest.mark.parametrize("bands", [
        [_band(priority="rarest", n=4)],
        [_band(priority=None, n=4)],
        [_band(kind="density"), _band(priority="rarest", n=5)],
        [_band(priority="rarest", label="A", n=2), _band(priority="majority", label="B", n=3)],
    ])
    def test_handle_count_mirrors_the_handles_actually_built(self, bands):
        assert pe._legend_handle_count(bands) == len(pe._legend_handles(bands, theme=None))

    def test_band_key_distinguishes_bands_that_differ_only_in_priority(self):
        a = pe._band_key(_panel_spec([_band(priority="rarest")]))
        b = pe._band_key(_panel_spec([_band(priority="majority")]))
        assert a != b

    def test_plain_band_key_keeps_its_original_shape(self):
        key = pe._band_key(_panel_spec([_band(kind="density")]))
        assert key == (("Amplitude XX", ("#111111", "#222222"), None),)

    def test_export_png_with_a_priority_caption(self, tmp_path):
        spec = _panel_spec([_band(priority="rarest", n=12)])
        img = np.full((200, 300), 0xFF000000, dtype=np.uint32)
        out = pe.export_png([RenderedPanel(spec=spec, image=img, viewport=None)],
                            str(tmp_path / "cap.png"))
        assert os.path.getsize(out) > 0


# ---------------------------------------------------------------------------
# 5. Widget level -- real MS, small selection
# ---------------------------------------------------------------------------

def _ms_path() -> str:
    path = os.environ.get("MS", "sis14_twhya_calibrated_flagged.ms")
    if not os.path.isdir(path):
        pytest.skip(f"Test MS not found at {path!r}; set MS=")
    return path


@pytest.fixture(scope="module")
def backend():
    from cubevis.toolbox.visplot.data.msv2_backend import MSv2Backend
    b = MSv2Backend(_ms_path())
    b.open()
    yield b
    b.close()


def _small_selection(backend) -> SelectionSpec:
    t0, t1 = backend.metadata()["time_range"]
    return SelectionSpec(time_range=(t0, t0 + (t1 - t0) * 0.06), channel_range=(0, 8))


def _widget(backend, layers):
    from cubevis.toolbox.visplot.visibility_scatter import VisibilityScatter
    return VisibilityScatter(
        backend=backend, selection=_small_selection(backend),
        x_axis=Axis.UVDIST, layers=layers, width=400, height=300,
    )


def _layer(backend, pol_index=0, **kw):
    from cubevis.toolbox.visplot.visibility_scatter import ScatterLayer
    pol = backend.metadata()["correlation_labels"][pol_index]
    return ScatterLayer(y_axis=Axis.AMPLITUDE, polarization=pol, **kw)


class TestScatterLayerPriority:
    def test_default_and_validation(self):
        from cubevis.toolbox.visplot.visibility_scatter import ScatterLayer
        assert ScatterLayer(y_axis=Axis.AMPLITUDE).category_priority == "rarest"
        with pytest.raises(ValueError, match="category_priority"):
            ScatterLayer(y_axis=Axis.AMPLITUDE, category_priority="x")


class TestWidgetColorizePriority:
    def test_update_colorize_sets_priority_and_rerenders(self, backend):
        vs = _widget(backend, [_layer(backend)])
        vs.update_colorize(0, coloring="categorical", colorize_axis="SCAN",
                           category_priority="majority")
        assert vs.layers[0].coloring == "categorical"
        assert vs.layers[0].category_priority == "majority"
        assert vs._layer_categories[0]                       # really rendered
        vs.update_colorize(0, category_priority="rarest")
        assert vs.layers[0].category_priority == "rarest"

    def test_omitting_priority_keeps_the_current_value(self, backend):
        vs = _widget(backend, [_layer(backend, coloring="categorical",
                                      colorize_axis=Axis.SCAN,
                                      category_priority="majority")])
        vs.update_colorize(0, colorize_axis="ANTENNA1")
        assert vs.layers[0].category_priority == "majority"

    def test_unknown_priority_is_rejected_before_any_render(self, backend):
        vs = _widget(backend, [_layer(backend)])
        with pytest.raises(ValueError, match="category_priority"):
            vs.update_colorize(0, coloring="categorical", category_priority="x")
        assert vs.layers[0].coloring == "continuous"          # nothing changed

    def test_priority_survives_a_trip_through_continuous(self, backend):
        vs = _widget(backend, [_layer(backend, coloring="categorical",
                                      colorize_axis=Axis.SCAN,
                                      category_priority="majority")])
        vs.update_colorize(0, coloring="continuous")
        assert vs.layers[0].category_priority == "majority"
        vs.update_colorize(0, coloring="categorical", colorize_axis="SCAN")
        assert vs.layers[0].category_priority == "majority"

    def test_priority_reaches_the_backend_spec(self, backend):
        vs = _widget(backend, [_layer(backend, coloring="categorical",
                                      colorize_axis=Axis.SCAN)])
        seen = []
        real = vs._backend.query_columns

        def spy(x_dim, layer_specs, *a, **k):
            seen.extend(s.category_priority for s in layer_specs)
            return real(x_dim, layer_specs, *a, **k)

        vs._backend.query_columns = spy
        try:
            vs.update_colorize(0, category_priority="majority")
        finally:
            del vs._backend.query_columns
        assert seen == ["majority"]

    def test_j2p_axis_change_message_parses_priority(self, backend):
        vs = _widget(backend, [_layer(backend)])
        pol = backend.metadata()["correlation_labels"][0]
        entry = {"y_axis": "AMPLITUDE", "polarization": pol,
                 "coloring": "categorical", "colorize_axis": "SCAN",
                 "category_priority": "majority"}
        asyncio.run(vs._handle_update_axes_scatter({"x_dim": "UVDIST", "layers": [entry]}))
        assert vs.layers[0].category_priority == "majority"
        del entry["category_priority"]                        # older sender
        asyncio.run(vs._handle_update_axes_scatter({"x_dim": "UVDIST", "layers": [entry]}))
        assert vs.layers[0].category_priority == "rarest"


class TestWidgetOpacity:
    """A categorical layer ignores the density-based auto_alpha; a continuous
    one still uses it.  ``_layer_n_in_view`` is set high to make the density
    alpha bite regardless of how small this test's selection is."""

    def _alphas(self, vs):
        comp = vs._collapse_and_composite()
        occupied = (comp >> 24) > 0
        return set(np.unique(comp[occupied] >> 24).tolist())

    def test_categorical_is_opaque_even_when_dense(self, backend):
        vs = _widget(backend, [_layer(backend, coloring="categorical",
                                      colorize_axis=Axis.SCAN)])
        vs._layer_n_in_view = [10 ** 8]
        assert self._alphas(vs) == {255}

    def test_continuous_is_still_dimmed_by_density(self, backend):
        vs = _widget(backend, [_layer(backend)])
        vs._layer_n_in_view = [10 ** 8]
        assert self._alphas(vs) == {80}                       # the auto_alpha floor

    def test_the_users_layer_alpha_still_applies_to_categorical(self, backend):
        vs = _widget(backend, [_layer(backend, coloring="categorical",
                                      colorize_axis=Axis.SCAN)])
        vs._layer_n_in_view = [10 ** 8]
        vs.set_alpha(0, 0.5)
        assert self._alphas(vs) == {127}
        vs.set_alpha(0, 0.0)
        assert self._alphas(vs) == set()                      # hidden


class TestWidgetLegend:
    def _mixed(self, backend):
        return _widget(backend, [
            _layer(backend, 0, coloring="categorical", colorize_axis=Axis.SCAN),
            _layer(backend, 1),
        ])

    def test_single_layer_panel_has_no_layer_label(self, backend):
        vs = _widget(backend, [_layer(backend, coloring="categorical",
                                      colorize_axis=Axis.SCAN)])
        html = vs._full_legend_html()
        assert html and vs.layers[0].label not in html

    def test_one_categorical_plus_one_continuous_layer_is_labelled(self, backend):
        """The screenshot case: previously an unlabelled legend beside a
        labelled colorbar."""
        vs = self._mixed(backend)
        assert vs.layers[0].label in vs._full_legend_html()

    def test_legend_states_the_draw_priority(self, backend):
        vs = _widget(backend, [_layer(backend, coloring="categorical",
                                      colorize_axis=Axis.SCAN)])
        assert CATEGORY_PRIORITY_CAPTIONS["rarest"] in vs._full_legend_html()
        vs.update_colorize(0, category_priority="majority")
        html = vs._full_legend_html()
        assert CATEGORY_PRIORITY_CAPTIONS["majority"] in html
        assert CATEGORY_PRIORITY_CAPTIONS["rarest"] not in html

    def test_continuous_only_panel_has_no_legend(self, backend):
        assert _widget(backend, [_layer(backend)])._full_legend_html() == ""


class TestWidgetPanelSpecAndControls:
    def test_band_carries_priority_only_for_categorical_layers(self, backend):
        vs = _widget(backend, [
            _layer(backend, 0, coloring="categorical", colorize_axis=Axis.SCAN,
                   category_priority="majority"),
            _layer(backend, 1),
        ])
        bands = vs._panel_spec().bands
        assert bands[0].category_priority == "majority"
        assert bands[0].priority_caption() == CATEGORY_PRIORITY_CAPTIONS["majority"]
        assert bands[1].category_priority is None

    def test_controls_expose_a_priority_select(self, backend):
        vs = _widget(backend, [_layer(backend, coloring="categorical",
                                      colorize_axis=Axis.SCAN,
                                      category_priority="majority")])
        _controls, handles = vs.colorize_controls(0)
        sel = handles["priority_select"]
        assert sel.value == "majority"
        assert [o[0] for o in sel.options] == list(CATEGORY_PRIORITIES)
        assert sel.visible is True

    def test_priority_select_is_hidden_for_a_continuous_layer(self, backend):
        vs = _widget(backend, [_layer(backend)])
        _controls, handles = vs.colorize_controls(0)
        assert handles["priority_select"].visible is False
        assert handles["priority_select"].value == "rarest"


# ---------------------------------------------------------------------------
# 6. Plotter helper
# ---------------------------------------------------------------------------

class TestMakeScatterLayers:
    def _make(self, overrides):
        from cubevis.toolbox.visplot.visibility_plotter import _make_scatter_layers
        return _make_scatter_layers(Axis.AMPLITUDE, ["XX", "YY"],
                                    colorize_overrides=overrides)

    def test_priority_from_the_payload_is_carried_onto_the_layer(self):
        layers = self._make([{"coloring": "categorical", "colorize_axis": "SCAN",
                              "category_priority": "majority"}, None])
        assert layers[0].category_priority == "majority"
        assert layers[1].coloring == "continuous"

    def test_missing_priority_defaults_to_rarest(self):
        layers = self._make([{"coloring": "categorical", "colorize_axis": "SCAN"}, None])
        assert layers[0].category_priority == "rarest"

    def test_a_continuous_layer_never_takes_a_priority(self):
        layers = self._make([{"coloring": "continuous",
                              "category_priority": "majority"}, None])
        assert layers[0].category_priority == "rarest"


class TestColorizeChangeDetection:
    """``_handle_plot`` re-renders only if the staged colorize state differs
    from the live layers'.  These helpers are that comparison."""

    def _keys(self):
        from cubevis.toolbox.visplot.visibility_plotter import (
            _colorize_key_from_layer, _colorize_key_from_override,
            _make_scatter_layers,
        )
        return _colorize_key_from_override, _colorize_key_from_layer, _make_scatter_layers

    CAT = {"coloring": "categorical", "colorize_axis": "SCAN",
           "excluded_categories": ["12"], "category_priority": "rarest"}

    def test_a_priority_only_change_is_a_change(self):
        key, _, _ = self._keys()
        assert key(self.CAT) != key({**self.CAT, "category_priority": "majority"})

    def test_a_categorical_override_without_a_priority_means_the_default(self):
        key, _, _ = self._keys()
        legacy = {k: v for k, v in self.CAT.items() if k != "category_priority"}
        assert key(legacy) == key(self.CAT)

    def test_priority_is_ignored_for_a_continuous_layer(self):
        key, _, _ = self._keys()
        assert (key({"coloring": "continuous", "category_priority": "majority"})
                == key(None) == ("continuous", None, (), None, None))

    @pytest.mark.parametrize("override", [
        None,
        {"coloring": "continuous"},
        {"coloring": "categorical", "colorize_axis": "SCAN"},
        {"coloring": "categorical", "colorize_axis": "ANTENNA1",
         "excluded_categories": ["DA42", "DV05"], "category_priority": "majority"},
        {"coloring": "categorical", "colorize_axis": "SPW", "category_priority": "rarest"},
    ])
    def test_layer_built_from_an_override_has_that_overrides_key(self, override):
        """If this breaks, a Plot press that changed nothing would compare
        unequal to the current layers and re-render every single time."""
        from_override, from_layer, make = self._keys()
        layer = make(Axis.AMPLITUDE, ["XX"], colorize_overrides=[override])[0]
        assert from_layer(layer) == from_override(override)

    def test_an_unchanged_press_is_not_a_change(self):
        from_override, from_layer, make = self._keys()
        overrides = [self.CAT, None]
        layers = make(Axis.AMPLITUDE, ["XX", "YY"], colorize_overrides=overrides)
        assert ([from_layer(l) for l in layers]
                == [from_override(o) for o in overrides])
