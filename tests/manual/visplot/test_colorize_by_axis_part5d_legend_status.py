"""
test_colorize_by_axis_part5d_legend_status.py -- Tests for Part 5d: the live
legend's column width follows its longest label, and a categorical layer with
every value unchecked posts a red warning in the notification line.

Location in repository:
    cubevis/tests/manual/visplot/test_colorize_by_axis_part5d_legend_status.py

Tests against:
    visibility_scatter.py   _legend_column_width, _legend_html,
                            VisibilityScatter.empty_categorical_warnings
    visibility_plotter.py   _colorize_warning_text, _NOTIFY_WARN_COLOR

Run from the cubevis repository root:

    MS=sis14_twhya_calibrated_flagged.ms \\
        pytest cubevis/tests/manual/visplot/test_colorize_by_axis_part5d_legend_status.py -v

The end-to-end check through ``VisibilityPlotter._handle_plot`` lives in
test_info_block_integration.py's style (a real plotter on a real MS) and is
in section 5 below.
"""
from __future__ import annotations

import asyncio
import os
import re
from types import SimpleNamespace

import numpy as np
import pytest

pytest.importorskip("datashader")

from cubevis.toolbox.visplot.axes import Axis
from cubevis.toolbox.visplot.selection import SelectionSpec
from cubevis.toolbox.visplot import visibility_scatter as vsm
from cubevis.toolbox.visplot.visibility_scatter import _legend_column_width


# ---------------------------------------------------------------------------
# 1. The width helper
# ---------------------------------------------------------------------------

class TestLegendColumnWidth:
    def test_short_labels_keep_the_original_width(self):
        assert _legend_column_width(["4", "7", "10", "12"]) == vsm._LEGEND_MIN_COL_PX == 110

    def test_no_labels_is_the_minimum(self):
        assert _legend_column_width([]) == 110

    def test_a_binned_baseline_label_gets_a_wider_column(self):
        """The reported case: "DA42&DA44–DA42&DV13" was clipped at 110 px."""
        w = _legend_column_width(["DA42&DA44\u2013DA42&DV13", "DA42&DA48"])
        assert 150 <= w <= vsm._LEGEND_MAX_COL_PX

    def test_the_width_is_capped(self):
        assert _legend_column_width(["x" * 500]) == vsm._LEGEND_MAX_COL_PX == 320

    def test_it_follows_the_longest_label_not_the_first_or_the_average(self):
        assert (_legend_column_width(["a", "b", "c" * 20])
                == _legend_column_width(["c" * 20]))

    def test_it_never_shrinks_as_labels_grow(self):
        widths = [_legend_column_width(["x" * n]) for n in range(0, 60, 3)]
        assert widths == sorted(widths)

    def test_non_string_labels_are_tolerated(self):
        assert _legend_column_width([1, 22, 333]) >= 110


# ---------------------------------------------------------------------------
# 2. The legend HTML, against real data
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


def _widget(backend, layers):
    from cubevis.toolbox.visplot.visibility_scatter import VisibilityScatter
    return VisibilityScatter(backend=backend, selection=SelectionSpec(scan=["12", "14"], channel_range=(0, 8)),
                             x_axis=Axis.UVDIST, layers=layers, width=400, height=300)


def _layer(backend, pol_index=0, **kw):
    from cubevis.toolbox.visplot.visibility_scatter import ScatterLayer
    pol = backend.metadata()["correlation_labels"][pol_index]
    return ScatterLayer(y_axis=Axis.AMPLITUDE, polarization=pol, **kw)


def _column_width_of(html: str) -> int:
    return int(re.search(r"column-width:(\d+)px", html).group(1))


class TestLegendHtml:
    def test_scan_legend_is_unchanged(self, backend):
        vs = _widget(backend, [_layer(backend, coloring="categorical", colorize_axis=Axis.SCAN)])
        assert _column_width_of(vs._legend_html(0)) == 110

    def test_binned_baseline_legend_is_wider_than_the_old_fixed_110(self, backend):
        """Every baseline checked: 190 values in 20 buckets whose labels are
        ranges -- the reported screenshot."""
        vs = _widget(backend, [_layer(backend, coloring="categorical", colorize_axis=Axis.BASELINE)])
        cats = vs._layer_categories[0]
        assert len(cats) == 20 and max(len(c) for c in cats) > 15
        assert _column_width_of(vs._legend_html(0)) == _legend_column_width(cats) > 110

    def test_the_width_reaches_the_combined_legend(self, backend):
        vs = _widget(backend, [_layer(backend, coloring="categorical", colorize_axis=Axis.BASELINE)])
        assert f"column-width:{_legend_column_width(vs._layer_categories[0])}px" in vs._full_legend_html()

    def test_a_label_too_long_for_any_column_carries_its_full_text_on_hover(self, backend):
        vs = _widget(backend, [_layer(backend, coloring="categorical", colorize_axis=Axis.SCAN)])
        long_label = "L" * 80
        vs._layer_categories[0] = (long_label, "short")
        vs._layer_category_colors[0] = {long_label: "#111111", "short": "#222222"}
        vs._layer_category_members[0] = {long_label: (long_label,), "short": ("short",)}
        html = vs._legend_html(0)
        assert f'title="{long_label}"' in html
        assert 'title="short"' not in html
        assert _column_width_of(html) == 320


# ---------------------------------------------------------------------------
# 3. The widget's warning text
# ---------------------------------------------------------------------------

class TestEmptyCategoricalWarnings:
    def _all_unchecked(self, backend, **kw):
        vs = _widget(backend, [_layer(backend)])
        pol = vs.layers[0].polarization
        vals = vs._colorize_category_values(Axis.SCAN, pol)
        vs.update_colorize(0, coloring="categorical", colorize_axis="SCAN",
                           excluded_categories=vals, **kw)
        return vs

    def test_silent_for_a_normal_plot(self, backend):
        vs = _widget(backend, [_layer(backend, coloring="categorical", colorize_axis=Axis.SCAN)])
        assert vs.empty_categorical_warnings() == []

    def test_silent_for_a_continuous_layer(self, backend):
        assert _widget(backend, [_layer(backend)]).empty_categorical_warnings() == []

    def test_unchecking_everything_produces_one_actionable_warning(self, backend):
        vs = self._all_unchecked(backend)
        (msg,) = vs.empty_categorical_warnings()
        assert vs.layers[0].label in msg
        assert "Scan" in msg
        assert "nothing to plot" in msg and "press Plot" in msg

    def test_gray_mode_all_unchecked_is_a_valid_render_not_a_warning(self, backend):
        vs = self._all_unchecked(backend, excluded_display="gray")
        assert vs._layer_skip_reason[0] is None
        assert vs.empty_categorical_warnings() == []

    def test_a_layer_with_no_data_is_not_this_warning(self, backend):
        vs = _widget(backend, [_layer(backend, coloring="categorical", colorize_axis=Axis.SCAN)])
        vs._layer_skip_reason[0] = "no Scan data for this selection"
        assert vs.empty_categorical_warnings() == []

    def test_it_recovers_once_something_is_checked_again(self, backend):
        vs = self._all_unchecked(backend)
        assert vs.empty_categorical_warnings()
        vs.update_colorize(0, excluded_categories=[])
        assert vs.empty_categorical_warnings() == []

    def test_text_is_html_escaped(self, backend):
        vs = self._all_unchecked(backend)
        vs._layers[0].label = "A<b>&"
        (msg,) = vs.empty_categorical_warnings()
        assert "A&lt;b&gt;&amp;" in msg and "<b>" not in msg


# ---------------------------------------------------------------------------
# 4. The plotter's collector
# ---------------------------------------------------------------------------

class TestColorizeWarningText:
    def _fns(self):
        from cubevis.toolbox.visplot import visibility_plotter as vp
        return vp._colorize_warning_text, vp._NOTIFY_WARN_COLOR, vp

    def _empty_scatter(self, backend):
        vs = _widget(backend, [_layer(backend)])
        pol = vs.layers[0].polarization
        vs.update_colorize(0, coloring="categorical", colorize_axis="SCAN",
                           excluded_categories=vs._colorize_category_values(Axis.SCAN, pol))
        return vs

    def test_the_color_is_the_files_existing_warning_red(self):
        _f, color, vp = self._fns()
        assert color == "#f38ba8" == vp._EDIT_TITLE_COLOR

    def test_no_slots_no_text(self):
        f, _c, _vp = self._fns()
        assert f([]) == ""

    def test_an_active_scatter_slot_is_reported_with_its_panel(self, backend):
        f, _c, _vp = self._fns()
        slot = SimpleNamespace(id="B", kind="scatter", scatter=self._empty_scatter(backend))
        text = f([slot])
        assert text.startswith("\u26a0 Panel B \u2014 ") and "nothing to plot" in text

    def test_an_idle_scatter_object_never_raises_a_warning(self, backend):
        """A slot currently showing a raster must not warn about its idle
        scatter object's stale state."""
        f, _c, _vp = self._fns()
        slot = SimpleNamespace(id="A", kind="raster", scatter=self._empty_scatter(backend))
        assert f([slot]) == ""

    def test_several_slots_are_joined_one_per_line(self, backend):
        f, _c, _vp = self._fns()
        s1 = SimpleNamespace(id="A", kind="scatter", scatter=self._empty_scatter(backend))
        s2 = SimpleNamespace(id="B", kind="scatter", scatter=self._empty_scatter(backend))
        text = f([s1, s2])
        assert text.count("<br>") == 1 and "Panel A" in text and "Panel B" in text


# ---------------------------------------------------------------------------
# 5. End to end through VisibilityPlotter._handle_plot (real plotter, real MS)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def plotter():
    import warnings
    warnings.filterwarnings("ignore")
    from cubevis.toolbox.visplot import VisibilityPlotter
    return VisibilityPlotter(ms=_ms_path(), backend="auto", field="",
                             correlation="XX,YY", layout="side")


def _run(coro):
    return asyncio.run(coro)


def _msg(plotter, colorize=None):
    """Same message shape test_info_block_integration.py builds."""
    W = plotter._panel_axis_widgets
    return {
        "field": "", "correlation": "XX,YY", "datacolumn": "data", "reload": False,
        "panels": {
            "A": {"kind": "raster",
                  "y": W["A"]["raster"]["y_sel"].value,
                  "x": W["A"]["raster"]["x_sel"].value,
                  "qty": W["A"]["raster"]["q_sel"].value},
            "B": {"kind": "scatter",
                  "x": W["B"]["scatter"]["x_sel"].value,
                  "y": W["B"]["scatter"]["y_sel"].value,
                  "colorize": colorize if colorize is not None else [None, None]},
        },
    }


def _baseline_override(plotter, *, checked=(), display="hide"):
    """A categorical-by-Baseline override for layer 0 with only *checked*
    values ticked (nothing ticked by default)."""
    scatter = plotter._slots[1].scatter
    labels = scatter._colorize_category_values(Axis.BASELINE, scatter.layers[0].polarization)
    return {"coloring": "categorical", "colorize_axis": "BASELINE",
            "excluded_categories": [b for b in labels if b not in set(checked)],
            "excluded_display": display}


class TestHandlePlotWarning:
    def test_nothing_checked_posts_a_red_warning_in_the_response(self, plotter):
        resp = _run(plotter._handle_plot(_msg(plotter, [_baseline_override(plotter), None])))
        assert resp["status"] == "ok"
        assert "Panel B" in resp["notify_text"] and "nothing to plot" in resp["notify_text"]
        assert resp["notify_color"] == "#f38ba8"

    def test_the_live_notify_div_is_updated_too(self, plotter):
        _run(plotter._handle_plot(_msg(plotter, [_baseline_override(plotter), None])))
        assert "nothing to plot" in plotter._notify_div.text
        assert plotter._notify_div.styles["color"] == "#f38ba8"

    def test_the_next_press_clears_it_once_something_is_checked(self, plotter):
        _run(plotter._handle_plot(_msg(plotter, [_baseline_override(plotter), None])))
        resp = _run(plotter._handle_plot(_msg(plotter, [None, None])))
        assert resp["notify_text"] == "" and "notify_color" not in resp
        assert plotter._notify_div.text == ""

    def test_it_survives_a_repeated_press_with_nothing_changed(self, plotter):
        msg = _msg(plotter, [_baseline_override(plotter), None])
        _run(plotter._handle_plot(msg))
        assert "nothing to plot" in _run(plotter._handle_plot(msg))["notify_text"]

    def test_gray_mode_with_nothing_checked_is_not_a_warning(self, plotter):
        resp = _run(plotter._handle_plot(
            _msg(plotter, [_baseline_override(plotter, display="gray"), None])))
        assert resp["notify_text"] == ""

    def test_some_values_checked_is_not_a_warning(self, plotter):
        scatter = plotter._slots[1].scatter
        first = scatter._colorize_category_values(Axis.BASELINE, scatter.layers[0].polarization)[:3]
        resp = _run(plotter._handle_plot(
            _msg(plotter, [_baseline_override(plotter, checked=first), None])))
        assert resp["notify_text"] == ""

    def test_the_response_carries_a_wider_legend_for_binned_labels(self, plotter):
        override = {"coloring": "categorical", "colorize_axis": "BASELINE",
                    "excluded_categories": [], "excluded_display": "hide"}
        resp = _run(plotter._handle_plot(_msg(plotter, [override, None])))
        html = resp["panels"]["B"]["legend_html"]
        assert _column_width_of(html) > 110


# ---------------------------------------------------------------------------
# 6. The exported PNG's legend (found while checking the live legend)
# ---------------------------------------------------------------------------
# The export chose columns by COUNT (min(n, 6)) whatever the labels said, so 20
# binned-baseline labels such as "DA42&DA44–DA42&DV13" were laid out six across
# and ran off both edges of the image.

from cubevis.toolbox.visplot import png_export as pe                    # noqa: E402
from cubevis.toolbox.visplot.panel_spec import ColorBand                # noqa: E402


def _long_band(n=20, label="Amplitude XX"):
    cats = tuple(f"DA{40 + i:02d}&DA{50 + i:02d}\u2013DA{41 + i:02d}&DV{10 + i:02d}"
                 for i in range(n))
    return ColorBand(label=label, cmap=("#111111", "#222222"), scaling="linear",
                     kind="categorical", categories=cats,
                     category_colors={c: "#3a7ebf" for c in cats},
                     category_members={c: (c,) for c in cats}, category_priority=None)


def _short_band(n=20):
    cats = tuple(str(i) for i in range(1, n + 1))
    return ColorBand(label="Scan", cmap=("#111111", "#222222"), scaling="linear",
                     kind="categorical", categories=cats,
                     category_colors={c: "#3a7ebf" for c in cats},
                     category_members={c: (c,) for c in cats}, category_priority=None)


class TestExportLegendColumns:
    def test_without_a_width_budget_the_original_count_rule_is_unchanged(self):
        assert pe._legend_ncol([_long_band()]) == 6
        assert pe._legend_ncol([_short_band()]) == 6

    def test_long_labels_get_fewer_columns_in_a_narrow_budget(self):
        assert pe._legend_ncol([_long_band()], avail_pt=500) < 6

    def test_a_generous_budget_still_caps_at_six(self):
        assert pe._legend_ncol([_long_band()], avail_pt=10_000) == 6

    def test_an_absurdly_small_budget_still_draws_one_column(self):
        assert pe._legend_ncol([_long_band()], avail_pt=1) == 1

    def test_short_labels_keep_their_six_columns_where_long_ones_lose_them(self):
        assert pe._legend_ncol([_short_band()], avail_pt=500) == 6
        assert pe._legend_ncol([_long_band()], avail_pt=500) < 6

    def test_more_room_never_means_fewer_columns(self):
        cols = [pe._legend_ncol([_long_band()], avail_pt=w) for w in range(50, 1200, 50)]
        assert cols == sorted(cols)

    def test_the_chosen_columns_fit_the_budget(self):
        for w in (300, 450, 600, 800):
            n = pe._legend_ncol([_long_band()], avail_pt=w)
            if n > 1:
                assert n * pe._legend_column_pt([_long_band()]) <= w

    def test_rows_are_consistent_with_columns_at_the_same_budget(self):
        b = [_long_band()]
        for w in (300, 500, 800, None):
            n, rows = pe._legend_ncol(b, avail_pt=w), pe._legend_rows(b, avail_pt=w)
            assert rows == -(-pe._legend_handle_count(b) // n)

    def test_plain_non_categorical_legends_are_untouched(self):
        plain = ColorBand(label="Amplitude XX", cmap=("#111111", "#222222"),
                          scaling="linear", kind="density")
        assert pe._legend_ncol([plain], avail_pt=1) == pe._legend_ncol([plain])

    def test_both_placements_have_metrics(self):
        for mode in ("panel", "figure"):
            assert pe._legend_column_pt([_long_band()], mode) > 0


class TestExportLegendFitsTheImage:
    """Measured, not assumed: the drawn legend's bounding box against the
    figure edges."""

    def _draw(self, avail_pt):
        import matplotlib
        matplotlib.use("Agg")
        from matplotlib.figure import Figure
        fig = Figure(figsize=(8.0, 3.0), dpi=100)
        fig.canvas.draw()                                   # give it a renderer
        pe._draw_figure_legend(fig, [_long_band()], pe.THEMES["dark"], 0.95, avail_pt=avail_pt)
        fig.canvas.draw()
        return fig, fig.legends[0].get_window_extent()

    def test_the_old_layout_really_did_overflow(self):
        fig, box = self._draw(avail_pt=None)
        assert box.x0 < 0 or box.x1 > fig.bbox.width       # documents the reported bug

    def test_the_width_aware_layout_fits(self):
        fig, box = self._draw(avail_pt=8.0 * 72.0 * 0.96)
        assert box.x0 >= 0 and box.x1 <= fig.bbox.width

    def test_export_png_end_to_end_on_long_labels(self, tmp_path):
        from cubevis.toolbox.visplot.panel_spec import PanelSpec, RenderedPanel
        spec = PanelSpec(kind="scatter", title="t", x_label="x", y_label="y",
                         x_range=(0.0, 1.0), y_range=(0.0, 1.0), x_is_time=False,
                         y_is_time=False, agg_n_x=10, agg_n_y=10, color_mode="global",
                         bands=(_long_band(),), theme="dark", status="ok")
        img = np.full((120, 300), 0xFF804020, dtype=np.uint32)
        out = pe.export_png([RenderedPanel(spec=spec, image=img, viewport=None)],
                            str(tmp_path / "long_legend.png"))
        assert os.path.getsize(out) > 0

    def test_the_layout_reserves_rows_for_the_budget_the_draw_uses(self, tmp_path, monkeypatch):
        """If the rows reserved above the axes were computed for a different
        width than the columns actually drawn, a legend could be clipped or
        leave dead space -- so the two must see the same budget."""
        from cubevis.toolbox.visplot.panel_spec import PanelSpec, RenderedPanel
        reserved, drawn = [], []
        real_rows, real_fig, real_panel = pe._legend_rows, pe._draw_figure_legend, pe._draw_panel_legend

        def spy_rows(bands, cap=6, avail_pt=None, mode="figure"):
            reserved.append(round(avail_pt, 3) if avail_pt is not None else None)
            return real_rows(bands, cap, avail_pt, mode)

        def spy_fig(fig, bands, theme, y, avail_pt=None):
            drawn.append(round(avail_pt, 3) if avail_pt is not None else None)
            return real_fig(fig, bands, theme, y, avail_pt=avail_pt)

        def spy_panel(ax, spec, theme):
            drawn.append(round(pe._axes_width_pt(ax), 3))
            return real_panel(ax, spec, theme)

        monkeypatch.setattr(pe, "_legend_rows", spy_rows)
        monkeypatch.setattr(pe, "_draw_figure_legend", spy_fig)
        monkeypatch.setattr(pe, "_draw_panel_legend", spy_panel)
        spec = PanelSpec(kind="scatter", title="t", x_label="x", y_label="y",
                         x_range=(0.0, 1.0), y_range=(0.0, 1.0), x_is_time=False,
                         y_is_time=False, agg_n_x=10, agg_n_y=10, color_mode="global",
                         bands=(_long_band(),), theme="dark", status="ok")
        img = np.full((120, 300), 0xFF804020, dtype=np.uint32)
        pe.export_png([RenderedPanel(spec=spec, image=img, viewport=None)], str(tmp_path / "x.png"))
        assert drawn and None not in drawn and None not in reserved
        assert set(drawn) <= set(reserved)
