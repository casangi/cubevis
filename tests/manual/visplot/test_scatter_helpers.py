"""
test_scatter_helpers.py
========================
Unit tests for VisibilityScatter's new pure-logic helpers
(_click_window, _rect_title, _probe_region_page).

Imports the real VisibilityScatter class directly -- no AST extraction,
no bespoke env var. `_rect_title`/`_probe_region_page` are plain
@staticmethod attributes; accessed via the class in modern Python they
are already the plain, callable function -- no `.__func__` unwrapping
needed (or valid: calling `.__func__` on an already-unwrapped function
raises AttributeError, which is exactly what an earlier version of this
file did).

    python test_scatter_helpers.py
"""
from __future__ import annotations

import math
import sys

from cubevis_test_paths import ensure_cubevis_importable

ensure_cubevis_importable()

from cubevis.toolbox.visplot.visibility_scatter import VisibilityScatter

_click_window = VisibilityScatter._click_window
_rect_title = VisibilityScatter._rect_title
_probe_region_page = VisibilityScatter._probe_region_page

FAILURES = []


def check(label, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {label}" + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(label)


class FakeSelf:
    def __init__(self, viewport, x_range, y_range, cw, ch):
        self._current_viewport = viewport
        self._x_range = x_range
        self._y_range = y_range
        self._canvas_width = cw
        self._canvas_height = ch


def call_click_window(fake_self, x, y):
    return _click_window(fake_self, x, y)


def run_checks():
    # -------------------------------------------------------------
    # In-range click: should return a small rectangle centred on
    # (x, y), sized as (viewport width / canvas width) etc.
    # -------------------------------------------------------------
    fs = FakeSelf(viewport=(0.0, 100.0, 0.0, 50.0), x_range=(0.0, 100.0),
                  y_range=(0.0, 50.0), cw=1000, ch=500)
    xr_, yr_ = call_click_window(fs, 50.0, 25.0)
    check("in-range click returns non-None ranges", xr_ is not None and yr_ is not None,
          (xr_, yr_))
    if xr_ is not None:
        expected_dx = 100.0 / 1000  # = 0.1
        expected_dy = 50.0 / 500    # = 0.1
        check("x window is centred on the click x, one canvas pixel wide",
              math.isclose(xr_[0], 50.0 - expected_dx / 2) and
              math.isclose(xr_[1], 50.0 + expected_dx / 2),
              xr_)
        check("y window is centred on the click y, one canvas pixel tall",
              math.isclose(yr_[0], 25.0 - expected_dy / 2) and
              math.isclose(yr_[1], 25.0 + expected_dy / 2),
              yr_)

    # -------------------------------------------------------------
    # Out-of-range click: outside the current viewport -> (None, None).
    # -------------------------------------------------------------
    xr2, yr2 = call_click_window(fs, 500.0, 25.0)   # x way outside [0,100]
    check("out-of-range click (x) returns (None, None)", xr2 is None and yr2 is None,
          (xr2, yr2))

    xr3, yr3 = call_click_window(fs, 50.0, -10.0)   # y outside [0,50]
    check("out-of-range click (y) returns (None, None)", xr3 is None and yr3 is None,
          (xr3, yr3))

    # -------------------------------------------------------------
    # Degenerate canvas size (0 or negative width/height should not
    # raise, and should fall back to a full-extent-derived window
    # rather than a zero-width one).
    # -------------------------------------------------------------
    fs_degenerate = FakeSelf(viewport=None, x_range=(0.0, 100.0), y_range=(0.0, 50.0),
                              cw=0, ch=0)
    try:
        xr4, yr4 = call_click_window(fs_degenerate, 50.0, 25.0)
        check("degenerate canvas size (0x0) doesn't raise, produces a "
              "non-degenerate (nonzero-width) window",
              xr4 is not None and xr4[1] > xr4[0] and yr4[1] > yr4[0],
              (xr4, yr4))
    except Exception as exc:
        check("degenerate canvas size (0x0) doesn't raise", False, repr(exc))

    # -------------------------------------------------------------
    # _current_viewport=None falls back to _x_range/_y_range.
    # -------------------------------------------------------------
    fs_no_viewport = FakeSelf(viewport=None, x_range=(10.0, 20.0), y_range=(0.0, 10.0),
                               cw=100, ch=100)
    xr5, yr5 = call_click_window(fs_no_viewport, 15.0, 5.0)
    check("_current_viewport=None falls back to _x_range/_y_range for the bounds check",
          xr5 is not None, (xr5, yr5))

    # -------------------------------------------------------------
    # _rect_title
    # -------------------------------------------------------------
    title = _rect_title((1.23456, 7.891), (0.001, 0.002))
    check("_rect_title formats both ranges with en-dash separators",
          title.startswith("(") and title.endswith(")") and "\u2013" in title,
          title)
    check("_rect_title returns empty string when either range is None",
          _rect_title(None, (0, 1)) == "" and _rect_title((0, 1), None) == "",
          (_rect_title(None, (0, 1)), _rect_title((0, 1), None)))

    # -------------------------------------------------------------
    # _probe_region_page
    # -------------------------------------------------------------
    page = _probe_region_page("Test Title", "<p>body</p>")
    check("_probe_region_page produces a full HTML document",
          page.strip().startswith("<!DOCTYPE html>") and "<html>" in page,
          page[:60])
    check("_probe_region_page embeds the given title in <title> and <h2>",
          "<title>Test Title</title>" in page and "<h2>Test Title</h2>" in page,
          page)
    check("_probe_region_page embeds the given body verbatim",
          "<p>body</p>" in page, page)

    # HTML-escaping safety: a title/body containing '<'/'>'/'&' shouldn't
    # break the surrounding markup structure when it's from
    # untrusted-ish data (e.g. a field name with special characters).
    page2 = _probe_region_page("<script>x</script>", "<p>ok</p>")
    check("_probe_region_page escapes '<'/'>' in the title (no raw <script> tag)",
          "<script>x</script>" not in page2 and "&lt;script&gt;" in page2,
          page2)


if __name__ == "__main__":
    run_checks()
    print("\n" + "=" * 60)
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S):")
        for f in FAILURES:
            print(" -", f)
        sys.exit(1)
    else:
        print("All checks passed.")
