"""test_colormap_sync.py -- color-scaling controls follow the picture.

Pre-preview feedback, 2026-11-18: the colormap histogram was built once,
when the sidebar was built, so it kept showing the first quantity drawn;
Reset used histogram edges frozen into its CustomJS; the equation label
lost its color whenever the scaling changed; threshold scaling showed a
max that it does not use.  ``colormap_sync`` is the fix.

Python: :func:`colormap_state`'s shape.  JavaScript: the shipped strings
(``APPLY_JS``, ``RESET_EDGES_JS``, ``THRESHOLD_JS``, ``quiet_set_js`` /
``QUIET_GUARD_JS``, and the plotter's ``_BCOMBINE_SCALAR_JS``) run under
``node`` against plain-object stand-ins for the Bokeh models -- the
convention of ``test_iteration_step.py`` / ``antenna_baseline_select``.

Run:  python -m pytest test_colormap_sync.py -q
"""
from __future__ import annotations

import json
import math
import shutil
import subprocess

import numpy as np
import pytest

from cubevis_test_paths import ensure_cubevis_importable

ensure_cubevis_importable()

from cubevis.toolbox.visplot import colormap_sync as cs  # noqa: E402

NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node not on PATH")


# --------------------------------------------------------------------------
# Python: colormap_state
# --------------------------------------------------------------------------

class TestColormapState:
    def test_bars_from_histogram(self):
        counts, edges = np.histogram(np.arange(10.0), bins=5)
        st = cs.colormap_state(counts, edges, scaling="linear", alpha=10,
                               gamma=1, vmin=None, vmax=None)
        assert st["left"] == edges[:-1].tolist()
        assert st["right"] == edges[1:].tolist()
        assert st["top"] == counts.astype(float).tolist()
        assert st["vmin"] is None and st["vmax"] is None
        json.dumps(st)                       # goes into a comm reply

    def test_nothing_drawn_has_no_bars(self):
        st = cs.colormap_state(np.array([]), np.array([]), scaling="eq_hist",
                               alpha=10, gamma=1, vmin=-180, vmax=180)
        assert st["left"] == st["right"] == st["top"] == []
        assert (st["vmin"], st["vmax"]) == (-180.0, 180.0)

    def test_non_finite_values_are_dropped(self):
        st = cs.colormap_state([1, 2], [0.0, 1.0, math.inf], scaling="log",
                               alpha=math.nan, gamma=1, vmin=math.inf, vmax=2)
        assert st["left"] == []                  # non-finite edge: no bars
        assert st["alpha"] is None and st["vmin"] is None and st["vmax"] == 2.0
        json.dumps(st, allow_nan=False)

    def test_raster_and_scatter_offer_it(self):
        from cubevis.toolbox.visplot.visibility_raster import VisibilityRaster
        from cubevis.toolbox.visplot.visibility_scatter import VisibilityScatter
        assert callable(VisibilityRaster.colormap_state)
        assert callable(VisibilityScatter.colormap_state)
        assert callable(VisibilityScatter.colormap_states)


# --------------------------------------------------------------------------
# JavaScript under node
# --------------------------------------------------------------------------

_HARNESS = r"""
const window = {};
function model(props) { return Object.assign({id: 'm' + Math.random()}, props); }
function run(code, args, cb_obj) {
    const names = Object.keys(args);
    const f = new Function('window', 'cb_obj', ...names, code);
    return f(window, cb_obj, ...names.map(k => args[k]));
}
"""


def _node(script: str):
    out = subprocess.run([NODE, "-e", _HARNESS + script], capture_output=True,
                         text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


def _controls_js() -> str:
    return r"""
const hist_source = model({data: {left: [0], right: [1], top: [0], bottom: [0]}});
const min_span = model({location: 0, visible: true});
const max_span = model({location: 1, visible: true});
const min_input = model({value: '0', title: 'min', visible: true});
const max_input = model({value: '1', title: 'max', visible: true});
const scaling_select = model({value: 'eq_hist'});
const alpha_input = model({value: '10', visible: false});
const gamma_input = model({value: '1', visible: false});
const equation = model({text: ''});
const equations = {linear: 'y = x', threshold: 'y = 1 if x >= cutoff else 0',
                   eq_hist: 'y = histogram-equalized(x)', log: 'y = log'};
const A = {hist_source, min_span, max_span, min_input, max_input, scaling_select,
           alpha_input, gamma_input, equation, equations};
function snap() { return {
    left: hist_source.data.left, min: min_input.value, max: max_input.value,
    min_loc: min_span.location, max_loc: max_span.location, min_title: min_input.title,
    max_vis: max_input.visible, span_max_vis: max_span.visible,
    scaling: scaling_select.value, eq: equation.text, alpha_vis: alpha_input.visible,
    quiet: window.__cvQuietScalingFor || null}; }
"""


@needs_node
class TestApplyJs:
    def _apply(self, state: dict) -> dict:
        return _node(_controls_js() + f"""
run({json.dumps(cs.APPLY_JS)}, A, {{data: {{state: [{json.dumps(state)}]}}}});
console.log(JSON.stringify(snap()));
""")

    def test_new_histogram_and_automatic_range(self):
        st = cs.colormap_state([3, 4], [227.0, 500.0, 9149.0], scaling="eq_hist",
                               alpha=10, gamma=1, vmin=None, vmax=None)
        r = self._apply(st)
        assert r["left"] == [227.0, 500.0]
        assert (r["min"], r["max"]) == ("227", "9149")
        assert (r["min_loc"], r["max_loc"]) == (227.0, 9149.0)

    def test_explicit_range_and_scaling_shown_quietly(self):
        st = cs.colormap_state([1], [44.5, 45.5], scaling="linear", alpha=10,
                               gamma=1, vmin=-180, vmax=180)
        r = self._apply(st)
        assert (r["min"], r["max"]) == ("-180", "180")
        assert r["scaling"] == "linear" and r["eq"] == "y = x"
        # The dropdown change is marked so its own callback does not send it.
        assert list(r["quiet"].values()) == ["linear"]

    def test_threshold_shows_one_cutoff(self):
        st = cs.colormap_state([1], [0.0, 10.0], scaling="threshold", alpha=10,
                               gamma=1, vmin=3.5, vmax=None)
        r = self._apply(st)
        assert r["min_title"] == "cutoff" and r["min"] == "3.5"
        assert r["max_vis"] is False and r["span_max_vis"] is False

    def test_empty_state_keeps_existing_bars(self):
        st = cs.colormap_state([], [], scaling="eq_hist", alpha=10, gamma=1,
                               vmin=None, vmax=None)
        r = self._apply(st)
        assert r["left"] == [0]

    def test_no_state_is_a_no_op(self):
        r = _node(_controls_js() + f"""
run({json.dumps(cs.APPLY_JS)}, A, {{data: {{state: [null]}}}});
console.log(JSON.stringify(snap()));
""")
        assert r["min"] == "0" and r["scaling"] == "eq_hist"


@needs_node
class TestResetAndQuiet:
    def test_reset_reads_current_histogram(self):
        r = _node(_controls_js() + f"""
hist_source.data = {{left: [5, 6], right: [6, 70], top: [1, 1], bottom: [0, 0]}};
run({json.dumps(cs.RESET_EDGES_JS)}, A, null);
console.log(JSON.stringify(snap()));
""")
        assert (r["min"], r["max"]) == ("5", "70")
        assert (r["min_loc"], r["max_loc"]) == (5, 70)

    def test_quiet_set_is_consumed_by_guard_once(self):
        r = _node(f"""
const sel = model({{value: 'eq_hist'}});
run({json.dumps(cs.quiet_set_js("sel", "'linear'"))}, {{sel}}, null);
const guard = 'const s = cb_obj.value;' + {json.dumps(cs.QUIET_GUARD_JS)} + 'return "sent";';
const first = run(guard, {{}}, sel);           // the quiet change: swallowed
const second = run(guard, {{}}, sel);          // a later, user change: sent
console.log(JSON.stringify({{value: sel.value, first: first || null, second}}));
""")
        assert r == {"value": "linear", "first": None, "second": "sent"}

    def test_quiet_set_same_value_leaves_no_token(self):
        r = _node(f"""
const sel = model({{value: 'linear'}});
run({json.dumps(cs.quiet_set_js("sel", "'linear'"))}, {{sel}}, null);
console.log(JSON.stringify(window.__cvQuietScalingFor || null));
""")
        assert r is None


@needs_node
class TestBaselinesCombinedScalarGuard:
    def _run(self, averaging: str, value: str) -> dict:
        from cubevis.toolbox.visplot import visibility_plotter as vp
        opts = [list(o) for o in vp._BCOMBINE_OPTIONS]
        return _node(f"""
const rm_sel = model({{value: {json.dumps(value)}, options: {json.dumps(opts)}}});
run({json.dumps(vp._BCOMBINE_SCALAR_JS)}, {{rm_sel, all_opts: {json.dumps(opts)}}},
    {{value: {json.dumps(averaging)}}});
console.log(JSON.stringify({{value: rm_sel.value, values: rm_sel.options.map(o => o[0])}}));
""")

    def test_label(self):
        from cubevis.toolbox.visplot import visibility_plotter as vp
        assert dict(vp._BCOMBINE_OPTIONS)["coherent"] == "Vector (coherent)"

    def test_scalar_withdraws_coherent(self):
        r = self._run("scalar", "coherent")
        assert r == {"value": "mean", "values": ["mean", "median", "max"]}

    def test_scalar_keeps_other_choice(self):
        assert self._run("scalar", "max")["value"] == "max"

    def test_vector_offers_it_again(self):
        r = self._run("vector", "mean")
        assert r["values"] == ["mean", "median", "max", "coherent"]


class TestWiring:
    """Source-level checks that the pieces are connected (no browser)."""

    def _src(self, name):
        import inspect
        from cubevis.toolbox.visplot import visibility_plotter, visibility_raster, visibility_scatter
        return inspect.getsource({"plotter": visibility_plotter, "raster": visibility_raster,
                                  "scatter": visibility_scatter}[name])

    def test_plot_response_carries_cmap(self):
        src = self._src("plotter")
        assert src.count('"cmap":    self._cmap_states(panel)') == 1
        assert src.count('"cmap":          self._cmap_states(panel)') == 1
        assert "panel0_raster_cmap_sync" in src and "panel1_scatter_cmap_sync" in src

    def test_live_scaling_replies_carry_cmap(self):
        assert '"cmap":          self.colormap_state()' in self._src("raster")
        assert "cmap          = self.colormap_state(idx)" in self._src("scatter")

    def test_reset_no_longer_uses_frozen_edges(self):
        for k in ("raster", "scatter"):
            src = self._src(k)
            assert "hist_lo.toFixed" not in src and "_csync.RESET_EDGES_JS" in src

    def test_equation_label_colored_through_styles(self):
        src = self._src("plotter")
        assert "<span style='color:#a6adc8;font-size:11px'>" not in src
        assert "cmap_text_divs" in src and "_step('cmap equation labels'" in src

    def test_page_wide_no_autofill_installed(self):
        src = self._src("plotter")
        assert "self._install_no_autofill()" in src
        assert "addEventListener('pointerdown', mark, true)" in src


# --------------------------------------------------------------------------
# Scatter: one dragged line is enough (2026-11-18, "does min/max restrict
# anything?" -- a sqrt scatter on TW Hya re-shaded unchanged)
# --------------------------------------------------------------------------

class TestScatterManualSpan:
    def _span(self, scaling, vmin, vmax, auto=(0.0, 100.0)):
        from types import SimpleNamespace
        from cubevis.toolbox.visplot.data._scatter_render import _manual_span
        lyr = SimpleNamespace(scaling=scaling, scaling_vmin=vmin, scaling_vmax=vmax)
        return _manual_span(lyr, list(auto) if auto is not None else None)

    def test_no_override_keeps_automatic(self):
        assert self._span("sqrt", None, None) == [0.0, 100.0]

    def test_max_alone_applies(self):
        assert self._span("sqrt", None, 40.0) == [0.0, 40.0]

    def test_min_alone_applies(self):
        assert self._span("linear", 25.0, None) == [25.0, 100.0]

    def test_both(self):
        assert self._span("log", 5.0, 50.0) == [5.0, 50.0]

    def test_crossed_bounds_fall_back(self):
        assert self._span("sqrt", 60.0, 40.0) == [0.0, 100.0]

    def test_threshold_uses_cutoff_only(self):
        assert self._span("threshold", 3.5, 10.0) == [3.5, 100.0]
        assert self._span("threshold", 3.5, None, auto=None) == [3.5, 4.5]


class TestTextInputViewGuard:
    """Gear-tab text fields: Bokeh's never-rendered twin view threw on every
    value write (TW Hya, Bokeh 3.10), so dragged lines never reached their
    min/max fields.  Checked live (headless Chrome, Bokeh 3.10): a guarded
    unrendered view takes the write; without the placeholder it throws
    "Cannot set properties of undefined (setting 'value')"."""

    def test_installed_at_page_load(self):
        import inspect
        from cubevis.toolbox.visplot import visibility_plotter as vp
        src = inspect.getsource(vp)
        assert "self._install_text_input_view_guard()" in src
        js = vp._TEXT_INPUT_VIEW_GUARD_JS
        assert "Bokeh.Models.get('TextInput').prototype.default_view" in js
        assert "if (this.input_el == null) this.input_el = document.createElement('input');" in js
        assert "window.__cvTextInputViewGuarded = true" in js

    @needs_node
    def test_wrapped_connect_gives_placeholder_then_original_runs(self):
        from cubevis.toolbox.visplot import visibility_plotter as vp
        r = _node(f"""
class TextLike {{ connect_signals() {{ this.connected = true; this.saw_input = !!this.input_el; }} }}
class TextInputView extends TextLike {{}}
const document = {{createElement: (t) => ({{tag: t, value: ''}})}};
const Bokeh = {{Models: {{get: () => ({{prototype: {{default_view: TextInputView}}}})}}}};
new Function('window', 'Bokeh', 'document', 'console', {json.dumps(vp._TEXT_INPUT_VIEW_GUARD_JS)})(window, Bokeh, document, console);
const orphan = new TextInputView(); orphan.connect_signals();
const real = new TextInputView(); real.input_el = {{tag: 'real'}}; real.connect_signals();
console.log(JSON.stringify({{guarded: !!window.__cvTextInputViewGuarded,
  orphan: [orphan.connected, orphan.saw_input, orphan.input_el.tag],
  real: [real.connected, real.input_el.tag]}}));
""")
        assert r == {"guarded": True, "orphan": [True, True, "input"], "real": [True, "real"]}


def test_live_scaling_requests_show_busy():
    """Every live update_scaling round trip sets the shared busy state and
    clears it in its reply (2026-11-18: with no busy cursor it was easy to
    keep adjusting while a reply was still on its way)."""
    import inspect
    from cubevis.toolbox.visplot import visibility_raster, visibility_scatter
    for mod in (visibility_raster, visibility_scatter):
        src = inspect.getsource(mod)
        sends = src.count("comm.send('{msg_update_scaling}'")
        assert sends == 5
        assert src.count("window.__cvSetBusy(true);\ncomm.send('{msg_update_scaling}'") == sends
        assert src.count("function(resp) {{\nwindow.__cvSetBusy(false);") == sends


def test_eq_hist_clamps_outside_band_to_colorbar_ends():
    """eq_hist with a manual band: values below the band take the colorbar's
    bottom (0), above it the top (1) -- as every other scaling clamps.
    (Before 2026-11-18, below-band values took the first bin's CDF share.)"""
    import numpy as np
    from cubevis.toolbox.gridplot import colormap_scaling as cms
    vals = np.array([5.0, 50.0, 110.0, 121.0, 130.0, np.nan])
    out = cms.equalize_histogram(vals, reference=np.array([119.5, 120.0, 121.0, 122.0]))
    assert out[0] == out[1] == out[2] == 0.0
    assert 0.0 < out[3] < 1.0 and out[4] == 1.0 and np.isnan(out[5])
