"""
test_info_panel.py
==================
Tests for ``info_panel.py``: the permanent per-panel info block (cursor
readout / legend / colorbar) in the configuration panel and the gear-tab
selectors that drive it.

Why the shape of this module
-----------------------------
There is no Bokeh server, so the block's behaviour lives in a client-side
script (``INFO_APPLY_JS``) that no Python test can execute directly.  It
is therefore covered three ways, none needing a browser or an MS:

1. ``resolve_key`` / ``colorbar_html`` -- pure logic, tested directly.
2. Bokeh structure -- selectors/blocks/wiring build, serialize, and carry
   the callbacks that make a ``.text`` write re-derive visibility.
3. PARITY -- the shipped JS is run under node over the WHOLE state space
   and compared with ``apply_defaults`` (its Python twin).  Same approach
   as ``test_iteration_step.py``'s JS/node harness: the copy that runs in
   the browser is the copy under test, so the two cannot drift.

Loaded by file path (not through the ``cubevis.toolbox.visplot`` package
``__init__``, which imports the heavyweight backends) -- same reason
``test_spw_selection.py`` lifts by AST.

Test location
-------------
``cubevis/tests/manual/visplot/test_info_panel.py``
"""

import importlib
import itertools
import json
import pathlib
import shutil
import subprocess
import sys
import types

import numpy as np
import pytest


def _find_visplot() -> pathlib.Path:
    here = pathlib.Path(__file__).resolve()
    for base in here.parents:
        cand = base / "cubevis" / "toolbox" / "visplot"
        if (cand / "info_panel.py").is_file():
            return cand
    # flat layout (all modules beside the tests)
    for base in (here.parent, here.parent.parent):
        if (base / "info_panel.py").is_file():
            return base
    raise RuntimeError("could not locate info_panel.py")


_PKG_DIR = _find_visplot()


def _load(name):
    pkg = sys.modules.get("_vp_info_pkg")
    if pkg is None:
        pkg = types.ModuleType("_vp_info_pkg")
        pkg.__path__ = [str(_PKG_DIR)]
        sys.modules["_vp_info_pkg"] = pkg
    return importlib.import_module(f"_vp_info_pkg.{name}")


ip = _load("info_panel")
ps = _load("panel_spec")
cms = _load("colormap_scaling")


# --------------------------------------------------------------------------- #
# 2. colorbar_html                                                             #
# --------------------------------------------------------------------------- #

_VIRIDIS = tuple(f"#{i:02x}{255 - i:02x}80" for i in range(0, 256, 5))


def _band(label="Amplitude", scaling="linear", kind="value", vals=None,
          visible=True, cmap=_VIRIDIS):
    vals = np.linspace(0.0, 2.0, 500) if vals is None else vals
    return ps.ColorBand(
        label=label, cmap=cmap, scaling=scaling, kind=kind, visible=visible,
        mapping=cms.ScalarMapping.from_values(vals, scaling),
    )


def test_colorbar_empty_when_nothing_to_draw():
    assert ip.colorbar_html([]) == ""
    # no mapping yet (never rendered)
    b = ps.ColorBand(label="A", cmap=_VIRIDIS, scaling="linear")
    assert ip.colorbar_html([b]) == ""
    # hidden band and categorical band are skipped, as in the export
    assert ip.colorbar_html([_band(visible=False)]) == ""
    cat = ps.ColorBand(label="A", cmap=(), scaling="linear", kind="categorical")
    assert ip.colorbar_html([cat]) == ""


def test_colorbar_single_band_has_gradient_label_and_three_tick_values():
    html = ip.colorbar_html([_band()])
    assert html.count("linear-gradient(to right,") == 1
    assert "Amplitude" in html
    # min, mid, max of a 0..2 linear ramp
    assert "<span>0</span>" in html and "<span>1</span>" in html and "<span>2</span>" in html


def test_colorbar_scaling_is_named_like_the_export():
    # bar_label() folds the scaling into the label; the live bar must not
    # present a non-linear ramp as linear.
    html = ip.colorbar_html([_band(scaling="log")])
    assert "(log)" in html


def test_colorbar_density_band_is_labelled_density_not_the_layer():
    html = ip.colorbar_html([_band(label="Amplitude XX", kind="density")])
    assert "Density" in html


def test_colorbar_multi_band_prefixes_layer_label():
    html = ip.colorbar_html([_band(label="Amplitude XX", kind="density"),
                             _band(label="Amplitude YY", kind="density")])
    assert html.count("linear-gradient") == 2
    assert "Amplitude XX: Density" in html and "Amplitude YY: Density" in html


def test_colorbar_names_its_layer_when_a_sibling_layer_is_categorical():
    # One layer categorical (legend), one continuous (bar): the lone bar
    # must still say which layer it is.
    cat = ps.ColorBand(label="Amplitude XX", cmap=(), scaling="linear",
                       kind="categorical")
    html = ip.colorbar_html([cat, _band(label="Amplitude YY", kind="density")])
    assert html.count("linear-gradient") == 1
    assert "Amplitude YY: Density" in html
    # ... and a lone-band panel (a raster) needs no prefix
    assert "Amplitude:" not in ip.colorbar_html([_band()])


def test_colorbar_non_linear_ticks_are_not_evenly_spaced_in_value():
    vals = np.concatenate([np.random.default_rng(0).exponential(0.1, 5000)])
    b = _band(scaling="eq_hist", vals=vals)
    t = b.mapping.ticks(3)
    # under eq_hist the midpoint tick sits well below the value midpoint,
    # and the bar labels that value, not the arithmetic mid
    assert t[1] < 0.5 * (t[0] + t[2])
    assert f"{t[1]:.3g}" in ip.colorbar_html([b])


def test_colorbar_escapes_labels_and_rejects_hostile_colors():
    evil = _band(label="<img src=x onerror=alert(1)>",
                 cmap=("#000000", "red;background:url(x)", "#ffffff", "#123456"))
    html = ip.colorbar_html([evil])
    assert "<img" not in html and "&lt;img" in html
    assert "url(" not in html                      # the bad stop was dropped


def test_colorbar_survives_a_mapping_that_cannot_tick():
    class _Broken:
        vmin, vmax = 0.0, 1.0
        def ticks(self, n):
            raise RuntimeError("no")
    b = ps.ColorBand(label="A", cmap=_VIRIDIS, scaling="linear", mapping=_Broken())
    html = ip.colorbar_html([b])
    assert "<span>0</span>" in html and "<span>1</span>" in html


def test_colorbar_text_inherits_theme_colour():
    # no hard-coded light-on-dark text: the dark/light restyle recolours
    # the Div, and the bar must follow it
    assert "#cdd6f4" not in ip.colorbar_html([_band()])


# --------------------------------------------------------------------------- #
# 3. Bokeh structure                                                           #
# --------------------------------------------------------------------------- #

def _widgets_and_sel(kind, *, legend_text="", cursor_text="hover text", cbar_text=""):
    from bokeh.models import Div
    cur = Div(text=cursor_text)
    cbar = Div(text=cbar_text)
    sel = ip.build_info_selectors(kind, width=260)
    divs = {"cursor": cur, "colorbar": cbar}
    if kind == "scatter":
        divs["legend"] = Div(text=legend_text)
    return divs, sel


def test_item_labels_and_keys_per_kind():
    assert ip.ITEM_LABELS["scatter"] == ["Cursor tracking", "Legend", "Colorbar"]
    assert ip.ITEM_LABELS["raster"] == ["Cursor tracking", "Colorbar"]
    assert ip.ITEM_KEYS["raster"] == ["cursor", "colorbar"]
    with pytest.raises(ValueError):
        ip.build_info_selectors("grid", width=200)


def test_selector_defaults_all_checked():
    sel = ip.build_info_selectors("scatter", width=200)
    assert sel.all_cb.active is True
    assert sel.items.active == [0, 1, 2]
    assert sel.item_keys == ["cursor", "legend", "colorbar"]
    assert sel.item_label == {"cursor": "Cursor tracking", "legend": "Legend",
                              "colorbar": "Colorbar"}


def test_two_selector_instances_are_fully_independent():
    a = ip.build_info_selectors("scatter", width=200)
    b = ip.build_info_selectors("scatter", width=200)
    assert a.all_cb is not b.all_cb and a.items is not b.items


def test_wire_attaches_visibility_callback_to_checklist_and_legend_colorbar_only():
    divs, sel = _widgets_and_sel("scatter")
    vis_cb, rotate_cb = ip.wire_info_display(divs, sel)
    assert vis_cb in sel.items.js_property_callbacks["change:active"]
    assert vis_cb in divs["legend"].js_property_callbacks["change:text"]
    assert vis_cb in divs["colorbar"].js_property_callbacks["change:text"]
    # cursor deliberately NOT wired -- see wire_info_display()'s docstring:
    # its own text changes every mouse move, and has nothing new to decide.
    assert "change:text" not in divs["cursor"].js_property_callbacks
    assert rotate_cb in sel.rotate_btn.js_event_callbacks["button_click"]


def test_wire_raster_never_touches_a_legend_key():
    divs, sel = _widgets_and_sel("raster")
    assert "legend" not in divs
    ip.wire_info_display(divs, sel)   # must not raise / attempt a lookup


def test_widgets_needing_restyle_are_enumerated():
    sel = ip.build_info_selectors("raster", width=200)
    ws = sel.widgets()
    assert sel.all_cb in ws and sel.items in ws and sel.rotate_btn in ws
    assert len(ws) == 3


def test_icon_stylesheets_style_only_the_rotate_button():
    from bokeh.models import InlineStyleSheet
    plain, icon = InlineStyleSheet(css=".a{}"), InlineStyleSheet(css=".b{}")
    sel = ip.build_info_selectors("scatter", width=200,
                                  stylesheets=[plain], icon_stylesheets=[icon])
    assert sel.rotate_btn.stylesheets == [icon]
    assert sel.all_cb.stylesheets == [plain] and sel.items.stylesheets == [plain]


def test_icon_stylesheets_falls_back_to_stylesheets_when_omitted():
    from bokeh.models import InlineStyleSheet
    plain = InlineStyleSheet(css=".a{}")
    sel = ip.build_info_selectors("scatter", width=200, stylesheets=[plain])
    assert sel.rotate_btn.stylesheets == [plain]


def test_item_heights_are_small_and_fixed():
    # The regression this pins: the cursor readout must always have a
    # bounded height (an earlier version left it uncapped, letting a long
    # hover readout push the page's status bar out of the viewport).
    for key in ("cursor", "legend", "colorbar"):
        assert 0 < ip.ITEM_HEIGHTS[key] <= 120


def test_wired_widgets_serialize_into_a_document():
    from bokeh.document import Document
    from bokeh.layouts import column
    from bokeh.embed import json_item
    divs, sel = _widgets_and_sel("scatter")
    ip.wire_info_display(divs, sel)
    root = column(column(divs["cursor"], divs["legend"], divs["colorbar"]), sel.column)
    doc = Document()
    doc.add_root(root)
    assert json_item(root) is not None


# --------------------------------------------------------------------------- #
# 4. apply_info_defaults behaviour                                            #
# --------------------------------------------------------------------------- #

def test_defaults_scatter_with_no_legend_content_starts_with_a_2item_checklist():
    # (colorbar and tracking for Continuous) -- 2026-09, on request.
    divs, sel = _widgets_and_sel("scatter")   # legend empty
    ip.apply_info_defaults(divs, sel)
    assert list(sel.items.labels) == ["Cursor tracking", "Colorbar"]
    assert divs["cursor"].visible is True
    assert divs["colorbar"].visible is False   # colorbar also has no content yet


def test_defaults_scatter_with_legend_content_starts_with_a_3item_checklist():
    # (colorbar, tracking and legend for Categorical).
    divs, sel = _widgets_and_sel("scatter", legend_text="<div>L</div>")
    ip.apply_info_defaults(divs, sel)
    assert list(sel.items.labels) == ["Cursor tracking", "Legend", "Colorbar"]
    assert divs["legend"].visible is True


def test_defaults_raster_checklist_is_always_2item():
    divs, sel = _widgets_and_sel("raster")
    ip.apply_info_defaults(divs, sel)
    assert list(sel.items.labels) == ["Cursor tracking", "Colorbar"]


def test_defaults_unchecked_item_stays_hidden_even_with_content():
    divs, sel = _widgets_and_sel("scatter", cbar_text="<div>bar</div>")
    sel.items.active = [0]                 # colorbar (index 2) unchecked
    ip.apply_info_defaults(divs, sel)
    assert divs["colorbar"].visible is False


def test_defaults_sets_natural_1_to_n_order_over_the_narrowed_list():
    divs, sel = _widgets_and_sel("scatter")   # no legend -> 2 applicable items
    ip.apply_info_defaults(divs, sel)
    assert divs["cursor"].styles["order"] == "1"
    assert divs["colorbar"].styles["order"] == "2"


# --------------------------------------------------------------------------- #
# 5. Parity / node-level checks for the client-side scripts                    #
# --------------------------------------------------------------------------- #

_NODE = shutil.which("node")


@pytest.mark.skipif(_NODE is None, reason="node not available")
def test_js_and_python_agree_on_the_initial_paint_over_the_whole_state_space():
    # apply_info_defaults() only runs once, at construction, before any
    # rotation -- so items.labels/active are still in their natural,
    # never-rotated order at that point, matching what INFO_APPLY_JS
    # assumes on a document's very first run too.
    cases = []
    for scatter in (True, False):
        item_keys = ip.ITEM_KEYS["scatter" if scatter else "raster"]
        n = len(item_keys)
        for r in range(n + 1):
            for combo in itertools.combinations(range(n), r):
                for legend_text in (("", "<div>L</div>") if scatter else ("",)):
                    for cbar_text in ("", "<div>C</div>"):
                        cases.append(dict(item_keys=item_keys, active=list(combo),
                                          legend=legend_text, cbar=cbar_text))
    harness = r"""
const src = %(src)s;
const cases = %(cases)s;
const item_label_all = {cursor: 'Cursor tracking', legend: 'Legend', colorbar: 'Colorbar'};
const out = [];
for (const c of cases) {
    const divs = {cursor: {text: 'x', visible: false, styles: {}},
                  colorbar: {text: c.cbar, visible: false, styles: {}}};
    if (c.item_keys.includes('legend')) divs.legend = {text: c.legend, visible: false, styles: {}};
    const labels = c.item_keys.map(k => item_label_all[k]);
    const items = {labels: labels, active: c.active};
    const all_cb = {active: true};
    const window_ = {};
    const fn = new Function('divs','items','item_label','all_cb','window', src);
    fn(divs, items, item_label_all, all_cb, window_);
    out.push({labels: items.labels, active: items.active, all_cb: all_cb.active,
              vis: Object.fromEntries(Object.keys(divs).map(k => [k, divs[k].visible]))});
}
console.log(JSON.stringify(out));
"""
    harness = harness % {"src": json.dumps(ip.INFO_APPLY_JS), "cases": json.dumps(cases)}
    res = subprocess.run([_NODE, "-e", harness], capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stderr
    got = json.loads(res.stdout)
    assert len(got) == len(cases) >= 20

    from bokeh.models import Div
    for c, js in zip(cases, got):
        divs = {"cursor": Div(text="x"), "colorbar": Div(text=c["cbar"])}
        if "legend" in c["item_keys"]:
            divs["legend"] = Div(text=c["legend"])
        sel = types.SimpleNamespace(
            item_keys=c["item_keys"],
            item_label={"cursor": "Cursor tracking", "legend": "Legend", "colorbar": "Colorbar"},
            items=types.SimpleNamespace(
                labels=[{"cursor": "Cursor tracking", "legend": "Legend",
                        "colorbar": "Colorbar"}[k] for k in c["item_keys"]],
                active=c["active"]),
            all_cb=types.SimpleNamespace(active=True))
        ip.apply_info_defaults(divs, sel)
        py_vis = {k: divs[k].visible for k in divs}
        assert js["vis"] == py_vis, (c, js, py_vis)
        assert js["all_cb"] == sel.all_cb.active, (c, js["all_cb"], sel.all_cb.active)


@pytest.mark.skipif(_NODE is None, reason="node not available")
def test_rotate_js_cycles_n_items_back_to_start_after_n_clicks():
    for n in (2, 3):
        keys = [f"k{i}" for i in range(n)]
        item_label = {k: k.upper() for k in keys}
        harness = r"""
const src = %(src)s;
const item_label = %(item_label)s;
const keys = %(keys)s;
const divs = {};
keys.forEach((k, i) => { divs[k] = {styles: {order: String(i + 1)}, text: 'x'}; });
const items = {labels: keys.map(k => item_label[k]), active: keys.map((_, i) => i)};
const fn = new Function('divs', 'items', 'item_label', src);
const seen = [];
for (let i = 0; i < keys.length; i++) {
    fn(divs, items, item_label);
    seen.push({order: keys.map(k => divs[k].styles.order), labels: items.labels.slice()});
}
console.log(JSON.stringify(seen));
""" % {"src": json.dumps(ip.ROTATE_JS), "item_label": json.dumps(item_label), "keys": json.dumps(keys)}
        res = subprocess.run([_NODE, "-e", harness], capture_output=True, text=True, timeout=30)
        assert res.returncode == 0, res.stderr
        seen = json.loads(res.stdout)
        assert seen[-1]["order"] == [str(i + 1) for i in range(n)], seen
        for step in seen:
            assert sorted(int(x) for x in step["order"]) == list(range(1, n + 1))
        # the checklist's own row order tracks the rotation too
        assert seen[0]["labels"][0] == item_label[keys[-1]]   # bottom -> top first


# --------------------------------------------------------------------------- #
# Wired-listener tests: the two scripts REGISTERED AS REAL, CHAINED EVENT      #
# LISTENERS on faithful mock models -- not called as isolated, disconnected   #
# functions.                                                                   #
#                                                                               #
# Why this harness exists, specifically
# ----------------------------------------
# A first round of testing (calling INFO_APPLY_JS and the "All -> items"
# script directly, each with hand-supplied inputs) missed a real bug: the
# reflective `all_cb.active = want_all` write INSIDE INFO_APPLY_JS is
# itself a real property change, and in the actual app it SYNCHRONOUSLY
# fires all_cb's OWN listener -- which, unguarded, rebuilt items.active
# from scratch and clobbered the very state INFO_APPLY_JS had just
# computed. Calling each script by itself, by construction, can never
# exercise that chain reaction. MockModel below uses a real getter/
# setter (not an explicit .set() call) specifically so that a PLAIN
# `obj.prop = val` assignment -- exactly what every script here actually
# writes -- fires listeners the same way a genuine Bokeh model property
# does.
# --------------------------------------------------------------------------- #

_MOCK_MODEL_JS = """
let _nextId = 1;
class MockModel {
    constructor(props) {
        this.id = props.id || ('m' + (_nextId++));
        this._listeners = {};
        this._values = {};
        for (const [k, v] of Object.entries(props)) {
            if (k === 'id') continue;
            this._values[k] = v;
            Object.defineProperty(this, k, {
                get: () => this._values[k],
                set: (val) => {
                    this._values[k] = val;
                    for (const fn of (this._listeners[k] || [])) fn();
                },
                enumerable: true,
            });
        }
    }
    on_change(prop, fn) { (this._listeners[prop] = this._listeners[prop] || []).push(fn); }
}
"""


def _wired_harness(all_to_items_src: str) -> str:
    return _MOCK_MODEL_JS + """
const info_apply_src = %(info_apply)s;
const all_to_items_src = %(all_to_items)s;
function Div(text) { return {text: text || '', visible: false, styles: {}}; }
function wire(divs, items, item_label, all_cb) {
    const info_apply_fn = new Function('divs','items','item_label','all_cb','window', info_apply_src);
    const all_to_items_fn = new Function('all_cb','items','window', all_to_items_src);
    items.on_change('active', function() { info_apply_fn(divs, items, item_label, all_cb, window); });
    all_cb.on_change('active', function() { all_to_items_fn(all_cb, items, window); });
}
""" % {"info_apply": json.dumps(ip.INFO_APPLY_JS), "all_to_items": json.dumps(all_to_items_src)}


def _run_js(script: str) -> dict:
    res = subprocess.run([_NODE, "-e", script], capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)


def _real_all_to_items_js() -> str:
    """The actual shipped "All -> items" script, extracted from a real
    built InfoSelectors -- never hand-copied, so it cannot drift."""
    sel = ip.build_info_selectors("scatter", width=200)
    return sel.all_cb.js_property_callbacks["change:active"][0].code


@pytest.mark.skipif(_NODE is None, reason="node not available")
def test_unchecking_one_item_does_not_uncheck_the_others():
    # Regression test for a real, reported bug, found in TWO rounds of
    # live testing. Round 1: the original design had two listeners
    # (items -> all_cb, all_cb -> items) each writing back to the
    # other, guarded by a flag assumed sufficient. Redesigned so
    # INFO_APPLY_JS computes "All"'s value as its own side effect and
    # all_cb's listener is one-directional. Round 2 (this test, added
    # after the round-1 fix ALSO failed live): that reflective write
    # to all_cb.active is itself a real property change and
    # synchronously fires all_cb's OWN listener, which (unguarded)
    # rebuilds items.active from scratch -- reintroducing the exact
    # same symptom through a different path. Fixed with
    # window.__cvAllCbReflecting, keyed by all_cb.id, so that listener
    # can tell "this change came from INFO_APPLY_JS reflecting reality"
    # from "the user actually clicked All". Wired as REAL chained
    # listeners here (see this section's docstring) -- a call-each-
    # script-in-isolation test cannot see this class of bug at all.
    window_ = ""  # placeholder; real 'window' object is created in JS
    script = _wired_harness(_real_all_to_items_js()) + """
global.window = {};
const divs = {cursor: Div('x'), colorbar: Div('<div>c</div>')};
const items = new MockModel({labels: ['Cursor tracking', 'Colorbar'], active: [0, 1]});
const all_cb = new MockModel({active: true});
wire(divs, items, {cursor: 'Cursor tracking', colorbar: 'Colorbar'}, all_cb);
items.active = items.active.filter(function(v) { return v !== 0; });  // uncheck "Cursor tracking"
console.log(JSON.stringify({active: items.active, all_cb: all_cb.active,
                            cursor_visible: divs.cursor.visible, colorbar_visible: divs.colorbar.visible}));
"""
    r = _run_js(script)
    assert r["active"] == [1], r               # colorbar (only) stays checked
    assert r["all_cb"] is False                 # "All" correctly reads unchecked
    assert r["colorbar_visible"] is True        # colorbar itself stays visible
    assert r["cursor_visible"] is False


@pytest.mark.skipif(_NODE is None, reason="node not available")
def test_unchecking_the_middle_of_three_leaves_the_other_two_checked():
    script = _wired_harness(_real_all_to_items_js()) + """
global.window = {};
const divs = {cursor: Div('x'), legend: Div('<div>L</div>'), colorbar: Div('<div>c</div>')};
const items = new MockModel({labels: ['Cursor tracking', 'Legend', 'Colorbar'], active: [0, 1, 2]});
const all_cb = new MockModel({active: true});
wire(divs, items, {cursor: 'Cursor tracking', legend: 'Legend', colorbar: 'Colorbar'}, all_cb);
items.active = items.active.filter(function(v) { return v !== 1; });  // uncheck Legend
console.log(JSON.stringify({active: items.active, all_cb: all_cb.active,
                            cursor_visible: divs.cursor.visible,
                            legend_visible: divs.legend.visible,
                            colorbar_visible: divs.colorbar.visible}));
"""
    r = _run_js(script)
    assert sorted(r["active"]) == [0, 2], r
    assert r["all_cb"] is False
    assert r["cursor_visible"] is True and r["colorbar_visible"] is True
    assert r["legend_visible"] is False


@pytest.mark.skipif(_NODE is None, reason="node not available")
def test_all_click_checks_everything_and_settles_without_oscillating():
    script = _wired_harness(_real_all_to_items_js()) + """
global.window = {};
const divs = {cursor: Div('x'), colorbar: Div('<div>c</div>')};
const items = new MockModel({labels: ['Cursor tracking', 'Colorbar'], active: [1]});
const all_cb = new MockModel({active: false});
wire(divs, items, {cursor: 'Cursor tracking', colorbar: 'Colorbar'}, all_cb);
all_cb.active = true;   // user clicks "All"
console.log(JSON.stringify({active: items.active, all_cb: all_cb.active,
                            cursor_visible: divs.cursor.visible, colorbar_visible: divs.colorbar.visible}));
"""
    r = _run_js(script)
    assert sorted(r["active"]) == [0, 1], r
    assert r["all_cb"] is True
    assert r["cursor_visible"] is True and r["colorbar_visible"] is True


@pytest.mark.skipif(_NODE is None, reason="node not available")
def test_rechecking_the_last_unchecked_item_brings_all_back_to_checked():
    script = _wired_harness(_real_all_to_items_js()) + """
global.window = {};
const divs = {cursor: Div('x'), colorbar: Div('<div>c</div>')};
const items = new MockModel({labels: ['Cursor tracking', 'Colorbar'], active: [1]});
const all_cb = new MockModel({active: false});
wire(divs, items, {cursor: 'Cursor tracking', colorbar: 'Colorbar'}, all_cb);
items.active = [0, 1];   // user re-checks cursor
console.log(JSON.stringify({active: items.active, all_cb: all_cb.active}));
"""
    r = _run_js(script)
    assert sorted(r["active"]) == [0, 1]
    assert r["all_cb"] is True


@pytest.mark.skipif(_NODE is None, reason="node not available")
def test_two_sequential_unchecks_never_wipe_more_than_intended():
    script = _wired_harness(_real_all_to_items_js()) + """
global.window = {};
const divs = {cursor: Div('x'), legend: Div('<div>L</div>'), colorbar: Div('<div>c</div>')};
const items = new MockModel({labels: ['Cursor tracking', 'Legend', 'Colorbar'], active: [0, 1, 2]});
const all_cb = new MockModel({active: true});
wire(divs, items, {cursor: 'Cursor tracking', legend: 'Legend', colorbar: 'Colorbar'}, all_cb);
items.active = items.active.filter(function(v) { return v !== 0; });   // uncheck cursor
const after1 = items.active.slice();
items.active = items.active.filter(function(v) { return v !== 2; });   // uncheck colorbar
console.log(JSON.stringify({after1: after1, after2: items.active.slice(),
                            legend_label_at: items.labels[items.active[0]],
                            legend_visible: divs.legend.visible}));
"""
    r = _run_js(script)
    assert len(r["after1"]) == 2, r
    assert len(r["after2"]) == 1, r
    assert r["legend_label_at"] == "Legend"
    assert r["legend_visible"] is True


def test_all_to_items_still_has_the_reflective_write_guard():
    # A cheaper, non-node sanity check that the shipped code still
    # contains the guard the tests above rely on -- catches an
    # accidental revert even when node isn't available to run the
    # wired tests themselves.
    code = _real_all_to_items_js() if _NODE else None
    if code is None:
        pytest.skip("node not available")
    assert "__cvAllCbReflecting" in code



@pytest.mark.skipif(_NODE is None, reason="node not available")
def test_legend_appearing_and_disappearing_updates_the_checklist():
    harness = r"""
const src = %(src)s;
const item_label = {cursor: 'Cursor tracking', legend: 'Legend', colorbar: 'Colorbar'};
const divs = {cursor: {text: 'x', visible: false, styles: {}},
              legend: {text: '', visible: false, styles: {}},
              colorbar: {text: '<div>c</div>', visible: false, styles: {}}};
const items = {labels: ['Cursor tracking', 'Colorbar'], active: [0, 1]};
const all_cb = {active: true};
const window_ = {};
const fn = new Function('divs','items','item_label','all_cb','window', src);
fn(divs, items, item_label, all_cb, window_);
const step1 = {labels: items.labels.slice(), n_active: items.active.length};
divs.legend.text = '<div>L</div>';
fn(divs, items, item_label, all_cb, window_);
const step2 = {labels: items.labels.slice(), n_active: items.active.length, legend_vis: divs.legend.visible};
divs.legend.text = '';
fn(divs, items, item_label, all_cb, window_);
const step3 = {labels: items.labels.slice(), legend_vis: divs.legend.visible};
console.log(JSON.stringify({step1, step2, step3}));
""" % {"src": json.dumps(ip.INFO_APPLY_JS)}
    res = subprocess.run([_NODE, "-e", harness], capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr
    r = json.loads(res.stdout)
    assert r["step1"]["labels"] == ["Cursor tracking", "Colorbar"]
    assert r["step2"]["labels"] == ["Cursor tracking", "Colorbar", "Legend"]
    assert r["step2"]["n_active"] == 3          # newly-applicable defaults to checked
    assert r["step2"]["legend_vis"] is True
    assert r["step3"]["labels"] == ["Cursor tracking", "Colorbar"]
    assert r["step3"]["legend_vis"] is False


def test_shipped_js_has_no_backslashes():
    # the embedded-JS rule this codebase follows everywhere (a backslash in
    # a non-raw Python string silently becomes a different character)
    for src in (ip.INFO_APPLY_JS, ip.ROTATE_JS, ip._APPLICABLE_JS, ip._CHECKED_KEYS_JS):
        assert "\\" not in src


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
