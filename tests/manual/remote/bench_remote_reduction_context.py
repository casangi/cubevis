#!/usr/bin/env python3
"""bench_remote_reduction_context.py

Chunk 2 -- latency benchmark for RemoteReductionContext.query_raster(),
at output sizes representative of an actual VisibilityRaster screen
plot, not a single pass/fail check like try_remote_reduction_context.py.

Deliberately separates three different numbers that matter for
different reasons, rather than reporting one end-to-end time:

  1. CONNECT cost -- kernel start + RemoteAppLink.open() + create_context
     + create_object. Paid once per session, not per interaction. Large
     and highly variable (seconds locally, ~2-3 minutes cold against a
     real sshpyk cluster kernel per the developer guide's own measurement)
     but largely irrelevant to whether the GUI *feels* responsive once
     connected, which is what steps 2 matters for.
  2. PER-CALL remote latency, at several max_cells sizes spanning what a
     real VisibilityRaster pan/zoom would actually request (a thumbnail-
     scale preview up through the local 2,000,000-cell default). This is
     the number that actually predicts whether interactive panning/
     zooming over cubevis.remote feels smooth or laggy -- see
     chunk2-patch-notes.md's open item about DEFAULT_REMOTE_MAX_CELLS
     tuning; this script is how you'd get a real number to tune it
     against, instead of guessing.
  3. The same sizes against a LOCAL LocalVisibilityReader, when
     --compare-local is given and --ms is resolvable from wherever this
     script itself runs, so the remote numbers have something to be
     "how much overhead" relative to, rather than being read in
     isolation.

Run against a local kernel:
    python bench_remote_reduction_context.py --kernel-name python3 \\
        --ms /path/to/test.ms --compare-local

Run against a real cluster kernel (skip --compare-local if --ms is only
resolvable on the remote host):
    python bench_remote_reduction_context.py --kernel-name zuul06_python312 \\
        --ms /path/on/remote/host.ms

Example:
    bash$ python bench_remote_reduction_context.py --kernel-name zuul06_python312 --ms "/home/zuul06-2/dschieb/casa/visplot/sis14_twhya_calibrated_flagged.ms"

"""
from __future__ import annotations

import argparse
import logging
import statistics
import sys
import time

import numpy as np


def _banner(title: str) -> None:
    print()
    print(f"--- {title} " + "-" * max(0, 60 - len(title)))


def _time_calls(fn, repeats: int, warmup: int):
    """Runs `fn()` `warmup` times untimed, then `repeats` times timed.
    Returns (elapsed_seconds_list, last_result)."""
    result = None
    for _ in range(warmup):
        result = fn()
    times = []
    for _ in range(repeats):
        t0 = time.perf_counter()
        result = fn()
        times.append(time.perf_counter() - t0)
    return times, result


def _fmt_stats(times) -> str:
    ms = [t * 1000 for t in times]
    mean = statistics.mean(ms)
    med = statistics.median(ms)
    stdev = statistics.stdev(ms) if len(ms) > 1 else 0.0
    return (f"mean={mean:8.1f}ms  median={med:8.1f}ms  "
            f"min={min(ms):8.1f}ms  max={max(ms):8.1f}ms  stdev={stdev:6.1f}ms")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--kernel-name", default="python3")
    parser.add_argument("--ms", required=True,
                         help="Path resolvable on the KERNEL's host. Only needs to also "
                              "be resolvable from wherever this script runs if "
                              "--compare-local is given.")
    parser.add_argument("--backend-kind", default="msv2", choices=["msv2", "msv4"])
    parser.add_argument("--open-timeout", type=float, default=60.0)
    parser.add_argument("--create-context-timeout", type=float, default=180.0)
    parser.add_argument(
        "--max-cells", default="30000,120000,500000,1000000,2000000",
        help="Comma-separated max_cells values to benchmark, representing output sizes "
             "from a small thumbnail (30k =~ 200x150) up through the local default "
             "(2,000,000). Default spans that range.",
    )
    parser.add_argument("--repeats", type=int, default=10,
                         help="Timed calls per size. Default 10.")
    parser.add_argument("--warmup", type=int, default=2,
                         help="Untimed calls per size before timing starts, to exclude "
                              "any first-call-only cost (e.g. lazy imports inside the "
                              "backend itself). Default 2.")
    parser.add_argument("--compare-local", action="store_true",
                         help="Also open --ms LOCALLY (from wherever this script runs) "
                              "and benchmark the same sizes against LocalVisibilityReader, "
                              "for a real 'remote overhead per call' number. Requires --ms "
                              "to be resolvable locally, not just on the kernel's host.")
    parser.add_argument("--log-level", default="INFO",
                         help="Set to INFO (default) to see RemoteReductionContext's "
                              "per-phase connect timing (start_kernel / RemoteAppLink.open "
                              "/ create_context / create_object) alongside the per-call "
                              "numbers below. Set to WARNING to suppress it.")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(name)s %(levelname)s: %(message)s",
    )

    sizes = [int(s.strip()) for s in args.max_cells.split(",") if s.strip()]

    from cubevis.toolbox.visplot.remote_reduction_context import RemoteReductionContext
    from cubevis.toolbox.visplot.axes import Axis
    from cubevis.toolbox.visplot.selection import SelectionSpec

    print("=" * 70)
    print(f"query_raster() latency benchmark -- kernel_name={args.kernel_name!r} "
          f"sizes={sizes}")
    print("=" * 70)

    _banner("Connect (timed separately -- one-time session cost, not per-interaction)")
    t0 = time.perf_counter()
    ctx = RemoteReductionContext(
        args.ms, args.kernel_name,
        backend_kind=args.backend_kind,
        open_timeout=args.open_timeout,
        create_context_timeout=args.create_context_timeout,
    )
    connect_s = time.perf_counter() - t0
    print(f"    connect: {connect_s:.1f}s")

    local_reader = None
    if args.compare_local:
        _banner("Opening --ms LOCALLY for comparison")
        from cubevis.toolbox.visplot.local_visibility_reader import LocalVisibilityReader
        if args.backend_kind == "msv2":
            from cubevis.toolbox.visplot.data.msv2_backend import MSv2Backend
            local_backend = MSv2Backend(args.ms)
        else:
            from cubevis.toolbox.visplot.data.msv4_backend import MSv4Backend
            local_backend = MSv4Backend(args.ms)
        t0 = time.perf_counter()
        local_backend.open()
        print(f"    local open: {time.perf_counter() - t0:.1f}s")
        local_reader = LocalVisibilityReader(local_backend)

    selection = SelectionSpec()
    y_dim, x_dim, quantity = Axis.BASELINE, Axis.TIME, Axis.AMPLITUDE

    rows = []
    try:
        for max_cells in sizes:
            _banner(f"max_cells={max_cells}  ({args.repeats} calls, {args.warmup} warmup)")

            remote_times, remote_result = _time_calls(
                lambda mc=max_cells: ctx.query_raster(
                    y_dim=y_dim, x_dim=x_dim, quantity=quantity,
                    selection=selection, max_cells=mc,
                ),
                args.repeats, args.warmup,
            )
            shape = remote_result[0].shape
            print(f"    remote (shape={shape}): {_fmt_stats(remote_times)}")

            local_mean_ms = None
            if local_reader is not None:
                local_times, local_result = _time_calls(
                    lambda mc=max_cells: local_reader.query_raster(
                        y_dim=y_dim, x_dim=x_dim, quantity=quantity,
                        selection=selection, max_cells=mc,
                    ),
                    args.repeats, args.warmup,
                )
                print(f"    local  (shape={local_result[0].shape}): {_fmt_stats(local_times)}")
                local_mean_ms = statistics.mean(local_times) * 1000

            remote_mean_ms = statistics.mean(remote_times) * 1000
            rows.append((max_cells, shape, remote_mean_ms, local_mean_ms))

        _banner("Summary")
        header = f"{'max_cells':>10}  {'shape':>14}  {'remote (ms)':>12}"
        if local_reader is not None:
            header += f"  {'local (ms)':>11}  {'overhead':>9}"
        print("    " + header)
        for max_cells, shape, remote_ms, local_ms in rows:
            line = f"{max_cells:>10}  {str(shape):>14}  {remote_ms:>12.1f}"
            if local_ms is not None:
                overhead = f"{remote_ms / local_ms:.1f}x" if local_ms > 0 else "n/a"
                line += f"  {local_ms:>11.1f}  {overhead:>9}"
            print("    " + line)

        if local_reader is not None:
            local_backend.close()

    finally:
        _banner("close()")
        t0 = time.perf_counter()
        ctx.close()
        print(f"    closed in {time.perf_counter() - t0:.1f}s")

    print()
    print("=" * 70)
    print(f"connect: {connect_s:.1f}s  (one-time; see script docstring on why this "
          f"is reported separately from the per-call numbers above)")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
