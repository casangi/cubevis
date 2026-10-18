"""
test_raster_correlation.py
==========================
Which correlation a raster shows (2026-10-18).  Until then always the
first ticked one, so on RR / LL data a problem in LL alone did not show
unless RR was unticked.  Each raster panel now has "Correlation shown"
(``raster_corr``): ``auto`` (first ticked), ``parallel`` (the mean of the
parallel hands, valid where both are unflagged) or a product.

1. ``reader`` helpers: expand, label, select (numbers against numpy).
2. Both backends: a parallel-hands raster equals the numpy mean of the two
   hands; a product that is not ticked is still read.
3. The plotter: argument and ``panels`` key, validation, gear-tab control,
   Plot re-renders when the choice changes, reproducing call, title.
4. Flagging: a box on a parallel-hands raster flags both hands.

Parts 2-4 use the TW Hya MS (``MS`` / ``PS``), field 3c279.
"""
from __future__ import annotations

import asyncio
import dataclasses
import os
import warnings

import numpy as np
import pytest
import xarray as xr

from cubevis.toolbox.visplot.data.reader import (
    PARALLEL_HANDS, correlation_for_query, expand_polarization, is_parallel,
    parallel_label, select_polarization)

warnings.filterwarnings("ignore")

MS = os.environ.get("MS", "")
PS = os.environ.get("PS", "")
FIELD = "3c279"
needs_ms = pytest.mark.skipif(not os.path.isdir(MS), reason="TW Hya MS not available (MS=)")


# ---------------------------------------------------------------------- #
# 1. Helpers                                                               #
# ---------------------------------------------------------------------- #

def test_expand():
    assert expand_polarization("parallel", ["XX", "XY", "YX", "YY"]) == ["XX", "YY"]
    assert expand_polarization("PARALLEL", ["RR", "RL", "LR", "LL"]) == ["RR", "LL"]
    assert expand_polarization("parallel", ["LL", "RR"]) == ["LL", "RR"]   # data order
    assert expand_polarization("parallel", ["RR"]) == ["RR"]               # one hand
    assert expand_polarization("parallel", ["XY", "YX"]) == []
    assert expand_polarization("YY", ["XX", "YY"]) == ["YY"]
    assert expand_polarization("LL", ["XX", "YY"]) == []
    assert expand_polarization(None, ["XX", "YY"]) == ["XX", "YY"]
    assert is_parallel(PARALLEL_HANDS) and not is_parallel("XX") and not is_parallel(None)


def test_label_and_query_correlation():
    assert parallel_label(["XX", "YY"]) == "(XX+YY)/2"
    assert parallel_label(["RR", "LL"]) == "(RR+LL)/2"
    assert parallel_label(["XX"]) == "parallel hands"
    assert set(correlation_for_query("parallel")) >= {"XX", "YY", "RR", "LL"}
    assert correlation_for_query("LL") == ["LL"]
    assert correlation_for_query(None) is None


def test_select_numbers():
    rng = np.random.default_rng(1)
    v = rng.standard_normal((3, 4, 2)) + 1j * rng.standard_normal((3, 4, 2))
    f = np.zeros_like(v, dtype=bool)
    f[0, 0, 1] = True                                   # YY flagged here
    freq = xr.DataArray(np.arange(4.0), dims="frequency", attrs={"spectral_window_name": "w"})
    coords = {"polarization": ["XX", "YY"], "frequency": freq}
    vis = xr.DataArray(v, dims=("time", "frequency", "polarization"), coords=coords)
    flag = xr.DataArray(f, dims=vis.dims, coords=coords)
    pv, pf = select_polarization(vis, flag, "parallel")
    np.testing.assert_allclose(pv.values, (v[..., 0] + v[..., 1]) / 2)
    assert pf.values[0, 0] and pf.values.sum() == 1      # either hand flagged
    assert pv.coords["frequency"].attrs.get("spectral_window_name") == "w"
    xv, xf = select_polarization(vis, flag, "XX")
    np.testing.assert_allclose(xv.values, v[..., 0])
    assert select_polarization(vis, flag, "LL") is None


# ---------------------------------------------------------------------- #
# 2. Backends                                                              #
# ---------------------------------------------------------------------- #

def _reference_parallel(field):
    """|mean over channels of (XX+YY)/2|, both hands unflagged, per
    (time, baseline_id), from xarray-ms directly."""
    dt = xr.open_datatree(MS, engine="xarray-ms:msv2")
    out = []
    for k in dt.children:
        ds = dt[k].ds
        if field not in set(np.asarray(ds.coords["field_name"]).ravel().astype(str)):
            continue
        fn = ds.coords["field_name"]
        if "time" in fn.dims:
            ds = ds.isel(time=np.flatnonzero(np.asarray(fn.values).astype(str) == field))
        vis = ds.VISIBILITY.sel(polarization=["XX", "YY"]).values
        flg = ds.FLAG.sel(polarization=["XX", "YY"]).values.astype(bool)
        p = (vis[..., 0] + vis[..., 1]) / 2
        p = np.where(flg[..., 0] | flg[..., 1], np.nan, p)
        with np.errstate(invalid="ignore"):
            out.append((ds.coords["time"].values, ds.coords["baseline_id"].values,
                        np.abs(np.nanmean(p, axis=2))))
    return out


@needs_ms
def test_msv2_parallel_matches_numpy():
    from cubevis.toolbox.visplot import VisibilityPlotter
    from cubevis.toolbox.visplot.axes import Axis
    vp = VisibilityPlotter(ms=MS, field=FIELD, headless=True, layout="raster")
    try:
        sel = dataclasses.replace(vp._build_selection(), correlation=["XX", "YY"])
        agg, *_ = vp._reader.query_raster(Axis.TIME, Axis.BASELINE, Axis.AMPLITUDE,
                                          sel, polarization="parallel")
        ref = _reference_parallel(FIELD)
        assert ref
        t, b, a = ref[0]
        got = agg.sel(time=t, baseline_id=b, method="nearest").values
        m = np.isfinite(a) & np.isfinite(got)
        assert m.sum() > 1000
        np.testing.assert_allclose(got[m], a[m], rtol=1e-4)
    finally:
        vp.close()


@needs_ms
@pytest.mark.parametrize("fmt", ["ms", "ps"])
def test_backends_agree_and_unticked_product_is_read(fmt):
    from cubevis.toolbox.visplot import VisibilityPlotter
    from cubevis.toolbox.visplot.axes import Axis
    if fmt == "ps" and not os.path.isdir(PS):
        pytest.skip("PS not set")
    src = {"ms": MS} if fmt == "ms" else {"ps": PS}
    vp = VisibilityPlotter(**src, field=FIELD, headless=True, layout="raster",
                           correlation="XX", raster_corr="YY")
    try:
        r = vp._slots[0].raster
        assert r._polarization == "YY"
        assert np.isfinite(np.asarray(r._agg.values, float)).any()
        assert "pol=YY" in vp._slots[0].raster._title_text if hasattr(r, "_title_text") else True
        sel = dataclasses.replace(vp._build_selection(), correlation=["XX", "YY"])
        par, *_ = vp._reader.query_raster(Axis.TIME, Axis.BASELINE, Axis.AMPLITUDE,
                                          sel, polarization="parallel")
        xx, *_ = vp._reader.query_raster(Axis.TIME, Axis.BASELINE, Axis.AMPLITUDE,
                                         sel, polarization="XX")
        # The parallel view differs from either hand alone.
        assert not np.allclose(np.nan_to_num(par.values), np.nan_to_num(xx.values))
        test_backends_agree_and_unticked_product_is_read.__dict__.setdefault(
            "vals", {})[fmt] = par.values
    finally:
        vp.close()
    vals = test_backends_agree_and_unticked_product_is_read.__dict__["vals"]
    if len(vals) == 2:
        a, b = vals["ms"], vals["ps"]
        np.testing.assert_allclose(np.nan_to_num(a), np.nan_to_num(b), rtol=1e-5)


# ---------------------------------------------------------------------- #
# 3. Plotter                                                               #
# ---------------------------------------------------------------------- #

@needs_ms
def test_argument_validation_and_panels():
    from cubevis.toolbox.visplot import VisibilityPlotter
    with pytest.raises(ValueError, match="raster_corr='LL'"):
        VisibilityPlotter(ms=MS, field=FIELD, headless=True, raster_corr="LL")
    with pytest.raises(ValueError, match="raster_corr='RR'"):
        VisibilityPlotter(ms=MS, field=FIELD, headless=True,
                          panels=[dict(kind="raster", raster_corr="RR")])
    vp = VisibilityPlotter(ms=MS, field=FIELD, headless=True, layout="side",
                           panels=[dict(kind="raster", raster_corr="parallel"),
                                   dict(kind="raster", raster_corr="yy")])
    try:
        a, b = vp._slots
        assert a.raster._polarization == PARALLEL_HANDS and a.raster._corr_choice == "parallel"
        assert b.raster._polarization == "YY"
        call = vp.reproducing_call()
        assert "raster_corr='parallel'" in call and "raster_corr='YY'" in call
        page = vp.describe(slot="A", html=True)
        assert "(XX+YY)/2" in page
    finally:
        vp.close()


@needs_ms
def test_gear_control_and_plot_rerenders():
    from cubevis.toolbox.visplot import VisibilityPlotter
    vp = VisibilityPlotter(ms=MS, field=FIELD, layout="side", correlation="XX,YY")
    try:
        vp._build_layout()
        W = vp._panel_axis_widgets
        rk = W["A"]["raster"]["corr_sel"]
        vals = [v for v, _ in rk.options]
        assert vals[:2] == ["auto", "parallel"] and "XX" in vals and "YY" in vals
        assert "(XX+YY)/2" in dict(rk.options)["parallel"]
        assert rk.value == "auto"
        assert hasattr(vp, "_hint_rcorr")
        r = vp._slots[0].raster
        assert r._polarization == "XX"

        def msg(corr):
            return {"correlation": "XX,YY", "panels": {
                "A": {"kind": "raster", "y": W["A"]["raster"]["y_sel"].value,
                      "x": W["A"]["raster"]["x_sel"].value,
                      "qty": W["A"]["raster"]["q_sel"].value, "raster_corr": corr},
                "B": {"kind": "scatter", "x": W["B"]["scatter"]["x_sel"].value,
                      "y": W["B"]["scatter"]["y_sel"].value, "colorize": [None, None]}}}
        before = np.array(r._agg.values, float)
        rep = asyncio.run(vp._handle_plot(msg("YY")))
        assert rep["status"] != "error"
        assert r._polarization == "YY" and r._corr_choice == "YY"
        assert not np.allclose(np.nan_to_num(before), np.nan_to_num(np.asarray(r._agg.values, float)))
        rep = asyncio.run(vp._handle_plot(msg("parallel")))
        assert r._polarization == PARALLEL_HANDS
        assert "parallel hands" in r._effective_title()
        # The payload JS carries the choice.
        assert "raster_corr: rk_sel ? rk_sel.value : null" in vp._do_plot_js
    finally:
        vp.close()


# ---------------------------------------------------------------------- #
# 4. Flagging                                                              #
# ---------------------------------------------------------------------- #

@needs_ms
def test_flag_box_on_parallel_flags_both_hands():
    from cubevis.toolbox.visplot import VisibilityPlotter
    vp = VisibilityPlotter(ms=MS, field=FIELD, layout="side", correlation="XX,YY",
                           raster_corr="parallel")
    try:
        vp._build_layout()
        r = vp._slots[0].raster
        x0, x1 = r._x_range
        y0, y1 = r._y_range
        box = {"x0": x0 + 0.1 * (x1 - x0), "x1": x0 + 0.3 * (x1 - x0),
               "y0": y0 + 0.1 * (y1 - y0), "y1": y0 + 0.3 * (y1 - y0)}
        req = vp._flags.build_request(r, "raster", box, True)
        assert req["polarization"] == PARALLEL_HANDS
        req.pop("filter_obj", None)
        out = vp._reader.evaluate_flag_request(req)
        d = out["delta"]
        assert d is not None, out["warnings"]
        assert set(d.get("correlation") or ()) == {"XX", "YY"}
    finally:
        vp.close()
