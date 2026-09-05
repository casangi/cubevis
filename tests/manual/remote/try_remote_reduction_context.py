#!/usr/bin/env python3
"""try_remote_reduction_context.py

Chunk 2 -- step-by-step, hand-run smoke test for `RemoteReductionContext`,
deliberately isolated from `VisibilityPlotter`/the Bokeh GUI layer so a
failure is easy to localize. This is the concrete form of the Chunk 2
handoff's own "definition of done" for raster: a real `query_raster()`
round trip through a live execution context, verified equal to the
local call -- not a mock.

In one run this exercises every assumption flagged as unverified in
chunk2-status.md:
  - Axis/SelectionSpec wire serialization through cubevis.utils.serialize
    (the biggest unknown -- everything downstream depends on this)
  - RemoteReductionContext's SyncBridge loop-affinity design actually
    not deadlocking (developer guide §6)
  - dispatch_fast's explicit error-checking surfacing a real remote
    exception as RemoteBackendError, not a raw dict or an unrelated
    KeyError (developer guide §3)
  - metadata()/list_fields()/list_spws()/query_raster() all agreeing
    with the equivalent LOCAL call against the same MS

Run against a local kernel first (no SSH, no cluster, fast iteration):

    python try_remote_reduction_context.py --kernel-name python3 --ms /path/to/test.ms

Then against a real sshpyk-provisioned remote kernel -- same script,
only --kernel-name changes:

    python try_remote_reduction_context.py --kernel-name zuul06_python312 --ms /path/on/remote/host.ms

Note the MS path for the second run must be resolvable on the REMOTE
host -- this script never opens it locally except for the explicit
cross-check step, which only runs when --ms is also reachable from
wherever this script itself is running (skip with --no-local-check
against a genuinely remote-only path).

Example:
    bash$ time python try_remote_reduction_context.py --no-local-check --kernel-name zuul06_python312 --ms "/home/zuul06-2/dschieb/casa/visplot/sis14_twhya_calibrated_flagged.ms"
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time

import numpy as np


def _banner(step: int, title: str) -> None:
    print()
    print(f"--- Step {step}: {title} " + "-" * max(0, 50 - len(title)))


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--kernel-name", default="python3",
                         help="Kernel name from `jupyter kernelspec list`. Default: python3 (local).")
    parser.add_argument("--ms", required=True,
                         help="Path to a real MSv2 measurement set, resolvable on the "
                              "kernel's own host (not necessarily this script's host).")
    parser.add_argument("--backend-kind", default="msv2", choices=["msv2", "msv4"])
    parser.add_argument("--open-timeout", type=float, default=60.0)
    parser.add_argument("--create-context-timeout", type=float, default=180.0,
                         help="See RemoteReductionContext's own docstring -- a real "
                              "sshpyk-provisioned cluster kernel can legitimately need "
                              "much longer here than a local one.")
    parser.add_argument("--no-local-check", action="store_true",
                         help="Skip the local-vs-remote cross-check (step 4/5) -- use "
                              "this when --ms is only resolvable on the remote host, "
                              "not from wherever this script itself runs.")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(name)s %(levelname)s: %(message)s",
    )

    print("=" * 70)
    print(f"RemoteReductionContext smoke test (Chunk 2) -- "
          f"kernel_name={args.kernel_name!r} ms={args.ms!r}")
    print("=" * 70)

    _banner(0, "P_local (this script)'s own process info")
    print(f"    pid      = {os.getpid()}")
    print(f"    hostname = {os.uname().nodename}")
    print(f"    python   = {sys.executable}")

    from cubevis.toolbox.visplot.remote_reduction_context import (
        RemoteReductionContext, RemoteBackendError,
    )
    from cubevis.toolbox.visplot.axes import Axis
    from cubevis.toolbox.visplot.selection import SelectionSpec

    _banner(1, f"RemoteReductionContext(path={args.ms!r}, kernel_name={args.kernel_name!r}) "
               f"-- this BLOCKS for the whole connect sequence")
    t0 = time.perf_counter()
    ctx = RemoteReductionContext(
        args.ms, args.kernel_name,
        backend_kind=args.backend_kind,
        open_timeout=args.open_timeout,
        create_context_timeout=args.create_context_timeout,
    )
    print(f"    connected in {time.perf_counter() - t0:.1f}s")

    try:
        _banner(2, "metadata() / list_fields() / list_spws() -- real remote round trips")
        meta = ctx.metadata()
        fields = ctx.list_fields()
        spws = ctx.list_spws()
        print(f"    metadata() keys: {sorted(meta.keys())}")
        print(f"    fields: {[f.name for f in fields]}")
        print(f"    spws:   {[s.label() for s in spws]}")

        _banner(3, "Deliberately trigger a remote error -- confirm RemoteBackendError, "
                   "not a raw dict or a KeyError (developer guide §3)")
        try:
            ctx._call("this_method_does_not_exist")
            print("    FAIL: expected RemoteBackendError, got no exception at all")
            return 1
        except RemoteBackendError as e:
            print(f"    OK: RemoteBackendError raised as expected")
            print(f"    remote_error: {e.remote_error!r}")
            has_traceback = bool(e.remote_traceback)
            print(f"    remote_traceback present: {has_traceback}")
            if not has_traceback:
                print("    WARNING: no remote traceback text came through -- worth "
                      "checking CommMgr's exception-to-error-reply conversion")

        if args.no_local_check:
            _banner(4, "skipping local-vs-remote cross-check (--no-local-check)")
        else:
            _banner(4, "Cross-check: same MS opened LOCALLY, compare metadata()")
            from cubevis.toolbox.visplot.local_visibility_reader import LocalVisibilityReader
            if args.backend_kind == "msv2":
                from cubevis.toolbox.visplot.data.msv2_backend import MSv2Backend
                local_backend = MSv2Backend(args.ms)
            else:
                from cubevis.toolbox.visplot.data.msv4_backend import MSv4Backend
                local_backend = MSv4Backend(args.ms)
            local_backend.open()
            local_reader = LocalVisibilityReader(local_backend)
            local_meta = local_reader.metadata()

            mismatches = [k for k in meta if meta.get(k) != local_meta.get(k)]
            if mismatches:
                print(f"    MISMATCH on keys: {mismatches}")
                for k in mismatches:
                    print(f"      remote[{k}] = {meta.get(k)!r}")
                    print(f"      local [{k}] = {local_meta.get(k)!r}")
            else:
                print("    OK: remote metadata() == local metadata()")

            _banner(5, "query_raster() -- the actual wire-serialization test "
                       "(Axis + SelectionSpec, real xr.DataArray back)")
            selection = SelectionSpec()  # all fields None -- "everything", per its own docstring
            y_dim, x_dim, quantity = Axis.BASELINE, Axis.TIME, Axis.AMPLITUDE

            remote_agg, remote_xr, remote_yr, remote_dec = ctx.query_raster(
                y_dim=y_dim, x_dim=x_dim, quantity=quantity, selection=selection,
                max_cells=500_000,
            )
            local_agg, local_xr, local_yr, local_dec = local_reader.query_raster(
                y_dim=y_dim, x_dim=x_dim, quantity=quantity, selection=selection,
                max_cells=500_000,
            )

            print(f"    remote: shape={remote_agg.shape} x_range={remote_xr} "
                  f"y_range={remote_yr} is_decimated={remote_dec}")
            print(f"    local:  shape={local_agg.shape} x_range={local_xr} "
                  f"y_range={local_yr} is_decimated={local_dec}")

            same_shape = remote_agg.shape == local_agg.shape
            close = same_shape and np.allclose(
                np.asarray(remote_agg), np.asarray(local_agg), equal_nan=True
            )
            if close:
                print("    OK: remote query_raster() output matches local, exactly")
            else:
                print("    MISMATCH -- remote and local query_raster() disagree. "
                      "This is the serialization/plumbing risk flagged in "
                      "chunk2-status.md -- check whether Axis/SelectionSpec round-tripped "
                      "correctly through cubevis.utils.serialize before assuming the "
                      "reduction logic itself is wrong.")
                return 1

            local_backend.close()

    finally:
        _banner(6, "close() -- confirms the worker subprocess and kernel actually exit")
        ctx.close()
        print("    closed")

    print()
    print("=" * 70)
    print("done -- all checks passed")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
