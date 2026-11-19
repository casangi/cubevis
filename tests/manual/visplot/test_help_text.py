"""test_help_text.py -- status-area help, its ⓘ, and ? to the documentation.

Pre-preview feedback (2026-11-18): several hover-help texts were far
longer than the fixed five-line status area.  Reworked 2026-11-19: the
long form of each help is in the documentation
(docs/source/applications/visplot_help.md); a hint spec is a string or
``(tracking, label)``.  What the status area shows with a label (a help,
or a message carrying a marker) gets a ⓘ, and **?** opens
``<docs>/help/<label>.html`` for the first such thing displayed (**h**
did too until it was removed, 2026-11-19).  A help without a label must
fit the area (``TRACKING_BUDGET``).  That every label exists in the documentation is
test_help_labels.py's.

The key handler runs under ``node`` against plain-object stand-ins (the
convention of test_iteration_step.py); the live behavior was checked in
headless Chrome (Bokeh 3.10) against a local documentation build:
Outlier from neighbors -> visplot_help.html#vp-flagf-outlier, the Scan
warning -> #vp-scan; the ⓘ on help and on marked messages.

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
from cubevis.toolbox.visplot.flag_controls import FlagController, DOCUMENTED_FILTERS  # noqa: E402
from cubevis.toolbox.visplot.flag_filters import FilterRegistry  # noqa: E402

NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node not on PATH")


def _fake_flags():
    return SimpleNamespace(registry=FilterRegistry(None),
                           capabilities=lambda: {"write": True, "casa": True})


def _all_specs() -> dict:
    """Every hint spec reachable without building a plotter: the fixed
    ones, the preset buttons', the flag filters' and the export group's."""
    specs = {f"static:{k}": v for k, v in vp._STATIC_HINTS.items()}
    specs.update({f"preset:{name}": text for name, _l, _w, text in vp._PRESET_BUTTONS})
    fake = _fake_flags()
    specs.update({f"filter:{k}": v for k, v in FlagController.filter_hints(fake).items()})
    specs["flag:export"] = FlagController.export_hint(fake)
    return specs


SPECS = _all_specs()


@pytest.mark.parametrize("name", sorted(SPECS))
def test_unlabelled_text_fits(name):
    """Without a documentation entry there is nowhere to read the rest."""
    tracking, label = ht.split_spec(SPECS[name])
    if label:
        assert label.startswith(ht.LABEL_PREFIX), name
        return
    n = len(ht.visible_text(tracking))
    assert n <= ht.TRACKING_BUDGET, f"{name}: {n} characters"


@pytest.mark.parametrize("name", sorted(SPECS))
def test_no_cue_written_into_the_text(name):
    """The cue is drawn in the browser where the text is cut off."""
    tracking, _label = ht.split_spec(SPECS[name])
    assert "press <b>h</b>" not in tracking and "for more" not in tracking, name


def test_the_long_ones_have_entries():
    for name in ("static:flag_reach", "static:bcombine", "static:s_avg", "static:r_axes",
                 "preset:antmatrix", "filter:flagf_outlier", "flag:export"):
        assert ht.split_spec(SPECS[name])[1], name


def test_every_builtin_filter_has_an_entry():
    reg = FilterRegistry(None)
    assert set(reg.gui_names()) == set(DOCUMENTED_FILTERS)
    for n in reg.gui_names():
        assert ht.split_spec(SPECS[f"filter:flagf_{n}"])[1] == "vp-flagf-" + n.replace("_", "-")


def test_markers_and_selection_labels():
    m = ht.marker("vp-scan")
    assert ht.visible_text(m) == "" and ht.label_in("text" + m) == "vp-scan"
    assert ht.label_in("plain") is None and ht.marker(None) == ""
    assert ht.label_in(ht.with_selection_label("\u26a0 Scan: '2-15' bad")) == "vp-scan"
    assert ht.label_in(ht.with_selection_label("\u26a0 Time range: x")) == "vp-time"
    assert ht.label_in(ht.with_selection_label("UV range: x")) == "vp-uvrange"
    assert ht.with_selection_label("\u26a0 Field: x") == "\u26a0 Field: x"


def test_urls(monkeypatch):
    assert ht.help_url("vp-scan", "https://x/v1.2/") == "https://x/v1.2/help/vp-scan.html"
    assert ht.help_url(None, "https://x/") == "https://x/help/index.html"
    monkeypatch.setenv("CUBEVIS_DOCS_URL", "file:///tmp/site")
    assert ht.docs_base() == "file:///tmp/site/"
    monkeypatch.delenv("CUBEVIS_DOCS_URL")
    from cubevis import _docs
    monkeypatch.setattr(_docs, "_version", lambda: "1.2.3")
    assert ht.docs_base() == _docs.SITE + "v1.2/"
    monkeypatch.setattr(_docs, "_version", lambda: "1.2.4.dev3+gabc")
    assert ht.docs_base() == _docs.SITE


def test_no_british_spelling_in_filter_help():
    for k, spec in SPECS.items():
        assert "neighbour" not in ht.split_spec(spec)[0], k


# --------------------------------------------------------------------------
# JavaScript under node
# --------------------------------------------------------------------------

_HARNESS = r"""
const listeners = [];
const opened = [];
const window = {open: (url, name) => { opened.push({url, name}); return {focus() {}}; }};
const document = {addEventListener: (t, f) => listeners.push([t, f])};
function key(k, path, extra) {
    let prevented = false;
    const e = Object.assign({key: k, ctrlKey: false, metaKey: false, altKey: false, repeat: false,
        defaultPrevented: false, composedPath: () => path, preventDefault: () => { prevented = true; },
        stopPropagation() {}, stopImmediatePropagation() {}}, extra || {});
    for (const [t, f] of listeners) if (t === 'keydown') f(e);
    return prevented;
}
const hint = (visible, label, text) => ({visible, text: text || '', tags: label ? [{cv_help: label}] : []});
const rowdiv = (text) => ({visible: true, text: text || '', tags: [{cv_status_row: true}]});
const status_row = {visible: true};
"""


def _node(script: str):
    out = subprocess.run([NODE, "-e", _HARNESS + script], capture_output=True,
                         text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


def _install(sources_js: str) -> str:
    return (f"const sources = {sources_js}; const base = 'https://d/v1.1/';\n"
            f"new Function('window', 'document', 'sources', 'status_row', 'base', 'console', "
            f"{json.dumps(ht.KEY_HANDLER_JS)})(window, document, sources, status_row, base, console);\n")


@needs_node
class TestHelpKey:
    SOURCES = "[hint(false, 'vp-a'), hint(true, 'vp-b'), rowdiv(''), rowdiv('status')]"

    def test_question_mark_opens_the_entry_of_the_help_showing(self):
        r = _node(_install(self.SOURCES) + """
const p = key('?', [{tagName: 'DIV'}, {tagName: 'BODY'}]);
console.log(JSON.stringify({p, opened}));""")
        assert r == {"p": True, "opened": [{"url": "https://d/v1.1/help/vp-b.html",
                                            "name": "visplot-help"}]}

    def test_letters_do_not_open_help(self):
        # h worked too for a day; removed rather than left undocumented,
        # which keeps every letter free (vi-style h/j/k/l, I/J/K/L).
        r = _node(_install(self.SOURCES) + """
let stopped = 0;
for (const k of ['h', 'H', 'i']) key(k, [{tagName: 'DIV'}], {stopPropagation: () => stopped++});
console.log(JSON.stringify([opened.length, stopped]));""")
        assert r == [0, 0]

    def test_a_help_without_entry_passes_to_the_marked_warning(self):
        # The help showing has no entry (no ⓘ) and the warning above it
        # has one (a ⓘ): ? opens the warning's, the one thing marked.
        r = _node(_install("[hint(true, null), rowdiv('w<span data-cv-help=\"vp-scan\"></span>')]") + """
const p = key('?', [{tagName: 'DIV'}]); console.log(JSON.stringify({p, o: opened.map(o => o.url)}));""")
        assert r == {"p": True, "o": ["https://d/v1.1/help/vp-scan.html"]}

    def test_the_help_showing_wins_over_the_warning(self):
        r = _node(_install("[hint(true, 'vp-a'), rowdiv('w<span data-cv-help=\"vp-scan\"></span>')]") + """
key('?', [{tagName: 'DIV'}]); console.log(JSON.stringify(opened.map(o => o.url)));""")
        assert r == ["https://d/v1.1/help/vp-a.html"]

    def test_a_handled_key_goes_no_further(self):
        # Jupyter (which has its own keyboard shortcuts) must not see
        # it; an unhandled key it must.
        r = _node(_install(self.SOURCES) + """
let stopped = 0;
const ex = {stopPropagation: () => stopped++, stopImmediatePropagation: () => stopped++};
key('?', [{tagName: 'DIV'}], ex);
const before = stopped;
sources[1].visible = false;
key('?', [{tagName: 'DIV'}], ex);
console.log(JSON.stringify([before, stopped, window.__cvHelpBase]));""")
        assert r == [2, 2, "https://d/v1.1/"]

    def test_marked_warning_when_no_help_shows(self):
        r = _node(_install("[hint(false, 'vp-a'), rowdiv('\u26a0 Scan: x<span data-cv-help=\"vp-scan\"></span>'), rowdiv('s')]") + """
key('?', [{tagName: 'DIV'}]); console.log(JSON.stringify(opened.map(o => o.url)));""")
        assert r == ["https://d/v1.1/help/vp-scan.html"]

    def test_status_row_hidden_is_not_displayed(self):
        r = _node(_install("[hint(false, 'vp-a'), rowdiv('w<span data-cv-help=\"vp-scan\"></span>')]") + """
status_row.visible = false; key('?', [{tagName: 'DIV'}]); console.log(JSON.stringify(opened.length));""")
        assert r == 0

    def test_nothing_labelled_does_nothing(self):
        r = _node(_install("[hint(false, 'vp-a'), rowdiv(''), rowdiv('Field: all')]") + """
const p = key('?', [{tagName: 'DIV'}]); console.log(JSON.stringify({p, n: opened.length}));""")
        assert r == {"p": False, "n": 0}

    def test_typing_in_a_field_inside_shadow_dom_does_not_open(self):
        r = _node(_install(self.SOURCES) + """
key('?', [{tagName: 'INPUT'}, {tagName: 'DIV'}, {tagName: 'FIELDSET'}]);
key('?', [{tagName: 'SELECT'}, {tagName: 'DIV'}]);
key('?', [{tagName: 'DIV', isContentEditable: true}]);
console.log(JSON.stringify(opened.length));""")
        assert r == 0

    def test_modifiers_and_other_keys_ignored(self):
        r = _node(_install(self.SOURCES) + """
key('?', [{tagName: 'DIV'}], {metaKey: true});
key('?', [{tagName: 'DIV'}], {ctrlKey: true});
key('/', [{tagName: 'DIV'}]);
console.log(JSON.stringify(opened.length));""")
        assert r == 0


@needs_node
def test_help_button_opens_the_index():
    r = _node("""
const base = 'https://d/';
new Function('window', 'base', 'console', %s)(window, base, console);
console.log(JSON.stringify(opened));
""" % json.dumps(ht.HELP_BUTTON_JS))
    assert r == [{"url": "https://d/help/index.html", "name": "visplot-help"}]


@needs_node
def test_cue_js_is_safe_without_bokeh():
    r = _node("new Function('window', %s)(window); window.__cvHelpCue({visible: true, tags: []});"
              "console.log(JSON.stringify(typeof window.__cvHelpCue));" % json.dumps(ht.CUE_JS))
    assert r == "function"


_CUE_HARNESS = r"""
const el = (tag) => ({tag, children: [], style: {}, className: '', textContent: '', title: '',
    listeners: {}, appendChild(c) { this.children.push(c); c.parent = this; return c; },
    addEventListener(t, f) { this.listeners[t] = f; }, remove() {
        const p = this.parent; if (p) p.children = p.children.filter(x => x !== this); },
    querySelectorAll(sel) { const out = []; const walk = (n) => { for (const c of n.children) {
        if (c.className === 'cv-info') out.push(c); walk(c); } }; walk(this); return out; }});
const cueDocument = {createElement: el};
const requestAnimationFrame = (f) => f();
const getComputedStyle = () => ({backgroundColor: '#000'});
function viewOf(div) {
    // As in Bokeh: the markup element is inside the shadow root.
    const v = {model: div, el: {scrollHeight: 50, clientHeight: 96}, shadow_el: el('shadow'),
               markup_el: el('markup'), _child_views: new Map()};
    v.shadow_el.appendChild(v.markup_el);
    return v;
}
"""


def _cue(script: str):
    return _node(_CUE_HARNESS + "const Bokeh = {index: {}};\n"
                 + "new Function('window', 'Bokeh', 'document', 'requestAnimationFrame', "
                 + "'getComputedStyle', 'console', %s)(window, Bokeh, cueDocument, "
                   "requestAnimationFrame, getComputedStyle, console);\n" % json.dumps(ht.CUE_JS)
                 + script)


@needs_node
class TestInfoMark:
    def test_help_with_entry_gets_a_corner_mark_even_when_it_fits(self):
        r = _cue("""
const d = {visible: true, tags: [{cv_help: 'vp-a'}], text: 'short'};
const v = viewOf(d); Bokeh.index.x = {model_view: v};
window.__cvHelpCue(d);
const box = v.shadow_el.children[1];
console.log(JSON.stringify([v.shadow_el.children.length, box.style.position, box.children[0].textContent,
                            v.markup_el.children.length]));""")
        assert r == [2, "sticky", "\u24d8", 0]

    def test_message_with_marker_gets_an_inline_mark_that_opens_it(self):
        r = _cue("""
const got = [];
window.__cvHelpBase = 'https://d/'; window.__cvOpenHelp = (b, l) => got.push(b + l);
const d = {visible: true, tags: [{cv_status_row: true}], text: '\u26a0 x<span data-cv-help="vp-scan"></span>'};
const v = viewOf(d); Bokeh.index.x = {model_view: v};
window.__cvHelpCue(d);
const m = v.markup_el.children[0];
m.listeners.click({stopPropagation() {}});
console.log(JSON.stringify([m.textContent, got]));""")
        assert r == ["\u24d8", ["https://d/vp-scan"]]

    def test_no_entry_no_mark_and_a_stale_one_goes(self):
        r = _cue("""
const d = {visible: true, tags: [{cv_status_row: true}], text: 'x<span data-cv-help="vp-scan"></span>'};
const v = viewOf(d); Bokeh.index.x = {model_view: v};
window.__cvHelpCue(d);
const n1 = v.markup_el.children.length;
d.text = 'Plotted 3 panels';
window.__cvHelpCue(d);
const h = {visible: true, tags: [], text: 'plain help'};
const hv = viewOf(h); Bokeh.index.y = {model_view: hv};
window.__cvHelpCue(h);
console.log(JSON.stringify([n1, v.markup_el.children.length, hv.shadow_el.children.length]));""")
        assert r == [1, 0, 1]   # the plain help: its markup only


def test_wiring_in_plotter():
    import inspect
    src = inspect.getsource(vp)
    assert "tags=_helptext.help_tag(label)" in src
    assert 'self._hover(self._help_button(dark_btn), "tb_help")' in src
    assert "_helptext.KEY_HANDLER_JS" in src
    assert "window.__cvHelpCue(hint)" in vp._SHOW_HINT_JS
    assert "window.__cvHelpCue(_shown)" in vp._NOTIFY_CHANGED_JS
    assert "window.__cvHelpCue(notify_div)" in vp._HIDE_HINT_JS
    assert "__cvHelpCue(cb_obj)" in vp._STATUS_TEXT_CUE_JS
    assert "d.js_on_change(\"text\", CustomJS(code=_STATUS_TEXT_CUE_JS))" in src


# --------------------------------------------------------------------------
# A warning stays visible above the help (2026-11-18)
# --------------------------------------------------------------------------

def _run_status(script: str):
    js = {k: getattr(vp, k) for k in ("_SHOW_HINT_JS", "_HIDE_HINT_JS", "_NOTIFY_CHANGED_JS")}
    code = f"""
const window = {{}};
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
fire('keydown', [input, host], {key: '?'});
console.log(JSON.stringify(input.blurred));""")
        assert r == 1

    def test_installed_with_the_help_key(self):
        assert ht.LEAVE_FIELD_JS in ht.KEY_HANDLER_JS
