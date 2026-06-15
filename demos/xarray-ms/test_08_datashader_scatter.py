"""
test_08_datashader_scatter.py — Datashader scatter plot pipeline for msvis.

Tests the full pipeline from xarray-ms DataTree → flat columnar data →
Datashader Canvas → uint8 RGBA image, for the scatter plot modes that
VisibilityPlotter will support.

The key steps under test:
  1. Stack 4D (time, baseline_id, frequency, polarization) arrays into a
     flat "sample" dimension — must stay lazy through Dask
  2. Feed stacked arrays to datashader.Canvas.points() as x/y columns
  3. Verify the output is a uint8 RGBA DataArray of the expected shape
  4. Verify NaN (flagged/padded) points are transparent (alpha=0)
  5. Verify unflagged signal points are opaque (alpha=255)
  6. Test all primary scatter axis combinations:
       - amplitude vs time
       - amplitude vs uvdist (metres)
       - amplitude vs uvwave (kilolambda, frequency-dependent)
       - phase vs time
       - phase vs uvdist
       - real vs time
       - imaginary vs time
       - U vs V  (UV-coverage plot — both axes from UVW)
  7. Test per-polarization scatter (separate canvas per polarization)
  8. Test that canvas resolution parameters produce the right output shape
  9. Test timing: full-partition scatter should complete in < 30s

Pipeline pattern (same for all scatter modes):

    # 1. Compute derived quantities (lazy)
    amp  = abs(VISIBILITY).where(~FLAG)           # (time, bl, freq, pol)
    time = ds.coords["time"]                       # (time,) broadcast → 4D

    # 2. Broadcast time/uvdist to match visibility shape, then stack
    time_bc  = time.broadcast_like(amp)
    stacked  = xr.Dataset({"y": amp, "x": time_bc}).stack(
                   sample=("time", "baseline_id", "frequency", "polarization")
               )

    # 3. Drop NaN rows (flagged/padded), trigger Dask compute
    df = stacked.to_dataframe().dropna()

    # 4. Datashader canvas → aggregation → transfer function → uint8 RGBA
    cvs    = ds_canvas.Canvas(plot_width=800, plot_height=600)
    agg    = cvs.points(df, "x", "y", agg=ds_agg.mean("y"))
    img    = ds_tf.shade(agg)           # xr.DataArray, dtype uint8, shape (H, W, 4)

Notes:
  - datashader.transfer_functions.shade() returns an xr.DataArray with
    dtype uint8 and dims (y_range, x_range) with a 4-element RGBA axis,
    OR it returns an Image object wrapping that array depending on version.
    We handle both.
  - The "sample" stacking dimension name is arbitrary; "sample" is used
    throughout for consistency with the msvis design doc.
  - uvwave is frequency-dependent so it has shape (time, baseline_id, frequency)
    — one fewer dimension than amp.  We select a single polarization before
    stacking so dimensions align.

Usage:
    MS=sis14_twhya_calibrated_flagged.ms python test_08_datashader_scatter.py
    MS=sis14_twhya_calibrated_flagged.ms pytest test_08_datashader_scatter.py -v
"""

import os
import time as time_mod
import warnings
import numpy as np
from numpy.exceptions import ComplexWarning
import xarray as xr
import xarray_ms  # noqa: F401

try:
    import datashader as ds
    import datashader.transfer_functions as ds_tf
    import datashader.reductions as ds_agg
    HAS_DATASHADER = True
except ImportError:
    HAS_DATASHADER = False

C_MS = 299_792_458.0
PLOT_W = 400
PLOT_H = 300


# ---------------------------------------------------------------------------
# Skip all tests if datashader is not installed
# ---------------------------------------------------------------------------

def _require_datashader():
    if not HAS_DATASHADER:
        raise RuntimeError(
            "datashader not installed — install with: pip install datashader"
        )


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
    return xr.open_datatree(
        ms_path,
        engine="xarray-ms:msv2",
        partition_schema=["DATA_DESC_ID", "OBSERVATION_ID"],
        chunks={"time": 100, "baseline_id": 100},
    )


def _first_partition(dt):
    for node in dt.children.values():
        if node.ds.sizes.get("time", 0) > 0:
            return node.ds
    raise RuntimeError("No partition found")


def _small(ds, n_time=20, n_bl=50, n_chan=8):
    """Small slice for fast tests — 8 channels keeps sample counts manageable."""
    return ds.isel(
        time=slice(0, n_time),
        baseline_id=slice(0, n_bl),
        frequency=slice(0, n_chan),
    )


def _amp(vis):
    return xr.apply_ufunc(
        np.abs, vis, dask="parallelized", output_dtypes=[float]
    )


def _phase_deg(vis):
    return xr.apply_ufunc(
        lambda x: np.degrees(np.angle(x)),
        vis,
        dask="parallelized",
        output_dtypes=[float],
    )


def _flag_mask(ds):
    return ds["FLAG"].astype(bool)


def _uvdist_m(ds):
    """UV-distance in metres, shape (time, baseline_id)."""
    uvw = ds["UVW"]
    u = uvw.sel(uvw_label="u")
    v = uvw.sel(uvw_label="v")
    return np.sqrt(u**2 + v**2)


def _uvwave_klambda(ds):
    """UV-distance in kilolambda, shape (time, baseline_id, frequency)."""
    uvdist = _uvdist_m(ds)
    freq   = ds.coords["frequency"]
    return uvdist * freq / (C_MS * 1000.0)


def _stack_to_df(x_da, y_da, x_name="x", y_name="y"):
    """
    Stack two DataArrays into a flat pandas DataFrame with columns x, y.

    Both arrays must share the same dimensions (or be broadcastable).
    NaN rows (flagged/padded) are dropped before returning.

    This is the core msvis scatter preparation step.  The stack is done
    lazily via xarray and Dask; .to_dataframe() triggers the compute.
    """
    # Align dims: broadcast x to match y's shape if needed
    x_bc = x_da.broadcast_like(y_da)

    dataset = xr.Dataset({x_name: x_bc, y_name: y_da})

    # Stack all dims into a single "sample" dimension
    sample_dims = list(y_da.dims)
    stacked = dataset.stack(sample=sample_dims)

    # Convert to DataFrame and drop NaN (flagged/padded/all-flagged rows)
    df = stacked.to_dataframe()[[x_name, y_name]].dropna()
    return df


def _make_canvas():
    return ds.Canvas(plot_width=PLOT_W, plot_height=PLOT_H)


def _shade_to_uint8(agg):
    """
    Apply Datashader transfer function and return uint8 RGBA numpy array.

    datashader.transfer_functions.shade() returns an Image object in recent
    versions.  We extract the underlying uint8 ndarray regardless of version.
    Shape: (H, W, 4) — RGBA channels last.
    """
    img = ds_tf.shade(agg)
    # Extract raw array: Image wraps an xr.DataArray with dtype uint32
    # (packed RGBA).  We convert to uint8 RGBA explicitly.
    if hasattr(img, "data"):
        arr32 = np.asarray(img.data)          # (H, W) uint32 packed RGBA
    elif hasattr(img, "values"):
        arr32 = img.values
    else:
        arr32 = np.asarray(img)

    # Unpack uint32 → uint8 RGBA (little-endian: R, G, B, A)
    rgba = arr32.view(np.uint8).reshape(arr32.shape + (4,))
    return rgba


def _assert_uint8_image(rgba, label=""):
    """Assert the image is uint8 RGBA with expected canvas dimensions."""
    assert rgba.dtype == np.uint8, (
        f"{label}: expected uint8, got {rgba.dtype}"
    )
    assert rgba.shape == (PLOT_H, PLOT_W, 4), (
        f"{label}: expected shape ({PLOT_H}, {PLOT_W}, 4), got {rgba.shape}"
    )


# ---------------------------------------------------------------------------
# Pipeline verification helpers
# ---------------------------------------------------------------------------

def _run_scatter(x_da, y_da, label=""):
    """
    Full pipeline: broadcast → stack → DataFrame → Canvas → uint8 RGBA.
    Returns (df, rgba) for further inspection.
    """
    t0 = time_mod.perf_counter()
    df = _stack_to_df(x_da, y_da)
    t_stack = time_mod.perf_counter() - t0

    assert len(df) > 0, f"{label}: DataFrame is empty after dropna()"
    assert not df["x"].isna().any(), f"{label}: NaN x values survived dropna()"
    assert not df["y"].isna().any(), f"{label}: NaN y values survived dropna()"

    t0 = time_mod.perf_counter()
    cvs  = _make_canvas()
    agg  = cvs.points(df, "x", "y", agg=ds_agg.mean("y"))
    rgba = _shade_to_uint8(agg)
    t_render = time_mod.perf_counter() - t0

    _assert_uint8_image(rgba, label)

    n_samples = len(df)
    n_opaque  = int((rgba[..., 3] > 0).sum())
    print(
        f"  {label}: {n_samples} samples, "
        f"stack={t_stack*1000:.0f}ms, render={t_render*1000:.0f}ms, "
        f"opaque pixels={n_opaque}/{PLOT_W*PLOT_H}"
    )
    return df, rgba


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_datashader_installed():
    """Confirm datashader is importable before running other tests."""
    _require_datashader()
    print(f"  datashader version: {ds.__version__}")


def test_stack_stays_lazy():
    """
    Stacking 4D xarray arrays into a 'sample' dimension must remain a
    lazy Dask operation until .to_dataframe() is called.

    If this materialises eagerly the full-partition scatter will OOM.
    """
    _require_datashader()
    import dask.array as da

    ms = _get_ms()
    dt = open_ms(ms)
    small = _small(_first_partition(dt))

    vis  = small["VISIBILITY"]
    flag = _flag_mask(small)
    amp  = _amp(vis).where(~flag).sel(polarization=small.coords["polarization"].values[0])

    time_bc = small.coords["time"].broadcast_like(amp)

    dataset = xr.Dataset({"x": time_bc, "y": amp})
    stacked = dataset.stack(sample=list(amp.dims))

    # The stacked variables should still be Dask-backed
    assert isinstance(stacked["y"].data, da.Array), (
        "Stacked amplitude is not a Dask array — stacking triggered eager compute"
    )
    print(f"  Stacked 'y' remains lazy: shape={stacked['y'].shape}, "
          f"chunks={stacked['y'].chunks}")


def test_nan_transparency():
    """
    Flagged/padded NaN samples must produce transparent pixels (alpha=0).
    Unflagged samples must produce opaque pixels (alpha=255).

    We verify this by checking that:
    - The image has at least some transparent pixels (from flags/padding)
    - The image has at least some opaque pixels (from real data)
    - alpha values are only 0 or 255 (no partial transparency from shade())
    """
    _require_datashader()
    ms = _get_ms()
    dt = open_ms(ms)
    small = _small(_first_partition(dt))

    vis  = small["VISIBILITY"]
    flag = _flag_mask(small)
    amp  = _amp(vis).where(~flag)
    pol  = small.coords["polarization"].values[0]
    amp_pol = amp.sel(polarization=pol)

    time_bc = small.coords["time"].broadcast_like(amp_pol)
    df, rgba = _run_scatter(time_bc, amp_pol, label="amp_vs_time_nan_check")

    alpha = rgba[..., 3]
    n_transparent = int((alpha == 0).sum())
    n_opaque      = int((alpha == 255).sum())
    n_partial     = int(((alpha > 0) & (alpha < 255)).sum())

    print(f"  Transparent pixels: {n_transparent}, "
          f"opaque: {n_opaque}, partial: {n_partial}")

    assert n_opaque > 0, "No opaque pixels — all data rendered as transparent"
    assert n_transparent > 0, "No transparent pixels — flags/padding not producing NaN"
    # datashader.shade() with default colormap produces only fully transparent
    # or fully opaque pixels (no antialiasing at scatter level)
    assert n_partial == 0, (
        f"Unexpected partially transparent pixels: {n_partial}. "
        "shade() should produce only alpha=0 or alpha=255."
    )


def test_amplitude_vs_time():
    """Amplitude vs time scatter — the most common msvis plot."""
    _require_datashader()
    ms = _get_ms()
    dt = open_ms(ms)
    small = _small(_first_partition(dt))

    vis  = small["VISIBILITY"]
    flag = _flag_mask(small)
    amp  = _amp(vis).where(~flag)
    pol  = small.coords["polarization"].values[0]
    amp_pol  = amp.sel(polarization=pol)
    time_bc  = small.coords["time"].broadcast_like(amp_pol)

    df, rgba = _run_scatter(time_bc, amp_pol, label="amp_vs_time")
    _assert_uint8_image(rgba, "amp_vs_time")

    # x values should be MJD seconds (large positive numbers)
    assert df["x"].min() > 1e9, "Time values look wrong — expected MJD seconds"
    # y values should be non-negative amplitudes
    assert df["y"].min() >= 0, "Negative amplitude values in scatter data"


def test_amplitude_vs_uvdist():
    """Amplitude vs UV-distance in metres."""
    _require_datashader()
    ms = _get_ms()
    dt = open_ms(ms)
    small = _small(_first_partition(dt))

    vis    = small["VISIBILITY"]
    flag   = _flag_mask(small)
    amp    = _amp(vis).where(~flag)
    pol    = small.coords["polarization"].values[0]
    amp_pol = amp.sel(polarization=pol)

    uvdist = _uvdist_m(small)                      # (time, baseline_id)
    uvdist_bc = uvdist.broadcast_like(amp_pol)     # → (time, baseline_id, frequency)

    df, rgba = _run_scatter(uvdist_bc, amp_pol, label="amp_vs_uvdist")
    _assert_uint8_image(rgba, "amp_vs_uvdist")
    assert df["x"].min() >= 0, "Negative UV-distance values"


def test_amplitude_vs_uvwave():
    """
    Amplitude vs UV-distance in kilolambda.

    uvwave has shape (time, baseline_id, frequency) — same as amp after
    polarization selection, so no extra broadcast needed.
    """
    _require_datashader()
    ms = _get_ms()
    dt = open_ms(ms)
    small = _small(_first_partition(dt))

    vis    = small["VISIBILITY"]
    flag   = _flag_mask(small)
    amp    = _amp(vis).where(~flag)
    pol    = small.coords["polarization"].values[0]
    amp_pol = amp.sel(polarization=pol)

    uvwave = _uvwave_klambda(small)                # (time, baseline_id, frequency)

    # flag also needs to be polarization-selected for masking uvwave
    flag_pol = flag.sel(polarization=pol)
    uvwave_masked = uvwave.where(~flag_pol)

    df, rgba = _run_scatter(uvwave_masked, amp_pol, label="amp_vs_uvwave")
    _assert_uint8_image(rgba, "amp_vs_uvwave")
    assert df["x"].min() >= 0, "Negative UV-wave values"


def test_phase_vs_time():
    """Phase (degrees) vs time scatter."""
    _require_datashader()
    ms = _get_ms()
    dt = open_ms(ms)
    small = _small(_first_partition(dt))

    vis  = small["VISIBILITY"]
    flag = _flag_mask(small)
    pol  = small.coords["polarization"].values[0]

    phase_pol = _phase_deg(vis).where(~flag).sel(polarization=pol)
    time_bc   = small.coords["time"].broadcast_like(phase_pol)

    df, rgba = _run_scatter(time_bc, phase_pol, label="phase_vs_time")
    _assert_uint8_image(rgba, "phase_vs_time")
    assert df["y"].between(-180, 180).all(), "Phase values outside [-180, 180]"


def test_phase_vs_uvdist():
    """Phase vs UV-distance — classic calibration diagnostic plot."""
    _require_datashader()
    ms = _get_ms()
    dt = open_ms(ms)
    small = _small(_first_partition(dt))

    vis  = small["VISIBILITY"]
    flag = _flag_mask(small)
    pol  = small.coords["polarization"].values[0]

    phase_pol  = _phase_deg(vis).where(~flag).sel(polarization=pol)
    uvdist     = _uvdist_m(small)
    uvdist_bc  = uvdist.broadcast_like(phase_pol)
    uvdist_masked = uvdist_bc.where(~flag.sel(polarization=pol))

    df, rgba = _run_scatter(uvdist_masked, phase_pol, label="phase_vs_uvdist")
    _assert_uint8_image(rgba, "phase_vs_uvdist")


def test_real_vs_time():
    """Real part of visibility vs time."""
    _require_datashader()
    ms = _get_ms()
    dt = open_ms(ms)
    small = _small(_first_partition(dt))

    vis  = small["VISIBILITY"]
    flag = _flag_mask(small)
    pol  = small.coords["polarization"].values[0]

    real_pol = vis.real.where(~flag).sel(polarization=pol)
    time_bc  = small.coords["time"].broadcast_like(real_pol)

    df, rgba = _run_scatter(time_bc, real_pol, label="real_vs_time")
    _assert_uint8_image(rgba, "real_vs_time")


def test_imaginary_vs_time():
    """Imaginary part of visibility vs time."""
    _require_datashader()
    ms = _get_ms()
    dt = open_ms(ms)
    small = _small(_first_partition(dt))

    vis  = small["VISIBILITY"]
    flag = _flag_mask(small)
    pol  = small.coords["polarization"].values[0]

    imag_pol = vis.imag.where(~flag).sel(polarization=pol)
    time_bc  = small.coords["time"].broadcast_like(imag_pol)

    df, rgba = _run_scatter(time_bc, imag_pol, label="imag_vs_time")
    _assert_uint8_image(rgba, "imag_vs_time")


def test_uv_coverage():
    """
    U vs V scatter — UV-coverage plot.

    Both axes come from UVW, not from VISIBILITY, so no polarization
    selection is needed.  We plot both the baseline and its conjugate
    (−U, −V) to show the full UV-plane.
    """
    _require_datashader()
    ms = _get_ms()
    dt = open_ms(ms)
    small = _small(_first_partition(dt))

    uvw = small["UVW"]
    u   = uvw.sel(uvw_label="u")   # (time, baseline_id)
    v   = uvw.sel(uvw_label="v")

    # Stack baseline and its conjugate
    import pandas as pd
    u_r = u.compute().values.ravel()
    v_r = v.compute().values.ravel()
    # Filter NaN (padded baselines)
    finite = np.isfinite(u_r) & np.isfinite(v_r)
    u_r, v_r = u_r[finite], v_r[finite]
    # Add conjugate baselines
    u_full = np.concatenate([u_r, -u_r])
    v_full = np.concatenate([v_r, -v_r])

    df = pd.DataFrame({"x": u_full, "y": v_full})

    cvs  = _make_canvas()
    agg  = cvs.points(df, "x", "y", agg=ds_agg.count())
    rgba = _shade_to_uint8(agg)

    _assert_uint8_image(rgba, "uv_coverage")
    n_opaque = int((rgba[..., 3] > 0).sum())
    print(f"  UV-coverage: {len(df)} points, opaque pixels={n_opaque}")
    assert n_opaque > 0, "No UV-coverage points rendered"


def test_per_polarization_separate_canvas():
    """
    Separate Datashader canvas per polarization.

    VisibilityPlotter renders each polarization as a separate colour layer.
    This test verifies that each polarization produces an independent valid
    uint8 image and that the images differ (different polarizations have
    different data).
    """
    _require_datashader()
    ms = _get_ms()
    dt = open_ms(ms)
    small = _small(_first_partition(dt))

    vis  = small["VISIBILITY"]
    flag = _flag_mask(small)
    pols = small.coords["polarization"].values

    images = {}
    for pol in pols:
        amp_pol  = _amp(vis).where(~flag).sel(polarization=pol)
        time_bc  = small.coords["time"].broadcast_like(amp_pol)
        _, rgba  = _run_scatter(time_bc, amp_pol, label=f"amp_vs_time_{pol}")
        images[pol] = rgba

    if len(pols) == 2:
        # Images should differ between polarizations
        diff = np.abs(
            images[pols[0]].astype(int) - images[pols[1]].astype(int)
        ).sum()
        print(f"  Sum of pixel differences between {pols[0]} and {pols[1]}: {diff}")
        assert diff > 0, (
            "XX and YY scatter images are identical — "
            "polarization selection may not be working"
        )


def test_canvas_resolution():
    """
    Different canvas resolutions produce correctly shaped uint8 images.
    The plotter will expose width/height as user-configurable parameters.
    """
    _require_datashader()
    ms = _get_ms()
    dt = open_ms(ms)
    small = _small(_first_partition(dt))

    vis  = small["VISIBILITY"]
    flag = _flag_mask(small)
    pol  = small.coords["polarization"].values[0]
    amp_pol = _amp(vis).where(~flag).sel(polarization=pol)
    time_bc = small.coords["time"].broadcast_like(amp_pol)
    df      = _stack_to_df(time_bc, amp_pol)

    for w, h in [(200, 150), (800, 600), (1920, 1080)]:
        cvs  = ds.Canvas(plot_width=w, plot_height=h)
        agg  = cvs.points(df, "x", "y", agg=ds_agg.mean("y"))
        rgba = _shade_to_uint8(agg)
        assert rgba.dtype == np.uint8
        assert rgba.shape == (h, w, 4), (
            f"Canvas ({w}×{h}): expected shape ({h}, {w}, 4), got {rgba.shape}"
        )
        print(f"  Canvas {w}×{h}: shape={rgba.shape} ✓")


def test_count_aggregation():
    """
    ds_agg.count() aggregation — used for density/occupancy plots where
    the colour encodes how many visibility samples fell in each pixel.
    This is an alternative to mean() for some msvis display modes.
    """
    _require_datashader()
    ms = _get_ms()
    dt = open_ms(ms)
    small = _small(_first_partition(dt))

    vis  = small["VISIBILITY"]
    flag = _flag_mask(small)
    pol  = small.coords["polarization"].values[0]
    amp_pol = _amp(vis).where(~flag).sel(polarization=pol)
    time_bc = small.coords["time"].broadcast_like(amp_pol)
    df      = _stack_to_df(time_bc, amp_pol)

    cvs  = _make_canvas()
    agg  = cvs.points(df, "x", "y", agg=ds_agg.count())
    rgba = _shade_to_uint8(agg)

    _assert_uint8_image(rgba, "count_aggregation")
    # count agg values should be non-negative integers
    assert agg.values[np.isfinite(agg.values)].min() >= 0
    print(f"  Count agg: max count per pixel = {int(np.nanmax(agg.values))}")


def test_full_partition_scatter_timing():
    """
    Full first partition scatter (no isel slice) — timing benchmark.

    For sis14_twhya (bandpass calibrator partition_000):
      ~40 time × 325 baseline × 384 freq × 1 pol = ~5M samples

    Target: stack + DataFrame + Canvas + shade in < 30 seconds.
    This is a realistic upper bound for an interactive plot response.
    """
    _require_datashader()
    ms = _get_ms()
    dt = open_ms(ms)
    ds_part = _first_partition(dt)

    vis  = ds_part["VISIBILITY"]
    flag = _flag_mask(ds_part)
    pol  = ds_part.coords["polarization"].values[0]
    amp_pol = _amp(vis).where(~flag).sel(polarization=pol)
    time_bc = ds_part.coords["time"].broadcast_like(amp_pol)

    n_total = int(np.prod([ds_part.sizes[d] for d in amp_pol.dims]))
    print(f"  Full partition: {n_total:,} samples (before dropna)")

    t0  = time_mod.perf_counter()
    df  = _stack_to_df(time_bc, amp_pol)
    t1  = time_mod.perf_counter()
    cvs  = _make_canvas()
    agg  = cvs.points(df, "x", "y", agg=ds_agg.mean("y"))
    rgba = _shade_to_uint8(agg)
    t2  = time_mod.perf_counter()

    _assert_uint8_image(rgba, "full_partition")
    print(
        f"  After dropna: {len(df):,} samples, "
        f"stack+df={t1-t0:.2f}s, render={t2-t1:.2f}s, "
        f"total={t2-t0:.2f}s"
    )
    assert t2 - t0 < 30.0, (
        f"Full partition scatter took {t2-t0:.1f}s — exceeds 30s target"
    )


# ---------------------------------------------------------------------------
# Standalone runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    warnings.filterwarnings(
        "ignore",
        message="Casting complex values to real discards the imaginary part",
        category=ComplexWarning,
    )
    warnings.filterwarnings(
        "ignore",
        message="The return type of `Dataset.dims` will be changed",
        category=FutureWarning,
    )

    tests = [
        test_datashader_installed,
        test_stack_stays_lazy,
        test_nan_transparency,
        test_amplitude_vs_time,
        test_amplitude_vs_uvdist,
        test_amplitude_vs_uvwave,
        test_phase_vs_time,
        test_phase_vs_uvdist,
        test_real_vs_time,
        test_imaginary_vs_time,
        test_uv_coverage,
        test_per_polarization_separate_canvas,
        test_canvas_resolution,
        test_count_aggregation,
        test_full_partition_scatter_timing,
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
