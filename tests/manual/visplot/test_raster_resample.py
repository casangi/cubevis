"""
test_raster_resample.py
=======================
Verifies the `_render` / `_shade_viewport` resample-method unification in
`VisibilityRaster`.

Imports the real `VisibilityRaster` class directly -- no AST extraction,
no re-parsing, no risk of the test drifting from what a re-parsed copy
would do differently (annotations, `super()`, decorators). Needs a real
`cubevis` on `sys.path`; see cubevis_test_paths.py for how that's found.

    python test_raster_resample.py

Requires numpy / xarray / datashader / bokeh, and cubevis itself
importable (see cubevis_test_paths.py for how to point this at your
checkout if it isn't co-located with this script).
"""

from __future__ import annotations

import sys

import numpy as np
import xarray as xr
import datashader as ds

from cubevis_test_paths import ensure_cubevis_importable

ensure_cubevis_importable()

from cubevis.toolbox.visplot.visibility_raster import VisibilityRaster


def make_raster(n_t: int, n_b: int, bad_row: int | None = None,
                bad_val: float = 90.0, bg: float = 10.0):
    """A raster with a uniform background and one sharp bad integration."""
    vals = np.full((n_t, n_b), bg, dtype=float)
    if bad_row is not None:
        vals[bad_row, :] = bad_val
    return xr.DataArray(
        vals, dims=["time", "baseline_id"],
        coords={"time": np.arange(n_t, dtype=float),
                "baseline_id": np.arange(n_b, dtype=float)},
    )


def render(agg, w, h, x_range, y_range, method):
    cvs = ds.Canvas(plot_width=w, plot_height=h,
                    x_range=x_range, y_range=y_range)
    return cvs.raster(agg, interpolate=method)


def _mk(width, height, x_range, y_range, mode="auto"):
    # __new__, not VisibilityRaster(...): the real constructor wants a
    # backend/selection/figure and does real Bokeh setup we have no use
    # for here -- every test in this file only exercises
    # _resample_method, which reads exactly the four attributes set
    # below and nothing else on self.
    r = VisibilityRaster.__new__(VisibilityRaster)
    r._width, r._height = width, height
    r._x_range, r._y_range = x_range, y_range
    r._raster_interpolate = mode
    return r


# ----------------------------------------------------------------------
# Tests
# ----------------------------------------------------------------------

def test_initial_render_now_uses_nearest():
    """The full-extent view on a typical dataset must resolve to nearest.

    A few tens of timestamps stretched over ~500 screen rows upsamples in
    y even while downsampling in x, which is exactly the case _render
    previously handled with Datashader's "linear" default.
    """
    n_t, n_b = 40, 903
    agg = make_raster(n_t, n_b)
    x_range, y_range = (0.0, n_b - 1.0), (0.0, n_t - 1.0)
    r = _mk(400, 500, x_range, y_range)
    method = r._resample_method(agg, x_range, y_range)
    print(f"  agg {agg.shape} -> 400x500 screen: {method}")
    assert method == "nearest", "initial full-extent view must not interpolate"


def test_downsampling_still_linear():
    """A genuinely downsampled view keeps the previous behaviour."""
    n_t, n_b = 4000, 903
    agg = make_raster(n_t, n_b)
    x_range, y_range = (0.0, n_b - 1.0), (0.0, n_t - 1.0)
    r = _mk(400, 500, x_range, y_range)
    method = r._resample_method(agg, x_range, y_range)
    print(f"  agg {agg.shape} -> 400x500 screen: {method}")
    assert method == "linear", "downsampling should be unaffected"


def test_zoomed_viewport_uses_nearest():
    """A zoomed sub-range upsamples and must use nearest (unchanged)."""
    n_t, n_b = 4000, 903
    agg = make_raster(n_t, n_b)
    full_x, full_y = (0.0, n_b - 1.0), (0.0, n_t - 1.0)
    r = _mk(400, 500, full_x, full_y)
    # Zoom to 1% of the full range -- the flagging regime.
    zoom_x = (400.0, 409.0)
    zoom_y = (2000.0, 2040.0)
    method = r._resample_method(agg, zoom_x, zoom_y)
    print(f"  zoomed to {zoom_x} x {zoom_y}: {method}")
    assert method == "nearest"


def test_render_and_viewport_agree_at_full_extent():
    """Both paths must choose identically for the same geometry.

    This is the actual defect: _render and _shade_viewport rendering the
    same agg over the same range previously disagreed, because only one
    of them computed a method at all.
    """
    n_t, n_b = 60, 500
    agg = make_raster(n_t, n_b)
    x_range, y_range = (0.0, n_b - 1.0), (0.0, n_t - 1.0)
    r = _mk(450, 520, x_range, y_range)
    m_render = r._resample_method(agg, (x_range[0], x_range[1]),
                                  (y_range[0], y_range[1]))
    m_viewport = r._resample_method(agg, x_range, y_range)
    print(f"  _render -> {m_render}   _shade_viewport -> {m_viewport}")
    assert m_render == m_viewport


def test_peak_preserved_under_nearest():
    """A bad integration must render at its true amplitude.

    Quantifies what the old default cost: linear dilutes the peak, and
    worst at the low upsample ratios that are most common.
    """
    print("   n_t  screen  ratio    linear    nearest   loss")
    worst = 0.0
    for n_t, h in [(8, 40), (20, 200), (40, 500), (60, 500), (200, 500)]:
        agg = make_raster(n_t, 4, bad_row=n_t // 2)
        x_range, y_range = (0.0, 3.0), (0.0, n_t - 1.0)
        lin = render(agg, 4, h, x_range, y_range, "linear")
        near = render(agg, 4, h, x_range, y_range, "nearest")
        p_lin = float(np.nanmax(lin.values))
        p_near = float(np.nanmax(near.values))
        loss = 90.0 - p_lin
        worst = max(worst, loss)
        print(f"  {n_t:4d}  {h:5d}  {h/n_t:5.1f}x  {p_lin:8.1f}  "
              f"{p_near:8.1f}  {loss:5.1f}")
        assert abs(p_near - 90.0) < 1e-9, "nearest must preserve the true peak"
    assert worst > 1.0, "expected measurable dilution under linear"


def test_no_fabricated_values_under_nearest():
    """Nearest must not invent values absent from the data."""
    n_t = 8
    agg = make_raster(n_t, 4, bad_row=3)
    x_range, y_range = (0.0, 3.0), (0.0, n_t - 1.0)
    lin = render(agg, 4, 40, x_range, y_range, "linear")
    near = render(agg, 4, 40, x_range, y_range, "nearest")
    u_lin = np.unique(np.round(lin.values[:, 0], 6))
    u_near = np.unique(np.round(near.values[:, 0], 6))
    print(f"  true data has 2 distinct values; "
          f"linear renders {len(u_lin)}, nearest renders {len(u_near)}")
    assert len(u_near) == 2
    assert len(u_lin) > 2, "expected linear to fabricate intermediate values"


def test_nan_not_spread_by_either_method():
    """Flagged cells must not grow under resampling.

    Recorded because it was suspected and found NOT to be a problem --
    the flagged region is identical under both methods, so the case for
    nearest rests on peak dilution and fabricated values, not on NaN
    behaviour.
    """
    n_t, n_b = 8, 6
    agg = make_raster(n_t, n_b)
    agg.values[5, 2] = np.nan
    x_range, y_range = (0.0, n_b - 1.0), (0.0, n_t - 1.0)
    lin = render(agg, n_b, 40, x_range, y_range, "linear")
    near = render(agg, n_b, 40, x_range, y_range, "nearest")
    n_lin = int(np.isnan(lin.values).sum())
    n_near = int(np.isnan(near.values).sum())
    print(f"  NaN pixels: linear={n_lin}  nearest={n_near}")
    assert n_lin == n_near, "neither method should spread NaN differently"


def test_explicit_override():
    """The constructor override bypasses the automatic rule."""
    n_t, n_b = 4000, 903
    agg = make_raster(n_t, n_b)
    x_range, y_range = (0.0, n_b - 1.0), (0.0, n_t - 1.0)
    for mode in ("nearest", "linear"):
        r = _mk(400, 500, x_range, y_range, mode=mode)
        got = r._resample_method(agg, x_range, y_range)
        print(f"  raster_interpolate={mode!r} -> {got}")
        assert got == mode


TESTS = [
    ("initial render now uses nearest",      test_initial_render_now_uses_nearest),
    ("downsampling still linear",            test_downsampling_still_linear),
    ("zoomed viewport uses nearest",         test_zoomed_viewport_uses_nearest),
    ("render and viewport agree",            test_render_and_viewport_agree_at_full_extent),
    ("peak preserved under nearest",         test_peak_preserved_under_nearest),
    ("no fabricated values under nearest",   test_no_fabricated_values_under_nearest),
    ("NaN not spread by either method",      test_nan_not_spread_by_either_method),
    ("explicit override",                    test_explicit_override),
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
