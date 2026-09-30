#!/usr/bin/env python3
"""
bench_remote_overhead.py
========================
Estimate what the Jupyter-kernel execution model costs, per call, on top of
the computation itself.

For each operation the GUI performs (render queries, flag evaluation, pending
flag installation, InfoTool probe), this times

* **local**   -- the call on an in-process ``LocalVisibilityReader``;
* **remote**  -- the same call through ``RemoteReductionContext`` (client
                 round trip, as the plotter experiences it);
* **worker**  -- the time the worker itself spent in that method
                 (``VisplotRemoteBackend`` timing, see ``call_stats``);

and reports **overhead = remote - worker** (transport, serialization,
kernel dispatch) and the **payload** (pickled size of the result, a close
proxy for what crosses the wire).  Medians over ``--repeat`` runs after one
warm-up call, so cold-start costs (first MS open, dask graph caches) are
excluded and reported separately.

Usage::

    python bench_remote_overhead.py --ms PATH [--kernel python3] [--field NAME]
           [--repeat 5] [--remote-ms PATH_ON_KERNEL_HOST] [--markdown out.md]

Example::

    bash$ python bench_remote_overhead.py --ms sis14_twhya_calibrated_flagged.ms
                 --remote-ms /home/zuul-2/dschieb/casa/visplot/sis14_twhya_calibrated_flagged.ms
                 --kernel cvpost140_python312 --field Ceres --gui --markdown cvpost140-13.md

A local kernel measures the protocol/serialization overhead alone; a real
remote kernel (e.g. sshpyk) adds the network.  Run both to separate them.
"""
from __future__ import annotations

import argparse
import dataclasses
import pickle
import statistics
import time
import warnings

warnings.filterwarnings("ignore")


def _size(obj) -> int:
    try:
        return len(pickle.dumps(obj, protocol=pickle.HIGHEST_PROTOCOL))
    except Exception:
        return -1


def _fmt_bytes(n: int) -> str:
    if n < 0:
        return "?"
    for unit in ("B", "kB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ms", required=True)
    ap.add_argument("--remote-ms", default=None, help="path of the MS on the kernel's host")
    ap.add_argument("--kernel", default="python3")
    ap.add_argument("--field", default=None)
    ap.add_argument("--repeat", type=int, default=5)
    ap.add_argument("--markdown", default=None)
    ap.add_argument("--ref-scales", default="0,1,2,4",
                    help="scatter reference scales to compare for a full remote scatter "
                         "render (0 = no reference); empty to skip")
    ap.add_argument("--gui", action="store_true",
                    help="also time user-visible GUI operations through VisibilityPlotter "
                         "(flag box + panel redraws, undo, report), local vs kernel")
    a = ap.parse_args(argv)

    from cubevis.toolbox.visplot.axes import Axis
    from cubevis.toolbox.visplot.data.msv2_backend import MSv2Backend
    from cubevis.toolbox.visplot.data.reader import ScatterLayerSpec
    from cubevis.toolbox.visplot.flag_model import FlagDelta
    from cubevis.toolbox.visplot.local_visibility_reader import LocalVisibilityReader
    from cubevis.toolbox.visplot.remote_reduction_context import RemoteReductionContext
    from cubevis.toolbox.visplot.selection import SelectionSpec

    t0 = time.perf_counter()
    b = MSv2Backend(a.ms); b.open()
    local = LocalVisibilityReader(b)
    t_local_open = time.perf_counter() - t0
    t0 = time.perf_counter()
    remote = RemoteReductionContext(a.remote_ms or a.ms, a.kernel, backend_kind="msv2",
                                    call_timeout=600)
    t_remote_open = time.perf_counter() - t0
    rinfo = remote.runtime_info()
    print("remote runtime:", rinfo, flush=True)
    if "error" in rinfo or not rinfo.get("frame_stats") or rinfo.get("frame_debug"):
        print("WARNING: the remote kernel environment is NOT running this cubevis "
              "(relay counters missing or frame debugging on); w-encode will read 0 and "
              "the relay numbers include the old per-chunk debug writes.", flush=True)

    sel = SelectionSpec(field_names=[a.field] if a.field else None)
    pols = [str(p) for p in next(iter(b._iter_visibility_partitions())).polarization.values]
    pol = pols[0]
    part = next(iter(b._iter_visibility_partitions(sel)))
    t = part.time.values
    tm = (float(t[len(t) // 3]), float(t[len(t) // 2]))

    raster_kw = dict(y_dim=Axis.TIME, x_dim=Axis.CHANNEL, quantity=Axis.AMPLITUDE,
                     selection=sel, polarization=pol)
    layers = [ScatterLayerSpec(y_axis=Axis.AMPLITUDE, polarization=pol,
                               cmap=("#000000", "#ffffff"))]
    req_region = dict(flag=True, selection=sel, kind="raster", x_axis="TIME", x0=tm[0],
                      x1=tm[1], y_axis="BASELINE", y0=-0.5, y1=1e6, polarization=pol)
    amp_hi = float(abs(part.VISIBILITY.sel(polarization=pol)).quantile(0.999).values)
    req_scatter = dict(flag=True, selection=sel, kind="scatter", x_axis="TIME",
                       x0=float(t.min()) - 1, x1=float(t.max()) + 1, y0=amp_hi, y1=1e30,
                       layers=[{"y_axis": "AMPLITUDE", "polarization": pol}])
    req_z = dict(req_region, filter={"name": "zscore", "params": {"cutoff": 5.0}})

    region_d = FlagDelta.from_dict(local.evaluate_flag_request(dict(req_region))["delta"])
    samp = local.evaluate_flag_request(dict(req_scatter))["delta"]
    samp_d = FlagDelta.from_dict(samp) if samp else region_d

    ops = [
        ("flag_spw_table (ping)", lambda r: r.flag_spw_table(), "flag_spw_table"),
        ("query_raster time x channel", lambda r: r.query_raster(**raster_kw), "query_raster"),
        ("query_columns scatter 800x600",
         lambda r: r.query_columns(Axis.TIME, layers, sel, width=800, height=600), "query_columns"),
        ("set_pending_flags 1 region", lambda r: r.set_pending_flags([region_d], 1),
         ("set_pending_flags", "sync_pending_flags")),
        ("set_pending_flags 100 regions (full resend)",
         lambda r: (r.__dict__.pop("_cv_sent_ids", None), r.set_pending_flags(
             [dataclasses.replace(region_d, delta_id=f"r{i}") for i in range(100)], 2)),
         ("set_pending_flags", "sync_pending_flags")),
        ("set_pending_flags +1 of 100 (incremental)",
         lambda r: r.set_pending_flags(
             [dataclasses.replace(region_d, delta_id=f"r{i}") for i in range(100)]
             + [dataclasses.replace(region_d, delta_id=f"x{time.perf_counter_ns()}")], 3),
         ("set_pending_flags", "sync_pending_flags")),
        (f"set_pending_flags 1 sample set ({samp_d.n_samples or 0} samples)",
         lambda r: r.set_pending_flags([samp_d], 4), ("set_pending_flags", "sync_pending_flags")),
        ("evaluate_flag_request raster box", lambda r: r.evaluate_flag_request(dict(req_region)),
         "evaluate_flag_request"),
        ("evaluate_flag_request scatter box", lambda r: r.evaluate_flag_request(dict(req_scatter)),
         "evaluate_flag_request"),
        ("evaluate_flag_request Z-Score filter", lambda r: r.evaluate_flag_request(dict(req_z)),
         "evaluate_flag_request"),
        ("probe_flag_region (InfoTool box)", lambda r: r.probe_flag_region(dict(req_scatter)),
         "probe_flag_region"),
    ]

    rows = []
    for label, fn, method in ops:
        for r in (local, remote):              # warm-up (not timed)
            fn(r)
        lt, rt, wt = [], [], []
        payload = -1
        for _ in range(a.repeat):
            s = time.perf_counter(); out = fn(local); lt.append(time.perf_counter() - s)
            remote.call_stats(reset=True)
            s = time.perf_counter(); out = fn(remote); rt.append(time.perf_counter() - s)
            st = remote.call_stats()["worker"]
            names = method if isinstance(method, tuple) else (method,)
            ws = [st[m][1] for m in names if m in st]     # total worker time this call
            if ws:
                wt.append(sum(ws))
            payload = _size(out)
        L, R = statistics.median(lt), statistics.median(rt)
        W = statistics.median(wt) if wt else float("nan")
        rows.append((label, L, R, W, R - W, payload))
        print(f"{label:48s} local {L*1e3:9.1f} ms  remote {R*1e3:9.1f} ms  worker "
              f"{W*1e3:9.1f} ms  overhead {(R-W)*1e3:8.1f} ms  payload {_fmt_bytes(payload)}",
              flush=True)
    ref_rows = []
    if a.ref_scales:
        from cubevis.utils._conversion import remote_serialize
        for rs in [float(v) for v in a.ref_scales.split(",") if v.strip()]:
            kw = dict(width=500, height=550, probe_grid_max_cells=3072,
                      ref_scale=rs if rs > 0 else None, color_mode="global")
            two = [ScatterLayerSpec(y_axis=Axis.AMPLITUDE, polarization=p_,
                                    cmap=("#000000", "#ffffff")) for p_ in pols[:2]]
            wire = len(remote_serialize(local.query_columns(Axis.UVDIST, two, sel, **kw)))
            remote.query_columns(Axis.UVDIST, two, sel, **kw)          # warm
            rts, wts = [], []
            for _ in range(a.repeat):
                remote.call_stats(reset=True)
                s_ = time.perf_counter(); remote.query_columns(Axis.UVDIST, two, sel, **kw)
                rts.append(time.perf_counter() - s_)
                wts.append(remote.call_stats()["worker"]["query_columns"][1])
            R, W = statistics.median(rts), statistics.median(wts)
            ref_rows.append((rs, wire, R, W))
            print(f"scatter full render ref_scale={rs:g}: wire {_fmt_bytes(wire)}  remote "
                  f"{R*1e3:.0f} ms  worker {W*1e3:.0f} ms  overhead {(R-W)*1e3:.0f} ms", flush=True)
    for rd in (local, remote):
        rd.set_pending_flags([], 0)
    remote.close(); b.close()

    lines = [f"# Remote execution overhead ({a.kernel})", "",
             f"MS: `{a.ms}`" + (f", field `{a.field}`" if a.field else ""),
             f"Open: local {t_local_open:.2f} s, remote session (kernel start + worker + MS open) "
             f"{t_remote_open:.2f} s",
             f"Remote runtime: `{rinfo}`", "",
             "| operation | local | remote | worker | overhead | payload |",
             "|---|---:|---:|---:|---:|---:|"]
    for label, L, R, W, O, P in rows:
        lines.append(f"| {label} | {L*1e3:.1f} ms | {R*1e3:.1f} ms | {W*1e3:.1f} ms | "
                     f"{O*1e3:.1f} ms | {_fmt_bytes(P)} |")
    if ref_rows:
        lines += ["", "## Full scatter render vs reference scale (2 layers, 500x550)", "",
                  "| ref_scale | wire | remote | worker | overhead |", "|---:|---:|---:|---:|---:|"]
        for rs, wire, R, W in ref_rows:
            lines.append(f"| {rs:g} | {_fmt_bytes(wire)} | {R*1e3:.0f} ms | {W*1e3:.0f} ms | "
                         f"{(R-W)*1e3:.0f} ms |")
    if a.gui:
        lines += ["", "## GUI operations (VisibilityPlotter, user-visible latency)", "",
                  "| operation | local | kernel | difference |", "|---|---:|---:|---:|"]
        for label, L, R in _gui_bench(a):
            lines.append(f"| {label} | {L*1e3:.0f} ms | {R*1e3:.0f} ms | {(R-L)*1e3:+.0f} ms |")
        lines += _breakdown_lines(getattr(_gui_bench, "breakdown", {}))
    text = "\n".join(lines) + "\n"
    print("\n" + text)
    if a.markdown:
        open(a.markdown, "w").write(text)


def _gui_bench(a):
    """Time what a user waits for: a flag box (evaluation + pending-state
    push) and the two panel redraws that follow, undo, and the report."""
    import asyncio
    from cubevis.toolbox.visplot import VisibilityPlotter

    def run(kernel):
        kw = dict(layout="side", correlation="XX,YY")
        if a.field:
            kw["field"] = a.field
        t0 = time.perf_counter()
        if kernel:
            kw.update(backend="remote", kernel_name=kernel)
        vp = VisibilityPlotter(ms=(a.remote_ms or a.ms) if kernel else a.ms, **kw)
        if kernel:
            assert type(vp._reader).__name__ == "RemoteReductionContext", \
                "GUI benchmark did not get a remote reader"
        out = {"construct (first render)": time.perf_counter() - t0}
        if kernel:
            breakdown["construct (first render)"] = (out["construct (first render)"],
                                                     vp.remote_call_stats())
        r, sc = vp._slots[0].raster, vp._slots[1].scatter
        x0, x1 = r._x_range; y0, y1 = r._y_range
        view = dict(x0=x0, x1=x1, y0=y0, y1=y1)
        sview = dict(x0=sc._x_range[0], x1=sc._x_range[1], y0=sc._y_range[0], y1=sc._y_range[1])

        def timed(label, fn):
            ts = []
            for _ in range(max(1, a.repeat)):
                if kernel:
                    vp.remote_call_stats(reset=True)
                t = time.perf_counter(); fn(); ts.append(time.perf_counter() - t)
                if kernel:
                    breakdown[label] = (ts[-1], vp.remote_call_stats())
                asyncio.run(vp.flags.handle_action({"action": "clear"}))
                for p in (r, sc):
                    p._handle_rerender(dict(view) if p is r else dict(sview))
            out[label] = statistics.median(ts)

        def raster_box():
            asyncio.run(vp._handle_box_select(dict(x0=x0 + (x1 - x0) * 0.3, x1=x0 + (x1 - x0) * 0.4,
                                                   y0=y0 + (y1 - y0) * 0.3, y1=y0 + (y1 - y0) * 0.4,
                                                   flag=True), "raster", r))

        def redraw():
            r._handle_rerender(dict(view)); sc._handle_rerender(dict(sview))

        timed("raster flag box", raster_box)
        timed("raster flag box + both redraws", lambda: (raster_box(), redraw()))
        hi = sc._y_range[0] + (sc._y_range[1] - sc._y_range[0]) * 0.8
        timed("scatter flag box + both redraws", lambda: (asyncio.run(vp._handle_box_select(
            dict(sview, y0=hi, flag=True), "scatter", sc)), redraw()))
        timed("undo + both redraws", lambda: (raster_box(), asyncio.run(
            vp.flags.handle_action({"action": "undo"})), redraw()))
        timed("describe pending flags", lambda: (raster_box(), vp.flags.report_html()))

        # Zoomed in: both panels showing the central quarter of their data
        # (Level-2 views), then a raster flag + both redraws at that zoom.
        zr = dict(x0=x0 + (x1 - x0) * 0.375, x1=x0 + (x1 - x0) * 0.625,
                  y0=y0 + (y1 - y0) * 0.375, y1=y0 + (y1 - y0) * 0.625)
        sx0, sx1, sy0, sy1 = sview["x0"], sview["x1"], sview["y0"], sview["y1"]
        zs = dict(x0=sx0 + (sx1 - sx0) * 0.375, x1=sx0 + (sx1 - sx0) * 0.625,
                  y0=sy0, y1=sy0 + (sy1 - sy0) * 0.25)

        def zoomed():
            r._handle_rerender(dict(zr)); sc._handle_rerender(dict(zs))

        zoomed()
        view_saved, sview_saved = dict(view), dict(sview)
        view.update(zr); sview.update(zs)
        timed("zoomed: raster flag box + both redraws",
              lambda: (asyncio.run(vp._handle_box_select(
                  dict(x0=zr["x0"] + (zr["x1"] - zr["x0"]) * 0.3,
                       x1=zr["x0"] + (zr["x1"] - zr["x0"]) * 0.4,
                       y0=zr["y0"] + (zr["y1"] - zr["y0"]) * 0.3,
                       y1=zr["y0"] + (zr["y1"] - zr["y0"]) * 0.4, flag=True), "raster", r)),
                       zoomed()))
        view.update(view_saved); sview.update(sview_saved)
        vp.close()
        return out

    breakdown = {}
    run(None)                       # warm-up: first-use imports/JIT, OS file cache
    loc, rem = run(None), run(a.kernel)
    _gui_bench.breakdown = breakdown
    return [(k, loc[k], rem[k]) for k in loc]


def _breakdown_lines(breakdown) -> list:
    """Where the remote time of each GUI operation went (last repetition).

    worker     -- time the worker spent computing (sum over calls)
    w-encode   -- worker serializing its replies
    c-decode   -- P_local deserializing them
    relay/net  -- everything else: kernel decode + re-encode, Jupyter
                  messaging, the network, and P_local work between calls
    """
    lines = ["", "## Remote time breakdown per GUI operation (last repetition)", "",
             "| operation | total | calls | bytes in | worker | w-encode | c-decode | relay/net |",
             "|---|---:|---|---:|---:|---:|---:|---:|"]
    per_method = ["", "## Worker time per method (last repetition)", "",
                  "| operation | worker time by method |", "|---|---|"]
    for label, (total, st) in breakdown.items():
        client = st.get("client", {})
        worker = st.get("worker", {})
        relay = st.get("relay", {}) or {}
        calls = ", ".join(f"{m}×{v[0]}" for m, v in sorted(client.items(), key=lambda kv: -kv[1][1])
                          if m != "call_stats")
        wsum = sum(v[1] for m, v in worker.items() if m != "call_stats" and isinstance(v, list))
        wf = relay.get("worker") or {}
        cf = relay.get("client") or {}
        enc, dec = float(wf.get("encode_s", 0.0)), float(cf.get("decode_s", 0.0))
        other = total - wsum - enc - dec
        per_method.append(f"| {label} | " + ", ".join(
            f"{m} {v[1]*1e3:.0f} ms" + (f" (×{v[0]})" if v[0] > 1 else "")
            for m, v in sorted(worker.items(), key=lambda kv: -(kv[1][1] if isinstance(kv[1], list) else 0))
            if m != "call_stats" and isinstance(v, list)) + " |")
        lines.append(f"| {label} | {total*1e3:.0f} ms | {calls} | "
                     f"{_fmt_bytes(int(cf.get('bytes_in', 0)))} | {wsum*1e3:.0f} ms | "
                     f"{enc*1e3:.0f} ms | {dec*1e3:.0f} ms | {other*1e3:.0f} ms |")
    return lines + per_method


if __name__ == "__main__":
    main()
