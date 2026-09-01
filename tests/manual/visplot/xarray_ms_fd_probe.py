#!/usr/bin/env python3
"""xarray_ms_fd_probe.py
=======================

Standalone file-descriptor probe for ``xarray-ms`` -- **no visplot code
involved**.

Why this exists
----------------
An investigation into a file-descriptor leak reported against ``visplot``
found the leak is in ``visplot`` itself: ``VisibilityPlotter`` never
called ``.close()`` on the backend it opens, so every
``VisibilityPlotter(ms=...)`` call leaked that backend's open file
descriptors for the life of the process (fixed separately in
``visibility_plotter.py``).

That fix only helps if ``xarray-ms``'s own ``.close()`` actually releases
everything it opened. This script isolates that one question: open an
MSv2 measurement set via ``xr.open_datatree(..., engine="xarray-ms:msv2")``
and close it, in a loop, while tracking this process's open-file-
descriptor count. If the count returns to (roughly) baseline after each
``close()``, xarray-ms is clean and the visplot-side fix is the whole
story. If the count grows roughly linearly with the number of iterations
despite every DataTree being closed, that is a genuine xarray-ms leak and
this script's output (plus the printed environment info) is what to
attach to an upstream bug report.

Usage
-----
::

    pip install psutil          # only extra dependency beyond xarray-ms
    python xarray_ms_fd_probe.py /path/to/some.ms --iterations 50

On macOS, if you want headroom beyond the default per-process limit::

    ulimit -n 8096
    python xarray_ms_fd_probe.py /path/to/some.ms --iterations 200

What "leak" means here
-----------------------
This script measures *process-wide* FD count (``lsof -p <pid>`` on
macOS, ``/proc/<pid>/fd`` on Linux, via ``psutil``), not FDs attributed
to a specific object. It open+closes in a tight loop with everything
else held constant, so any steady per-iteration growth is attributable
to the open/close cycle under test rather than to Python/dask/bokeh
startup overhead (which shows up once, before iteration 1, not on every
iteration).

Two independent checks are run:

1. **Context-manager form** -- ``with xr.open_datatree(...) as dt:``.
   This is the form most likely to be fully clean, since ``__exit__``
   is guaranteed to run.
2. **Explicit open()/close() form** -- ``dt = xr.open_datatree(...)``
   then ``dt.close()`` -- mirroring how ``MSv2Backend`` in visplot
   actually drives it (open now, close later, no ``with``).

If (1) is clean but (2) leaks, that points at something not fully
released outside the context-manager path specifically (e.g. a node in
the DataTree that ``DataTree.close()`` doesn't recurse into, or a
lazily-created reader keyed off first access that a bare ``close()``
never touches, but ``__exit__`` does).
"""

from __future__ import annotations

import argparse
import gc
import sys
from dataclasses import dataclass, field


def _num_open_fds() -> int:
    """Return the current process's open file-descriptor count.

    Uses ``psutil`` (works on macOS and Linux) rather than shelling out
    to ``lsof`` on every sample, which would itself transiently open
    file descriptors and skew the measurement.
    """
    import psutil

    p = psutil.Process()
    # macOS: num_fds(); some psutil builds also expose it on Linux, but
    # fall back to len(open_files()) + len(connections()) if not present.
    if hasattr(p, "num_fds"):
        return p.num_fds()
    return len(p.open_files())  # best-effort fallback


@dataclass
class Sample:
    iteration: int
    phase: str  # "after_open" / "after_close"
    fds: int


@dataclass
class Results:
    baseline_fds: int
    samples: list[Sample] = field(default_factory=list)

    def summarize(self, label: str) -> None:
        after_close = [s.fds for s in self.samples if s.phase == "after_close"]
        if not after_close:
            print(f"[{label}] no samples collected")
            return
        first, last = after_close[0], after_close[-1]
        growth = last - self.baseline_fds
        per_iter = (last - first) / max(1, len(after_close) - 1)
        print(f"[{label}] baseline={self.baseline_fds}  "
              f"first_after_close={first}  last_after_close={last}  "
              f"net_growth_vs_baseline={growth}  "
              f"avg_growth_per_iter(after first)={per_iter:.3f}")
        if growth <= 2:
            print(f"[{label}] VERDICT: clean -- FDs returned to ~baseline "
                  f"after close(). xarray-ms does not appear to leak here.")
        elif per_iter >= 0.5:
            print(f"[{label}] VERDICT: LEAKING -- FD count grows roughly "
                  f"linearly with iterations even though every DataTree "
                  f"was closed. This points at xarray-ms/arcae, not "
                  f"visplot.")
        else:
            print(f"[{label}] VERDICT: inconclusive -- some net growth but "
                  f"not clearly linear. Re-run with more --iterations to "
                  f"disambiguate one-time warmup cost from a real leak.")


def _run(path: str, iterations: int, use_context_manager: bool,
          partition_schema: list[str], chunks: dict) -> Results:
    import xarray as xr

    gc.collect()
    baseline = _num_open_fds()
    results = Results(baseline_fds=baseline)

    for i in range(iterations):
        if use_context_manager:
            with xr.open_datatree(
                path,
                engine="xarray-ms:msv2",
                partition_schema=partition_schema,
                chunks=chunks,
            ) as dt:
                # Touch something cheap to force at least metadata access,
                # mirroring what a real caller would do -- an unused,
                # never-touched handle is a less faithful reproduction.
                _ = list(dt.groups)
                results.samples.append(
                    Sample(i, "after_open", _num_open_fds())
                )
            results.samples.append(Sample(i, "after_close", _num_open_fds()))
        else:
            dt = xr.open_datatree(
                path,
                engine="xarray-ms:msv2",
                partition_schema=partition_schema,
                chunks=chunks,
            )
            try:
                _ = list(dt.groups)
                results.samples.append(
                    Sample(i, "after_open", _num_open_fds())
                )
            finally:
                dt.close()
            results.samples.append(Sample(i, "after_close", _num_open_fds()))

        # No gc.collect() inside the loop on purpose for most of the run --
        # a real caller doing back-to-back visplot() calls in an ipython
        # REPL won't force a collection either, and reference cycles that
        # only die on a gc sweep are part of what makes this leak show up
        # in practice. See the --gc-every-iter flag to test the opposite
        # hypothesis.

    return results


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("ms_path", help="Path to an MSv2 .ms directory")
    ap.add_argument("--iterations", type=int, default=50,
                     help="Number of open/close cycles per phase (default: 50)")
    ap.add_argument("--partition-schema", nargs="+",
                     default=["DATA_DESC_ID", "OBSERVATION_ID"],
                     help="Partition schema columns forwarded to xarray-ms "
                          "(default: DATA_DESC_ID OBSERVATION_ID, matching "
                          "visplot's MSv2Backend default)")
    ap.add_argument("--gc-every-iter", action="store_true",
                     help="Force gc.collect() after every close(). If this "
                          "makes a leak disappear, the issue is reference "
                          "cycles delaying collection rather than a true "
                          "unreachable-resource leak.")
    args = ap.parse_args()

    try:
        import psutil  # noqa: F401
    except ImportError:
        print("This script requires psutil: pip install psutil",
              file=sys.stderr)
        return 2

    try:
        import xarray  # noqa: F401
        import xarray_ms  # noqa: F401
    except ImportError as exc:
        print(f"xarray / xarray-ms not importable: {exc}", file=sys.stderr)
        return 2

    try:
        import xarray as xr
        print(f"xarray {xr.__version__}")
    except Exception:
        pass
    try:
        import xarray_ms
        print(f"xarray_ms {getattr(xarray_ms, '__version__', 'unknown')}")
    except Exception:
        pass
    try:
        import arcae
        print(f"arcae {getattr(arcae, '__version__', 'unknown')}")
    except Exception:
        pass

    chunks = {"time": 100, "baseline_id": 100}  # matches visplot's default

    print("\n--- Phase 1: context-manager form (with xr.open_datatree(...) as dt) ---")
    r1 = _run(args.ms_path, args.iterations, use_context_manager=True,
              partition_schema=args.partition_schema, chunks=chunks)
    if args.gc_every_iter:
        import gc as _gc
        _gc.collect()
    r1.summarize("context-manager")

    print("\n--- Phase 2: explicit open()/close() form (matches visplot's MSv2Backend) ---")
    r2 = _run(args.ms_path, args.iterations, use_context_manager=False,
              partition_schema=args.partition_schema, chunks=chunks)
    if args.gc_every_iter:
        import gc as _gc
        _gc.collect()
    r2.summarize("explicit-close")

    print("\nRaw samples (iteration, phase, fds):")
    for label, r in (("context-manager", r1), ("explicit-close", r2)):
        for s in r.samples:
            print(f"  [{label}] {s.iteration:4d}  {s.phase:12s}  {s.fds}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
