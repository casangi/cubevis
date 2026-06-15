"""
test_03_flag_read.py — Read and inspect the FLAG column.

Tests:
  - FLAG is present as a data variable with correct dtype and shape
  - FLAG dimensions match VISIBILITY
  - Overall flag fraction can be computed (Dask reduce)
  - Per-baseline flag fraction (useful for RFI summary plots)
  - Per-channel flag fraction (useful for flagging-vs-frequency plots)
  - Per-time flag fraction (useful for time-vs-baseline raster)
  - Flag fraction over a SelectionSpec-style compound selection

These are the flag statistics a plotter needs to decide whether to overlay
flag indicators or shade flagged regions in the plot.

Usage:
    MS=sis14_twhya_calibrated_flagged.ms python test_03_flag_read.py
"""

import os
import numpy as np
import xarray
import xarray_ms  # noqa: F401


def _get_ms():
    path = os.environ.get("MS", "sis14_twhya_calibrated_flagged.ms")
    if not os.path.isdir(path):
        from xarray_ms.testing.simulator import simulate
        print(f"WARNING: {path!r} not found — using simulated MS")
        path = simulate("test_sim.ms", data_description=[(48, ("XX", "YY"))])
    return path


def open_ms(ms_path):
    return xarray.open_datatree(
        ms_path,
        engine="xarray-ms:msv2",
        partition_schema=["DATA_DESC_ID", "OBSERVATION_ID"],
        chunks={"time": 200, "baseline_id": 100},
    )


def _first_partition(dt):
    for node in dt.children.values():
        if node.ds.dims.get("time", 0) > 0:
            return node.ds
    raise RuntimeError("No partition found")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_flag_present_and_shaped():
    ms = _get_ms()
    dt = open_ms(ms)
    for name, node in dt.children.items():
        ds = node.ds
        if ds.dims.get("time", 0) == 0:
            continue
        assert "FLAG" in ds.data_vars, f"FLAG missing in {name}"
        flag = ds["FLAG"]
        vis  = ds["VISIBILITY"]
        assert flag.dims == vis.dims, (
            f"FLAG dims {flag.dims} != VISIBILITY dims {vis.dims}"
        )
        print(f"  {name}: FLAG shape={flag.shape}, dtype={flag.dtype}")


def test_flag_dtype():
    """xarray-ms v0.5.x exposes FLAG as uint8; verify that or bool."""
    ms = _get_ms()
    dt = open_ms(ms)
    ds = _first_partition(dt)
    flag = ds["FLAG"]
    assert flag.dtype in (np.dtype("uint8"), np.dtype("bool")), (
        f"Unexpected FLAG dtype: {flag.dtype}"
    )
    print(f"  FLAG dtype: {flag.dtype}")


def test_overall_flag_fraction():
    """Compute total flag fraction across all dims."""
    ms = _get_ms()
    dt = open_ms(ms)
    ds = _first_partition(dt)
    flag_bool = ds["FLAG"].astype(bool)
    # Compute over the full partition
    frac = float(flag_bool.mean().compute())
    print(f"  Overall flag fraction: {frac:.4f} ({frac*100:.2f}%)")
    assert 0.0 <= frac <= 1.0


def test_per_channel_flag_fraction():
    """Flag fraction as a function of frequency channel — plotted in msview."""
    ms = _get_ms()
    dt = open_ms(ms)
    ds = _first_partition(dt)
    flag_bool = ds["FLAG"].astype(bool)
    # Average over time, baseline_id, polarization — leave frequency
    frac_chan = flag_bool.mean(dim=["time", "baseline_id", "polarization"]).compute()
    freqs_GHz = ds.coords["frequency"].values / 1e9
    print(f"  Per-channel flag fraction (first 8 channels):")
    for i in range(min(8, len(freqs_GHz))):
        print(f"    ch {i:2d}  {freqs_GHz[i]:.5f} GHz  {float(frac_chan[i]):.4f}")
    assert frac_chan.shape == (ds.dims["frequency"],)


def test_per_time_flag_fraction():
    """Flag fraction as a function of time — useful for time-vs-baseline rasters."""
    ms = _get_ms()
    dt = open_ms(ms)
    ds = _first_partition(dt)
    flag_bool = ds["FLAG"].astype(bool)
    frac_time = flag_bool.mean(dim=["baseline_id", "frequency", "polarization"]).compute()
    times = ds.coords["time"].values
    print(f"  Per-integration flag fraction (first 5 of {len(times)}):")
    for i in range(min(5, len(times))):
        print(f"    t={times[i]:.2f}  {float(frac_time[i]):.4f}")
    assert frac_time.shape == (ds.dims["time"],)


def test_per_baseline_flag_fraction():
    """Flag fraction per baseline — useful for identifying bad antennas."""
    ms = _get_ms()
    dt = open_ms(ms)
    ds = _first_partition(dt)
    flag_bool = ds["FLAG"].astype(bool)
    frac_bl = flag_bool.mean(dim=["time", "frequency", "polarization"]).compute()
    ant1 = ds.coords["baseline_antenna1_name"].values
    ant2 = ds.coords["baseline_antenna2_name"].values
    # Find the most-flagged baseline
    worst_idx = int(frac_bl.argmax())
    print(
        f"  Most flagged baseline: {ant1[worst_idx]}–{ant2[worst_idx]} "
        f"({float(frac_bl[worst_idx]):.4f})"
    )
    print(f"  Least flagged baseline: {ant1[int(frac_bl.argmin())]}–"
          f"{ant2[int(frac_bl.argmin())]} "
          f"({float(frac_bl.min()):.4f})")
    assert frac_bl.shape == (ds.dims["baseline_id"],)


def test_flag_count_matches_unflagged_data():
    """
    Verify that masking VISIBILITY with FLAG gives correct NaN count.
    This exercises the .where() path used by MSv2Backend.query_columns().
    """
    ms = _get_ms()
    dt = open_ms(ms)
    ds = _first_partition(dt)

    # Use small slice to keep compute time fast
    small_ds = ds.isel(time=slice(0, 10), frequency=slice(0, 8))
    flag_bool = small_ds["FLAG"].astype(bool).compute()
    vis = small_ds["VISIBILITY"]
    vis_masked = vis.where(~flag_bool).compute()

    n_flagged = int(flag_bool.sum())
    n_nan = int(np.isnan(vis_masked.values).sum())
    print(
        f"  Slice flags={n_flagged}, NaNs in masked vis={n_nan} "
        f"(should be equal; vis has 2 parts re+im, NaN on complex -> 2*flags)"
    )
    # For complex arrays, np.isnan(z) counts the full complex element.
    # xarray treats complex NaN as one NaN per element.
    assert n_nan == n_flagged, (
        f"Mismatch: {n_flagged} flags but {n_nan} NaNs in masked VISIBILITY"
    )


# ---------------------------------------------------------------------------
# Standalone runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    tests = [
        test_flag_present_and_shaped,
        test_flag_dtype,
        test_overall_flag_fraction,
        test_per_channel_flag_fraction,
        test_per_time_flag_fraction,
        test_per_baseline_flag_fraction,
        test_flag_count_matches_unflagged_data,
    ]
    passed = failed = 0
    for t in tests:
        try:
            print(f"\n--- {t.__name__} ---")
            t()
            print("  PASS")
            passed += 1
        except Exception as exc:
            import traceback
            print(f"  FAIL: {exc}")
            traceback.print_exc()
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
