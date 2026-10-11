"""
test_layout_pin_and_stepping.py
===============================
2026-10-10, from Darrell's macOS test of H6 slice 2:

1. Pin layout      presets keep the current layout when "Pin layout" is
                   ticked (sidebar checkbox, ``pin_layout=`` argument)
2. Rows with data  ``baselines_with_rows`` (both backends) and the 1 / 0
                   ``has_data`` columns of the Antenna and Baseline tables,
                   sent again with every Plot
3. Stepping        Baseline / Antenna Prev / Next skip rows without data
                   (run under node)
4. Wheel zoom      off until its toolbar button is clicked

Uses the TW Hya MS (``MS`` / ``PS`` environment variables, as
devel/tools/visplot_headless/runtests.sh sets them), field 3c279: on it
DA41 and DA42 (and three more antennas) have no rows.

What is NOT covered: the browser itself (checked live with
devel/tools/visplot_headless).
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess

import pytest

from cubevis.toolbox.visplot.selection import SelectionSpec

MS = os.environ.get("MS", "")
PS = os.environ.get("PS", "")
FIELD = "3c279"

needs_ms = pytest.mark.skipif(not os.path.isdir(MS), reason="TW Hya MS not available (MS=)")


def _run(c):
    return asyncio.run(c)


@pytest.fixture(scope="module")
def plotter():
    if not os.path.isdir(MS):
        pytest.skip("TW Hya MS not available (MS=)")
    from cubevis.toolbox.visplot import VisibilityPlotter
    vp = VisibilityPlotter(ms=MS, field=FIELD, kind="scatter", scatter_x="CHANNEL",
                           correlation="XX")
    vp._build_layout()
    yield vp
    vp.close()


# ---------------------------------------------------------------------------
# 1. Pin layout
# ---------------------------------------------------------------------------

@needs_ms
class TestPinLayout:

    @pytest.mark.parametrize("preset, pin, layout, want", [
        ("spectrum", False, "side", "over"),
        ("spectrum", True,  "side", "side"),
        ("spectrum", True,  "one",  "one"),
        ("vplot",    False, "over", "side"),
        ("vplot",    True,  "over", "over"),
    ])
    def test_constructor(self, preset, pin, layout, want):
        from cubevis.toolbox.visplot import VisibilityPlotter
        vp = VisibilityPlotter(ms=MS, field=FIELD, preset=preset, layout=layout,
                               pin_layout=pin, headless=True)
        try:
            assert vp._layout == want
            if preset == "spectrum":       # pinned or not, both panels scatter
                assert [s.kind for s in vp._slots] == ["scatter", "scatter"]
        finally:
            vp.close()

    def test_checkbox_help_and_preset_js(self, plotter):
        vp = plotter
        cb = vp._pin_layout_cb
        assert cb.labels == ["Pin layout"] and cb.active == []
        assert vp._hint_pin_layout.text.startswith("<b>Pin layout</b>")
        assert "Pin layout" in vp._hint_tb_layout.text
        for name, btn in vp._preset_buttons.items():
            js = btn.js_event_callbacks["button_click"][0]
            assert js.args["pin_cb"] is cb, name
            assert "(pin_cb && pin_cb.active.length > 0) ? layout_rbg.active" in js.code
            assert "layout_rbg.active = lay;" in js.code.replace("      ", " ")
            assert "pos1_scatter_layout.visible = (lay !== 0);" in js.code

    def test_starts_ticked(self):
        from cubevis.toolbox.visplot import VisibilityPlotter
        vp = VisibilityPlotter(ms=MS, field=FIELD, pin_layout=True)
        try:
            vp._build_layout()
            assert vp._pin_layout_cb.active == [0]
        finally:
            vp.close()


# ---------------------------------------------------------------------------
# 2. Rows with data
# ---------------------------------------------------------------------------

def _backend(kind):
    if kind == "msv2":
        from cubevis.toolbox.visplot.data.msv2_backend import MSv2Backend
        return MSv2Backend(MS)
    if not os.path.isdir(PS):
        pytest.skip("TW Hya Processing Set not available (PS=)")
    from cubevis.toolbox.visplot.data.msv4_backend import MSv4Backend
    return MSv4Backend(PS)


@needs_ms
class TestRowsWithData:

    @pytest.mark.parametrize("kind", ["msv2", "msv4"])
    def test_baselines_with_rows(self, kind):
        import arcae
        import numpy as np
        t = arcae.table(MS)
        f = np.asarray(t.getcol("FIELD_ID"))
        a1 = np.asarray(t.getcol("ANTENNA1"))
        a2 = np.asarray(t.getcol("ANTENNA2"))
        names = list(arcae.table(MS + "::ANTENNA").getcol("NAME"))
        fnames = list(arcae.table(MS + "::FIELD").getcol("NAME"))
        m = f == fnames.index(FIELD)
        want = sorted({(names[i], names[j]) for i, j in zip(a1[m], a2[m])})
        b = _backend(kind)
        b.open()
        try:
            # the antenna / baseline choice is ignored
            sel = SelectionSpec(field_names=[FIELD], baselines=[("DA44", "DA45")])
            assert b.baselines_with_rows(sel) == want
            assert ("DA41", "DA42") not in want and len(want) == 171
            # every field: more
            assert len(b.baselines_with_rows(None)) > len(want)
        finally:
            b.close()

    def test_tables_and_plot_response(self, plotter):
        vp = plotter
        bl = vp._baseline_source.data
        ant = vp._antenna_source.data
        assert sum(bl["has_data"]) == 171 and len(bl["has_data"]) == 325
        names = list(bl["name"])
        assert bl["has_data"][names.index("DA41&DA42")] == 0
        assert bl["has_data"][names.index("DA44&DA45")] == 1
        assert ant["has_data"][list(ant["name"]).index("DA41")] == 0
        # one Plot with a single baseline ticked: still every baseline with
        # rows in the field, not just the ticked one
        sid = vp._slots[0].id
        resp = _run(vp._handle_plot({
            "field": FIELD, "baselines": [["DA44", "DA45"]],
            "panels": {sid: {"kind": "scatter", "x": "CHANNEL", "y": "AMPLITUDE"}}}))
        assert resp["status"] == "ok", resp
        assert resp["bl_has_data"] == list(bl["has_data"])
        assert resp["ant_has_data"] == list(ant["has_data"])
        assert "bl_src.data['has_data'] = resp.bl_has_data" in vp._do_plot_js

    def test_names_are_dimmed(self, plotter):
        col = plotter._baseline_table.columns[0]
        assert "(no data)" in col.formatter.template
        assert "has_data === 0" in col.formatter.template
        assert plotter._antenna_table.columns[0].formatter is not col.formatter


# ---------------------------------------------------------------------------
# 3. Stepping (node)
# ---------------------------------------------------------------------------

_NODE = shutil.which("node")


@pytest.mark.skipif(_NODE is None, reason="node not available")
def test_stepping_skips_rows_without_data():
    from cubevis.toolbox.visplot.iteration_step import STEP_INDEX_JS
    from cubevis.toolbox.visplot import antenna_baseline_select as a
    js = STEP_INDEX_JS + a.iterate_antenna_js("") + a.iterate_baseline_js("") + r"""
const ant_src = {data: {name: ['A','B','C','D'], has_data: [0,1,0,1]}, selected: {indices: []}};
const bl_src = {data: {ant1: ['A','A','A','B','B','C'], ant2: ['B','C','D','C','D','D'],
                       has_data: [0,0,0,1,1,0]}, selected: {indices: []}};
const out = {ant: [], bl: []};
for (let i = 0; i < 3; i++) { doIterateAntenna(1); out.ant.push(ant_src.selected.indices[0]); }
ant_src.selected.indices = [2]; doIterateAntenna(1);  out.ant.push(ant_src.selected.indices[0]);
ant_src.selected.indices = [2]; doIterateAntenna(-1); out.ant.push(ant_src.selected.indices[0]);
ant_src.selected.indices = [];
for (let i = 0; i < 3; i++) { doIterateBaseline(1); out.bl.push(bl_src.selected.indices[0]); }
// with antenna B ticked: B's baselines with data (3: B&C, 4: B&D)
ant_src.selected.indices = [1]; bl_src.selected.indices = [];
doIterateBaseline(1); out.bl.push(bl_src.selected.indices[0]);
doIterateBaseline(1); out.bl.push(bl_src.selected.indices[0]);
// no has_data column (an older page): every row, as before
delete bl_src.data.has_data; ant_src.selected.indices = []; bl_src.selected.indices = [];
doIterateBaseline(1); out.bl.push(bl_src.selected.indices[0]);
console.log(JSON.stringify(out));
"""
    r = subprocess.run([_NODE, "-e", js], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert out == {"ant": [1, 3, 1, 3, 1], "bl": [3, 4, 3, 3, 4, 0]}


# ---------------------------------------------------------------------------
# 4. Wheel zoom
# ---------------------------------------------------------------------------

def test_wheel_zoom_is_off_until_clicked(plotter):
    from bokeh.models import WheelZoomTool
    for slot in plotter._slots:
        for panel in (slot.raster, slot.scatter):
            tb = panel.figure.toolbar
            assert tb.active_scroll is None
            assert any(isinstance(t, WheelZoomTool) for t in tb.tools)


# ---------------------------------------------------------------------------
# 5. After a long Plot (2026-10-10, second report): the redraws that follow
#    a Plot must not read the data again
# ---------------------------------------------------------------------------

@needs_ms
class TestRedrawsAfterPlot:

    def test_averaged_points_answer_from_the_memo_without_a_read(self, monkeypatch):
        """Frames larger than the frame cache are re-read on every call;
        averaged points must still come from the memo."""
        from cubevis.toolbox.visplot.axes import Axis
        from cubevis.toolbox.visplot.data.msv2_backend import MSv2Backend
        from cubevis.toolbox.visplot.data.reader import _FlagViewContext
        b = MSv2Backend(MS)
        b.open()
        cache = b._frame_cache_obj()
        saved = cache.max_bytes                    # process-wide: put it back
        try:
            cache.max_bytes = 1                    # "on", but nothing fits
            sel = SelectionSpec(field_names=[FIELD], correlation=["XX"], avg_chan="all")
            key = (Axis.AMPLITUDE, "XX")
            with _FlagViewContext("effective"):
                first = b._query_columns_cached_raw(Axis.TIME, [key], sel, cache, None)[key]
            calls = []
            orig = type(b)._query_columns_raw
            monkeypatch.setattr(type(b), "_query_columns_raw",
                                lambda self, *a, **k: calls.append(a) or orig(self, *a, **k))
            with _FlagViewContext("effective"):
                again = b._query_columns_cached_raw(Axis.TIME, [key], sel, cache, None)[key]
                # another state (the pending-flag version) is computed again
                other = b._query_columns_cached_raw(
                    Axis.TIME, [key], SelectionSpec(field_names=[FIELD], correlation=["XX"],
                                                    avg_chan="all", pending_version=7),
                    cache, None)[key]
            assert len(calls) == 1
            assert again["y"].tolist() == first["y"].tolist()
            assert other["y"].tolist() == first["y"].tolist()
        finally:
            cache.max_bytes = saved
            b.close()

    def test_resize_keeps_the_reference(self, plotter, monkeypatch):
        sc = plotter._slots[0].scatter
        assert any(r is not None for r in sc._layer_reference)
        cw, ch = sc._canvas_width, sc._canvas_height
        w, h = sc._width, sc._height
        calls = []
        monkeypatch.setattr(type(sc), "_rerender",
                            lambda self, *a, **k: calls.append(1) or None)
        x0, x1 = sc._x_range
        y0, y1 = sc._y_range
        out = sc._handle_rerender(dict(x0=x0, x1=x1, y0=y0, y1=y1, w=w * 2, h=h // 2,
                                       size_only=True))
        assert calls == []                       # Level-1: no backend query
        assert sc._canvas_width == round(cw * 2) and abs(sc._canvas_height - ch * (h // 2) / h) <= 1
        assert out["image"].shape == (sc._canvas_height, sc._canvas_width)

    def test_status_line_follows_the_layout(self, plotter):
        vp = plotter
        sid = vp._slots[0].id
        resp = _run(vp._handle_plot({
            "field": FIELD, "layout": "over",
            "panels": {sid: {"kind": "scatter", "x": "CHANNEL", "y": "AMPLITUDE"}}}))
        assert "Layout: Over / Under" in resp["status_text"]
        assert "layout:      ['one', 'side', 'over'][layout_rbg.active]," in vp._do_plot_js
