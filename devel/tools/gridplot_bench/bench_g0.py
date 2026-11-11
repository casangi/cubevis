"""G0: what a page of grid cells costs through today's headless path
(``devel/docs/gridplot/grid_and_calplot_plan.md`` section 3.4 and stage G0).

One headless ``VisibilityPlotter`` holds the open data (as a ``GridSource``
will); each cell is the same panel re-selected to one baseline and
rendered to a ``RenderedPanel`` (``_apply_selection_overrides`` then
``render_result``: the path ``visplot(plotfile=...)`` iterates with); the
page is then composited by ``png_export.export_png``.  For each view and
page size this prints and records:

    open        constructing the plotter (opening the data, first render)
    cell        the page, cell by cell, twice (the second pass with
                whatever the frame cache and memos kept)
    compose     export_png of the page (matplotlib)
    union       the page's baselines selected together, one render: the
                cost of reading the page once -- the floor that "read once,
                split by the iteration axis" (plan 3.4) could approach

each twice: ``all`` re-selects through ``_apply_selection_overrides``,
which re-renders all four panel objects (both kinds in both slots, shown
or not) -- what a ``plotfile=`` loop pays today; ``active`` re-renders
only the panel shown, as a grid source would.

Views:  waterfall    Time x Channel raster of one baseline (SPFLG's view)
        timeseries   Amplitude vs Time scatter of one baseline (VPLOT's)

    python devel/tools/gridplot_bench/bench_g0.py --make /tmp/g0.ms
    python devel/tools/gridplot_bench/bench_g0.py --ms DATA.ms [--cells 9 16] [--json out.json]

``--make`` writes a synthetic MSv2 first with the quick tests' generator
(``tests/ci/make_tiny_ms.py``) at a larger size (``--nant``, ``--nchan``,
``--ntimes``, ``--nspw``; default 10 antennas -- a VLBA-sized array, 45
baselines --, 64 channels, 600 times, 1 SPW).  A real MS gives the numbers
that matter; the synthetic one makes the run repeatable anywhere.
"""
from __future__ import annotations

import argparse
import itertools
import json
import logging
import os
import statistics
import sys
import tempfile
import time
import warnings

warnings.filterwarnings("ignore")
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))

VIEWS = {
    "waterfall": dict(preset="waterfall"),
    "timeseries": dict(kind="scatter", scatter_x="time", scatter_y="amplitude"),
}


def make_ms(path: str, nant: int, nchan: int, ntimes: int, nspw: int) -> str:
    sys.path.insert(0, os.path.join(ROOT, "tests", "ci"))
    import make_tiny_ms as m
    m.NANT, m.NCHAN, m.NTIMES, m.NSPW = nant, nchan, ntimes, nspw
    t0 = time.perf_counter()
    m.make(path)
    print(f"made {path}: {nant} antennas, {nchan} channels, {ntimes} times, {nspw} SPW "
          f"in {time.perf_counter() - t0:.1f} s", flush=True)
    return path


def grid_shape(n: int) -> tuple:
    rows = int(n ** 0.5)
    while n % rows:
        rows -= 1
    return rows, n // rows


def bench_view(ms: str, view: str, cells: list, outdir: str) -> dict:
    from cubevis.toolbox.visplot.visibility_plotter import VisibilityPlotter
    from cubevis.toolbox.visplot.png_export import export_png

    t0 = time.perf_counter()
    p = VisibilityPlotter(ms=ms, headless=True, layout="one", **VIEWS[view])
    t_open = time.perf_counter() - t0
    names = [a.name for a in p._meta.antennas]
    pairs = list(itertools.combinations(names, 2))
    out = {"view": view, "open_s": t_open, "baselines": len(pairs), "pages": {}}
    try:
        for n in cells:
            page = pairs[:n]
            if len(page) < n:
                print(f"  {view}: only {len(page)} baselines, page of {n} skipped")
                continue
            from cubevis.toolbox.visplot.refresh import RefreshLevel

            def select(baselines, how):
                if how == "all":
                    p._apply_selection_overrides({"baselines": baselines})
                else:                       # the shown panel only
                    p._selection.baselines = baselines
                    p._pos0.apply_refresh(RefreshLevel.QUERY)

            res = {}
            for label in ("all_cold", "all_warm", "active_cold", "active_warm"):
                how = label.split("_")[0]
                per, rendered = [], []
                for bl in page:
                    t = time.perf_counter()
                    select([bl], how)
                    rendered.append(p._pos0.render_result())
                    per.append(time.perf_counter() - t)
                res[label] = per
                bad = [r.spec.status for r in rendered if r.spec.status != "ok"]
                if bad:
                    res.setdefault("status", bad)
            rows, cols = grid_shape(n)
            t = time.perf_counter()
            path = export_png(rendered, os.path.join(outdir, f"g0_{view}_{n}.png"),
                              nrows=rows, ncols=cols)
            res["compose_s"] = time.perf_counter() - t
            res["png"] = path
            t = time.perf_counter()
            select(list(page), "active")
            p._pos0.render_result()
            res["union_s"] = time.perf_counter() - t
            select(None, "active")
            out["pages"][n] = res

            def tot(k):
                return f"{sum(res[k]):6.2f} s (median {statistics.median(res[k]):.2f})"
            print(f"  {view:10s} {n:3d} cells ({rows}x{cols}): all panels {tot('all_cold')} / "
                  f"warm {sum(res['all_warm']):.2f};  shown panel {tot('active_cold')} / "
                  f"warm {sum(res['active_warm']):.2f};  compose {res['compose_s']:.2f} s;  "
                  f"union {res['union_s']:.2f} s"
                  + (f"  status {sorted(set(res['status']))}" if "status" in res else ""),
                  flush=True)
    finally:
        p.close()
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--ms", help="an existing MSv2/MSv4 to read")
    src.add_argument("--make", metavar="PATH", help="write a synthetic MSv2 here first")
    ap.add_argument("--nant", type=int, default=10)
    ap.add_argument("--nchan", type=int, default=64)
    ap.add_argument("--ntimes", type=int, default=600)
    ap.add_argument("--nspw", type=int, default=1)
    ap.add_argument("--cells", type=int, nargs="+", default=[9, 16])
    ap.add_argument("--views", nargs="+", default=list(VIEWS), choices=list(VIEWS))
    ap.add_argument("--json", help="write the numbers here")
    ap.add_argument("--outdir", default=None, help="where the page PNGs go (default: a temp dir)")
    a = ap.parse_args()
    logging.basicConfig(level=logging.WARNING)
    for name in ("cubevis", "xarray_ms", "distributed"):
        logging.getLogger(name).setLevel(logging.ERROR)
    ms = a.ms or make_ms(a.make, a.nant, a.nchan, a.ntimes, a.nspw)
    outdir = a.outdir or tempfile.mkdtemp(prefix="g0-")
    print(f"pages written to {outdir}")
    results = {"ms": ms, "cpu_count": os.cpu_count(), "views": []}
    for view in a.views:
        r = bench_view(ms, view, a.cells, outdir)
        print(f"  {view}: open {r['open_s']:.2f} s, {r['baselines']} baselines")
        results["views"].append(r)
    if a.json:
        with open(a.json, "w") as f:
            json.dump(results, f, indent=1, default=str)
        print(f"numbers in {a.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
