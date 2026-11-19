"""test_help_text.py -- status-area help that fits, with a long form.

Pre-preview feedback (2026-11-18): several hover-help texts were far
longer than the fixed five-line status area, and could not be read,
since the help follows the pointer.  ``help_text`` makes every hint a
spec, a string or ``(tracking, long_html)``; these tests hold every
tracking text to ``TRACKING_BUDGET`` and keep the "press h for more" cue
exactly where a long form exists.

The key handler and the help page run under ``node`` against plain-object
stand-ins (the convention of test_iteration_step.py); the live behavior
was checked in headless Chrome (Bokeh 3.10).

Run:  python -m pytest test_help_text.py -q
"""
from __future__ import annotations

import json
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from cubevis_test_paths import ensure_cubevis_importable

ensure_cubevis_importable()

from cubevis.toolbox.visplot import help_text as ht            # noqa: E402
from cubevis.toolbox.visplot import visibility_plotter as vp   # noqa: E402
from cubevis.toolbox.visplot.flag_controls import FlagController  # noqa: E402
from cubevis.toolbox.visplot.flag_filters import FilterRegistry  # noqa: E402

NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node not on PATH")


def _all_specs() -> dict:
    """Every hint spec reachable without building a plotter: the fixed
    ones, the preset buttons' and the flag filters'."""
    specs = {f"static:{k}": v for k, v in vp._STATIC_HINTS.items()}
    specs.update({f"preset:{name}": text for name, _l, _w, text in vp._PRESET_BUTTONS})
    fake = SimpleNamespace(registry=FilterRegistry(None))
    specs.update({f"filter:{k}": v for k, v in FlagController.filter_hints(fake).items()})
    return specs


SPECS = _all_specs()


@pytest.mark.parametrize("name", sorted(SPECS))
def test_tracking_text_fits(name):
    tracking, _long = ht.split_spec(SPECS[name])
    n = len(ht.visible_text(tracking))
    assert n <= ht.TRACKING_BUDGET, f"{name}: {n} characters"


@pytest.mark.parametrize("name", sorted(SPECS))
def test_cue_exactly_where_a_long_form_exists(name):
    tracking, long_html = ht.split_spec(SPECS[name])
    has_cue = tracking.endswith(ht.MORE_CUE)
    assert has_cue == bool(long_html), name
    if long_html:
        assert ht.MORE_CUE not in long_html


def test_the_reported_ones_now_have_long_forms():
    for name in ("static:flag_reach", "static:bcombine", "static:s_avg", "static:r_axes",
                 "preset:antmatrix", "filter:flagf_outlier"):
        assert ht.split_spec(SPECS[name])[1], name


def test_long_forms_keep_the_full_text():
    """Nothing was lost by shortening: the long form holds every point
    the old single text had (spot-checked on the longest)."""
    _t, long_html = ht.split_spec(SPECS["static:flag_reach"])
    for phrase in ("refused if they do not share exactly one", "Whole scan",
                   "Esc</b> before letting go"):
        assert phrase in long_html
    _t, long_html = ht.split_spec(SPECS["static:bcombine"])
    assert "find and flag those" in long_html and "Offered only with Vector averaging" in long_html


def test_points_html_and_title():
    assert ht.points_html("<b>A</b> — lead  | one  | two") == \
        "<p><b>A</b> — lead</p><ul><li>one</li><li>two</li></ul>"
    assert ht.points_html("only") == "<p>only</p>"
    assert ht.title_of("<b>Flag reaches</b> — x", "fallback") == "Flag reaches"
    assert ht.title_of("plain", "fallback") == "fallback"


def test_no_british_spelling_in_filter_help():
    for k, spec in SPECS.items():
        for part in ht.split_spec(spec):
            assert "neighbour" not in (part or ""), k


# --------------------------------------------------------------------------
# JavaScript under node
# --------------------------------------------------------------------------

_HARNESS = r"""
const listeners = [];
const opened = [];
const window = {open: (url, name) => { const w = {name, html: '', document: {
    open() {}, write(h) { w.html += h; }, close() {}, getElementById() { return {scrollIntoView() {}}; }},
    focus() {}}; opened.push(w); return w; }};
const document = {addEventListener: (t, f) => listeners.push([t, f])};
function key(k, path, extra) {
    let prevented = false;
    const e = Object.assign({key: k, ctrlKey: false, metaKey: false, altKey: false, repeat: false,
        defaultPrevented: false, composedPath: () => path, preventDefault: () => { prevented = true; }}, extra || {});
    for (const [t, f] of listeners) if (t === 'keydown') f(e);
    return prevented;
}
const div = (visible) => ({visible});
"""


def _node(script: str):
    out = subprocess.run([NODE, "-e", _HARNESS + script], capture_output=True,
                         text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


def _install(entries_js: str, light: bool = False) -> str:
    return (f"const entries = {entries_js}; const theme_btn = {{active: {str(light).lower()}}};\n"
            f"new Function('window', 'document', 'entries', 'theme_btn', 'console', "
            f"{json.dumps(ht.KEY_HANDLER_JS)})(window, document, entries, theme_btn, console);\n")


@needs_node
class TestHelpKey:
    ENTRIES = ("[{id: 'help-a', title: 'A', long: '<p>long A</p>', div: div(false)},"
               " {id: 'help-b', title: 'B', long: '<p>long B</p>', div: div(true)}]")

    def test_h_opens_the_showing_entry_first(self):
        r = _node(_install(self.ENTRIES) + """
const p = key('h', [{tagName: 'DIV'}, {tagName: 'BODY'}]);
const w = opened[0];
console.log(JSON.stringify({p, n: opened.length, name: w.name,
   bFirst: w.html.indexOf("id='help-b'") < w.html.indexOf("id='help-a'"),
   cur: w.html.indexOf("id='help-b' class='cur'") >= 0}));
""")
        assert r == {"p": True, "n": 1, "name": "visplot-help", "bFirst": True, "cur": True}

    def test_question_mark_too(self):
        r = _node(_install(self.ENTRIES) + "key('?', [{tagName: 'DIV'}]); console.log(JSON.stringify(opened.length));")
        assert r == 1

    def test_typing_in_a_field_inside_shadow_dom_does_not_open(self):
        r = _node(_install(self.ENTRIES) + """
key('h', [{tagName: 'INPUT'}, {tagName: 'DIV'}, {tagName: 'FIELDSET'}]);
key('h', [{tagName: 'SELECT'}, {tagName: 'DIV'}]);
key('h', [{tagName: 'DIV', isContentEditable: true}]);
console.log(JSON.stringify(opened.length));""")
        assert r == 0

    def test_modifiers_and_other_keys_ignored(self):
        r = _node(_install(self.ENTRIES) + """
key('h', [{tagName: 'DIV'}], {metaKey: true});
key('h', [{tagName: 'DIV'}], {ctrlKey: true});
key('j', [{tagName: 'DIV'}]);
console.log(JSON.stringify(opened.length));""")
        assert r == 0

    def test_nothing_showing_or_no_long_form_does_nothing(self):
        r = _node(_install("[{id: 'help-a', title: 'A', long: 'x', div: div(false)}]") + """
const p = key('h', [{tagName: 'DIV'}]); console.log(JSON.stringify({p, n: opened.length}));""")
        assert r == {"p": False, "n": 0}

    def test_light_theme_page(self):
        r = _node(_install(self.ENTRIES, light=True) + """
key('h', [{tagName: 'DIV'}]); console.log(JSON.stringify(opened[0].html.indexOf('#f8fafc') >= 0));""")
        assert r is True


@needs_node
def test_help_button_opens_index_without_anchor():
    r = _node("""
const entries = [{id: 'help-a', title: 'A', long: '<p>long A</p>', div: div(false)}];
const theme_btn = {active: false};
new Function('window', 'entries', 'theme_btn', 'console', %s)(window, entries, theme_btn, console);
console.log(JSON.stringify({n: opened.length, cur: opened[0].html.indexOf("class='cur'") >= 0,
  toc: opened[0].html.indexOf("<a href='#help-a'>A</a>") >= 0}));
""" % json.dumps(ht.HELP_BUTTON_JS))
    assert r == {"n": 1, "cur": False, "toc": True}


def test_wiring_in_plotter():
    import inspect
    src = inspect.getsource(vp)
    assert "tags=_helptext.long_tag(long_html)" in src
    assert 'self._hover(self._help_button(dark_btn), "tb_help")' in src
    assert "_helptext.KEY_HANDLER_JS" in src


# --------------------------------------------------------------------------
# A warning stays visible above the help (2026-11-18)
# --------------------------------------------------------------------------

def _run_status(script: str):
    js = {k: getattr(vp, k) for k in ("_SHOW_HINT_JS", "_HIDE_HINT_JS", "_NOTIFY_CHANGED_JS")}
    code = f"""
const M = (o) => Object.assign({{visible: true, styles: {{}}, text: ''}}, o);
const hint = M({{visible: false}}), other = M({{visible: true}});
const all_hints = [hint, other];
const status_row = M({{}}), status_div = M({{}}), notify_div = M({{styles: {{'text-align': 'right'}}}});
const run = (src, cb_obj) => (new Function('hint', 'all_hints', 'status_row', 'status_div', 'notify_div', 'cb_obj', src))(
    hint, all_hints, status_row, status_div, notify_div, cb_obj);
const S = {json.dumps(js)};
const snap = () => ({{hint: hint.visible, other: other.visible, row: status_row.visible, summary: status_div.visible,
    maxh: hint.styles['max-height'], align: notify_div.styles['text-align']}});
{script}
"""
    out = subprocess.run([NODE, "-e", code], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


@needs_node
class TestWarningStaysVisible:
    def test_no_warning_help_takes_the_whole_area(self):
        r = _run_status("run(S._SHOW_HINT_JS); console.log(JSON.stringify(snap()));")
        assert r == {"hint": True, "other": False, "row": False, "summary": True,
                     "maxh": f"{vp._STATUS_HEIGHT}px", "align": "right"}

    def test_warning_keeps_its_line_above_the_help(self):
        r = _run_status("notify_div.text = '\\u26a0 Scan: bad'; run(S._SHOW_HINT_JS); console.log(JSON.stringify(snap()));")
        assert r["row"] is True and r["summary"] is False and r["hint"] is True
        assert r["maxh"] == f"{vp._STATUS_HEIGHT - vp._HINT_ALERT_ROW_PX}px" and r["align"] == "left"

    def test_warning_arriving_while_help_shows(self):
        r = _run_status("""run(S._SHOW_HINT_JS);
notify_div.text = '\\u26a0 Scan: bad'; run(S._NOTIFY_CHANGED_JS, notify_div);
const a = snap();
notify_div.text = ''; run(S._NOTIFY_CHANGED_JS, notify_div);
console.log(JSON.stringify({a, b: snap()}));""")
        assert r["a"]["row"] is True and r["a"]["align"] == "left"
        assert r["b"]["row"] is False and r["b"]["maxh"] == f"{vp._STATUS_HEIGHT}px"

    def test_leaving_restores_the_status_row(self):
        r = _run_status("""notify_div.text = '\\u26a0 x'; run(S._SHOW_HINT_JS); run(S._HIDE_HINT_JS);
console.log(JSON.stringify(snap()));""")
        assert r["hint"] is False and r["row"] is True and r["summary"] is True and r["align"] == "right"

    def test_non_warning_notes_do_not_take_the_line(self):
        r = _run_status("notify_div.text = 'Saved 3 flags'; run(S._SHOW_HINT_JS); console.log(JSON.stringify(snap()));")
        assert r["row"] is False


# --------------------------------------------------------------------------
# Leaving a text field when the user has moved on (2026-11-18)
# --------------------------------------------------------------------------

def _run_leave(script: str):
    code = r"""
const listeners = [];
const document = {addEventListener: (t, f, o) => listeners.push([t, f]), activeElement: null};
const window = {};
function el(tag, type) { const e = {tagName: tag, blurred: 0, getAttribute: (k) => (k === 'type' ? (type || '') : null),
    blur() { this.blurred++; }}; return e; }
function fire(type, path, extra) {
    const e = Object.assign({type, composedPath: () => path, defaultPrevented: false}, extra || {});
    for (const [t, f] of listeners) if (t === type) f(e);
}
""" + f"new Function('window', 'document', {json.dumps(ht.LEAVE_FIELD_JS)})(window, document);\n" + script
    out = subprocess.run([NODE, "-e", code], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


@needs_node
class TestLeaveField:
    # The Scan box: an <input> inside a Bokeh widget's shadow root.
    SETUP = """
const input = el('INPUT', 'text');
const host = {tagName: 'DIV', shadowRoot: {activeElement: input}};
document.activeElement = host;
const other = {tagName: 'DIV'};
"""

    def test_scrolling_elsewhere_leaves_the_field(self):
        r = _run_leave(self.SETUP + "fire('wheel', [other, host]); console.log(JSON.stringify(input.blurred));")
        assert r == 1

    def test_touch_scrolling_too(self):
        r = _run_leave(self.SETUP + "fire('touchmove', [other]); console.log(JSON.stringify(input.blurred));")
        assert r == 1

    def test_scrolling_over_the_field_itself_does_not(self):
        r = _run_leave(self.SETUP + "fire('wheel', [input, host]); console.log(JSON.stringify(input.blurred));")
        assert r == 0

    def test_nothing_focused_or_not_a_text_field(self):
        r = _run_leave("""
const btn = el('BUTTON'); document.activeElement = btn; fire('wheel', [{tagName: 'DIV'}]);
const chk = el('INPUT', 'checkbox'); document.activeElement = chk; fire('wheel', [{tagName: 'DIV'}]);
console.log(JSON.stringify([btn.blurred, chk.blurred]));""")
        assert r == [0, 0]

    def test_escape_leaves_the_field(self):
        r = _run_leave(self.SETUP + """
fire('keydown', [input, host], {key: 'Escape'});
fire('keydown', [input, host], {key: 'h'});
console.log(JSON.stringify(input.blurred));""")
        assert r == 1

    def test_installed_with_the_help_key(self):
        assert ht.LEAVE_FIELD_JS in ht.KEY_HANDLER_JS
