"""
Unit test for the actual delivered `probe_scatter_region` implementation
(MSv2Backend and MSv4Backend), run against synthetic data rather than a
real MS -- specifically targeting the piece this conversation flagged as
highest-risk: the discrete `bl_ids` exact-match path, since it has no
precedent elsewhere in the codebase to have inherited correctness from.

Strategy: import the REAL, unmodified `probe_scatter_region` method from
the delivered source (no reimplementation), and monkeypatch only the
pre-existing, already-proven-elsewhere helper methods it calls
(`_iter_visibility_partitions`, `_apply_selection`, `_resolve_vis`,
`_flag_mask`, `_lazy_quantity`, `_lazy_x_axis`) with small synthetic
stand-ins. This tests exactly the new masking/accounting/reduction logic
in `probe_scatter_region` itself, independent of whether the (already
shipped, already working) lazy-quantity machinery is correct.

Not a substitute for the real GUI/data pass -- no real xarray backend,
no real dask graph, no real MS coordinates. But it exercises the actual
shipped code path, not a paraphrase of it.
"""
import os
import sys
import numpy as np
import xarray as xr

# Points at a package root containing cubevis/toolbox/visplot/... . The
# default matches the throwaway package this was assembled into for
# testing without a full cubevis checkout on hand (see this delivery's
# test README); point it at a real checkout instead if you have one --
# these tests otherwise need no other special setup, since they
# monkeypatch away every method that would need real MS access.
sys.path.insert(0, os.environ.get("CUBEVIS_PKG_ROOT", "/home/claude/testpkg"))

from cubevis.toolbox.visplot.axes import Axis
from cubevis.toolbox.visplot.selection import SelectionSpec
import cubevis.toolbox.visplot.data.msv2_backend as msv2
import cubevis.toolbox.visplot.data.msv4_backend as msv4

FAILURES = []


def check(label, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {label}" + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(label)


# ---------------------------------------------------------------------
# Synthetic data. Deliberately: a single contiguous y-range rectangle
# matches a NON-contiguous set of baseline_ids (0 and 3, skipping 1, 2,
# 4) -- exactly the real-world shape of the problem the bl_ids fix
# exists for (amplitude/UVdist have no monotonic relationship to
# baseline_id, so a clicked box's matched baseline_ids are commonly
# scattered, not a contiguous run).
# ---------------------------------------------------------------------
TIMES = [100.0, 101.0, 102.0, 103.0]
BLS   = [0, 1, 2, 3, 4]
FREQS = [100.0e9, 100.5e9, 101.0e9]

# y-value per baseline_id, by polarization. XX's mapping puts bl {0, 3}
# inside the test's y_range and {1, 2, 4} outside it, non-contiguously.
# YY's mapping puts every baseline outside the range -> "no_data".
Y_MAP = {
    "XX": {0: 10.0, 1: 50.0, 2: 60.0, 3: 12.0, 4: 70.0, 5: 999.0},
    "YY": {0: 1000.0, 1: 1050.0, 2: 1060.0, 3: 1012.0, 4: 1070.0, 5: 11.0},
}


def make_partition(times, bls, freqs, pols=("XX", "YY")):
    return xr.Dataset(
        coords={
            "time": ("time", np.array(times, dtype=float)),
            "baseline_id": ("baseline_id", np.array(bls, dtype=float)),
            "frequency": ("frequency", np.array(freqs, dtype=float)),
            "polarization": ("polarization", np.array(pols)),
        }
    )


def stub_lazy_quantity_v2(vis, flag, axis, pol, ds):
    """MSv2Backend signature: (vis, flag, axis, pol, ds)."""
    nt, nf = ds.sizes["time"], ds.sizes["frequency"]
    bl_vals = ds.coords["baseline_id"].values
    ymap = Y_MAP[str(pol)]
    y_per_bl = np.array([ymap[int(b)] for b in bl_vals])
    y = np.broadcast_to(y_per_bl[None, :, None], (nt, len(bl_vals), nf)).astype(float)
    coords = {k: ds.coords[k] for k in ("time", "baseline_id", "frequency")}
    return xr.DataArray(y, dims=("time", "baseline_id", "frequency"), coords=coords)


def stub_lazy_quantity_v4(vis, flag, axis, pol):
    """MSv4Backend signature has no `ds` argument -- so it can't build
    from `ds.coords` directly. Real MSv4Backend closes over `ds`
    differently; for this stub we cheat by stashing the current
    partition on a module-level variable set right before the call,
    which is fine for a controlled unit test."""
    ds = _current_ds_v4[0]
    return stub_lazy_quantity_v2(vis, flag, axis, pol, ds)


_current_ds_v4 = [None]


def stub_lazy_x_axis(ds, axis, template):
    nt, nb, nf = ds.sizes["time"], ds.sizes["baseline_id"], ds.sizes["frequency"]
    t_vals = ds.coords["time"].values
    x = np.broadcast_to(t_vals[:, None, None], (nt, nb, nf)).astype(float)
    coords = {k: ds.coords[k] for k in ("time", "baseline_id", "frequency")}
    return xr.DataArray(x, dims=("time", "baseline_id", "frequency"), coords=coords)


def make_backend(cls, partitions, is_v4=False):
    inst = cls.__new__(cls)
    inst._require_open = lambda: None
    inst._iter_visibility_partitions = lambda selection: iter(partitions)
    inst._apply_selection = lambda raw_ds, selection: raw_ds
    inst._resolve_vis = lambda ds: None
    inst._flag_mask = lambda ds: None
    if is_v4:
        def _lq(vis, flag, axis, pol):
            return stub_lazy_quantity_v4(vis, flag, axis, pol)
        inst._lazy_quantity = _lq
    else:
        inst._lazy_quantity = stub_lazy_quantity_v2
    inst._lazy_x_axis = stub_lazy_x_axis
    return inst


def run_basic_case(cls, is_v4, label_prefix):
    ds = make_partition(TIMES, BLS, FREQS)
    if is_v4:
        _current_ds_v4[0] = ds
    inst = make_backend(cls, [ds], is_v4=is_v4)

    result = inst.probe_scatter_region(
        x_axis=Axis.TIME,
        yaxes=[(Axis.AMPLITUDE, "XX"), (Axis.AMPLITUDE, "YY")],
        selection=SelectionSpec(),
        x_range=(100.5, 102.5),   # excludes t=100, t=103
        y_range=(9.0, 13.0),      # matches XX bl {0, 3} only, non-contiguous
        max_samples=200_000,
    )

    xx = result[(Axis.AMPLITUDE, "XX")]
    yy = result[(Axis.AMPLITUDE, "YY")]

    check(f"{label_prefix}: XX status is 'ok'", xx["status"] == "ok", xx)
    check(f"{label_prefix}: XX n_samples == 12 (2 times x 2 bls x 3 freqs)",
          xx["n_samples"] == 12, xx["n_samples"])
    check(f"{label_prefix}: XX t_range == (101.0, 102.0)",
          xx["t_range"] == (101.0, 102.0), xx["t_range"])
    check(f"{label_prefix}: XX bl_range == (0.0, 3.0)",
          xx["bl_range"] == (0.0, 3.0), xx["bl_range"])
    check(f"{label_prefix}: XX bl_ids == [0, 3] (discrete, NOT the contiguous "
          f"range 0-3 that would wrongly include baselines 1 and 2)",
          xx["bl_ids"] == [0, 3], xx["bl_ids"])
    check(f"{label_prefix}: XX freq_range == (100.0e9, 101.0e9)",
          xx["freq_range"] == (100.0e9, 101.0e9), xx["freq_range"])

    check(f"{label_prefix}: YY status is 'no_data' (all YY y-values are "
          f"outside the requested y_range)", yy["status"] == "no_data", yy)
    check(f"{label_prefix}: YY n_samples == 0", yy["n_samples"] == 0, yy["n_samples"])


def run_too_many_points_case(cls, is_v4, label_prefix):
    ds = make_partition(TIMES, BLS, FREQS)
    if is_v4:
        _current_ds_v4[0] = ds
    inst = make_backend(cls, [ds], is_v4=is_v4)

    result = inst.probe_scatter_region(
        x_axis=Axis.TIME,
        yaxes=[(Axis.AMPLITUDE, "XX")],
        selection=SelectionSpec(),
        x_range=(100.5, 102.5),
        y_range=(9.0, 13.0),
        max_samples=5,   # XX's real count here is 12 -> should trip the guard
    )
    xx = result[(Axis.AMPLITUDE, "XX")]
    check(f"{label_prefix}: too_many_points guard trips when count (12) > max_samples (5)",
          xx["status"] == "too_many_points", xx)
    check(f"{label_prefix}: too_many_points still reports a (lower-bound) n_samples",
          xx["n_samples"] >= 5, xx["n_samples"])
    check(f"{label_prefix}: too_many_points suppresses range fields",
          xx["t_range"] is None and xx["bl_range"] is None and xx["bl_ids"] is None,
          xx)


def run_multi_partition_case(cls, is_v4, label_prefix):
    """Two partitions: partition 1 gives XX enough matches to trip
    too_many_points; partition 2 uses a different baseline (5) whose
    mapped y-value is out of range for XX but in range for YY -- so
    this proves two things at once: XX's over-budget status from
    partition 1 isn't disturbed by partition 2 (it's skipped via the
    per-key `too_many` filter, not reprocessed), and YY -- never over
    budget -- genuinely incorporates partition 2's data (checked via
    its t_range reflecting partition 2's distinct time values), proving
    the skip is scoped to the specific over-budget key, not the whole
    partition."""
    ds1 = make_partition(TIMES, BLS, FREQS)                 # XX has 12 matches here
    ds2 = make_partition([200.0, 201.0], [5], [50.0e9])      # bl 5: in range for YY only

    if is_v4:
        # crude but adequate for this controlled test: swap the active
        # partition just before each is consumed, since the v4 stub
        # reads from a module-level slot instead of a ds argument.
        seq = iter([ds1, ds2])
        def _iter(selection):
            for d in seq:
                _current_ds_v4[0] = d
                yield d
        inst = make_backend(cls, [], is_v4=is_v4)
        inst._iter_visibility_partitions = _iter
    else:
        inst = make_backend(cls, [ds1, ds2], is_v4=is_v4)

    result = inst.probe_scatter_region(
        x_axis=Axis.TIME,
        yaxes=[(Axis.AMPLITUDE, "XX"), (Axis.AMPLITUDE, "YY")],
        selection=SelectionSpec(),
        x_range=(0.0, 300.0),     # wide enough to include both partitions
        y_range=(9.0, 13.0),      # in range for: XX@bl{0,3} (ds1), YY@bl5 (ds2)
        max_samples=5,            # ds1 alone (12) already exceeds this for XX
    )
    xx = result[(Axis.AMPLITUDE, "XX")]
    yy = result[(Axis.AMPLITUDE, "YY")]

    check(f"{label_prefix}: XX trips too_many_points from partition 1 alone",
          xx["status"] == "too_many_points", xx)
    check(f"{label_prefix}: YY processed normally from partition 2 "
          f"despite XX being over budget (per-key skip, not per-partition)",
          yy["status"] == "ok", yy)
    check(f"{label_prefix}: YY's t_range reflects partition 2's own times "
          f"(200.0, 201.0), proving partition 2's data was genuinely used",
          yy.get("t_range") == (200.0, 201.0), yy.get("t_range"))


for cls, is_v4, name in ((msv2.MSv2Backend, False, "MSv2Backend"),
                          (msv4.MSv4Backend, True, "MSv4Backend")):
    print(f"\n=== {name} ===")
    run_basic_case(cls, is_v4, name)
    run_too_many_points_case(cls, is_v4, name)
    run_multi_partition_case(cls, is_v4, name)

print("\n" + "=" * 60)
if FAILURES:
    print(f"{len(FAILURES)} FAILURE(S):")
    for f in FAILURES:
        print(" -", f)
    sys.exit(1)
else:
    print("All checks passed.")
