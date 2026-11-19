"""test_scan_list_format.py -- the cursor readout's scan list.

Pre-preview feedback (2026-11-18): the readout listed every scan behind a
cell, unsorted ("10, 11, ..., 19, 2, 20, ...") and at any length.
``selection_text.format_scan_list`` sorts numerically, writes consecutive
runs as ``a~b`` (the Scan box's syntax) and shortens beyond five pieces.

Run:  python -m pytest test_scan_list_format.py -q
"""
from cubevis_test_paths import ensure_cubevis_importable

ensure_cubevis_importable()

from cubevis.toolbox.visplot.selection_text import (  # noqa: E402
    format_scan_list, parse_scan_text)

REPORTED = ["10", "11", "12", "13", "14", "15", "16", "17", "18", "19", "2",
            "20", "21", "22", "3", "30", "4", "40", "41", "42", "5", "6", "7",
            "8", "9"]


def test_reported_case_sorted_and_ranged():
    assert format_scan_list(REPORTED) == "2~22, 30, 40~42 (25 scans)"


def test_ranges_never_hide_gaps():
    assert format_scan_list(["1", "2", "3", "5", "6", "7"]) == "1~3, 5~7 (6 scans)"


def test_pairs_stay_listed():
    assert format_scan_list(["4", "5", "9"]) == "4, 5, 9"


def test_short_list_unchanged_but_sorted():
    assert format_scan_list(["10", "2", "3"]) == "2, 3, 10"


def test_long_list_shortened_with_count():
    odd = [str(i) for i in range(1, 30, 2)]
    assert format_scan_list(odd) == "1, 3, 5, 7, 9, … (15 scans)"


def test_limit_none_never_shortens():
    odd = [str(i) for i in range(1, 30, 2)]
    assert format_scan_list(odd, limit=None) == ", ".join(odd)


def test_one_scan_and_duplicates():
    assert format_scan_list(["7", "7", " 7 "]) == "7"
    assert format_scan_list([]) == ""


def test_non_numeric_names_after_numbers():
    assert format_scan_list(["scanB", "12", "scanA", "2"]) == "2, 12, scanA, scanB"


def test_text_selects_the_same_scans():
    """A readout's list (before "(N scans)"), typed into the Scan box,
    selects what it lists."""
    have = [str(i) for i in range(1, 60)]
    text = format_scan_list(REPORTED, limit=None).split(" (")[0]
    assert sorted(parse_scan_text(text, have), key=int) == sorted(REPORTED, key=int)
