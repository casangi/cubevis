"""
test_05_axes.py — Derive plottable quantities from raw xarray-ms data.

These are the _compute_axis_values() cases from reader.py — the functions
that MSv2Backend and MSv4Backend call to convert raw VISIBILITY/UVW arrays
into the quantities shown on plot axes.

All computations are done with xarray/numpy; Dask arrays stay lazy until
an explicit .compute() call.  This script adds .compute() only on small
slices to keep the test fast.

Axes tested:
  - Amplitude   : abs(VISIBILITY)
  - Phase       : angle(VISIBILITY) in degrees
  - Real        : VISIBILITY.real
  - Imaginary   : VISIBILITY.imag
  - UV-distance : sqrt(U^2 + V^2)  in metres and in kilolambda
  - UV-wave     : UV-distance / (c / frequency)  per channel
  - U, V, W     : raw UVW components
  - Weight      : WEIGHT passthrough
  - Time (MJD)  : time coordinate passthrough

Usage:
    MS=sis14_twhya_calibrated_flagged.ms python test_05_axes.py
"""

import os
import numpy as np
import xarray
import xarray_ms  # noqa: F401

C_MS = 299_792_458.0   # speed of light, m/s


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


def _small(ds):
    """Return a tiny slice for fast .compute() in tests."""
    return ds.isel(time=slice(0, 10), baseline_id=slice(0, 20), frequency=slice(0, 8))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_amplitude():
    ms = _get_ms()
    dt = open_ms(ms)
    ds = _small(_first_partition(dt))
    vis = ds["VISIBILITY"]
    amp = xarray.apply_ufunc(np.abs, vis, dask="parallelized", output_dtypes=[float])
    result = amp.compute()
    assert result.dims == vis.dims
    assert (result.values >= 0).all(), "Amplitude should be non-negative"
    print(f"  Amplitude: min={result.values.min():.4f}, max={result.values.max():.4f}")


def test_phase_degrees():
    ms = _get_ms()
    dt = open_ms(ms)
    ds = _small(_first_partition(dt))
    vis = ds["VISIBILITY"]
    phase_rad = xarray.apply_ufunc(np.angle, vis, dask="parallelized", output_dtypes=[float])
    phase_deg = np.degrees(phase_rad)
    result = phase_deg.compute()
    assert result.dims == vis.dims
    in_range = (result.values >= -180) & (result.values <= 180)
    assert in_range.all(), "Phase values outside [-180, 180]"
    print(f"  Phase (deg): min={result.values.min():.1f}, max={result.values.max():.1f}")


def test_real_imaginary():
    ms = _get_ms()
    dt = open_ms(ms)
    ds = _small(_first_partition(dt))
    vis = ds["VISIBILITY"]
    real_part = vis.real
    imag_part = vis.imag
    r = real_part.compute()
    i = imag_part.compute()
    assert r.dims == vis.dims
    assert i.dims == vis.dims
    # real^2 + imag^2 == |vis|^2
    amp_sq_from_ri = (r**2 + i**2).values
    amp_sq_direct = np.abs(vis.compute().values)**2
    np.testing.assert_allclose(amp_sq_from_ri, amp_sq_direct, rtol=1e-5)
    print(f"  Real: range=[{r.values.min():.4f}, {r.values.max():.4f}]")
    print(f"  Imag: range=[{i.values.min():.4f}, {i.values.max():.4f}]")


def test_uvdist_metres():
    """UV-distance in metres — the most common scatter plot x-axis."""
    ms = _get_ms()
    dt = open_ms(ms)
    ds = _small(_first_partition(dt))

    uvw = ds["UVW"]  # (time, baseline_id, uvw_label)
    # uvw_label coords are 'u', 'v', 'w'
    u = uvw.sel(uvw_label="u")
    v = uvw.sel(uvw_label="v")
    uvdist = np.sqrt(u**2 + v**2)
    result = uvdist.compute()
    assert result.dims == ("time", "baseline_id")
    assert (result.values >= 0).all(), "UV-distance should be non-negative"
    print(
        f"  UV-distance (m): min={result.values.min():.1f}, "
        f"max={result.values.max():.1f}"
    )


def test_uvdist_kilolambda():
    """
    UV-distance in kilolambda — channel-dependent quantity.

    uvdist_kλ[t, bl, f] = uvdist_m[t, bl] * freq[f] / (c * 1000)

    This produces a 3D array (time, baseline_id, frequency) broadcast
    from uvdist (time, baseline_id) and frequency (frequency).
    The plotter uses this as the x-axis for amp/phase vs. uvdist when
    xaxis=Axis.UVWAVE.
    """
    ms = _get_ms()
    dt = open_ms(ms)
    ds = _small(_first_partition(dt))

    uvw = ds["UVW"]
    u = uvw.sel(uvw_label="u")
    v = uvw.sel(uvw_label="v")
    uvdist_m = np.sqrt(u**2 + v**2)   # (time, baseline_id)
    freq_hz  = ds.coords["frequency"]  # (frequency,)

    # Broadcast: xarray handles alignment automatically
    uvdist_klambda = uvdist_m * freq_hz / (C_MS * 1000.0)
    result = uvdist_klambda.compute()

    assert set(result.dims) == {"time", "baseline_id", "frequency"}
    assert (result.values >= 0).all()
    print(
        f"  UV-distance (kλ): min={result.values.min():.2f}, "
        f"max={result.values.max():.2f}"
    )


def test_uvw_components():
    """U, V, W individually — used for UV-coverage plots."""
    ms = _get_ms()
    dt = open_ms(ms)
    ds = _small(_first_partition(dt))

    uvw = ds["UVW"]
    for label in ("u", "v", "w"):
        comp = uvw.sel(uvw_label=label).compute()
        assert comp.dims == ("time", "baseline_id")
        print(f"  {label.upper()}: range=[{comp.values.min():.1f}, {comp.values.max():.1f}] m")


def test_weight():
    """WEIGHT passthrough — used for weighted averaging in raster mode."""
    ms = _get_ms()
    dt = open_ms(ms)
    ds = _small(_first_partition(dt))

    w = ds["WEIGHT"].compute()
    assert "WEIGHT" in ds.data_vars
    assert (w.values >= 0).all(), "Negative WEIGHT values"
    print(f"  WEIGHT: shape={w.shape}, min={w.values.min():.4f}, max={w.values.max():.4f}")


def test_time_axis():
    """Time coordinate: verify it is in seconds (Modified Julian Date seconds)."""
    ms = _get_ms()
    dt = open_ms(ms)
    ds = _first_partition(dt)

    times = ds.coords["time"].values  # MJD in seconds
    # Typical ALMA observation: MJD > 4e9 s (year ~2000+)
    # Just check it is a sensible large positive number
    assert times.min() > 1e9, f"Time values look wrong: {times[:3]}"
    dt_sec = np.diff(times)
    print(
        f"  Time: n_integrations={len(times)}, "
        f"MJD_start={times[0]:.2f} s, "
        f"integration_time={dt_sec[0]:.1f} s"
    )


def test_flagged_amplitude():
    """
    Masked amplitude — the quantity actually passed to Datashader.

    Combine flag masking with amplitude computation in one chain.
    Unflagged amplitude values should all be >= 0; flagged positions are NaN.
    """
    ms = _get_ms()
    dt = open_ms(ms)
    ds = _small(_first_partition(dt))

    vis = ds["VISIBILITY"]
    flag_bool = ds["FLAG"].astype(bool)
    amp = xarray.apply_ufunc(np.abs, vis, dask="parallelized", output_dtypes=[float])
    amp_masked = amp.where(~flag_bool)
    result = amp_masked.compute()

    n_nan = int(np.isnan(result.values).sum())
    n_flag = int(flag_bool.compute().sum())
    print(f"  Flagged amplitude: {n_flag} flags, {n_nan} NaNs in masked amp")
    assert n_nan == n_flag, "NaN count should equal flag count"
    # Non-NaN values should be non-negative
    good = result.values[~np.isnan(result.values)]
    assert (good >= 0).all(), "Non-flagged amplitudes include negative values"


# ---------------------------------------------------------------------------
# Standalone runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    tests = [
        test_amplitude,
        test_phase_degrees,
        test_real_imaginary,
        test_uvdist_metres,
        test_uvdist_kilolambda,
        test_uvw_components,
        test_weight,
        test_time_axis,
        test_flagged_amplitude,
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
