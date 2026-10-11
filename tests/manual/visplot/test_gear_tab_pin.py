"""
test_gear_tab_pin.py
====================
2026-10-11, on request: a "Pin" checkbox on each panel's configuration
(gear) tab, left of its close button.  Ticked, a successful Plot leaves
that tab open, so one panel can be re-plotted setting after setting (the
Ant×Ant quantities, say) without reopening it.  Unticked tabs close as
before.

1. Widgets   one Pin per tab, in the header row between Raster/Scatter and
             the close button, with status-area help
2. doPlot    the success branch, run under node with stand-in models:
             pinned tab stays open showing the plotted title, its close
             button then restores that title (not the one from before the
             tab was opened); unpinned tabs close; nothing pinned closes
             the tab set as before

Uses the TW Hya MS (``MS``), field 3c279.  The browser itself was checked
live with devel/tools/visplot_headless (Ant×Ant: Phase, then Real, with
Panel A pinned and Panel B not).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

MS = os.environ.get("MS", "")
_NODE = shutil.which("node")


@pytest.fixture(scope="module")
def plotter():
    if not os.path.isdir(MS):
        pytest.skip("TW Hya MS not available (MS=)")
    from cubevis.toolbox.visplot import VisibilityPlotter
    vp = VisibilityPlotter(ms=MS, field="3c279", correlation="XX", preset="antmatrix")
    vp._build_layout()
    yield vp
    vp.close()


def _walk(model):
    yield model
    for name in ("children", "tabs"):
        for c in (getattr(model, name, None) or []):
            yield from _walk(c)
    child = getattr(model, "child", None)
    if child is not None:
        yield from _walk(child)


class TestWidgets:

    def test_one_pin_per_tab_left_of_close(self, plotter):
        from bokeh.models import Button, CheckboxGroup
        for slot in plotter._slots:
            pin = plotter._panel_pin[slot.id]
            assert isinstance(pin, CheckboxGroup) and pin.labels == ["Pin"] and pin.active == []
            tab = plotter._panel_tabpanels[slot.id]
            header = tab.child.children[0]
            order = [m for m in _walk(header) if isinstance(m, (CheckboxGroup, Button))]
            assert [type(m).__name__ for m in order] == ["CheckboxGroup", "Button"]
            assert order[0] is pin and order[1].label == "✕"

    def test_help(self, plotter):
        from cubevis.toolbox.visplot.visibility_plotter import _STATIC_HINTS
        assert _STATIC_HINTS["gear_pin"].startswith("<b>Pin</b>")
        assert plotter._hint_gear_pin is not None

    def test_doplot_has_the_pins(self, plotter):
        args = plotter._plot_js_args
        assert args["panel_a_pin"] is plotter._panel_pin[plotter._slots[0].id]
        assert args["panel_b_pin"] is plotter._panel_pin[plotter._slots[1].id]


def _success_branch(js: str) -> str:
    start = js.index("        if (resp.status === 'ok') {")
    end = js.index("        } else if (resp.status === 'error' && resp.failed_slot != null) {")
    return js[start:end] + "        }\n"


@pytest.mark.skipif(_NODE is None, reason="node not available")
@pytest.mark.parametrize("pins, kind_a, want_tabs", [
    ((True, False), "raster", ["A"]),
    ((False, False), "raster", []),
    ((True, True), "raster", ["A", "B"]),
    ((True, False), "scatter", ["A"]),    # Panel A switched to Scatter while pinned
])
def test_success_branch(plotter, pins, kind_a, want_tabs):
    js = r"""
function fig(t) { return {title: {text: t, text_color: 'white', change: {emit() {}}}}; }
function src(t, k) { return {data: {text: [t], color: ['white'], kind: [k]}}; }
const panel0_raster_fig = fig('Panel A'), panel0_scatter_fig = fig('old A scatter');
const panel1_raster_fig = fig('old B raster'), panel1_scatter_fig = fig('Panel B');
panel0_raster_fig.title.text_color = 'red'; panel1_scatter_fig.title.text_color = 'red';
const panel_a_tab = {title: 'A'}, panel_b_tab = {title: 'B'};
const gear_tabs = {tabs: [panel_a_tab, panel_b_tab], visible: true, active: 1};
const panel_a_state = src('Amplitude before', 'raster');
const panel_b_state = src('B before', 'scatter');
const panel_a_pin = {active: PIN_A}, panel_b_pin = {active: PIN_B};
const resp = {status: 'ok'};
const p0_kind = KIND_A;
const p0 = {kind: p0_kind, title: 'Phase now'};
if (p0_kind === 'raster') panel0_raster_fig.title.text = p0.title;
else panel0_scatter_fig.title.text = p0.title;
const p1 = null, p1_kind = 'raster';
""" + _success_branch(plotter._do_plot_js) + r"""
console.log(JSON.stringify({tabs: gear_tabs.tabs.map(t => t.title), visible: gear_tabs.visible,
  active: gear_tabs.active, a_state: panel_a_state.data, b_state: panel_b_state.data,
  a_raster: [panel0_raster_fig.title.text, panel0_raster_fig.title.text_color],
  b_scatter: [panel1_scatter_fig.title.text, panel1_scatter_fig.title.text_color]}));
"""
    js = (js.replace("PIN_A", "[0]" if pins[0] else "[]")
            .replace("PIN_B", "[0]" if pins[1] else "[]")
            .replace("KIND_A", json.dumps(kind_a)))
    r = subprocess.run([_NODE, "-e", js], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert out["tabs"] == want_tabs and out["visible"] == bool(want_tabs)
    if want_tabs:
        assert out["active"] < len(want_tabs)
    # Panel B's figure always gets its own title and colour back
    assert out["b_scatter"] == ["B before", "white"]
    if kind_a == "raster":
        assert out["a_raster"] == ["Phase now", "white"]
    if pins[0]:
        # the close button now restores the panel as just plotted
        assert out["a_state"] == {"text": ["Phase now"], "color": ["white"], "kind": [kind_a]}
    else:
        assert out["a_state"]["text"] == ["Amplitude before"]
    if pins[1]:
        assert out["b_state"] == {"text": ["B before"], "color": ["white"], "kind": ["scatter"]}
