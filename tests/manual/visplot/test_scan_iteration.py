"""test_scan_iteration.py -- Prev / ✱ / Next for Scan (debug-0003).

Pre-preview feedback (#24): PlotMS's scan iteration.  Scan stays a text
box; the steppers walk the scans it selects (all when empty), only those
of the field shown, and Plot requests carry the current scan instead of
the box's text (``scan_iteration.py``).  The JavaScript runs under node
against plain-object stand-ins, as the other iteration tests do.

Run:  python -m pytest test_scan_iteration.py -q
"""
from __future__ import annotations

import json
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from cubevis_test_paths import ensure_cubevis_importable

ensure_cubevis_importable()

from cubevis.toolbox.visplot import scan_iteration as si          # noqa: E402
from cubevis.toolbox.visplot import visibility_plotter as vp      # noqa: E402
from cubevis.toolbox.visplot.iteration_step import STEP_INDEX_JS  # noqa: E402
from cubevis.toolbox.visplot.selection_text import SCAN_TEXT_JS   # noqa: E402

NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node not on PATH")

# Two fields, scans interleaved as on a calibrator / target cycle.
TAB = si.scan_table([SimpleNamespace(scan_name=str(n), field_name=("CAL" if n % 2 else "TGT"))
                     for n in range(1, 9)] + [SimpleNamespace(scan_name="x", field_name="CAL")])


def test_scan_table():
    assert TAB["nos"] == list(range(1, 9))                 # "x" is not a scan number
    assert TAB["fields"]["3"] == ["CAL"] and TAB["fields"]["4"] == ["TGT"]
    meta_like = [SimpleNamespace(name="12", field_name=""), SimpleNamespace(name="3", field_name="")]
    assert si.scan_table(meta_like) == {"nos": [3, 12], "fields": {"3": [], "12": []}}


def _run(script: str, text: str = "", field: str = "", tab=None):
    head = (f"const window = {{}};\n"
            f"const scan_inp = {{value: {json.dumps(text)}}};\n"
            f"const field_sel = {{value: {json.dumps(field)}}};\n"
            f"const scan_note = {{text: '', visible: false}};\n"
            f"const notify_div = {{text: ''}};\n"
            f"const scan_marker = '<m>';\n"
            f"const scan_tab = {json.dumps(TAB if tab is None else tab)};\n")
    code = (head + "(function () {" + STEP_INDEX_JS + SCAN_TEXT_JS + si.SCAN_ITER_JS
            + si.SCAN_ITER_JS + script + "})();")     # included twice, as in a Next button
    out = subprocess.run([NODE, "-e", code], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


@needs_node
class TestStepping:
    def test_empty_box_all_scans_then_wrap(self):
        r = _run("""
const seen = [];
for (let i = 0; i < 9; i++) { doIterateScan(1); seen.push(window.__cvScanCur.scan); }
console.log(JSON.stringify([seen, scan_note.text, scan_note.visible, cvScanPlotText()]));""")
        assert r[0] == [1, 2, 3, 4, 5, 6, 7, 8, 1]
        assert r[1] == "▶ Scan 1 · 1 of 8" and r[2] is True and r[3] == "1"

    def test_the_box_selects_the_scans(self):
        r = _run("""
const seen = [];
for (let i = 0; i < 4; i++) { doIterateScan(1); seen.push(window.__cvScanCur.scan); }
doIterateScan(-1); doIterateScan(-1);
console.log(JSON.stringify([seen, window.__cvScanCur.scan, scan_inp.value]));""", text="2~6, !4")
        assert r == [[2, 3, 5, 6], 3, "2~6, !4"]          # the box is left alone

    def test_only_the_field_shown(self):
        r = _run("""
const seen = [];
for (let i = 0; i < 5; i++) { doIterateScan(1); seen.push(window.__cvScanCur.scan); }
console.log(JSON.stringify([seen, scan_note.text]));""", field="TGT")
        assert r == [[2, 4, 6, 8, 2], "▶ Scan 2 · 1 of 4"]

    def test_field_change_continues_from_where_it_would_sort(self):
        r = _run("""
doIterateScan(1); doIterateScan(1); doIterateScan(1);      // scan 3 (CAL)
field_sel.value = 'TGT'; doIterateScan(1); const a = window.__cvScanCur.scan;
field_sel.value = 'CAL'; doIterateScan(-1); const b = window.__cvScanCur.scan;
console.log(JSON.stringify([a, b]));""")
        assert r == [4, 3]

    def test_end_and_plot_text(self):
        r = _run("""
doIterateScan(1); doIterateScan(1); const during = cvScanPlotText();
cvScanEnd();
console.log(JSON.stringify([during, cvScanPlotText(), scan_note.visible, window.__cvScanCur.scan]));""",
                 text="1~5")
        assert r == ["2", "1~5", False, None]

    def test_one_scan_or_a_bad_box_is_a_message(self):
        r = _run("""
doIterateScan(1); const a = [window.__cvScanCur.scan, notify_div.text];
scan_inp.value = '2-15'; doIterateScan(1);
console.log(JSON.stringify([a, notify_div.text]));""", text="7")
        assert r[0][0] is None and "nothing to step" in r[0][1]
        assert r[1].startswith("⚠ Scan: '2-15'") and r[1].endswith("<m>")


@needs_node
class TestScansWithData:
    """Issue #37: only the scans with data in the selection are stepped
    through (``window.__cvScanHas``, from the reader's ``scans_with_rows``)."""

    def test_the_first_page_list_narrows(self):
        r = _run("""
const seen = [];
for (let i = 0; i < 7; i++) { doIterateScan(1); seen.push(window.__cvScanCur.scan); }
console.log(JSON.stringify([seen, scan_note.text]));""",
                 tab=dict(TAB, has=["1", "2", "4", "6", "7", "8"]))
        assert r == [[1, 2, 4, 6, 7, 8, 1], "▶ Scan 1 · 1 of 6"]

    def test_a_later_answer_replaces_it(self):
        # A Plot reply or the rows refresh sets window.__cvScanHas.
        r = _run("""
doIterateScan(1); doIterateScan(1);                          // 1, 2
window.__cvScanHas = {names: ['2', '5', '8']};
doIterateScan(1); const a = window.__cvScanCur.scan;
doIterateScan(1); const b = window.__cvScanCur.scan;
console.log(JSON.stringify([a, b, scan_note.text]));""", tab=dict(TAB, has=["1", "2", "3"]))
        assert r == [5, 8, "▶ Scan 8 · 3 of 3"]

    def test_unknown_narrows_nothing(self):
        for tab in (dict(TAB, has=None), TAB):
            r = _run("""
const seen = [];
for (let i = 0; i < 8; i++) { doIterateScan(1); seen.push(window.__cvScanCur.scan); }
console.log(JSON.stringify(seen));""", tab=tab)
            assert r == list(range(1, 9))

    def test_with_the_box_and_the_field(self):
        r = _run("""
const seen = [];
for (let i = 0; i < 3; i++) { doIterateScan(1); seen.push(window.__cvScanCur.scan); }
console.log(JSON.stringify(seen));""", text="2~8", field="TGT",
                 tab=dict(TAB, has=["1", "2", "3", "6", "8"]))
        assert r == [2, 6, 8]

    def test_none_or_one_left_says_so(self):
        r = _run("""
doIterateScan(1); const a = [window.__cvScanCur.scan, notify_div.text];
window.__cvScanHas = {names: ['5']};
doIterateScan(1); const b = notify_div.text;
scan_inp.value = '7'; doIterateScan(1); const c = notify_div.text;
console.log(JSON.stringify([a, b, c]));""", tab=dict(TAB, has=[]))
        assert r[0][0] is None and "No scans with data in this selection" in r[0][1]
        assert "Only one scan with data in this selection" in r[1]
        assert "No scans with data in this selection" in r[2]

    def test_a_current_scan_without_data_continues_from_where_it_sorts(self):
        r = _run("""
doIterateScan(1); doIterateScan(1); doIterateScan(1);      // 3
window.__cvScanHas = {names: ['1', '2', '4', '5']};
doIterateScan(1); const a = window.__cvScanCur.scan;
console.log(JSON.stringify(a));""")
        assert r == 4


@needs_node
def test_grid_play_follows_a_current_scan():
    # The grid's Play continues the scan being stepped (cvPlayAxis).
    code = ("const window = {__cvScanCur: {scan: 3}};\n"
            "const bl_src = {selected: {indices: []}, data: {ant1: []}};\n"
            "const ant_src = {selected: {indices: []}, data: {name: []}};\n"
            "const spw_src = {selected: {indices: [0]}, data: {ident: ['0']}};\n"
            "const field_sel = null; const cb_obj = {};\n"
            + vp._GRID_PLAY_JS + "console.log(JSON.stringify(cb_obj.outer.label()));")
    out = subprocess.run([NODE, "-e", code], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout.strip()) == "scans"


def test_wiring():
    import inspect
    src = inspect.getsource(vp)
    assert "scan:        cvScanPlotText()," in src
    assert "cvIterClearAll(spw_src, ant_src, bl_src); cvScanEnd(); doPlot(false);" in src
    assert 'self._scan_iter.wire(self._plot_js_args, self._do_plot_js,' in src
    assert '"scan": _iterate_scan_js' in src
    # #37: the rows refresh and every Plot reply update the scans with data
    assert "window.__cvScanHas = {names: resp.scan_has_data};" in vp._ROWS_REFRESH_JS
    assert src.count("window.__cvScanHas = {names: resp.scan_has_data};") >= 2
    assert 'self._scan_tab["has"] = self._scans_with_data()' in src
