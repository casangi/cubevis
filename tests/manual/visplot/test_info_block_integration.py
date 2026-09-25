"""
test_info_block_integration.py
==============================
Integration tests for the permanent info blocks in ``VisibilityPlotter``'s
configuration panel (cursor readout / legend / colorbar), the gear-tab
selectors that drive them, and the ``CheckboxGroup`` colorize checklists
that replaced the ``DataTable`` ones.

Needs a real MS -- same convention as the other integration tests::

    ulimit -n 4096 && MS=<path>.ms pytest test_info_block_integration.py -v

Skipped when ``MS`` is unset.  No browser is involved: what is tested is
everything Python can promise -- the pre-built widget graph (there is no
Bokeh server, so it must be complete at construction), what
``_handle_plot()`` and the live-scaling handlers put in their responses
(the browser applies those verbatim), and that the whole document
serializes.  The client-side script itself is covered by
``test_info_panel.py`` (JS/Python parity under node); how the browser
renders any of it is not covered anywhere here.

Test location
-------------
``cubevis/tests/manual/visplot/test_info_block_integration.py``
"""

import asyncio
import os
import shutil
import warnings

import pytest

MS = os.environ.get("MS")

pytestmark = pytest.mark.skipif(not MS, reason="set MS=<path>.ms to run")


@pytest.fixture(scope="module")
def plotter():
    warnings.filterwarnings("ignore")
    from cubevis.toolbox.visplot import VisibilityPlotter
    return VisibilityPlotter(ms=MS, backend="auto", field="",
                             correlation="XX,YY", layout="side")


@pytest.fixture(scope="module")
def document(plotter):
    """The app's models in ONE Document.  A Bokeh model can belong to only
    one, so tests that need a document share this rather than each
    building their own."""
    from bokeh.document import Document
    doc = Document()
    doc.add_root(plotter._app_context.ui)
    return doc


def _run(coro):
    return asyncio.run(coro)


def _msg(plotter, colorize=None):
    W = plotter._panel_axis_widgets
    return {
        "field": "", "correlation": "XX,YY", "datacolumn": "data",
        "reload": False,
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


# --------------------------------------------------------------------------- #
# Widget graph (built once, at construction) -- info stays WITH its panel      #
# --------------------------------------------------------------------------- #

def test_info_widgets_stay_in_their_own_panels_layout(plotter):
    # This is the point of the revert (2026-09): NOT moved into
    # VisibilityPlotter's sidebar. Each panel's own layout is
    # column(figure, info_column) with nothing else.
    assert {s.id for s in plotter._slots} == {"A", "B"}
    n = 0
    for slot in plotter._slots:
        for kind in ("raster", "scatter"):
            panel = getattr(slot, kind)
            kids = panel.layout.children
            assert len(kids) == 2
            assert type(kids[0]).__name__ == "figure"
            info_col = kids[1]
            assert list(info_col.children) == [
                panel._info_div, panel._legend_content, panel._colorbar_content]
            n += 1
    assert n == 4


def test_sidebar_has_no_info_widgets_and_no_per_panel_bookkeeping(plotter):
    # The rejected design's bookkeeping (a block per (slot, kind), a
    # visibility-mirroring hook) must not exist at all -- there is
    # nothing to route since each panel keeps its own.
    assert not hasattr(plotter, "_info_blocks")
    assert not hasattr(plotter, "_sync_info_blocks")
    for slot in plotter._slots:
        for kind in ("raster", "scatter"):
            assert not hasattr(getattr(slot, kind), "detach_info")
    from bokeh.models import Div
    top_level_divs = [c for c in plotter._sidebar_col.children if isinstance(c, Div)]
    assert not any("Panel" in (d.text or "") and "·" in (d.text or "")
                   for d in top_level_divs)


def test_each_info_item_has_its_own_small_fixed_height(plotter):
    # No outer scrolling wrapper any more (removed on review: it was a
    # generous vh-based cap unconnected to the page's real, fixed height
    # budget, and a likely factor in a reported width-rendering bug).
    # Each of the three widgets is bounded individually instead, so the
    # panel's total height is bounded by construction -- sum of whichever
    # are checked -- rather than hoping a viewport percentage fits.
    from cubevis.toolbox.visplot.info_panel import ITEM_HEIGHTS
    for slot in plotter._slots:
        for kind in ("raster", "scatter"):
            panel = getattr(slot, kind)
            info_col = panel.layout.children[1]
            assert "max-height" not in info_col.styles
            assert "overflow-y" not in info_col.styles
            for key, div in (("cursor", panel._info_div),
                             ("legend", getattr(panel, "_legend_content", None)),
                             ("colorbar", panel._colorbar_content)):
                assert div.height == ITEM_HEIGHTS[key]
                assert div.styles["overflow-y"] == "auto"
                # Belt-and-suspenders width fix (2026-09, second attempt
                # at the reported colorbar-narrower-than-the-plot bug):
                # explicit width + border-box, in case sizing_mode alone
                # wasn't reliably overriding a shrink-to-fit interaction
                # with overflow-y on a flex item.
                assert div.styles["width"] == "100%"
                assert div.styles["box-sizing"] == "border-box"


def test_every_info_widget_has_exactly_one_parent_in_the_document(plotter, document):
    parents = {}
    for m in document.models:
        ch = getattr(m, "children", None)
        if isinstance(ch, list):
            for c in ch:
                parents.setdefault(c.id, []).append(m.id)
    for slot in plotter._slots:
        for kind in ("raster", "scatter"):
            panel = getattr(slot, kind)
            for w in (panel._info_div, panel._legend_content, panel._colorbar_content):
                assert len(parents.get(w.id, [])) == 1


def test_whole_document_serializes_to_html(plotter):
    from bokeh.embed import file_html
    from bokeh.resources import CDN
    html = file_html(plotter._app_context.ui, CDN, "t")
    for needle in ("cvSetBusy", "__cvCheckboxGroupViewGuarded",
                   "Cursor tracking", "colorbar_html", "bk-clearfix"):
        assert needle in html, needle


def test_document_has_no_fixed_sizing_mode_validation_warnings(plotter, caplog):
    # Regression test for a real bug: an earlier version of the info
    # display set sizing_mode="fixed" while explicitly nulling height,
    # which Bokeh's own validator flags (W-1005 FIXED_SIZING_MODE) --
    # seen in practice as WARNING lines in the Python console at
    # construction/embed time, not an exception, so nothing else here
    # would have caught it.
    import logging
    from bokeh.embed import file_html
    from bokeh.resources import CDN
    with caplog.at_level(logging.WARNING, logger="bokeh.core.validation.check"):
        file_html(plotter._app_context.ui, CDN, "t")
    hits = [r.message for r in caplog.records if "FIXED_SIZING_MODE" in r.message]
    assert hits == [], hits


def test_selectors_are_wired_directly_to_the_panels_own_widgets(plotter):
    W = plotter._panel_axis_widgets
    for sid in ("A", "B"):
        for kind in ("raster", "scatter"):
            sel = W[sid][kind]["info_selectors"]
            panel = getattr(next(s for s in plotter._slots if s.id == sid), kind)
            assert sel.items.js_property_callbacks.get("change:active")
            assert panel._colorbar_content.js_property_callbacks.get("change:text")
            if kind == "scatter":
                assert panel._legend_content.js_property_callbacks.get("change:text")
            else:
                # raster never receives a legend at all -- its checklist
                # has no "Legend" entry, and its panel has no wiring on
                # a legend Div because there is nothing to wire.
                assert "legend" not in sel.item_keys
            for w in sel.widgets():
                assert w in W[sid][kind]["cmap_widgets"]   # dark/light toggle reaches it


def test_selectors_still_live_in_the_gear_tab(plotter):
    # The one part of the original design that stays: the gear tab is
    # still where the user chooses what to show, for that panel.
    W = plotter._panel_axis_widgets
    for sid in ("A", "B"):
        for kind in ("raster", "scatter"):
            sel = W[sid][kind]["info_selectors"]
            assert sel.kind == kind


def test_each_panel_has_its_own_independent_all_cb_and_items(plotter):
    W = plotter._panel_axis_widgets
    seen_all_cb_ids = set()
    for sid in ("A", "B"):
        for kind in ("raster", "scatter"):
            sel = W[sid][kind]["info_selectors"]
            assert id(sel.all_cb) not in seen_all_cb_ids
            seen_all_cb_ids.add(id(sel.all_cb))


def test_all_cb_has_exactly_one_listener_no_reverse_listener_on_items(plotter):
    # Regression test for the reported bug (unchecking one item unchecked
    # all of them): the ORIGINAL design had a SECOND listener on items'
    # own change that wrote back to all_cb, racing against the first.
    # There must be exactly one listener on each side now.
    W = plotter._panel_axis_widgets
    for sid in ("A", "B"):
        for kind in ("raster", "scatter"):
            sel = W[sid][kind]["info_selectors"]
            assert len(sel.all_cb.js_property_callbacks.get("change:active", [])) == 1
            assert len(sel.items.js_property_callbacks.get("change:active", [])) == 1


def test_root_body_plot_area_and_sidebar_use_stretch_both_sizing(plotter):
    # Regression test for a real, reported bug -- and for the fact that
    # the FIRST fix attempt at it was itself a regression, worse than
    # what it replaced (single page-level scrollbar, status bar not
    # visible at startup, a gear tab's content pushing it further down).
    # That attempt found these elements by CSS class and set their
    # height/flex directly via an add_init_script. Built a headless-
    # browser test harness specifically to find out why it failed
    # (Playwright driving a cached Chrome-for-Testing binary, Bokeh
    # rendered with INLINE resources so no network access is needed) and
    # confirmed directly: Bokeh's own layout engine recomputes and
    # REASSERTS its own inline `style.height` on these elements on every
    # layout pass (load, resize, or any DOM change such as a tab
    # becoming visible) -- so a one-time external override, however
    # applied, is silently undone the next time that happens, which is
    # exactly the window-resize and gear-tab-opening triggers reported.
    #
    # sizing_mode="stretch_both" (root, body, plot_area) and
    # sizing_mode="stretch_height" (sidebar_col) instead ask Bokeh
    # itself to compute and maintain these continuously, as part of its
    # own reactive system -- there is nothing external left to silently
    # undo. Verified in that same harness across three window sizes and
    # a simulated gear-tab-content-growth: the status bar tracked the
    # true bottom of the window every time, with zero page-level scroll.
    root = plotter._app_context.ui
    body = root.children[2]
    plot_area = body.children[1]
    assert root.sizing_mode == "stretch_both"
    assert body.sizing_mode == "stretch_both"
    assert plot_area.sizing_mode == "stretch_both"
    assert plotter._sidebar_col.sizing_mode == "stretch_height"
    assert plotter._sidebar_col.styles.get("overflow-y") == "auto"
    assert plot_area.styles.get("overflow-y") == "auto"
    # No leftover raw CSS fight with Bokeh's own layout management --
    # not a height/flex override, and not the abandoned CSS-class-based
    # DOM search mechanism.
    assert "height" not in plotter._sidebar_col.styles
    assert "height" not in plot_area.styles
    assert root.css_classes != ["cv-root-shell"]
    assert body.css_classes != ["cv-body-row"]
    assert not any("cv-root-shell" in s[0].code for s in plotter._app_context.init_scripts)
    css_div = root.children[0]
    assert "html, body { height: 100%" in css_div.text


def test_info_divs_carry_the_clearfix_width_override(plotter):
    # Root cause of the reported "colorbar renders narrower than the
    # plot" bug, traced directly in Bokeh's own compiled JS: every Div
    # wraps its HTML in an internal element Bokeh creates itself with
    # `display: inline-block` hardcoded, unreachable via the model's own
    # `styles` property. Fixed with a stylesheet override targeting that
    # element's class directly.
    for slot in plotter._slots:
        for kind in ("raster", "scatter"):
            panel = getattr(slot, kind)
            for div in (panel._info_div, panel._colorbar_content):
                css = " ".join(getattr(ss, "css", "") for ss in div.stylesheets)
                assert ".bk-clearfix" in css and "!important" in css


def test_close_button_replaces_cancel(plotter):
    from bokeh.embed import file_html
    from bokeh.resources import CDN
    html = file_html(plotter._app_context.ui, CDN, "t")
    assert '"Cancel"' not in html
    assert "\u2715" in html or "\\u2715" in html


def test_close_button_is_on_the_same_row_as_the_kind_switch(plotter):
    # 2026-09, on request: previously grouped with Swap on a row below;
    # now shares the Raster/Scatter switch's row, pushed to the far
    # right via justify-content:space-between (no Spacer model needed).
    from bokeh.models import Button, RadioButtonGroup
    for sid in ("A", "B"):
        tp = plotter._panel_tabpanels[sid]
        top_row = tp.child.children[0]
        kids = [_unwrap(c) for c in top_row.children]
        assert any(isinstance(k, RadioButtonGroup) for k in kids)
        assert any(isinstance(k, Button) and k.label == "\u2715" for k in kids)
        assert top_row.styles.get("justify-content") == "space-between"
        # Swap is no longer on this row -- it has its own, one below.
        assert not any(isinstance(k, Button) and k.label == "Swap" for k in kids)


def test_close_button_uses_the_compact_icon_stylesheet(plotter):
    # Same stylesheet the existing prev/next iteration buttons already
    # use (2026-09, on request -- the default Button padding looked
    # oversized around a single glyph).
    from bokeh.models import Button, RadioButtonGroup
    found = 0
    for sid in ("A", "B"):
        tp = plotter._panel_tabpanels[sid]
        top_row = tp.child.children[0]
        for c in top_row.children:
            w = _unwrap(c)
            if isinstance(w, Button) and w.label == "\u2715":
                assert plotter._icon_btn_css in w.stylesheets
                assert w.width == 24 and w.height == 24
                found += 1
    assert found == 2


def test_all_cb_reflective_write_guard_is_present_in_the_built_page(plotter):
    # Regression test for a real bug found in a SECOND round of live
    # testing, after the first fix (removing a symmetric two-listener
    # design) still did not resolve it: INFO_APPLY_JS's own reflective
    # write to all_cb.active is a real property change that
    # synchronously fires all_cb's OWN listener, which would otherwise
    # rebuild the checklist from scratch and reintroduce the same
    # symptom through a different path. See test_info_panel.py's wired-
    # listener tests for the logic-level regression coverage; this just
    # confirms the guard actually ships in the real built page.
    from bokeh.embed import file_html
    from bokeh.resources import CDN
    html = file_html(plotter._app_context.ui, CDN, "t")
    assert "__cvAllCbReflecting" in html


def _unwrap(tip_or_widget):
    """A Tip wraps its target as ``.child``; return that if so, else the
    widget itself."""
    return getattr(tip_or_widget, "child", None) or tip_or_widget


def test_sidebar_has_no_leftover_height_override(plotter):
    # This app has always avoided a page-level scrollbar. The current
    # approach (sizing_mode="stretch_height", see _build_sidebar() and
    # the matching comment in _build_layout()) gets the sidebar's real,
    # bounded height from Bokeh's own reactive layout system rather than
    # any raw CSS this app sets itself -- confirming no leftover
    # height/max-height/flex override (from either of the two earlier,
    # abandoned attempts) sneaks back in.
    styles = plotter._sidebar_col.styles
    assert plotter._sidebar_col.sizing_mode == "stretch_height"
    assert styles.get("overflow-y") == "auto"
    assert "height" not in styles
    assert "max-height" not in styles
    assert "align-self" not in styles
    assert "flex" not in styles


# --------------------------------------------------------------------------- #
# Checklists: CheckboxGroup, not DataTable                                     #
# --------------------------------------------------------------------------- #

def test_no_checklist_datatables_remain(plotter, document):
    from bokeh.models import DataTable
    # the permanent SPW table is the only DataTable left in the app
    assert len([m for m in document.models if isinstance(m, DataTable)]) == 1
    for sid in ("A", "B"):
        assert plotter._panel_axis_widgets[sid]["scatter"]["cmap_tables"] == []


def test_checklist_values_ride_in_tags_and_default_fully_checked(plotter):
    from bokeh.models import CheckboxGroup
    h0 = plotter._panel_axis_widgets["B"]["scatter"]["colorize_handles"][0]
    assert set(h0["checklists"]) >= {"SCAN", "ANTENNA1"}
    for name, (grp, wrapper) in h0["checklists"].items():
        assert isinstance(grp, CheckboxGroup)
        assert len(grp.tags) == len(grp.labels) > 0
        assert all(isinstance(t, str) for t in grp.tags)      # raw values, JSON-safe
        assert list(grp.active) == list(range(len(grp.labels)))
        assert grp.stylesheets                                # themed like the rest


# --------------------------------------------------------------------------- #
# doPlot() response: what the browser applies                                  #
# --------------------------------------------------------------------------- #

def test_continuous_plot_response(plotter):
    resp = _run(plotter._handle_plot(_msg(plotter)))
    assert resp["status"] == "ok"
    a, b = resp["panels"]["A"], resp["panels"]["B"]
    assert a["colorbar_html"].count("linear-gradient") == 1
    assert "legend_html" not in a                 # raster has no legend
    assert b["legend_html"] == ""
    assert b["colorbar_html"].count("linear-gradient") == 2      # one per layer
    assert "legend_visible" not in b              # visibility is client-derived now
    assert "Amplitude XX" in b["colorbar_html"] and "Amplitude YY" in b["colorbar_html"]


def test_categorical_layer_gets_a_legend_and_its_sibling_a_labelled_bar(plotter):
    h0 = plotter._panel_axis_widgets["B"]["scatter"]["colorize_handles"][0]
    grp, _ = h0["checklists"]["ANTENNA1"]
    excluded = list(grp.tags[:2])
    cz = [{"coloring": "categorical", "colorize_axis": "ANTENNA1",
           "excluded_categories": excluded}, None]
    b = _run(plotter._handle_plot(_msg(plotter, cz)))["panels"]["B"]
    assert b["legend_html"].count("border-radius:2px") > 0
    for e in excluded:
        assert f">{e}<" not in b["legend_html"]
    assert b["colorbar_html"].count("linear-gradient") == 1
    assert "Amplitude YY" in b["colorbar_html"]   # says which layer the lone bar is


def test_all_categorical_means_no_colorbar(plotter):
    cz = [{"coloring": "categorical", "colorize_axis": "SCAN",
           "excluded_categories": []}] * 2
    b = _run(plotter._handle_plot(_msg(plotter, cz)))["panels"]["B"]
    assert b["colorbar_html"] == ""
    assert b["legend_html"] != ""


def test_back_to_continuous_clears_the_legend(plotter):
    b = _run(plotter._handle_plot(_msg(plotter)))["panels"]["B"]
    assert b["legend_html"] == ""
    assert b["colorbar_html"].count("linear-gradient") == 2


def test_checklist_narrows_and_widens_around_a_real_categorize_press(plotter):
    # Python has no way to fire a js_on_change("text", ...) hook outside a
    # browser, so this exercises apply_info_defaults() (the same logic
    # INFO_APPLY_JS runs client-side -- covered directly, over the full
    # state space, in test_info_panel.py's node parity test) against REAL
    # legend HTML from a real colorize press, rather than a synthetic
    # "<div>L</div>" stand-in -- catching anything about the real HTML's
    # shape that a synthetic test wouldn't.
    from cubevis.toolbox.visplot.info_panel import apply_info_defaults
    sel = plotter._panel_axis_widgets["B"]["scatter"]["info_selectors"]
    panel = plotter._slots[1].scatter
    divs = {"cursor": panel._info_div, "colorbar": panel._colorbar_content,
           "legend": panel._legend_content}

    _run(plotter._handle_plot(_msg(plotter)))     # continuous
    apply_info_defaults(divs, sel)
    assert "Legend" not in sel.items.labels

    h0 = plotter._panel_axis_widgets["B"]["scatter"]["colorize_handles"][0]
    cz = [{"coloring": "categorical", "colorize_axis": "ANTENNA1",
          "excluded_categories": []}, None]
    _run(plotter._handle_plot(_msg(plotter, cz)))
    assert panel._legend_content.text != ""
    apply_info_defaults(divs, sel)
    assert "Legend" in sel.items.labels
    assert divs["legend"].visible is True

    _run(plotter._handle_plot(_msg(plotter)))     # back to continuous
    assert panel._legend_content.text == ""
    apply_info_defaults(divs, sel)
    assert "Legend" not in sel.items.labels


# --------------------------------------------------------------------------- #
# Live scaling responses carry the bar too                                     #
# --------------------------------------------------------------------------- #

def test_live_scatter_scaling_update_returns_the_new_colorbar(plotter):
    _run(plotter._handle_plot(_msg(plotter)))
    sc = plotter._slots[1].scatter
    resp = _run(sc._handle_update_scaling({"layer_index": 0, "scaling": "linear"}))
    assert resp["status"] == "ok"
    assert "Amplitude XX" in resp["colorbar_html"]
    assert "(linear)" not in resp["colorbar_html"]            # linear is unlabelled
    resp = _run(sc._handle_update_scaling({"layer_index": 0, "scaling": "log"}))
    assert "(log)" in resp["colorbar_html"]


def test_live_raster_scaling_update_returns_the_new_colorbar(plotter):
    _run(plotter._handle_plot(_msg(plotter)))
    ra = plotter._slots[0].raster
    resp = _run(ra._handle_update_scaling_raster({"scaling": "log"}))
    assert resp["status"] == "ok" and "(log)" in resp["colorbar_html"]
    resp = _run(ra._handle_set_color_mode_raster({"mode": "global"}))
    assert resp["status"] == "ok" and resp["colorbar_html"]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_every_customjs_body_in_the_app_parses(plotter, document):
    """A syntax error in any embedded script fails silently in the browser
    (the callback just never runs), and this app has ~170 of them, many
    assembled from f-strings.  Parse every one under node."""
    import json
    import subprocess
    from bokeh.models import CustomJS
    codes = {m.id: (m.code, sorted(m.args))
             for m in document.models if isinstance(m, CustomJS)}
    assert len(codes) > 100
    js = (
        "const codes = JSON.parse(require('fs').readFileSync(0, 'utf8'));"
        "const bad = [];"
        "for (const [id, [code, args]] of Object.entries(codes)) {"
        "  try { new Function(...args, 'cb_obj', 'cb_data', code); }"
        "  catch (e) { bad.push(id + ': ' + e.message); }"
        "}"
        "console.log(JSON.stringify(bad));"
    )
    res = subprocess.run(["node", "-e", js], input=json.dumps(codes),
                         capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stderr
    assert json.loads(res.stdout) == []


def test_pan_zoom_rerender_shows_the_busy_cursor(plotter):
    # On request: a pan/zoom re-render had no busy indicator at all,
    # which was very plausibly what looked like "cursor tracking has
    # frozen" in earlier testing -- the panel was just mid-re-render,
    # not broken. Both raster and scatter panels share ONE base-class
    # method (VisibilityPlot._add_rerender_trigger) for this, so
    # checking one raster and one scatter panel covers all four.
    from bokeh.models import Button
    plot_btn = next(m for m in plotter._app_context.ui.references()
                    if isinstance(m, Button) and (m.label or "").startswith("Plot"))
    plot_code = plot_btn.js_event_callbacks["button_click"][0].code
    assert "window.__cvSetBusy = window.__cvSetBusy ||" in plot_code
    assert "window.__cvSetBusy(on);" in plot_code
    assert "plot_btn.disabled" in plot_code and "reload_btn.disabled" in plot_code

    for slot in plotter._slots:
        for kind in ("raster", "scatter"):
            panel = getattr(slot, kind)
            cbs = panel.figure.x_range.js_property_callbacks.get("change:end", [])
            assert len(cbs) == 1
            code = cbs[0].code
            assert "window.__cvSetBusy = window.__cvSetBusy ||" in code
            assert "window.__cvSetBusy(true);" in code
            assert "window.__cvSetBusy(false);" in code


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
