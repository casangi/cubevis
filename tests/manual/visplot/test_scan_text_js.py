"""test_scan_text_js.py -- the Scan box's browser-side check.

2026-11-18 (pre-preview feedback): the Scan text was only checked at
Plot, and the message was then hidden by any hover help.  It is now
checked when Return is pressed, by ``selection_text.SCAN_TEXT_JS``.  The
two parsers must agree, message for message: this runs both on the same
inputs (the JS/Python parity convention of test_iteration_step.py).

Run:  python -m pytest test_scan_text_js.py -q
"""
from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from cubevis_test_paths import ensure_cubevis_importable

ensure_cubevis_importable()

from cubevis.toolbox.visplot.selection_text import (  # noqa: E402
    SCAN_TEXT_JS, SelectionTextError, parse_scan_text)

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")

SCANS = [str(i) for i in (2, 3, 4, 5, 7, 8, 9, 12, 15, 16, 17, 20, 21, 30)]

CASES = ["", "  ", "4", "7~9", "9~7", ">15", "<5", "!5", "4, 7~9, !8", ">12, !16",
         "2-15", "2 - 15", "abc", "4,,7", "6", "40~50", "!2,!3,!4,!5,!7,!8,!9,!12,!15,!16,!17,!20,!21,!30",
         "2~30, !2~30", "3, x"]


def _python(text):
    try:
        got = parse_scan_text(text, SCANS)
        return {"ok": True, "scans": None if got is None else [int(x) for x in got]}
    except SelectionTextError as exc:
        return {"ok": False, "error": str(exc)}


def _js(texts):
    script = SCAN_TEXT_JS + f"""
const scans = {json.dumps(SCANS)};
console.log(JSON.stringify({json.dumps(texts)}.map(t => cvParseScanText(t, scans))));"""
    out = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_parity_with_python():
    js = _js(CASES)
    for text, j in zip(CASES, js):
        assert j == _python(text), text


def test_hyphen_range_says_how_to_write_it():
    r = _python("2-15")
    assert r == {"ok": False, "error": "Scan: '2-15' — a range is written with ~, as 2~15"}


def test_plotter_checks_on_return():
    import inspect
    from cubevis.toolbox.visplot import visibility_plotter as vp
    src = inspect.getsource(vp)
    assert "scan_inp.js_on_event(ValueSubmit, _scan_check)" in src
    # ...and when the value is committed by leaving the box (a scroll, Esc)
    assert 'scan_inp.js_on_change("value", _scan_check)' in src
    assert "const r = cvParseScanText(cb_obj.value, scans);" in src
