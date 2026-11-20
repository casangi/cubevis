"""test_custom_window.py -- "Custom…" in the statistic-window dropdowns.

Pre-preview feedback (#24): the Time window and Channel window offered a
fixed list only, though the constructor takes any value.  debug-0003
adds "Custom…": it opens a box under the dropdown, and Enter checks the
text, adds it to the list and selects it (``_CUSTOM_WINDOW_JS``).  The
JavaScript runs under node against plain-object stand-ins; every value it
produces must be one the Python normalizers accept.

Run:  python -m pytest test_custom_window.py -q
"""
from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from cubevis_test_paths import ensure_cubevis_importable

ensure_cubevis_importable()

from cubevis.toolbox.visplot import visibility_plotter as vp            # noqa: E402
from cubevis.toolbox.visplot.data._raster_stats import (                 # noqa: E402
    normalize_chan_window, normalize_time_window)

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")

_HARNESS = r"""
const sel = {value: START, options: OPTIONS};
const inp = {value: '', visible: false, tags: [{prev: START}]};
const notify_div = {text: ''};
const run = (cb_obj) => new Function('cb_obj', 'sel', 'inp', 'unit', 'what', 'notify_div', 'marker', CODE)(
    cb_obj, sel, inp, UNIT, 'Time window', notify_div, '<span data-cv-help="vp-twin"></span>');
"""


def _run(unit: str, script: str, start="auto", options=None):
    options = options or vp._window_options(
        vp._STAT_TIME_WINDOW_OPTIONS if unit == "s" else vp._STAT_CHAN_WINDOW_OPTIONS,
        start, unit)
    code = (_HARNESS.replace("START", json.dumps(start))
            .replace("OPTIONS", json.dumps([list(o) for o in options]))
            .replace("UNIT", json.dumps(unit))
            .replace("CODE", json.dumps(vp._CUSTOM_WINDOW_JS)) + script)
    out = subprocess.run([NODE, "-e", code], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_custom_is_offered_last():
    opts = vp._window_options(vp._STAT_TIME_WINDOW_OPTIONS, "auto", "s")
    assert opts[-1] == ("custom", "Custom…")
    opts = vp._window_options(vp._STAT_CHAN_WINDOW_OPTIONS, 12, "ch")
    assert ("12", "12 ch") in opts and opts[-1][0] == "custom"


def test_choosing_custom_keeps_the_value_and_opens_the_box():
    r = _run("s", """
sel.value = 'custom'; run(sel);
console.log(JSON.stringify([sel.value, inp.visible]));""")
    assert r == ["auto", True]


@pytest.mark.parametrize("text,value", [("45", "45"), ("45s", "45"), ("2min", "120"),
                                        ("1.5 h", "5400"), ("0.5", "0.5")])
def test_time_values(text, value):
    r = _run("s", f"""
sel.value = 'custom'; run(sel); inp.value = {json.dumps(text)}; run(inp);
console.log(JSON.stringify([sel.value, inp.visible, inp.value, notify_div.text,
                            sel.options[sel.options.length - 1][0]]));""")
    assert r == [value, False, "", "", "custom"]
    normalize_time_window(value)


def test_a_value_already_listed_is_selected_not_added():
    r = _run("s", """
const n = sel.options.length;
inp.value = '2 min'; run(inp);
console.log(JSON.stringify([sel.value, sel.options.length === n]));""")
    assert r == ["120", True]


@pytest.mark.parametrize("text", ["abc", "0", "-5", "5 days"])
def test_bad_time_is_a_warning_and_changes_nothing(text):
    r = _run("s", f"""
inp.visible = true; inp.value = {json.dumps(text)}; run(inp);
console.log(JSON.stringify([sel.value, inp.visible, notify_div.text]));""")
    assert r[0] == "auto" and r[1] is True
    assert r[2].startswith("⚠ Time window:") and 'data-cv-help="vp-twin"' in r[2]


def test_channels():
    r = _run("ch", """
inp.value = '12ch'; run(inp); const a = sel.value;
inp.value = '1'; inp.visible = true; run(inp);
console.log(JSON.stringify([a, sel.value, /at least 2/.test(notify_div.text)]));""", start="off")
    assert r == ["12", "12", True]
    normalize_chan_window("12")


def test_the_warning_clears_once_a_good_value_is_given():
    r = _run("s", """
inp.value = 'x'; run(inp); inp.value = '30'; run(inp);
console.log(JSON.stringify([sel.value, notify_div.text]));""")
    assert r == ["30", ""]


def test_wired_on_all_four_dropdowns():
    import inspect
    src = inspect.getsource(vp)
    for args in ('(rt_sel, "twin", "s", "Time window", "vp-twin")',
                 '(rc_sel, "cwin", "ch", "Channel window", "vp-cwin")',
                 '(st_sel, "s_twin", "s", "Time window", "vp-twin")',
                 '(sc_sel, "s_cwin", "ch", "Channel window", "vp-cwin")'):
        assert "_custom_window" + args in src
