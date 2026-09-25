#!/usr/bin/env python
"""visplot_perf_probe.py -- where does a scatter render spend its time?

Measurement only: imports the project, changes nothing in it.  Monkeypatching
is confined to this process.

    python visplot_perf_probe.py MS.ms --scan 12,14,16 [--pol XX] [--x UVDIST]

Stages reported (best of --repeat, one layer):
  1. _query_columns_raw   as shipped, then with the categorical-only string
                          columns suppressed, then with polarization/spw too
  2. render_layer         continuous vs categorical (scan), on the cached frame
  3. the coarse id grid   isolated (7-reduction summary vs one reduction)
  4. categorical path     current implementation vs a vectorised prototype
                          (asserts the two images are IDENTICAL)
"""
import argparse, gc, time, warnings
warnings.filterwarnings("ignore")
import numpy as np, pandas as pd
import datashader as ds, datashader.reductions as R
from cubevis.toolbox.visplot.data.msv2_backend import MSv2Backend
from cubevis.toolbox.visplot.data.reader import ScatterLayerSpec
from cubevis.toolbox.visplot.data import _scatter_render as sr
from cubevis.toolbox.visplot.selection import SelectionSpec
from cubevis.toolbox.visplot.axes import Axis
from cubevis.toolbox.visplot import palettes

T = time.perf_counter


def best(fn, n):
    ts, out = [], None
    for _ in range(n):
        gc.collect(); t = T(); out = fn(); ts.append(T() - t)
    return min(ts), out


def fast_categorical(df, column, cmap, cvs, excluded=None, cap=sr.CATEGORY_CAP):
    """PROTOTYPE, same result as _resolve_categories + _shade_categorical.
    factorize() hashes in C once; every str()/sort/bin step then runs over the
    K distinct values instead of the N rows."""
    codes, uniques = pd.factorize(df[column].to_numpy(), use_na_sentinel=True)
    u_str = np.array([str(u) for u in uniques])
    keep = np.ones(len(u_str), bool)
    if excluded:
        keep = ~np.isin(u_str, list(excluded))
    distinct = sorted(set(u_str[keep].tolist()), key=sr._category_sort_key)
    members = sr._bin_categories(distinct, cap)
    cats = list(members)
    idx = {c: i for i, c in enumerate(cats)}
    v2i = {v: idx[c] for c, ms in members.items() for v in ms}
    lut = np.full(len(u_str) + 1, -1, dtype=np.int32)      # last slot <- code -1
    for k, s in enumerate(u_str):
        if keep[k]:
            lut[k] = v2i[s]
    bucket = lut[codes]
    m = bucket >= 0
    df_cat = pd.DataFrame({"x": df["x"].to_numpy()[m], "y": df["y"].to_numpy()[m],
                           "__category__": pd.Categorical.from_codes(bucket[m], categories=cats)})
    agg = cvs.points(df_cat, "x", "y", R.by("__category__", R.count()))
    return sr._argmax_shade(agg, cats, cmap)[0]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("ms"); ap.add_argument("--scan", default=None, help="comma list of scan names")
    ap.add_argument("--pol", default="XX"); ap.add_argument("--x", default="UVDIST")
    ap.add_argument("--width", type=int, default=900); ap.add_argument("--height", type=int, default=600)
    ap.add_argument("--repeat", type=int, default=3)
    a = ap.parse_args()

    be = MSv2Backend(a.ms); be.open()
    sel = SelectionSpec(scan=a.scan.split(",") if a.scan else None)
    xa = Axis[a.x]; yaxes = [(Axis.AMPLITUDE, a.pol)]; n = a.repeat

    print("== 1. _query_columns_raw (paid on EVERY call: plot, pan, zoom, recolour, export)")
    def raw():
        d = be._query_columns_raw(xa, yaxes, sel); r = list(d[yaxes[0]].columns); return d, r
    t, (d, cols) = best(raw, n); df = d[yaxes[0]]; rows = len(df); del d
    print(f"   rows = {rows/1e6:.2f}M   as shipped        {t:6.2f} s   cols={cols}")
    mb = df.memory_usage(deep=True).sum() / 1e6
    print(f"   frame memory (deep; over-counts shared strings) = {mb:,.0f} MB = {mb*1e6/rows:.0f} B/row")
    o1, o2 = be._scan_time_index, be._antenna_lookup_table
    be._scan_time_index = lambda *x, **k: None; be._antenna_lookup_table = lambda *x, **k: None
    tb, _ = best(raw, n); print(f"   no scan/antenna string columns   {tb:6.2f} s   (-{t-tb:.2f} s)")
    orig = pd.DataFrame.__setitem__
    pd.DataFrame.__setitem__ = lambda s, k, v: None if k in ("polarization", "spw") else orig(s, k, v)
    tc, _ = best(raw, n); pd.DataFrame.__setitem__ = orig
    print(f"   ... and no polarization/spw      {tc:6.2f} s   (-{tb-tc:.2f} s more)")
    be._scan_time_index, be._antenna_lookup_table = o1, o2

    x0, x1 = float(df.x.min()), float(df.x.max()); y0, y1 = float(df.y.min()), float(df.y.max())
    W, H = a.width, a.height
    lc = ScatterLayerSpec(y_axis=Axis.AMPLITUDE, polarization=a.pol, cmap=tuple(palettes.scatter_cmaps(theme="dark")[0]))
    cat = tuple(palettes.categorical_cmap(theme="dark"))
    ls = ScatterLayerSpec(y_axis=Axis.AMPLITUDE, polarization=a.pol, cmap=cat, coloring="categorical", colorize_axis=Axis.SCAN)
    rl = lambda l: sr.render_layer(df, l, x0, x1, y0, y1, W, H, "global", (y0, y1), probe_grid_max_cells=3072)

    print("\n== 2. render_layer on the already-built frame")
    tcont, res = best(lambda: rl(lc), n); print(f"   continuous   {tcont*1000:7.0f} ms")
    tcat, _ = best(lambda: rl(ls), max(1, n - 1)); print(f"   categorical  {tcat*1000:7.0f} ms   ({tcat/tcont:.1f}x continuous)")

    print("\n== 3. the coarse id grid, isolated")
    cvs = ds.Canvas(plot_width=W, plot_height=H, x_range=(x0, x1), y_range=(y0, y1))
    iw, ih = sr._id_grid_size(W, H, 3072); icv = ds.Canvas(plot_width=iw, plot_height=ih, x_range=(x0, x1), y_range=(y0, y1))
    kw = dict(val=R.mean("y"), t_lo=R.min("time"), t_hi=R.max("time"), bl_lo=R.min("baseline_id"),
              bl_hi=R.max("baseline_id"), f_lo=R.min("frequency"), f_hi=R.max("frequency"))
    t7, _ = best(lambda: icv.points(df, "x", "y", R.summary(**kw)), n)
    t1, _ = best(lambda: icv.points(df, "x", "y", R.mean("y")), n)
    td, _ = best(lambda: cvs.points(df, "x", "y", R.mean("y")), n)
    print(f"   display agg (mean y)        {td*1000:6.0f} ms\n   id grid {iw}x{ih}, 7 reductions {t7*1000:6.0f} ms   (1 reduction: {t1*1000:.0f} ms)")
    img_b = res.image.nbytes; idg_b = sum(getattr(res, f).nbytes for f in dir(res) if f.startswith("id_grid_") and isinstance(getattr(res, f), np.ndarray))
    print(f"   payload/layer: image {img_b/1e6:.2f} MB, id grid {idg_b/1e6:.2f} MB ({idg_b/img_b*100:.0f}% of image)")

    print("\n== 4. categorical path: current vs vectorised prototype (scan)")
    def cur():
        m, c, mem, _ = sr._resolve_categories(df, "scan_name", "Scan")
        return sr._shade_categorical(df, "scan_name", m, c, mem, cat, cvs)[0]
    tcur, icur = best(cur, max(1, n - 1)); tnew, inew = best(lambda: fast_categorical(df, "scan_name", cat, cvs), n)
    print(f"   current {tcur*1000:7.0f} ms   prototype {tnew*1000:6.0f} ms   speed-up {tcur/tnew:.1f}x   identical image: {np.array_equal(icur, inew)}")


if __name__ == "__main__":
    main()
