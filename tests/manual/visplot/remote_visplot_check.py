#!/usr/bin/env python3
"""
remote_visplot_check.py
=======================
End-to-end check of ``visplot`` with a remote kernel, run from a terminal.

It builds two ``VisibilityPlotter``s on the same data -- one local, one with
``backend="remote"`` -- drives both through the same GUI handlers the
browser uses (no browser needed), and compares the results step by step:
open, metadata, first render, pan/zoom, hover and InfoTool probes, axis
changes, flagging (raster box, scatter box, filters, undo/redo, display
modes, InfoTool/FlagTool agreement), export and the pending-flag report.
Every step is timed, so it doubles as a latency survey of the remote path.

Exit status is 0 only if every step passes (SKIP does not fail).

    # remote kernel on this host (quick self-test)
    python remote_visplot_check.py --ms sis14_twhya_calibrated_flagged.ms

    # real remote kernel; the data path on the kernel's host may differ
    python remote_visplot_check.py --kernel cvpost106_python312 \\
        --ms sis14_twhya_calibrated_flagged.ms \\
        --remote-ms /home/zuul06-2/dschieb/casa/visplot/sis14_twhya_calibrated_flagged.ms \\
        --field J1037-295

    # remote only (no local copy of the data)
    python remote_visplot_check.py --kernel K --remote-ms /path/on/kernel/host.ms --no-local

    # afterwards, open the real GUI on the remote kernel for the manual checklist
    python remote_visplot_check.py --kernel K --ms ... --gui

Useful environment: ``CUBEVIS_DEBUG=1`` (debug logs from both sides, incl.
remote flag-call timings).  See ``REMOTE_TESTING.md`` for the full plan.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import tempfile
import time
import traceback
import warnings

import numpy as np

warnings.filterwarnings("ignore")

RESULTS: list = []


class Skip(Exception):
    pass


def step(name):
    """Decorator: run a check, record PASS/FAIL/SKIP with its duration."""
    def deco(fn):
        def run(*a, **k):
            t0 = time.perf_counter()
            try:
                note = fn(*a, **k) or ""
                RESULTS.append(("PASS", name, time.perf_counter() - t0, str(note)))
            except Skip as s:
                RESULTS.append(("SKIP", name, time.perf_counter() - t0, str(s)))
            except Exception as exc:                        # noqa: BLE001
                RESULTS.append(("FAIL", name, time.perf_counter() - t0,
                                f"{type(exc).__name__}: {exc}"))
                if ARGS.verbose:
                    traceback.print_exc()
            status = RESULTS[-1]
            print(f"  {status[0]:4s}  {status[2]:7.2f}s  {name}"
                  + (f"  -- {status[3]}" if status[3] else ""), flush=True)
        return run
    return deco


def run(coro):
    return asyncio.run(coro)


def both(fn):
    """Apply fn to (local, remote); local may be None (--no-local)."""
    return (fn(LOCAL) if LOCAL is not None else None), fn(REMOTE)


def same(a, b, what):
    if LOCAL is None:
        return
    if isinstance(a, np.ndarray) or isinstance(b, np.ndarray):
        a, b = np.asarray(a), np.asarray(b)
        if a.shape != b.shape or not np.allclose(a, b, equal_nan=True, rtol=1e-6, atol=1e-9):
            raise AssertionError(f"{what}: local and remote differ")
    elif a != b:
        raise AssertionError(f"{what}: local {a!r} != remote {b!r}")


def panels(vp):
    return vp._slots[0].raster, vp._slots[1].scatter


def full_box(obj, fx=(0.0, 1.0), fy=(0.0, 1.0)):
    x0, x1 = obj._x_range
    y0, y1 = obj._y_range
    return dict(x0=x0 + (x1 - x0) * fx[0], x1=x0 + (x1 - x0) * fx[1],
                y0=y0 + (y1 - y0) * fy[0], y1=y0 + (y1 - y0) * fy[1])


# ---------------------------------------------------------------------- #
# Steps                                                                    #
# ---------------------------------------------------------------------- #

@step("open remote plotter (kernel start, worker spawn, data open, first render)")
def open_remote():
    global REMOTE
    from cubevis.toolbox.visplot import VisibilityPlotter
    REMOTE = VisibilityPlotter(ms=ARGS.remote_ms, backend="remote", kernel_name=ARGS.kernel,
                               headless=True, layout="side", **PLOT_KW)
    kind = type(REMOTE._reader).__name__
    if kind != "RemoteReductionContext":
        raise AssertionError(f"reader is {kind}, not remote")
    return f"reader={kind}"


@step("open local plotter")
def open_local():
    global LOCAL
    if ARGS.no_local:
        raise Skip("--no-local")
    from cubevis.toolbox.visplot import VisibilityPlotter
    LOCAL = VisibilityPlotter(ms=ARGS.ms, headless=True, layout="side", **PLOT_KW)


@step("metadata")
def metadata():
    keys = ("field_names", "spw_ids", "n_baselines")
    a, b = both(lambda vp: {k: vp._reader.metadata().get(k) for k in keys}
                if hasattr(vp._reader, "metadata") else None)
    same(a, b, "metadata")
    return f"{len(b['field_names'])} field(s), {len(b['spw_ids'] or [])} SPW(s)"


@step("first render: raster aggregate and scatter extent")
def first_render():
    a, b = both(lambda vp: panels(vp)[0]._agg.values)
    same(a, b, "raster aggregate")
    a, b = both(lambda vp: tuple(round(v, 6) for v in panels(vp)[1]._x_range + panels(vp)[1]._y_range))
    same(a, b, "scatter extent")
    return f"raster {np.asarray(b).shape}"


@step("pan/zoom re-render (both panels, middle half in x)")
def pan_zoom():
    # Image SIZES may legitimately differ: a remote scatter keeps a larger
    # cached reference (ref_scale) so more pan/zooms are served locally
    # without a round trip.  What must hold is that both produce an image.
    notes = []
    for i in (0, 1):
        def rr(vp):
            o = panels(vp)[i]
            bx = full_box(o, (0.25, 0.75), (0.0, 1.0))
            img = o._handle_rerender(bx)["image"]
            return img.shape, int(np.count_nonzero(img))
        a, b = both(rr)
        if b[1] == 0:
            raise AssertionError(f"panel {i}: remote image is empty")
        notes.append(f"panel {i} {b[0]}")
    # back to full extent
    for vp in (LOCAL, REMOTE):
        if vp is not None:
            for o in panels(vp):
                o._handle_rerender(full_box(o))
    return ", ".join(notes)


@step("hover probe (raster centre)")
def hover():
    def pr(vp):
        o = panels(vp)[0]
        x0, x1 = o._x_range
        y0, y1 = o._y_range
        out = o._handle_probe({"x": (x0 + x1) / 2, "y": (y0 + y1) / 2})
        return out.get("text") if isinstance(out, dict) else out
    a, b = both(pr)
    same(a, b, "hover text")


@step("InfoTool box (scatter, upper half) == what Flag would flag")
def infotool():
    def pr(vp):
        o = panels(vp)[1]
        out = run(o._handle_probe_region(dict(tool="info_box", **full_box(o, fy=(0.5, 1.0)))))
        return out["status"], ("Flag box here" in out.get("info_html", ""))
    a, b = both(pr)
    same(a, b, "InfoTool status")
    if b[0] != "ok" or not b[1]:
        raise AssertionError(f"unexpected InfoTool result {b}")


@step("axis change: raster Baseline x Time, scatter UV distance")
def axes():
    from cubevis.toolbox.visplot.axes import Axis

    def ch(vp):
        r, s = panels(vp)
        r.update_axes(y_dim=Axis.TIME, x_dim=Axis.BASELINE, quantity=r._quantity,
                      polarization=r._polarization)
        s.update_axes(x_dim=Axis.UVDIST)
        return r._agg.values
    a, b = both(ch)
    same(a, b, "raster after axis change")


@step("flag: raster box (all selected)")
def flag_raster():
    def fl(vp):
        r = panels(vp)[0]
        resp = run(vp._handle_box_select(dict(flag=True, **full_box(r, fy=(0.0, 0.2))), "raster", r))
        return resp["notify_text"]
    a, b = both(fl)
    same(a, b, "raster flag result")
    if not b.startswith("✓"):
        raise AssertionError(b)
    return b


@step("flag: scatter box (upper third of amplitude)")
def flag_scatter():
    def fl(vp):
        s = panels(vp)[1]
        resp = run(vp._handle_box_select(dict(flag=True, **full_box(s, fy=(0.66, 1.0))), "scatter", s))
        return resp["notify_text"]
    a, b = both(fl)
    same(a, b, "scatter flag result")
    return b


@step("flag: Z-Score filter over the whole raster")
def flag_zscore():
    def fl(vp):
        run(vp.flags.handle_action({"action": "config", "filter": "zscore",
                                    "params": {"cutoff": 5.0}}))
        r = panels(vp)[0]
        resp = run(vp._handle_box_select(dict(flag=True, **full_box(r)), "raster", r))
        run(vp.flags.handle_action({"action": "config", "filter": "all"}))
        return resp["notify_text"]
    a, b = both(fl)
    same(a, b, "Z-Score flag result")
    return b


@step("rendering with pending flags applied (stale panels re-query)")
def rerender_pending():
    def rr(vp):
        r = panels(vp)[0]
        r._handle_rerender(full_box(r))
        return r._agg.values
    a, b = both(rr)
    same(a, b, "raster with pending flags")


@step("undo / redo")
def undo_redo():
    def ur(vp):
        n0 = len(vp.flag_db)
        run(vp.flags.handle_action({"action": "undo"}))
        n1 = len(vp.flag_db)
        run(vp.flags.handle_action({"action": "redo"}))
        return n0, n1, len(vp.flag_db)
    a, b = both(ur)
    same(a, b, "undo/redo counts")
    if not (b[1] == b[0] - 1 and b[2] == b[0]):
        raise AssertionError(f"counts {b}")


@step("display: show in colour (pending overlay on both panels)")
def colour():
    def co(vp):
        run(vp.flags.handle_action({"action": "config", "display": "color"}))
        shapes = []
        for o in panels(vp):
            shapes.append(o._handle_rerender(full_box(o))["image"].shape)
        run(vp.flags.handle_action({"action": "config", "display": "hide"}))
        return shapes
    a, b = both(co)
    same(a, b, "colour-mode images")


@step("user-supplied filter is refused cleanly on remote data")
def user_filter():
    vp = REMOTE
    if "loud" not in vp.flags.registry:
        raise Skip("no user filter registered")
    run(vp.flags.handle_action({"action": "config", "filter": "loud"}))
    r = panels(vp)[0]
    n = len(vp.flag_db)
    resp = run(vp._handle_box_select(dict(flag=True, **full_box(r)), "raster", r))
    run(vp.flags.handle_action({"action": "config", "filter": "all"}))
    if "local data" not in (resp["notify_text"] or "") or len(vp.flag_db) != n:
        raise AssertionError(resp["notify_text"])


@step("export flagdata + JSONL, pending-flag report")
def export():
    tmp = tempfile.mkdtemp(prefix="visplot_remote_check_")

    def ex(vp):
        tag = "remote" if vp is REMOTE else "local"
        fd = open(vp.export_flags(os.path.join(tmp, f"{tag}.flagcmd.txt"))).read().splitlines()[1:]
        vp.export_flags(os.path.join(tmp, f"{tag}.jsonl"), fmt="jsonl")
        rep = vp.flags.report_html()
        return fd, rep.count("<h3>#")
    a, b = both(ex)
    same(a, b, "flagdata lines and report sections")
    return f"{len(b[0])} flagdata line(s), {b[1]} report section(s); files in {tmp}"


@step("close")
def close():
    for vp in (LOCAL, REMOTE):
        if vp is not None:
            vp.close()


# ---------------------------------------------------------------------- #

def main():
    global ARGS, LOCAL, REMOTE, PLOT_KW
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--kernel", default=os.environ.get("CUBEVIS_TEST_KERNEL", "python3"))
    ap.add_argument("--ms", help="data path on THIS host (local comparison)")
    ap.add_argument("--remote-ms", help="data path on the kernel's host (default: --ms)")
    ap.add_argument("--field", default="", help="field selection, e.g. J1037-295")
    ap.add_argument("--correlation", default="XX,YY")
    ap.add_argument("--no-local", action="store_true", help="skip the local comparison")
    ap.add_argument("--gui", action="store_true",
                    help="after the checks, open the real GUI on the remote kernel")
    ap.add_argument("-v", "--verbose", action="store_true", help="print tracebacks")
    ARGS = ap.parse_args()
    ARGS.remote_ms = ARGS.remote_ms or ARGS.ms
    if not ARGS.remote_ms or (not ARGS.no_local and not ARGS.ms):
        ap.error("--ms (and/or --remote-ms with --no-local) is required")
    LOCAL = REMOTE = None
    PLOT_KW = dict(field=ARGS.field, correlation=ARGS.correlation,
                   flag_filters={"loud": lambda ds, level=20.0: ds["amp"] > level})
    print(f"visplot remote check: kernel={ARGS.kernel!r} remote data={ARGS.remote_ms!r}"
          + ("" if ARGS.no_local else f" local data={ARGS.ms!r}"))
    t0 = time.perf_counter()
    open_remote()
    if REMOTE is None:
        return summarize(t0)
    open_local()
    for fn in (metadata, first_render, pan_zoom, hover, infotool, axes, flag_raster,
               flag_scatter, flag_zscore, rerender_pending, undo_redo, colour,
               user_filter, export):
        fn()
    close()
    rc = summarize(t0)
    if ARGS.gui:
        print("\nOpening the GUI on the remote kernel -- follow the manual checklist in "
              "REMOTE_TESTING.md; close the browser tab to finish.")
        from cubevis import visplot
        visplot(ms=ARGS.remote_ms, backend="remote", kernel_name=ARGS.kernel,
                field=ARGS.field, correlation=ARGS.correlation)
    return rc


def summarize(t0):
    n = {k: sum(1 for r in RESULTS if r[0] == k) for k in ("PASS", "FAIL", "SKIP")}
    print(f"\n{n['PASS']} passed, {n['FAIL']} failed, {n['SKIP']} skipped "
          f"in {time.perf_counter() - t0:.1f}s")
    for r in RESULTS:
        if r[0] == "FAIL":
            print(f"  FAIL {r[1]}: {r[3]}")
    return 1 if n["FAIL"] else 0


if __name__ == "__main__":
    sys.exit(main())
