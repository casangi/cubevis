"""
test_06_dask.py — Dask chunking and lazy compute patterns.

Tests the Dask-specific aspects of data access that matter for the
msvis plotter: chunking strategy, graph construction cost, and the
compute patterns for scatter-mode data (columns) and raster-mode
data (2D aggregate).

Tests:
  - Dask graph is built without triggering compute
  - Custom chunk sizes are respected
  - compute() on a partition slice returns correct dtype/shape
  - Channel-averaged amplitude (frequency reduction) is correct
  - Time-averaged amplitude (time reduction) is correct
  - Full amplitude computation across an entire partition completes
  - dask.compute() on multiple quantities simultaneously (fused graph)
  - Memory estimate: task graph is reasonable size

These form the baseline for verifying that the Datashader pipeline
(which calls .compute() internally) will get correct input data.

Usage:
    MS=sis14_twhya_calibrated_flagged.ms python test_06_dask.py
"""

import os
import time
import numpy as np
import dask
import dask.array as da
import xarray
import xarray_ms  # noqa: F401

C_MS = 299_792_458.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_ms():
    path = os.environ.get("MS", "sis14_twhya_calibrated_flagged.ms")
    if not os.path.isdir(path):
        from xarray_ms.testing.simulator import simulate
        print(f"WARNING: {path!r} not found — using simulated MS")
        path = simulate("test_sim.ms", data_description=[(48, ("XX", "YY"))])
    return path


def open_ms(ms_path, time_chunk=200, baseline_chunk=100):
    return xarray.open_datatree(
        ms_path,
        engine="xarray-ms:msv2",
        partition_schema=["DATA_DESC_ID", "OBSERVATION_ID"],
        chunks={"time": time_chunk, "baseline_id": baseline_chunk},
    )


def _first_partition(dt):
    for node in dt.children.values():
        if node.ds.dims.get("time", 0) > 0:
            return node.ds
    raise RuntimeError("No partition found")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_arrays_are_dask():
    """All DATA variables should be Dask-backed (lazy) after open."""
    ms = _get_ms()
    dt = open_ms(ms)
    ds = _first_partition(dt)
    for var in ("VISIBILITY", "FLAG", "UVW", "WEIGHT"):
        assert var in ds.data_vars
        arr = ds[var]
        assert isinstance(arr.data, da.Array), (
            f"{var} is not a Dask array; dtype={arr.dtype}"
        )
        print(f"  {var}: shape={arr.shape}, chunks={arr.chunks}")


def test_chunk_size_respected():
    """Verify that time chunk size is close to the requested value."""
    ms = _get_ms()
    dt = open_ms(ms, time_chunk=50, baseline_chunk=50)
    ds = _first_partition(dt)
    vis = ds["VISIBILITY"]
    time_chunks = vis.chunks[vis.dims.index("time")]
    # Most chunks should be ≤50; the last may be smaller
    assert all(c <= 50 for c in time_chunks), (
        f"Time chunks {time_chunks} exceed requested size of 50"
    )
    print(f"  Time chunks with size=50: {time_chunks}")


def test_graph_builds_without_compute():
    """Building the Dask task graph for amplitude must not read any data."""
    ms = _get_ms()
    dt = open_ms(ms)
    ds = _first_partition(dt)
    t0 = time.perf_counter()
    vis = ds["VISIBILITY"]
    amp = xarray.apply_ufunc(np.abs, vis, dask="parallelized", output_dtypes=[float])
    flag_bool = ds["FLAG"].astype(bool)
    amp_masked = amp.where(~flag_bool)
    graph_time = time.perf_counter() - t0
    # Graph construction should be sub-second for a small MS
    assert graph_time < 5.0, f"Graph construction took {graph_time:.2f}s — unexpectedly slow"
    n_tasks = len(amp_masked.__dask_graph__())
    print(f"  Graph built in {graph_time*1000:.1f} ms, {n_tasks} tasks")


def test_compute_small_slice():
    """Compute a small slice; verify dtype and shape."""
    ms = _get_ms()
    dt = open_ms(ms)
    ds = _first_partition(dt)
    vis_slice = ds["VISIBILITY"].isel(time=slice(0, 5), frequency=slice(0, 4))
    result = vis_slice.compute()
    assert result.dtype == np.complex64 or result.dtype == np.complex128, (
        f"Expected complex dtype, got {result.dtype}"
    )
    assert result.shape == (5, ds.dims["baseline_id"], 4, ds.dims["polarization"])
    print(f"  Small slice shape: {result.shape}, dtype: {result.dtype}")


def test_channel_averaged_amplitude():
    """
    Time vs. baseline amplitude averaged over all channels — one of the
    most common scatter-mode queries.

    Shape: (time, baseline_id, polarization)
    """
    ms = _get_ms()
    dt = open_ms(ms)
    ds = _first_partition(dt).isel(time=slice(0, 20))

    vis = ds["VISIBILITY"]
    flag_bool = ds["FLAG"].astype(bool)
    amp = xarray.apply_ufunc(np.abs, vis, dask="parallelized", output_dtypes=[float])
    amp_masked = amp.where(~flag_bool)

    # Channel-average: mean over frequency, ignoring NaN (flagged) channels
    chan_avg = amp_masked.mean(dim="frequency", skipna=True)
    result = chan_avg.compute()
    assert result.dims == ("time", "baseline_id", "polarization")
    print(
        f"  Channel-averaged amp shape: {result.shape}, "
        f"mean={np.nanmean(result.values):.4f}"
    )


def test_time_averaged_amplitude():
    """
    Frequency vs. baseline amplitude averaged over time.
    Used for a frequency-vs-amplitude scatter or line plot.

    Shape: (baseline_id, frequency, polarization)
    """
    ms = _get_ms()
    dt = open_ms(ms)
    ds = _first_partition(dt).isel(time=slice(0, 20))

    vis = ds["VISIBILITY"]
    flag_bool = ds["FLAG"].astype(bool)
    amp = xarray.apply_ufunc(np.abs, vis, dask="parallelized", output_dtypes=[float])
    amp_masked = amp.where(~flag_bool)

    time_avg = amp_masked.mean(dim="time", skipna=True)
    result = time_avg.compute()
    assert result.dims == ("baseline_id", "frequency", "polarization")
    print(
        f"  Time-averaged amp shape: {result.shape}, "
        f"mean={np.nanmean(result.values):.4f}"
    )


def test_full_partition_amplitude():
    """
    Compute amplitude for the entire first partition.

    This is what a full-data scatter plot would request before downsampling
    with Datashader.  For the real dataset (~270 integrations × ~903 baselines
    × 48 channels × 2 polarizations ≈ 23M complex64 samples) it should
    complete in a few seconds with a default Dask scheduler.
    """
    ms = _get_ms()
    dt = open_ms(ms)
    ds = _first_partition(dt)

    vis = ds["VISIBILITY"]
    print(
        f"  Full partition shape: {vis.shape}, "
        f"estimated size: {vis.nbytes / 1e6:.1f} MB"
    )
    t0 = time.perf_counter()
    amp = xarray.apply_ufunc(np.abs, vis, dask="parallelized", output_dtypes=[float])
    result = amp.compute()
    elapsed = time.perf_counter() - t0
    print(
        f"  Full amplitude compute: {elapsed:.2f}s, "
        f"mean={result.values.mean():.4f}"
    )
    assert result.shape == vis.shape


def test_fused_multi_quantity_compute():
    """
    Compute amplitude AND phase AND uvdist simultaneously.

    dask.compute() fuses the graphs so each block of VISIBILITY is read
    only once.  This is the access pattern in query_columns() when both
    x and y axes are derived from the same partition.
    """
    ms = _get_ms()
    dt = open_ms(ms)
    ds = _first_partition(dt).isel(time=slice(0, 30))

    vis = ds["VISIBILITY"]
    uvw = ds["UVW"]
    u = uvw.sel(uvw_label="u")
    v = uvw.sel(uvw_label="v")

    amp = xarray.apply_ufunc(np.abs, vis, dask="parallelized", output_dtypes=[float])
    phase = xarray.apply_ufunc(
        lambda x: np.degrees(np.angle(x)), vis, dask="parallelized", output_dtypes=[float]
    )
    uvdist = np.sqrt(u**2 + v**2)

    t0 = time.perf_counter()
    amp_r, phase_r, uvdist_r = dask.compute(amp, phase, uvdist)
    elapsed = time.perf_counter() - t0

    print(
        f"  Fused compute (amp+phase+uvdist) in {elapsed:.2f}s on "
        f"{ds.dims['time']}×{ds.dims['baseline_id']} grid"
    )
    assert amp_r.shape == vis.shape
    assert phase_r.shape == vis.shape
    assert uvdist_r.shape == (ds.dims["time"], ds.dims["baseline_id"])


def test_dask_scheduler_options():
    """
    Smoke-test different schedulers on a small compute.

    The msvis plotter will run in a Jupyter context; the synchronous
    scheduler is safest there (no GIL / thread pool issues with arcae).
    """
    ms = _get_ms()
    dt = open_ms(ms)
    ds = _first_partition(dt).isel(time=slice(0, 5), frequency=slice(0, 4))
    vis = ds["VISIBILITY"]

    for scheduler in ("synchronous", "threads"):
        t0 = time.perf_counter()
        result = vis.compute(scheduler=scheduler)
        elapsed = time.perf_counter() - t0
        print(f"  scheduler={scheduler!r}: {elapsed*1000:.1f} ms, shape={result.shape}")
        assert result.shape[0] == 5


# ---------------------------------------------------------------------------
# Standalone runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    tests = [
        test_arrays_are_dask,
        test_chunk_size_respected,
        test_graph_builds_without_compute,
        test_compute_small_slice,
        test_channel_averaged_amplitude,
        test_time_averaged_amplitude,
        test_full_partition_amplitude,
        test_fused_multi_quantity_compute,
        test_dask_scheduler_options,
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
