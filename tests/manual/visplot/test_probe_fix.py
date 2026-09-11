"""
test_probe_fix.py
=================
Standalone reproduction of the visplot scatter hover-probe miss, plus
verification of the fixes.

Builds real Datashader aggs from synthetic two-polarization scatter
data, then exercises (a) the old probe algorithm and (b) the new one,
showing that the old one reports "empty" on points that are plainly
painted in the composite image.

    python test_probe_fix.py

Requires numpy / pandas / datashader / bokeh, and cubevis itself
importable (see cubevis_test_paths.py for how to point this at your
checkout if it isn't co-located with this script). Imports the real
`VisibilityScatter`/`reader.py` directly -- no AST extraction, no
re-parsing, so nothing here can drift from what a re-parsed copy would
do differently.
"""

from __future__ import annotations

import sys

import numpy as np
import pandas as pd
import datashader as ds
import datashader.reductions as ds_agg

from cubevis_test_paths import ensure_cubevis_importable

ensure_cubevis_importable()

from cubevis.toolbox.visplot.data.reader import (
    _cell_bounds, _widen_if_degenerate, _bin_membership, _agg_value,
)
from cubevis.toolbox.visplot.visibility_scatter import VisibilityScatter


def _new_probe() -> VisibilityScatter:
    """A bare, un-``__init__``-ed VisibilityScatter.

    The real constructor wants a backend/selection/figure and does real
    Bokeh setup no test in this file has any use for -- every test here
    only exercises the probe-geometry methods (`_agg_pixel`,
    `_nearest_populated_bin`, `_bin_screen_size`, `_search_radius_bins`),
    which read only whatever instance attributes each test sets
    explicitly on the object below, never anything from __init__.
    """
    return VisibilityScatter.__new__(VisibilityScatter)


# ----------------------------------------------------------------------
# Synthetic two-layer scatter data
# ----------------------------------------------------------------------

def make_layers(seed: int = 20260810, n: int = 60):
    """Two 'polarization' layers over the same x range, as visplot sees them.

    Mimics the reported configuration: UV distance on x, Real on y, XX
    and YY overlaid, zoomed far enough in that each sample owns many
    screen pixels.
    """
    rng = np.random.default_rng(seed)
    x   = np.sort(rng.uniform(26.0, 29.0, n))
    xx  = pd.DataFrame({"x": x, "y": rng.normal(-35.0, 0.4, n)})
    x2  = np.sort(rng.uniform(26.0, 29.0, n))
    yy  = pd.DataFrame({"x": x2, "y": rng.normal(-35.0, 0.4, n)})
    return [xx, yy]


def shade_aggs(dfs, x_range, y_range, w, h):
    """Exactly what _shade_all_layers does, minus the colour mapping."""
    aggs = []
    for df in dfs:
        cvs = ds.Canvas(plot_width=w, plot_height=h,
                        x_range=x_range, y_range=y_range)
        aggs.append(cvs.points(df, "x", "y", ds_agg.mean("y")))
    return aggs


# ----------------------------------------------------------------------
# The two probe algorithms
# ----------------------------------------------------------------------

def probe_old(aggs, dfs, x, y):
    """The shipped algorithm: index from agg[0], return on first layer."""
    canvas_agg = next((a for a in aggs if a is not None), None)
    if canvas_agg is None:
        return None, None
    xc = canvas_agg.coords[canvas_agg.dims[1]].values
    yc = canvas_agg.coords[canvas_agg.dims[0]].values
    px = max(0, min(int(np.argmin(np.abs(xc - x))), canvas_agg.shape[1] - 1))
    py = max(0, min(int(np.argmin(np.abs(yc - y))), canvas_agg.shape[0] - 1))
    for i, (agg, df) in enumerate(zip(aggs, dfs)):
        if agg is None or df is None:
            continue
        raw = float(agg.values[py, px])          # first layer always wins
        return (None if np.isnan(raw) else raw), i
    return None, None


def probe_new(aggs, dfs, x, y, radius=1):
    """The patched algorithm: every layer, exact bin preferred."""
    p = _new_probe()
    p._width, p._height = 900, 600
    candidates = []
    for i, (agg, df) in enumerate(zip(aggs, dfs)):
        if agg is None or df is None or len(df) == 0:
            continue
        idx = p._agg_pixel(agg, x, y)
        if idx is None:
            continue
        px, py = idx
        bw, bh = p._bin_screen_size(agg)
        hit = p._nearest_populated_bin(agg, px, py, radius, bw, bh)
        if hit is None:
            continue
        dist, hpx, hpy = hit
        candidates.append((dist, i, hpx, hpy))
    if not candidates:
        return None, None
    dist, i, px, py = min(candidates, key=lambda c: (c[0], c[1]))
    return _agg_value(aggs[i].values, py, px), i


# ----------------------------------------------------------------------
# Tests
# ----------------------------------------------------------------------

def test_multilayer_probe():
    """Every painted pixel must resolve, whichever layer painted it."""
    dfs   = make_layers()
    x_rng = (26.0, 29.0)
    y_rng = (-36.5, -33.5)
    w, h  = 45, 30                      # shrunken adaptive canvas
    aggs  = shade_aggs(dfs, x_rng, y_rng, w, h)

    # Every bin painted by *either* layer is visible in the composite.
    painted = np.isfinite(aggs[0].values) | np.isfinite(aggs[1].values)
    ys, xs  = np.nonzero(painted)
    xc = aggs[0].coords[aggs[0].dims[1]].values
    yc = aggs[0].coords[aggs[0].dims[0]].values

    old_miss = new_miss = 0
    old_layers, new_layers = set(), set()
    for gy, gx in zip(ys, xs):
        # Hover dead-centre on the painted bin.
        x, y = float(xc[gx]), float(yc[gy])
        v_old, l_old = probe_old(aggs, dfs, x, y)
        v_new, l_new = probe_new(aggs, dfs, x, y)
        if v_old is None:
            old_miss += 1
        else:
            old_layers.add(l_old)
        if v_new is None:
            new_miss += 1
        else:
            new_layers.add(l_new)

    total = len(ys)
    print(f"  painted bins in composite : {total}")
    print(f"  OLD algorithm misses      : {old_miss}  "
          f"({100*old_miss/total:.1f}%)  layers consulted={sorted(old_layers)}")
    print(f"  NEW algorithm misses      : {new_miss}  "
          f"({100*new_miss/total:.1f}%)  layers consulted={sorted(new_layers)}")

    assert old_miss > 0, "expected the old algorithm to miss painted pixels"
    assert new_miss == 0, f"new algorithm still misses {new_miss} painted bins"
    assert new_layers == {0, 1}, "new algorithm must consult every layer"
    # (old_miss, total available above if a caller ever wants them --
    # not returned: pytest warns on test functions returning non-None,
    # since pass/fail here is only ever signaled by the asserts above.)


def test_near_miss_slop():
    """A hover one bin off a lone point resolves when radius >= 1."""
    df   = pd.DataFrame({"x": [27.5], "y": [-35.0]})
    cvs  = ds.Canvas(plot_width=40, plot_height=30,
                     x_range=(26.0, 29.0), y_range=(-36.5, -33.5))
    agg  = cvs.points(df, "x", "y", ds_agg.mean("y"))
    p    = _new_probe()

    ys, xs = np.nonzero(np.isfinite(agg.values))
    gy, gx = int(ys[0]), int(xs[0])
    xc = agg.coords[agg.dims[1]].values
    yc = agg.coords[agg.dims[0]].values

    # One bin to the right of the populated bin.
    off = p._nearest_populated_bin(agg, gx + 1, gy, 0)
    on  = p._nearest_populated_bin(agg, gx + 1, gy, 1)
    print(f"  populated bin             : ({gx}, {gy})")
    print(f"  hover one bin off, r=0    : {off}")
    print(f"  hover one bin off, r=1    : {on}")
    assert off is None
    assert on is not None and on[1:] == (gx, gy)


def test_cell_bounds_gapped_axis():
    """Local spacing must not span an inter-scan gap."""
    # Two 'scans' of 1 s integrations, 600 s apart.
    t = np.concatenate([np.arange(0, 10, 1.0), np.arange(600, 610, 1.0)])
    lo, hi = _cell_bounds(t, 4)                     # mid-scan cell
    width_local = hi - lo
    width_global = 2 * (abs(t[-1] - t[0]) / (2 * (len(t) - 1)))
    print(f"  local  cell width         : {width_local:.3f} s")
    print(f"  global-average cell width : {width_global:.3f} s")
    assert abs(width_local - 1.0) < 1e-9, "should equal the 1 s integration"
    assert width_global > 20 * width_local, "old form inflated by the gap"

    # Boundary cells and a descending axis must not blow up.
    assert _cell_bounds(t, 0)[0] < t[0]
    assert _cell_bounds(t, len(t) - 1)[1] > t[-1]
    desc = t[::-1].copy()
    lo_d, hi_d = _cell_bounds(desc, 4)
    assert hi_d > lo_d
    assert abs(_cell_bounds(np.array([5.0]), 0)[0] - 5.0) < 1e-12


def test_uniform_axis_unchanged():
    """On a uniform axis the new bounds must equal the old ones exactly."""
    c = np.linspace(26.0, 29.0, 45)
    for i in (0, 1, 22, 43, 44):
        lo, hi = _cell_bounds(c, i)
        d_old  = abs(c[-1] - c[0]) / (2 * (len(c) - 1))
        assert abs((hi - lo) - 2 * d_old) < 1e-9
        assert abs(((lo + hi) / 2) - c[i]) < 1e-9
    print("  45-bin uniform canvas axis: identical to the previous formula")


def test_bin_membership_no_double_count():
    """A sample exactly on a shared edge belongs to exactly one bin."""
    c = np.linspace(0.0, 10.0, 11)          # bin width 1.0, edges at .5
    edge = float((c[3] + c[4]) / 2)          # 3.5, shared by bins 3 and 4
    s = pd.Series([edge])
    in3 = _bin_membership(s, _cell_bounds(c, 3), 3, len(c)).iloc[0]
    in4 = _bin_membership(s, _cell_bounds(c, 4), 4, len(c)).iloc[0]
    print(f"  sample at shared edge {edge}: bin3={in3} bin4={in4}")
    assert in3 != in4, "edge sample must land in exactly one bin"

    # The maximum sample must still be counted by the final bin.
    last = pd.Series([float(c[-1])])
    assert _bin_membership(
        last, _cell_bounds(c, len(c) - 1), len(c) - 1, len(c)
    ).iloc[0]


def test_degenerate_single_bin():
    """A one-bin canvas must not report zero samples."""
    c = np.array([27.5])
    bounds = _widen_if_degenerate(_cell_bounds(c, 0), c)
    # The exact value, and the same value round-tripped through float32
    # (MS columns are routinely float32 while agg coords are float64).
    s = pd.Series([27.5, float(np.float32(27.5000001))])
    n = int(_bin_membership(s, bounds, 0, 1).sum())
    print(f"  one-bin window            : {bounds}, samples counted = {n}")
    assert n == 2, "degenerate window must tolerate float32 round-trip"
    # ...but it must not swallow a genuinely different coordinate.
    far = pd.Series([27.4999])
    assert int(_bin_membership(far, bounds, 0, 1).sum()) == 0


def test_agg_value_dtypes():
    """count()/any() aggs use 0/False for empty, not NaN."""
    f = np.array([[np.nan, 1.5]])
    i = np.array([[0, 3]], dtype=np.int32)
    b = np.array([[False, True]])
    assert _agg_value(f, 0, 0) is None and _agg_value(f, 0, 1) == 1.5
    assert _agg_value(i, 0, 0) is None and _agg_value(i, 0, 1) == 3.0
    assert _agg_value(b, 0, 0) is None and _agg_value(b, 0, 1) == 1.0
    print("  float / int32 / bool aggs : empty detected correctly")


def test_stale_agg_shape_guard():
    """Per-layer index derivation survives a shape mismatch between layers."""
    dfs = make_layers()
    a_big   = shade_aggs(dfs[:1], (26.0, 29.0), (-36.5, -33.5), 45, 30)[0]
    a_small = shade_aggs(dfs[1:], (26.0, 29.0), (-36.5, -33.5), 12, 8)[0]
    p = _new_probe()
    x, y = 27.5, -35.0
    i_big   = p._agg_pixel(a_big,   x, y)
    i_small = p._agg_pixel(a_small, x, y)
    print(f"  45x30 agg -> {i_big},  12x8 agg -> {i_small}")
    assert i_big != i_small, "indices must be derived per layer, not shared"
    # The old code would have indexed the 12x8 agg with i_big and blown up.
    assert i_small[0] < a_small.shape[1] and i_small[1] < a_small.shape[0]



def test_screen_space_radius():
    """Slop budget must convert to a bin radius that tracks canvas shrink."""
    p = _new_probe()
    p._width, p._height = 900, 600
    p._probe_slop_px = 6.0
    p._probe_search_radius = None

    dfs = make_layers()
    cases = [
        ("shrunken canvas (the screenshot)", 25, 27),
        ("half-size canvas",                450, 300),
        ("full-resolution canvas",          900, 600),
    ]
    for name, w, h in cases:
        agg = shade_aggs(dfs[:1], (24.062, 32.2441), (-36.8111, -33.4934),
                         w, h)[0]
        bw, bh = p._bin_screen_size(agg)
        r      = p._search_radius_bins(agg)
        catch_w = (2 * r + 1) * bw
        catch_h = (2 * r + 1) * bh
        print(f"  {name:34s} bin={bw:5.1f}x{bh:4.1f}px  r={r}  "
              f"catch={catch_w:5.1f}x{catch_h:5.1f}px  (mark {bw:.0f}x{bh:.0f}px)")
        # Tolerance beyond the drawn mark stays within the budget.
        assert catch_w - bw <= 2 * p._probe_slop_px + bw + 1e-6
        assert catch_h - bh <= 2 * p._probe_slop_px + bh + 1e-6

    # The screenshot case specifically must collapse to exact-bin lookup:
    # a 36x22 px bin is already larger than a 6 px budget.
    agg = shade_aggs(dfs[:1], (24.062, 32.2441), (-36.8111, -33.4934),
                     25, 27)[0]
    assert p._search_radius_bins(agg) == 0, "big bins need no slop"

    # An explicit override still wins.
    p._probe_search_radius = 2
    assert p._search_radius_bins(agg) == 2


def test_distance_is_screen_pixels():
    """Reported distance must be screen px, and nearest must be visual."""
    p = _new_probe()
    p._width, p._height = 900, 600
    # Deliberately anisotropic bins: 4 px wide, 40 px tall.
    df  = pd.DataFrame({"x": [27.0, 27.0], "y": [-35.0, -34.0]})
    cvs = ds.Canvas(plot_width=225, plot_height=15,
                    x_range=(24.0, 32.0), y_range=(-36.5, -33.5))
    agg = cvs.points(df, "x", "y", ds_agg.mean("y"))
    bw, bh = p._bin_screen_size(agg)
    print(f"  bin = {bw:.1f} x {bh:.1f} screen px")

    ys, xs = np.nonzero(np.isfinite(agg.values))
    gy, gx = int(ys[0]), int(xs[0])

    d_x, _, _ = p._nearest_populated_bin(agg, gx + 1, gy, 3, bw, bh)
    d_y, _, _ = p._nearest_populated_bin(agg, gx, gy + 1, 3, bw, bh)
    print(f"  one bin off in x -> {d_x:.1f} px    one bin off in y -> {d_y:.1f} px")
    assert abs(d_x - bw) < 1e-6, "x distance must be scaled by bin width"
    assert abs(d_y - bh) < 1e-6, "y distance must be scaled by bin height"
    assert d_y > d_x, "the taller bin must read as farther away"

    # Unweighted, both would have read as exactly 1.0 -- indistinguishable.
    d_unw, _, _ = p._nearest_populated_bin(agg, gx + 1, gy, 3, 1.0, 1.0)
    assert abs(d_unw - 1.0) < 1e-6


def test_exact_hit_beats_near_hit():
    """Hovering a real point is never stolen by a neighbour."""
    p = _new_probe()
    p._width, p._height = 900, 600
    df  = pd.DataFrame({"x": [27.0, 27.4], "y": [-35.0, -35.0]})
    cvs = ds.Canvas(plot_width=60, plot_height=40,
                    x_range=(24.0, 32.0), y_range=(-36.5, -33.5))
    agg = cvs.points(df, "x", "y", ds_agg.mean("y"))
    bw, bh = p._bin_screen_size(agg)
    ys, xs = np.nonzero(np.isfinite(agg.values))
    pts = sorted(zip(xs.tolist(), ys.tolist()))
    assert len(pts) == 2, "expected two distinct populated bins"

    for gx, gy in pts:
        d, mx, my = p._nearest_populated_bin(agg, gx, gy, 4, bw, bh)
        assert d == 0.0 and (mx, my) == (gx, gy), \
            "an occupied bin must always resolve to itself"
    print(f"  two marks at {pts}: each resolves to itself with d=0.0")

    # Midway between them, the nearer one wins and the handoff is clean.
    (ax, ay), (bx, by) = pts
    left  = p._nearest_populated_bin(agg, (ax + bx) // 2 - 1, ay, 6, bw, bh)
    right = p._nearest_populated_bin(agg, (ax + bx) // 2 + 1, ay, 6, bw, bh)
    print(f"  just left of midpoint -> bin {left[1:]}, "
          f"just right -> bin {right[1:]}")
    assert left[1:] == (ax, ay) and right[1:] == (bx, by)


TESTS = [
    ("multi-layer probe (the reported bug)", test_multilayer_probe),
    ("near-miss search radius",              test_near_miss_slop),
    ("cell bounds on a gapped time axis",    test_cell_bounds_gapped_axis),
    ("cell bounds on a uniform axis",        test_uniform_axis_unchanged),
    ("half-open bin membership",             test_bin_membership_no_double_count),
    ("degenerate one-bin canvas",            test_degenerate_single_bin),
    ("agg empty-sentinel by dtype",          test_agg_value_dtypes),
    ("per-layer index derivation",           test_stale_agg_shape_guard),
    ("screen-space search radius",           test_screen_space_radius),
    ("distance metric in screen px",         test_distance_is_screen_pixels),
    ("exact hit beats near hit",             test_exact_hit_beats_near_hit),
]


def main() -> int:
    failures = 0
    for name, fn in TESTS:
        print(f"\n[{name}]")
        try:
            fn()
            print("  -> PASS")
        except AssertionError as exc:
            failures += 1
            print(f"  -> FAIL: {exc}")
        except Exception as exc:                  # noqa: BLE001
            failures += 1
            print(f"  -> ERROR: {type(exc).__name__}: {exc}")
    print("\n" + "=" * 62)
    print("ALL PASS" if not failures else f"{failures} FAILURE(S)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
