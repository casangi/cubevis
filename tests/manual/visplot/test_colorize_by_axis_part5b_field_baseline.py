"""
test_colorize_by_axis_part5b_field_baseline.py -- Tests for Part 5b: the
Field and Baseline colorize axes and "highlight mode" (unchecked values
shown in gray instead of hidden).

Location in repository:
    cubevis/tests/manual/visplot/test_colorize_by_axis_part5b_field_baseline.py

Tests against:
    data/reader.py            COLORIZE_AXIS_COLUMNS, EXCLUDED_DISPLAYS,
                              OTHER_CATEGORY_*, ScatterLayerSpec.excluded_display,
                              XArrayReader._identity_categoricals / _baseline_table
                              / _field_categories / _scan_lookup_for_partition
    data/_scatter_render.py   _categorize (categorical fast path, gray group),
                              _priority_shade (gray always loses)
    data/msv2_backend.py      field_name / baseline_name columns
    visibility_scatter.py     ScatterLayer.excluded_display, update_colorize,
                              _collapse_and_composite (alpha scaling), legend,
                              colorize_controls, _colorize_category_values
    visibility_plotter.py     _make_scatter_layers, change-detection keys

Run from the cubevis repository root:

    MS=sis14_twhya_calibrated_flagged.ms \\
        pytest cubevis/tests/manual/visplot/test_colorize_by_axis_part5b_field_baseline.py -v

Sections: 1 spec/constants  2 _categorize  3 gray shading  4 real MS columns
(checked against casacore, not against the code under test)  5 widget & UI
6 plotter.
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
from cubevis.toolbox.visplot.data import _scatter_render as sr
from cubevis.toolbox.visplot.data.reader import (
    COLORIZE_AXIS_COLUMNS, DEFAULT_EXCLUDED_DISPLAY, EXCLUDED_DISPLAYS,
    HIGH_CARDINALITY_THRESHOLD, OTHER_CATEGORY_ALPHA, OTHER_CATEGORY_COLOR,
    OTHER_CATEGORY_LABEL, ScatterLayerSpec, colorizable_axes,
)

CMAP = tuple(palettes.categorical_cmap(theme="dark"))


def _spec(axis=Axis.SCAN, excluded=(), display="hide", priority="rarest"):
    return ScatterLayerSpec(
        y_axis=Axis.AMPLITUDE, polarization="XX", cmap=CMAP,
        coloring="categorical", colorize_axis=axis,
        excluded_categories=tuple(excluded), excluded_display=display,
        category_priority=priority,
    )


def _df(groups, col="scan_name"):
    """``[(x, y, value, count), ...]`` -> shuffled DataFrame (see part5a)."""
    parts = [pd.DataFrame({"x": [x] * n, "y": [y] * n, col: [v] * n})
             for x, y, v, n in groups]
    return (pd.concat(parts, ignore_index=True)
            .sample(frac=1.0, random_state=0).reset_index(drop=True))


def _render(df, spec, size=(11, 11)):
    return sr.render_layer(df, spec, 0.0, 11.0, 0.0, 11.0, size[0], size[1],
                           "global", (0.0, 11.0))


def _rgb(result, category):
    r, g, b = sr._hex_to_rgb_uint8(result.category_colors[category])
    return r | (g << 8) | (b << 16)


# ---------------------------------------------------------------------------
# 1. Spec / constants
# ---------------------------------------------------------------------------

class TestSpecAndConstants:
    def test_field_and_baseline_are_colorizable(self):
        assert COLORIZE_AXIS_COLUMNS[Axis.FIELD] == "field_name"
        assert COLORIZE_AXIS_COLUMNS[Axis.BASELINE] == "baseline_name"
        assert {Axis.FIELD, Axis.BASELINE} <= set(colorizable_axes())

    def test_excluded_display_default_and_values(self):
        assert EXCLUDED_DISPLAYS == ("hide", "gray")
        assert DEFAULT_EXCLUDED_DISPLAY == "hide"
        assert _spec().excluded_display == "hide"

    @pytest.mark.parametrize("value", EXCLUDED_DISPLAYS)
    def test_both_values_accepted(self, value):
        assert _spec(display=value).excluded_display == value

    def test_unknown_value_rejected(self):
        with pytest.raises(ValueError, match="excluded_display"):
            _spec(display="blur")

    def test_the_high_cardinality_threshold_equals_the_render_cap(self):
        """The GUI's notion of "too many to color one by one" and the
        renderer's binning cap must be the same number."""
        assert HIGH_CARDINALITY_THRESHOLD == sr.CATEGORY_CAP

    def test_the_gray_is_not_a_palette_color(self):
        assert OTHER_CATEGORY_COLOR not in CMAP
        assert 0 < OTHER_CATEGORY_ALPHA < 255


# ---------------------------------------------------------------------------
# 2. _categorize: categorical fast path and the gray group
# ---------------------------------------------------------------------------

def _cat_df(values, categories):
    return pd.DataFrame({"c": pd.Categorical(values, categories=categories)})


class TestCategorizeCategorical:
    def test_reads_the_codes_and_matches_the_string_path(self):
        vals = ["b", "a", None, "c", "a", "b", "b"]
        as_cat = sr._categorize(_cat_df(vals, ["a", "b", "c", "d"]), "c", "X")
        as_str = sr._categorize(pd.DataFrame({"c": pd.Series(vals, dtype=object)}), "c", "X")
        assert as_cat.categories == as_str.categories == ["a", "b", "c"]
        assert as_cat.bucket.tolist() == as_str.bucket.tolist()
        assert as_cat.population.tolist() == as_str.population.tolist() == [2, 3, 1]

    def test_categories_with_no_rows_never_reach_the_legend(self):
        cat = sr._categorize(_cat_df(["a", "a"], ["a", "unused1", "unused2"]), "c", "X")
        assert cat.categories == ["a"]

    def test_all_missing_is_a_skip_not_a_crash(self):
        cat = sr._categorize(_cat_df([None, None], ["a", "b"]), "c", "X")
        assert cat.skip_reason and "no X data" in cat.skip_reason

    def test_exclusion_and_binning_work_on_categoricals(self):
        names = [f"B{i:03d}" for i in range(60)]
        df = _cat_df(names * 3, names)
        cat = sr._categorize(df, "c", "Baseline", excluded=frozenset(names[:10]))
        assert (cat.bucket == -1).sum() == 30                # 10 values x 3 rows
        assert len(cat.categories) <= sr.CATEGORY_CAP


class TestCategorizeGrayGroup:
    DF = pd.DataFrame({"scan_name": ["1"] * 5 + ["2"] * 3 + ["3"] * 2 + [None]})

    def test_hide_mode_drops_excluded_rows(self):
        cat = sr._categorize(self.DF, "scan_name", "Scan", excluded=frozenset({"3"}))
        assert cat.categories == ["1", "2"] and cat.other_index is None
        assert (cat.bucket == -1).sum() == 3                 # 2 excluded + 1 missing

    def test_gray_mode_gathers_them_into_a_last_category(self):
        cat = sr._categorize(self.DF, "scan_name", "Scan",
                             excluded=frozenset({"3"}), show_excluded=True)
        assert cat.categories == ["1", "2", OTHER_CATEGORY_LABEL]
        assert cat.other_index == 2
        assert cat.members[OTHER_CATEGORY_LABEL] == ("3",)
        assert cat.population.tolist() == [5, 3, 2]
        assert (cat.bucket == -1).sum() == 1                 # only the missing row

    def test_missing_values_are_never_gray(self):
        cat = sr._categorize(self.DF, "scan_name", "Scan",
                             excluded=frozenset({"3"}), show_excluded=True)
        assert cat.bucket[-1] == -1

    def test_nothing_excluded_present_means_no_gray_group(self):
        cat = sr._categorize(self.DF, "scan_name", "Scan",
                             excluded=frozenset({"nope"}), show_excluded=True)
        assert cat.other_index is None and OTHER_CATEGORY_LABEL not in cat.categories

    def test_everything_unchecked_is_a_valid_all_gray_render_in_gray_mode(self):
        cat = sr._categorize(self.DF, "scan_name", "Scan",
                             excluded=frozenset({"1", "2", "3"}), show_excluded=True)
        assert cat.skip_reason is None
        assert cat.categories == [OTHER_CATEGORY_LABEL] and cat.other_index == 0

    def test_everything_unchecked_is_still_a_skip_in_hide_mode(self):
        cat = sr._categorize(self.DF, "scan_name", "Scan",
                             excluded=frozenset({"1", "2", "3"}))
        assert cat.skip_reason == "all Scan categories excluded"

    def test_gray_group_does_not_count_toward_the_cap(self):
        names = [f"S{i:03d}" for i in range(50)]
        df = pd.DataFrame({"scan_name": names * 2})
        cat = sr._categorize(df, "scan_name", "Scan", cap=20,
                             excluded=frozenset(names[:5]), show_excluded=True)
        assert len(cat.categories) == 21 and cat.categories[-1] == OTHER_CATEGORY_LABEL
        assert set(cat.members[OTHER_CATEGORY_LABEL]) == set(names[:5])


# ---------------------------------------------------------------------------
# 3. Gray context in the rendered image
# ---------------------------------------------------------------------------

class TestGrayRender:
    def test_a_highlighted_category_beats_any_number_of_gray_samples(self):
        """5000 gray samples share a pixel with ONE highlighted sample: the
        pixel must show the highlight, in both draw priorities."""
        df = _df([(5.5, 5.5, "A", 1), (5.5, 5.5, "B", 5000)])
        for prio in ("rarest", "majority"):
            res = _render(df, _spec(excluded=("B",), display="gray", priority=prio))
            assert int(res.image[5, 5]) & 0x00FFFFFF == _rgb(res, "A"), prio
            assert (int(res.image[5, 5]) >> 24) == 255

    def test_a_gray_only_pixel_is_dim_gray(self):
        df = _df([(5.5, 5.5, "A", 3), (1.5, 1.5, "B", 40)])
        res = _render(df, _spec(excluded=("B",), display="gray"))
        gray = [int(v) for v in res.image[res.image != 0]
                if (int(v) >> 24) == OTHER_CATEGORY_ALPHA]
        assert len(gray) == 1
        r, g, b = sr._hex_to_rgb_uint8(OTHER_CATEGORY_COLOR)
        assert gray[0] & 0x00FFFFFF == (r | (g << 8) | (b << 16))

    def test_hide_mode_draws_nothing_for_excluded_values(self):
        df = _df([(5.5, 5.5, "A", 3), (1.5, 1.5, "B", 40)])
        res = _render(df, _spec(excluded=("B",), display="hide"))
        assert int((res.image != 0).sum()) == 1

    def test_legend_data_lists_the_gray_group_last_with_its_members(self):
        df = _df([(5.5, 5.5, "A", 3), (1.5, 1.5, "B", 40), (2.5, 2.5, "C", 4)])
        res = _render(df, _spec(excluded=("B", "C"), display="gray"))
        assert res.categories == ("A", OTHER_CATEGORY_LABEL)
        assert res.category_colors[OTHER_CATEGORY_LABEL] == OTHER_CATEGORY_COLOR
        assert res.category_members[OTHER_CATEGORY_LABEL] == ("B", "C")
        assert set(res.category_colors) == set(res.categories)

    def test_all_gray_when_nothing_is_highlighted(self):
        df = _df([(5.5, 5.5, "A", 3), (1.5, 1.5, "B", 4)])
        res = _render(df, _spec(excluded=("A", "B"), display="gray"))
        assert res.skip_reason is None
        assert res.categories == (OTHER_CATEGORY_LABEL,)
        assert {int(v) >> 24 for v in res.image[res.image != 0]} == {OTHER_CATEGORY_ALPHA}

    def test_the_highlight_colors_are_the_same_with_and_without_gray(self):
        """Turning gray on must not recolor what is highlighted."""
        df = _df([(5.5, 5.5, "A", 3), (1.5, 1.5, "B", 40), (8.5, 8.5, "C", 9)])
        hide = _render(df, _spec(excluded=("B",), display="hide"))
        gray = _render(df, _spec(excluded=("B",), display="gray"))
        assert hide.category_colors["A"] == gray.category_colors["A"]
        assert hide.category_colors["C"] == gray.category_colors["C"]
        occ = (hide.image != 0)
        assert np.array_equal(hide.image[occ], gray.image[occ])


# ---------------------------------------------------------------------------
# 4. Real MS: Field and Baseline columns, checked against casacore
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


@pytest.fixture(scope="module")
def truth():
    casacore = pytest.importorskip("casacore.tables")
    ms = _ms_path()
    t = casacore.table(ms, ack=False)
    fld = casacore.table(ms + "/FIELD", ack=False).getcol("NAME")
    ant = casacore.table(ms + "/ANTENNA", ack=False).getcol("NAME")
    return dict(scan=t.getcol("SCAN_NUMBER"), field=t.getcol("FIELD_ID"),
                a1=t.getcol("ANTENNA1"), a2=t.getcol("ANTENNA2"),
                field_names=fld, ant_names=ant)


@pytest.fixture(scope="module")
def frame(backend):
    sel = SelectionSpec(scan=["12", "14"], channel_range=(0, 4))
    return backend._query_columns_raw(
        Axis.UVDIST, [(Axis.AMPLITUDE, "XX")], sel)[(Axis.AMPLITUDE, "XX")]


class TestRealColumns:
    def test_columns_are_categorical_not_strings(self, frame):
        assert isinstance(frame["field_name"].dtype, pd.CategoricalDtype)
        assert isinstance(frame["baseline_name"].dtype, pd.CategoricalDtype)

    def test_no_row_is_missing_a_field_or_baseline(self, frame):
        assert not frame["field_name"].isna().any()
        assert not frame["baseline_name"].isna().any()

    def test_field_per_scan_matches_casacore(self, frame, truth):
        for scan in (12, 14):
            want = {truth["field_names"][i] for i in truth["field"][truth["scan"] == scan]}
            got = set(frame.loc[frame["scan_name"] == str(scan), "field_name"].astype(str))
            assert got == want, scan

    def test_baseline_labels_match_casacore(self, frame, truth):
        rows = truth["scan"] == 12
        want = {f"{truth['ant_names'][a]}&{truth['ant_names'][b]}"
                for a, b in zip(truth["a1"][rows], truth["a2"][rows])}
        got = set(frame.loc[frame["scan_name"] == "12", "baseline_name"].astype(str))
        assert got == want

    def test_baseline_label_agrees_with_the_two_antenna_columns(self, frame):
        # (Part 6b: the antenna columns are Categoricals now, which cannot be
        # concatenated with "&" directly -- compare as strings.)
        want = (frame["baseline_antenna1_name"].astype(str) + "&"
                + frame["baseline_antenna2_name"].astype(str))
        assert (frame["baseline_name"].astype(str) == want).all()

    def test_every_colorizable_axis_renders_from_the_real_frame(self, frame):
        x0, x1 = float(frame.x.min()), float(frame.x.max())
        y0, y1 = float(frame.y.min()), float(frame.y.max())
        for axis in (Axis.FIELD, Axis.BASELINE):
            res = sr.render_layer(frame, _spec(axis), x0, x1, y0, y1, 200, 150,
                                  "global", (y0, y1))
            assert res.skip_reason is None, axis
            assert res.categories

    def test_baseline_cardinality_is_binned_to_the_cap(self, frame):
        x0, x1 = float(frame.x.min()), float(frame.x.max())
        y0, y1 = float(frame.y.min()), float(frame.y.max())
        res = sr.render_layer(frame, _spec(Axis.BASELINE), x0, x1, y0, y1, 200, 150,
                              "global", (y0, y1))
        assert len(res.categories) == sr.CATEGORY_CAP
        assert sum(len(m) for m in res.category_members.values()) == frame["baseline_name"].nunique()

    def test_highlighting_one_antennas_baselines_gives_one_color_each(self, frame):
        """The workflow this feature exists for: pick an antenna, see its
        baselines colored and the rest as gray context."""
        labels = sorted(frame["baseline_name"].astype(str).unique())
        pick = "DV18"
        mine = [b for b in labels if pick in b.split("&")]
        others = tuple(b for b in labels if b not in mine)
        assert 0 < len(mine) <= sr.CATEGORY_CAP
        x0, x1 = float(frame.x.min()), float(frame.x.max())
        y0, y1 = float(frame.y.min()), float(frame.y.max())
        res = sr.render_layer(frame, _spec(Axis.BASELINE, excluded=others, display="gray"),
                              x0, x1, y0, y1, 200, 150, "global", (y0, y1))
        assert set(res.categories) == set(mine) | {OTHER_CATEGORY_LABEL}
        assert all(res.category_members[b] == (b,) for b in mine)     # not binned

    def test_partition_lookup_carries_field_codes(self, backend):
        for raw in backend._iter_visibility_partitions():
            lk = backend._scan_lookup_for_partition(raw)
            assert lk is not None and lk.field_codes is not None
            assert len(lk.field_codes) == len(lk.time_values)
            break

    def test_categories_are_shared_across_partitions(self, backend):
        """MS-wide category lists are what let per-partition frames
        concatenate without losing the categorical dtype."""
        assert backend._field_categories() is backend._field_categories()
        code_of_bid, labels = backend._baseline_table()
        assert len(set(labels.tolist())) == len(labels)             # unique


# ---------------------------------------------------------------------------
# 5. Widget and UI
# ---------------------------------------------------------------------------

def _small_selection(backend):
    # Two scans on purpose: highlighting needs at least two values to
    # separate (the first slice of the MS holds only scan 4).
    return SelectionSpec(scan=["12", "14"], channel_range=(0, 8))


def _widget(backend, layers):
    from cubevis.toolbox.visplot.visibility_scatter import VisibilityScatter
    return VisibilityScatter(backend=backend, selection=_small_selection(backend),
                             x_axis=Axis.UVDIST, layers=layers, width=400, height=300)


def _layer(backend, pol_index=0, **kw):
    from cubevis.toolbox.visplot.visibility_scatter import ScatterLayer
    pol = backend.metadata()["correlation_labels"][pol_index]
    return ScatterLayer(y_axis=Axis.AMPLITUDE, polarization=pol, **kw)


class TestWidgetHighlight:
    def test_layer_default_and_validation(self):
        from cubevis.toolbox.visplot.visibility_scatter import ScatterLayer
        assert ScatterLayer(y_axis=Axis.AMPLITUDE).excluded_display == "hide"
        with pytest.raises(ValueError, match="excluded_display"):
            ScatterLayer(y_axis=Axis.AMPLITUDE, excluded_display="x")

    def test_update_colorize_sets_display_and_renders_gray(self, backend):
        vs = _widget(backend, [_layer(backend)])
        vs.update_colorize(0, coloring="categorical", colorize_axis="SCAN",
                           excluded_categories=[], excluded_display="gray")
        vals = vs._colorize_category_values(Axis.SCAN, vs.layers[0].polarization)
        assert len(vals) >= 2
        vs.update_colorize(0, excluded_categories=vals[1:])
        assert vs.layers[0].excluded_display == "gray"
        assert OTHER_CATEGORY_LABEL in vs._layer_categories[0]

    def test_unknown_display_rejected_before_any_change(self, backend):
        vs = _widget(backend, [_layer(backend)])
        with pytest.raises(ValueError, match="excluded_display"):
            vs.update_colorize(0, coloring="categorical", excluded_display="x")
        assert vs.layers[0].coloring == "continuous"

    def test_omitting_display_keeps_it(self, backend):
        vs = _widget(backend, [_layer(backend, coloring="categorical",
                                      colorize_axis=Axis.SCAN, excluded_display="gray")])
        vs.update_colorize(0, colorize_axis="FIELD")
        assert vs.layers[0].excluded_display == "gray"

    def test_j2p_message_parses_display_and_defaults_when_absent(self, backend):
        vs = _widget(backend, [_layer(backend)])
        pol = backend.metadata()["correlation_labels"][0]
        entry = {"y_axis": "AMPLITUDE", "polarization": pol, "coloring": "categorical",
                 "colorize_axis": "SCAN", "excluded_display": "gray"}
        asyncio.run(vs._handle_update_axes_scatter({"x_dim": "UVDIST", "layers": [entry]}))
        assert vs.layers[0].excluded_display == "gray"
        del entry["excluded_display"]
        asyncio.run(vs._handle_update_axes_scatter({"x_dim": "UVDIST", "layers": [entry]}))
        assert vs.layers[0].excluded_display == "hide"

    def test_display_reaches_the_backend_spec(self, backend):
        vs = _widget(backend, [_layer(backend, coloring="categorical", colorize_axis=Axis.SCAN)])
        seen, real = [], vs._backend.query_columns

        def spy(x_dim, layer_specs, *a, **k):
            seen.extend(s.excluded_display for s in layer_specs)
            return real(x_dim, layer_specs, *a, **k)

        vs._backend.query_columns = spy
        try:
            vs.update_colorize(0, excluded_display="gray")
        finally:
            del vs._backend.query_columns
        assert seen == ["gray"]


class TestWidgetGrayCompositing:
    def _alphas(self, vs):
        comp = vs._collapse_and_composite()
        return {int(a) for a in np.unique(comp[(comp >> 24) > 0] >> 24)}

    def _gray_widget(self, backend):
        vs = _widget(backend, [_layer(backend, coloring="categorical", colorize_axis=Axis.SCAN)])
        vals = vs._colorize_category_values(Axis.SCAN, vs.layers[0].polarization)
        vs.update_colorize(0, excluded_categories=vals[1:], excluded_display="gray")
        return vs

    def test_context_stays_dimmer_than_the_highlight(self, backend):
        vs = self._gray_widget(backend)
        assert self._alphas(vs) <= {255, OTHER_CATEGORY_ALPHA}
        assert OTHER_CATEGORY_ALPHA in self._alphas(vs)

    def test_layer_alpha_scales_both(self, backend):
        vs = self._gray_widget(backend)
        vs.set_alpha(0, 0.5)
        assert self._alphas(vs) <= {127, (OTHER_CATEGORY_ALPHA * 127) // 255}

    def test_a_plain_categorical_layer_is_still_uniformly_opaque(self, backend):
        vs = _widget(backend, [_layer(backend, coloring="categorical", colorize_axis=Axis.SCAN)])
        assert self._alphas(vs) == {255}


class TestWidgetLegendAndEnumeration:
    def test_legend_lists_the_gray_group(self, backend):
        vs = TestWidgetGrayCompositing()._gray_widget(backend)
        assert OTHER_CATEGORY_LABEL in vs._full_legend_html()

    @pytest.mark.parametrize("axis", [Axis.FIELD, Axis.BASELINE])
    def test_new_axes_enumerate_their_values(self, backend, axis):
        vs = _widget(backend, [_layer(backend)])
        vals = vs._colorize_category_values(axis, vs.layers[0].polarization)
        assert vals
        if axis is Axis.BASELINE:
            assert all("&" in v for v in vals)

    def test_baseline_checklist_values_equal_the_backend_labels(self, backend, frame):
        vs = _widget(backend, [_layer(backend)])
        vals = set(vs._colorize_category_values(Axis.BASELINE, vs.layers[0].polarization))
        assert set(frame["baseline_name"].astype(str)) <= vals

    @pytest.mark.parametrize("axis", [Axis.FIELD, Axis.BASELINE])
    def test_new_axes_render_through_the_widget(self, backend, axis):
        vs = _widget(backend, [_layer(backend, coloring="categorical", colorize_axis=axis)])
        assert vs._layer_categories[0]


class TestControls:
    def _controls(self, backend, **kw):
        vs = _widget(backend, [_layer(backend, **kw)])
        return vs, vs.colorize_controls(0)

    def test_display_select_exists_and_follows_the_layer(self, backend):
        _vs, (_c, h) = self._controls(backend, coloring="categorical",
                                      colorize_axis=Axis.SCAN, excluded_display="gray")
        assert h["display_select"].value == "gray"
        assert [o[0] for o in h["display_select"].options] == list(EXCLUDED_DISPLAYS)
        assert h["display_select"].visible is True

    def test_display_select_hidden_for_continuous(self, backend):
        _vs, (_c, h) = self._controls(backend)
        assert h["display_select"].visible is False

    def test_axis_picker_offers_field_and_baseline(self, backend):
        _vs, (_c, h) = self._controls(backend)
        offered = {o[0] for o in h["axis_select"].options}
        assert {"FIELD", "BASELINE"} <= offered

    def test_every_axis_starts_with_everything_checked(self, backend):
        """Part 5c: including a high-cardinality axis (an earlier draft started
        Baseline empty, which left a blank canvas on a single layer)."""
        _vs, (_c, h) = self._controls(backend)
        group, _wrapper = h["checklists"]["BASELINE"]
        assert len(group.labels) > HIGH_CARDINALITY_THRESHOLD
        for name, (g, _w) in h["checklists"].items():
            assert g.active == list(range(len(g.labels))), name

    def test_unselected_values_default_to_hide(self, backend):
        _vs, (_c, h) = self._controls(backend)
        assert h["display_select"].value == "hide"

    def test_switching_axis_only_swaps_the_visible_checklist(self, backend):
        """No JS side effect on "Unselected values" (an earlier draft forced
        gray on a high-cardinality axis)."""
        _vs, (_c, h) = self._controls(backend)
        cbs = h["axis_select"].js_property_callbacks["change:value"]
        assert [set(cb.args) for cb in cbs] == [{"checklist_by_axis"}]
        assert not any("gray" in cb.code for cb in cbs)

    def test_high_cardinality_axes_carry_a_hint_that_they_share_the_palette(self, backend):
        from bokeh.models import Div
        _vs, (_c, h) = self._controls(backend)
        _g, wrapper = h["checklists"]["BASELINE"]
        text = " ".join(c.text for c in wrapper.children if isinstance(c, Div))
        assert f"share {HIGH_CARDINALITY_THRESHOLD} colors" in text
        _g, small = h["checklists"]["SCAN"]
        assert "share" not in " ".join(c.text for c in small.children if isinstance(c, Div))

    def test_baseline_checklist_carries_the_antenna_picker(self, backend):
        from bokeh.models import Select
        _vs, (_c, h) = self._controls(backend)
        _group, wrapper = h["checklists"]["BASELINE"]
        pickers = [c for c in wrapper.children if isinstance(c, Select)]
        assert len(pickers) == 1 and "antenna" in (pickers[0].title or "").lower()
        assert {o[0] for o in pickers[0].options} >= {"DV18", "DA42"}

    def test_small_axes_have_no_antenna_picker(self, backend):
        from bokeh.models import Select
        _vs, (_c, h) = self._controls(backend)
        _g, wrapper = h["checklists"]["SCAN"]
        assert not [c for c in wrapper.children if isinstance(c, Select)]


# ---------------------------------------------------------------------------
# 6. Plotter
# ---------------------------------------------------------------------------

class TestPlotterKeys:
    def _fns(self):
        from cubevis.toolbox.visplot.visibility_plotter import (
            _colorize_key_from_layer, _colorize_key_from_override, _make_scatter_layers)
        return _colorize_key_from_override, _colorize_key_from_layer, _make_scatter_layers

    CAT = {"coloring": "categorical", "colorize_axis": "BASELINE",
           "excluded_categories": ["DA41&DA42"], "category_priority": "rarest",
           "excluded_display": "gray"}

    def test_a_display_only_change_is_a_change(self):
        key, _, _ = self._fns()
        assert key(self.CAT) != key({**self.CAT, "excluded_display": "hide"})

    def test_missing_display_means_hide(self):
        key, _, _ = self._fns()
        legacy = {k: v for k, v in self.CAT.items() if k != "excluded_display"}
        assert key(legacy) == key({**self.CAT, "excluded_display": "hide"})

    def test_display_is_ignored_for_a_continuous_layer(self):
        key, _, _ = self._fns()
        assert key({"coloring": "continuous", "excluded_display": "gray"}) == key(None)

    @pytest.mark.parametrize("override", [
        None, {"coloring": "categorical", "colorize_axis": "FIELD"},
        {"coloring": "categorical", "colorize_axis": "BASELINE", "excluded_display": "gray",
         "excluded_categories": ["a&b"], "category_priority": "majority"},
    ])
    def test_round_trip_layer_key_equals_override_key(self, override):
        from_override, from_layer, make = self._fns()
        layer = make(Axis.AMPLITUDE, ["XX"], colorize_overrides=[override])[0]
        assert from_layer(layer) == from_override(override)

    def test_display_is_carried_onto_the_layer(self):
        _, _, make = self._fns()
        layer = make(Axis.AMPLITUDE, ["XX"], colorize_overrides=[self.CAT])[0]
        assert layer.excluded_display == "gray" and layer.colorize_axis is Axis.BASELINE


# ---------------------------------------------------------------------------
# 7. Only baselines that exist (reported: ticking DA41 baselines -> all gray)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def observed(truth):
    """Baselines / antennas that really have rows, straight from casacore."""
    names = list(truth["ant_names"])
    pairs = {(names[a], names[b]) for a, b in zip(truth["a1"], truth["a2"])}
    return dict(names=names, pairs=pairs,
                antennas={n for p in pairs for n in p},
                labels={f"{a}&{b}" for a, b in pairs})


class TestBaselinesThatExist:
    def test_identity_tables_report_the_baselines_that_have_rows(self, backend, observed):
        t = backend.identity_tables(SelectionSpec(), polarization="XX")
        got = {t.baseline_antennas[b] for b in t.baselines_with_data}
        assert got == observed["pairs"]

    def test_hover_dict_is_untouched(self, backend, observed):
        """``baseline_antennas`` also feeds the hover probe: it must still
        list every antenna pair the grid holds, phantoms included."""
        t = backend.identity_tables(SelectionSpec(), polarization="XX")
        n = len(observed["names"])
        assert len(t.baseline_antennas) == n * (n - 1) // 2 > len(observed["pairs"])

    def test_presence_follows_the_selection(self, backend, truth):
        names = list(truth["ant_names"])
        rows = truth["scan"] == 12
        want = {(names[a], names[b]) for a, b in zip(truth["a1"][rows], truth["a2"][rows])}
        t = backend.identity_tables(SelectionSpec(scan=["12"]), polarization="XX")
        assert {t.baseline_antennas[b] for b in t.baselines_with_data} == want

    def test_the_helper_needs_time_centroid_and_never_raises(self):
        import xarray as xr
        from cubevis.toolbox.visplot.data.reader import XArrayReader
        f = XArrayReader._baseline_ids_with_data
        nan = np.nan
        ds = xr.Dataset(
            {"TIME_CENTROID": (("time", "baseline_id"), np.array([[1.0, nan, 3.0], [2.0, nan, nan]]))},
            coords={"time": [0, 1], "baseline_id": [10, 11, 12]})
        assert f(ds).tolist() == [10, 12]
        assert f(ds.drop_vars("TIME_CENTROID")) is None
        bad = xr.Dataset({"TIME_CENTROID": (("time",), [1.0, 2.0])},
                         coords={"time": [0, 1], "baseline_id": ("time", [1, 2])})
        assert f(bad) is None
        assert f(object()) is None                              # never raises


class TestChecklistsOfferOnlyRealValues:
    def _vs(self, backend):
        return _widget(backend, [_layer(backend)])

    def test_baseline_checklist_has_only_observed_baselines(self, backend, observed):
        vs = self._vs(backend)
        vals = vs._colorize_category_values(Axis.BASELINE, vs.layers[0].polarization)
        # selection is scans 12+14 with 8 channels: a subset of the MS's baselines
        assert set(vals) <= observed["labels"] and vals
        assert not any("DA41" in v.split("&") for v in vals)

    @pytest.mark.parametrize("axis", [Axis.ANTENNA1, Axis.ANTENNA2])
    def test_antenna_checklists_skip_antennas_without_data(self, backend, observed, axis):
        vs = self._vs(backend)
        vals = set(vs._colorize_category_values(axis, vs.layers[0].polarization))
        assert vals <= observed["antennas"]
        assert not vals & {"DA41", "DV01", "DV04", "DV07", "DV21"}

    def test_the_antenna_picker_skips_antennas_without_data(self, backend):
        vs = self._vs(backend)
        _c, h = vs.colorize_controls(0)
        _g, wrapper = h["checklists"]["BASELINE"]
        from bokeh.models import Select
        picker = [c for c in wrapper.children if isinstance(c, Select)][0]
        offered = {o[0] for o in picker.options} - {""}
        assert offered and not offered & {"DA41", "DV01", "DV04", "DV07", "DV21"}

    def test_unknown_presence_filters_nothing(self, backend):
        import dataclasses
        vs = self._vs(backend)
        pol = vs.layers[0].polarization
        real = vs._colorize_category_values(Axis.BASELINE, pol)
        tables = dataclasses.replace(vs._ensure_identity_tables(pol), baselines_with_data=None)
        vs._ensure_identity_tables = lambda p: tables
        assert len(vs._colorize_category_values(Axis.BASELINE, pol)) > len(real)


class TestNoDataNote:
    """The legend says so when a CHECKED value drew nothing."""

    def _vs(self, backend, **kw):
        return _widget(backend, [_layer(backend, coloring="categorical",
                                        colorize_axis=Axis.SCAN, **kw)])

    def _pretend(self, vs, extra):
        """Make the enumeration offer *extra* values that have no data."""
        real = vs._colorize_category_values
        vs._colorize_category_values = lambda axis, pol: list(real(axis, pol)) + list(extra)

    def test_silent_when_everything_checked_has_data(self, backend):
        assert self._vs(backend)._no_data_note(0) == ""

    def test_names_checked_values_that_drew_nothing(self, backend):
        vs = self._vs(backend)
        self._pretend(vs, ["PHANTOM1", "PHANTOM2"])
        note = vs._no_data_note(0)
        assert "No data for: PHANTOM1, PHANTOM2" in note
        assert "No data for" in vs._full_legend_html()

    def test_an_unchecked_value_is_not_reported(self, backend):
        vs = self._vs(backend)
        self._pretend(vs, ["PHANTOM1"])
        vs.update_colorize(0, excluded_categories=["PHANTOM1"])
        assert vs._no_data_note(0) == ""

    def test_long_lists_are_truncated(self, backend):
        vs = self._vs(backend)
        self._pretend(vs, [f"P{i}" for i in range(7)])
        assert "P0, P1, P2, P3 (+3 more)" in vs._no_data_note(0)

    def test_the_gray_context_is_not_reported_as_missing(self, backend):
        vs = self._vs(backend, excluded_display="gray")
        vals = vs._colorize_category_values(Axis.SCAN, vs.layers[0].polarization)
        vs.update_colorize(0, excluded_categories=vals[1:])
        assert OTHER_CATEGORY_LABEL in vs._layer_categories[0]
        assert vs._no_data_note(0) == ""

    def test_it_can_never_break_a_render(self, backend):
        vs = self._vs(backend)

        def boom(*a, **k):
            raise RuntimeError("enumeration failed")

        vs._colorize_category_values = boom
        assert vs._no_data_note(0) == ""
        assert vs._full_legend_html()                                   # still renders

    def test_replay_of_the_reported_case(self, backend):
        """Baseline axis, everything else unchecked, three baselines of an
        antenna with no rows checked: the plot is all gray -- and now the
        legend says why."""
        vs = _widget(backend, [_layer(backend)])
        pol = vs.layers[0].polarization
        real = vs._colorize_category_values(Axis.BASELINE, pol)
        phantom = ["DA41&DA45", "DA41&DA48", "DA41&DA50"]
        self._pretend(vs, phantom)                     # an older backend would have offered them
        vs.update_colorize(0, coloring="categorical", colorize_axis="BASELINE",
                           excluded_categories=real, excluded_display="gray")
        assert vs._layer_categories[0] == (OTHER_CATEGORY_LABEL,)
        html = vs._full_legend_html()
        assert OTHER_CATEGORY_LABEL in html
        assert "No data for: DA41&amp;DA45, DA41&amp;DA48, DA41&amp;DA50" in html


# ---------------------------------------------------------------------------
# 8. Stacking order and the empty state (Part 5c)
# ---------------------------------------------------------------------------

class TestStackingOrder:
    """Categorical layers are drawn above continuous ones, whatever their
    layer index -- so the colors the user asked for are never underneath."""

    def _layers(self, backend, kinds):
        out = []
        for k, kind in enumerate(kinds):
            if kind == "cat":
                out.append(_layer(backend, k % 2, coloring="categorical",
                                  colorize_axis=Axis.BASELINE))
            else:
                out.append(_layer(backend, k % 2))
        return out

    @pytest.mark.parametrize("kinds,expected", [
        (["cont", "cat"], [0, 1]),
        (["cat", "cont"], [1, 0]),
        (["cat", "cont", "cat"], [1, 0, 2]),      # groups keep layer order
        (["cont", "cont"], [0, 1]),
        (["cat", "cat"], [0, 1]),
    ])
    def test_stack_order(self, backend, kinds, expected):
        vs = _widget(backend, self._layers(backend, kinds))
        assert vs._stack_order() == expected

    def _mixed(self, backend, cat_first=True):
        """A highlighted categorical XX layer plus a continuous YY layer."""
        vs = _widget(backend, self._layers(backend, ["cat", "cont"] if cat_first else ["cont", "cat"]))
        i = 0 if cat_first else 1
        pol = vs.layers[i].polarization
        labels = vs._colorize_category_values(Axis.BASELINE, pol)
        pick = labels[:3]
        vs.update_colorize(i, coloring="categorical", colorize_axis="BASELINE",
                           excluded_categories=[b for b in labels if b not in pick])
        return vs, i

    @pytest.mark.parametrize("cat_first", [True, False])
    def test_highlighted_points_are_never_covered_by_the_continuous_layer(self, backend, cat_first):
        vs, i = self._mixed(backend, cat_first)
        comp = vs._collapse_and_composite()
        cat = vs._layer_images[i]
        solid = (cat >> 24) == 255
        assert solid.any()
        cont = vs._layer_images[1 - i]
        assert ((cont >> 24) > 0)[solid].any(), "test needs overlap to mean anything"
        assert np.array_equal(comp[solid] & 0x00FFFFFF, cat[solid] & 0x00FFFFFF)
        assert ((comp[solid] >> 24) == 255).all()

    def test_a_hidden_categorical_layer_covers_nothing(self, backend):
        vs, i = self._mixed(backend, cat_first=True)
        vs.set_alpha(i, 0.0)
        comp = vs._collapse_and_composite()
        cont = vs._layer_images[1 - i]
        occupied = (cont >> 24) > 0
        # exactly the continuous layer: same footprint, same colors
        assert np.array_equal((comp >> 24) > 0, occupied)
        assert np.array_equal(comp[occupied] & 0x00FFFFFF, cont[occupied] & 0x00FFFFFF)

    def test_the_stack_never_reorders_the_layers_themselves(self, backend):
        vs, _i = self._mixed(backend, cat_first=True)
        assert [l.coloring for l in vs.layers] == ["categorical", "continuous"]


class TestEmptyState:
    def test_unchecking_everything_says_so(self, backend):
        vs = _widget(backend, [_layer(backend)])
        pol = vs.layers[0].polarization
        vals = vs._colorize_category_values(Axis.SCAN, pol)
        vs.update_colorize(0, coloring="categorical", colorize_axis="SCAN",
                           excluded_categories=vals)
        assert vs._layer_skip_reason[0] == "all Scan categories excluded"
        assert "All Scan values are unchecked" in vs._legend_html(0)

    def test_a_layer_with_no_data_keeps_the_generic_wording(self, backend):
        vs = _widget(backend, [_layer(backend, coloring="categorical", colorize_axis=Axis.SCAN)])
        vs._layer_skip_reason[0] = "no Scan data for this selection"
        vs._layer_categories[0] = None
        assert "no categories in current selection" in vs._legend_html(0)
