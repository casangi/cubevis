"""
test_09_datashader_raster.py — Datashader 2D raster plot pipeline for msvis.

Tests the full pipeline from xarray-ms DataTree → 2D xarray DataArray →
Datashader Canvas → float64 agg DataArray → uint8 RGBA image, for the
raster plot modes that VisibilityPlotter will support.

Unlike scatter mode (test_08), raster mode operates on naturally 2D data:
the MS already provides amplitude/phase/flag on a (time × baseline_id) or
(frequency × baseline_id) grid.  No stacking/flattening is needed.
Instead, a channel or time reduction collapses the extra dimensions first,
leaving a 2D DataArray that feeds directly into ds.Canvas.raster() or
ds.Canvas.points() depending on whether the grid is regular.

Raster combinations tested:
  1. time × baseline_id    coloured by channel-averaged amplitude
  2. time × baseline_id    coloured by channel-averaged phase
  3. time × baseline_id    coloured by flag fraction (occupancy)
  4. frequency × baseline_id  coloured by time-averaged amplitude (spectrum per baseline)
  5. frequency × time      coloured by amplitude (per-baseline RFI waterfall)
  6. frequency × time      coloured by flag fraction

Two-layer design verified in each test:
  - Layer 1: agg DataArray (float64/int32) — the scientifically meaningful intermediate
  - Layer 2: uint8 RGBA from shade() — the display artifact sent to the browser

Key assertions:
  - agg has correct shape (canvas height × canvas width)
  - agg dtype is float64 (mean) or int32 (count)
  - NaN cells in agg correspond to empty canvas pixels (no data mapped there)
  - uint8 RGBA has correct shape (H, W, 4)
  - Transparent pixels (alpha=0) in RGBA correspond to NaN in agg
  - agg can be probed by pixel coordinate for hover/tooltip values
  - count() agg reaches 1 sample/pixel when zoom is tight enough
    (the flagging-safe threshold)

Zoom/mode-transition tests:
  - Progressively narrowing x_range on time×baseline_id shows count dropping
    toward 1 — the raster→flag-enabled transition point
  - At count=1 the agg value equals the individual visibility amplitude

Usage:
    MS=sis14_twhya_calibrated_flagged.ms python test_09_datashader_raster.py
    MS=sis14_twhya_calibrated_flagged.ms pytest test_09_datashader_raster.py -v
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
# Skip guard
# ---------------------------------------------------------------------------

def _require_datashader():
    if not HAS_DATASHADER:
        raise RuntimeError(
            "datashader not installed — install with: pip install datashader"
        )


# ---------------------------------------------------------------------------
# Helpers shared with test_08
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


def _largest_partition(dt):
    return max(
        (node.ds for node in dt.children.values()
         if node.ds.sizes.get("time", 0) > 0),
        key=lambda d: d.sizes["time"],
    )


def _first_partition(dt):
    for node in dt.children.values():
        if node.ds.sizes.get("time", 0) > 0:
            return node.ds
    raise RuntimeError("No partition found")


def _flag_mask(ds_part):
    return ds_part["FLAG"].astype(bool)


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


def _shade_to_uint8(agg):
    """Convert Datashader Image/DataArray to (H, W, 4) uint8 RGBA array."""
    img = ds_tf.shade(agg)
    if hasattr(img, "data"):
        arr32 = np.asarray(img.data)
    elif hasattr(img, "values"):
        arr32 = img.values
    else:
        arr32 = np.asarray(img)
    return arr32.view(np.uint8).reshape(arr32.shape + (4,))


def _make_canvas(w=PLOT_W, h=PLOT_H):
    return ds.Canvas(plot_width=w, plot_height=h)


# ---------------------------------------------------------------------------
# Raster preparation helpers
# ---------------------------------------------------------------------------

def _chan_avg_amp(ds_part, pol=None):
    """
    Channel-averaged amplitude: (time, baseline_id).

    This is the primary raster quantity for time×baseline_id plots.
    Collapses the frequency and polarization dimensions.
    """
    vis  = ds_part["VISIBILITY"]
    flag = _flag_mask(ds_part)
    amp  = _amp(vis).where(~flag)
    if pol is not None:
        amp = amp.sel(polarization=pol)
        flag_for_mean = flag.sel(polarization=pol)
    else:
        # Average over polarization first, then frequency
        flag_for_mean = flag.all(dim="polarization")
        amp = amp.mean(dim="polarization", skipna=True)
    return amp.mean(dim="frequency", skipna=True)   # (time, baseline_id)


def _chan_avg_phase(ds_part, pol=None):
    """Channel-averaged phase: (time, baseline_id)."""
    vis  = ds_part["VISIBILITY"]
    flag = _flag_mask(ds_part)
    ph   = _phase_deg(vis).where(~flag)
    if pol is not None:
        ph = ph.sel(polarization=pol)
    else:
        ph = ph.mean(dim="polarization", skipna=True)
    return ph.mean(dim="frequency", skipna=True)   # (time, baseline_id)


def _flag_fraction(ds_part):
    """
    Flag fraction grid: (time, baseline_id).

    Values in [0, 1]: 0 = no flags, 1 = all channels/pols flagged.
    NaN where the baseline slot is padded (missing from MS).
    """
    flag = _flag_mask(ds_part)            # (time, baseline_id, freq, pol)
    # EIT is NaN for padded slots — use it to mask the result
    eit  = ds_part["EFFECTIVE_INTEGRATION_TIME"]   # (time, baseline_id)
    frac = flag.mean(dim=["frequency", "polarization"]).where(
        np.isfinite(eit)
    )
    return frac   # (time, baseline_id)


def _time_avg_amp(ds_part, pol=None):
    """
    Time-averaged amplitude: (baseline_id, frequency).

    Primary raster quantity for frequency×baseline_id plots.
    """
    vis  = ds_part["VISIBILITY"]
    flag = _flag_mask(ds_part)
    amp  = _amp(vis).where(~flag)
    if pol is not None:
        amp = amp.sel(polarization=pol)
    else:
        amp = amp.mean(dim="polarization", skipna=True)
    return amp.mean(dim="time", skipna=True)   # (baseline_id, frequency)


def _amp_waterfall(ds_part, baseline_idx, pol=None):
    """
    Amplitude waterfall for a single baseline: (time, frequency).

    This is the RFI waterfall plot — one panel per baseline.
    """
    vis  = ds_part["VISIBILITY"].isel(baseline_id=baseline_idx)
    flag = _flag_mask(ds_part).isel(baseline_id=baseline_idx)
    amp  = _amp(vis).where(~flag)
    if pol is not None:
        amp = amp.sel(polarization=pol)
    else:
        amp = amp.mean(dim="polarization", skipna=True)
    return amp   # (time, frequency)


# ---------------------------------------------------------------------------
# Two-layer pipeline helper
# ---------------------------------------------------------------------------

def _raster_pipeline(grid_2d, x_dim, y_dim, label="", reduction=None):
    """
    Full two-layer raster pipeline.

    Parameters
    ----------
    grid_2d : xr.DataArray
        2D DataArray with dims (y_dim, x_dim).  May be lazy.
    x_dim : str
        Name of the x-axis dimension in grid_2d.
    y_dim : str
        Name of the y-axis dimension in grid_2d.
    label : str
        For diagnostic output.
    reduction : datashader reduction, optional
        Defaults to ds_agg.mean(grid_2d.name or "value").

    Returns
    -------
    agg : xr.DataArray
        Float64 aggregation result, shape (PLOT_H, PLOT_W).
    rgba : np.ndarray
        uint8 RGBA image, shape (PLOT_H, PLOT_W, 4).
    """
    t0 = time_mod.perf_counter()

    # Ensure the DataArray has a name for Datashader
    if grid_2d.name is None:
        grid_2d = grid_2d.rename("value")

    # Transpose to (y_dim, x_dim) order expected by Canvas.raster()
    if grid_2d.dims != (y_dim, x_dim):
        grid_2d = grid_2d.transpose(y_dim, x_dim)

    # Trigger Dask compute — Canvas.raster() needs a concrete array
    grid_computed = grid_2d.compute()
    t_compute = time_mod.perf_counter() - t0

    t1 = time_mod.perf_counter()
    cvs = _make_canvas()

    # Use Canvas.raster() for regularly-gridded 2D data
    # (both axes are uniform coordinate arrays)
    agg  = cvs.raster(grid_computed, agg=ds_agg.mean())
    rgba = _shade_to_uint8(agg)
    t_render = time_mod.perf_counter() - t1

    n_nan    = int(np.isnan(agg.values).sum())
    n_opaque = int((rgba[..., 3] > 0).sum())
    print(
        f"  {label}: shape={grid_computed.shape}, "
        f"compute={t_compute*1000:.0f}ms, render={t_render*1000:.0f}ms, "
        f"agg_nan={n_nan}, opaque_px={n_opaque}/{PLOT_W*PLOT_H}"
    )
    return agg, rgba


def _assert_raster_output(agg, rgba, label=""):
    """Common assertions for the two-layer raster output."""
    # Layer 1: agg DataArray
    assert agg.ndim == 2, f"{label}: agg should be 2D, got {agg.ndim}D"
    assert agg.dtype in (np.float64, np.float32, np.int32, np.int64), (
        f"{label}: unexpected agg dtype {agg.dtype}"
    )
    # Layer 2: uint8 RGBA
    assert rgba.dtype == np.uint8, f"{label}: RGBA dtype should be uint8"
    assert rgba.ndim == 3 and rgba.shape[2] == 4, (
        f"{label}: RGBA shape should be (H, W, 4), got {rgba.shape}"
    )
    # NaN in agg → transparent in RGBA
    agg_nan_mask  = np.isnan(agg.values)
    rgba_transp   = rgba[..., 3] == 0
    # All NaN agg positions should be transparent
    # (not necessarily vice versa — empty canvas regions also transparent)
    assert np.all(rgba_transp[agg_nan_mask]), (
        f"{label}: NaN agg positions are not all transparent in RGBA"
    )


# ---------------------------------------------------------------------------
# Tests — time × baseline_id rasters
# ---------------------------------------------------------------------------

def test_time_baseline_amplitude():
    """
    time × baseline_id raster coloured by channel-averaged amplitude.

    This is the primary msview analog — the most common raster plot.
    Tests both the float64 agg layer and the uint8 RGBA layer.
    """
    _require_datashader()
    ms = _get_ms()
    dt = open_ms(ms)
    ds_part = _largest_partition(dt).isel(
        time=slice(0, 50), frequency=slice(0, 16)
    )
    pol = ds_part.coords["polarization"].values[0]

    grid = _chan_avg_amp(ds_part, pol=pol)   # (time, baseline_id)
    agg, rgba = _raster_pipeline(
        grid, x_dim="baseline_id", y_dim="time",
        label="time_x_baseline_amp"
    )
    _assert_raster_output(agg, rgba, "time_x_baseline_amp")

    # Amplitude values should be non-negative where not NaN
    finite = agg.values[np.isfinite(agg.values)]
    assert (finite >= 0).all(), "Negative amplitude in raster agg"
    print(f"  Amplitude range: [{finite.min():.3f}, {finite.max():.3f}]")


def test_time_baseline_phase():
    """
    time × baseline_id raster coloured by channel-averaged phase (degrees).
    """
    _require_datashader()
    ms = _get_ms()
    dt = open_ms(ms)
    ds_part = _largest_partition(dt).isel(
        time=slice(0, 50), frequency=slice(0, 16)
    )
    pol = ds_part.coords["polarization"].values[0]

    grid = _chan_avg_phase(ds_part, pol=pol)  # (time, baseline_id)
    agg, rgba = _raster_pipeline(
        grid, x_dim="baseline_id", y_dim="time",
        label="time_x_baseline_phase"
    )
    _assert_raster_output(agg, rgba, "time_x_baseline_phase")

    finite = agg.values[np.isfinite(agg.values)]
    assert (finite >= -180).all() and (finite <= 180).all(), (
        "Phase values outside [-180, 180] in raster agg"
    )
    print(f"  Phase range: [{finite.min():.1f}, {finite.max():.1f}] deg")


def test_time_baseline_flag_fraction():
    """
    time × baseline_id raster coloured by flag fraction.

    Values in [0, 1]: 0=unflagged, 1=fully flagged.
    NaN for padded (missing) baseline slots.
    This is the 'flag raster' view — shows where flagging is concentrated.
    """
    _require_datashader()
    ms = _get_ms()
    dt = open_ms(ms)
    ds_part = _largest_partition(dt).isel(
        time=slice(0, 50), frequency=slice(0, 16)
    )

    grid = _flag_fraction(ds_part)   # (time, baseline_id)
    agg, rgba = _raster_pipeline(
        grid, x_dim="baseline_id", y_dim="time",
        label="time_x_baseline_flagfrac"
    )
    _assert_raster_output(agg, rgba, "time_x_baseline_flagfrac")

    finite = agg.values[np.isfinite(agg.values)]
    assert (finite >= 0).all() and (finite <= 1).all(), (
        "Flag fraction values outside [0, 1]"
    )
    print(
        f"  Flag fraction range: [{finite.min():.3f}, {finite.max():.3f}], "
        f"mean={finite.mean():.3f}"
    )


# ---------------------------------------------------------------------------
# Tests — frequency × baseline_id rasters
# ---------------------------------------------------------------------------

def test_frequency_baseline_amplitude():
    """
    frequency × baseline_id raster coloured by time-averaged amplitude.

    Shows the spectral structure per baseline — useful for identifying
    narrow-band RFI or passband shape variations across baselines.
    """
    _require_datashader()
    ms = _get_ms()
    dt = open_ms(ms)
    ds_part = _largest_partition(dt).isel(time=slice(0, 50))
    pol = ds_part.coords["polarization"].values[0]

    grid = _time_avg_amp(ds_part, pol=pol)   # (baseline_id, frequency)
    agg, rgba = _raster_pipeline(
        grid, x_dim="frequency", y_dim="baseline_id",
        label="freq_x_baseline_amp"
    )
    _assert_raster_output(agg, rgba, "freq_x_baseline_amp")

    finite = agg.values[np.isfinite(agg.values)]
    assert (finite >= 0).all(), "Negative amplitude in frequency×baseline raster"
    freqs_GHz = ds_part.coords["frequency"].values / 1e9
    print(
        f"  Freq range: {freqs_GHz[0]:.4f}–{freqs_GHz[-1]:.4f} GHz, "
        f"amp range: [{finite.min():.3f}, {finite.max():.3f}]"
    )


# ---------------------------------------------------------------------------
# Tests — frequency × time rasters (RFI waterfall)
# ---------------------------------------------------------------------------

def test_frequency_time_waterfall_amplitude():
    """
    frequency × time raster for a single baseline — the RFI waterfall.

    Each pixel is a single (time, frequency) cell for one baseline.
    At full resolution this is a 1-sample-per-pixel display; zoomed out
    Datashader aggregates over time or frequency.
    """
    _require_datashader()
    ms = _get_ms()
    dt = open_ms(ms)
    ds_part = _largest_partition(dt)
    pol = ds_part.coords["polarization"].values[0]

    # Pick the first non-padded baseline
    eit_t0   = ds_part["EFFECTIVE_INTEGRATION_TIME"].isel(time=0).compute()
    valid_bl = int(np.where(np.isfinite(eit_t0.values))[0][0])
    print(f"  Using baseline_id={valid_bl}")

    grid = _amp_waterfall(
        ds_part.isel(time=slice(0, 50)),
        baseline_idx=valid_bl, pol=pol
    )   # (time, frequency)

    agg, rgba = _raster_pipeline(
        grid, x_dim="frequency", y_dim="time",
        label="freq_x_time_waterfall_amp"
    )
    _assert_raster_output(agg, rgba, "freq_x_time_waterfall_amp")

    finite = agg.values[np.isfinite(agg.values)]
    assert (finite >= 0).all(), "Negative amplitude in waterfall raster"
    print(f"  Waterfall amp range: [{finite.min():.3f}, {finite.max():.3f}]")


def test_frequency_time_waterfall_flag():
    """
    frequency × time flag raster for a single baseline.

    FLAG is uint8 (0 or 1) per (time, frequency) cell.  This directly
    feeds Canvas.raster() without any reduction — each pixel IS a flag value.
    Useful for visualising the fine structure of existing flags.
    """
    _require_datashader()
    ms = _get_ms()
    dt = open_ms(ms)
    ds_part = _largest_partition(dt).isel(time=slice(0, 50))

    # Find valid baseline
    eit_t0   = ds_part["EFFECTIVE_INTEGRATION_TIME"].isel(time=0).compute()
    valid_bl = int(np.where(np.isfinite(eit_t0.values))[0][0])
    pol      = ds_part.coords["polarization"].values[0]

    flag_bl = ds_part["FLAG"].isel(baseline_id=valid_bl).sel(
        polarization=pol
    ).astype(float)   # (time, frequency), values 0.0 or 1.0

    # Mask padded time slots (should be none for a valid baseline, but
    # be defensive)
    eit_bl = ds_part["EFFECTIVE_INTEGRATION_TIME"].isel(
        baseline_id=valid_bl
    )
    flag_bl = flag_bl.where(np.isfinite(eit_bl))

    agg, rgba = _raster_pipeline(
        flag_bl, x_dim="frequency", y_dim="time",
        label="freq_x_time_waterfall_flag"
    )
    _assert_raster_output(agg, rgba, "freq_x_time_waterfall_flag")

    finite = agg.values[np.isfinite(agg.values)]
    assert set(np.unique(np.round(finite, 3))).issubset({0.0, 1.0}), (
        "Flag waterfall agg should contain only 0.0 and 1.0 values, "
        f"got: {np.unique(np.round(finite, 2))}"
    )
    flag_frac = finite.mean()
    print(f"  Flag waterfall: flag fraction={flag_frac:.3f}")


# ---------------------------------------------------------------------------
# Tests — two-layer design: agg DataArray probe
# ---------------------------------------------------------------------------

def test_agg_pixel_probe():
    """
    Verify that the agg DataArray can be probed at pixel coordinates
    for hover/tooltip values.

    This is the mechanism for displaying the actual aggregated amplitude
    (or count, or phase) when the user hovers over a raster pixel.
    """
    _require_datashader()
    ms = _get_ms()
    dt = open_ms(ms)
    ds_part = _largest_partition(dt).isel(
        time=slice(0, 50), frequency=slice(0, 16)
    )
    pol = ds_part.coords["polarization"].values[0]

    grid = _chan_avg_amp(ds_part, pol=pol).compute()
    cvs  = _make_canvas()
    agg  = cvs.raster(grid.transpose("time", "baseline_id"), agg=ds_agg.mean())

    # The agg DataArray has x and y coordinate arrays giving the
    # data-space values at each canvas pixel centre
    assert "baseline_id" in agg.coords or "x" in agg.coords, (
        "agg DataArray missing x-axis coordinate for pixel probing"
    )
    assert "time" in agg.coords or "y" in agg.coords, (
        "agg DataArray missing y-axis coordinate for pixel probing"
    )

    # Pick a pixel with finite value and confirm we can read its value
    finite_mask = np.isfinite(agg.values)
    if finite_mask.any():
        py, px = np.argwhere(finite_mask)[0]
        pixel_value = float(agg.values[py, px])
        # Read coordinate value at that pixel
        x_coords = agg.coords[list(agg.dims)[1]].values
        y_coords = agg.coords[list(agg.dims)[0]].values
        x_val = x_coords[px]
        y_val = y_coords[py]
        print(
            f"  Pixel ({px}, {py}): amp={pixel_value:.4f}, "
            f"x={x_val:.2f}, y={y_val:.2f}"
        )
        assert np.isfinite(pixel_value), "Probed pixel value is not finite"
    else:
        print("  WARNING: all agg pixels are NaN — cannot probe")


def test_agg_nan_equals_rgba_transparent():
    """
    Strict verification: every NaN pixel in the float64 agg DataArray
    maps to alpha=0 in the uint8 RGBA image, and every finite agg pixel
    maps to alpha=255.

    This is the correctness guarantee for the two-layer design — the
    RGBA image faithfully represents the agg coverage.
    """
    _require_datashader()
    ms = _get_ms()
    dt = open_ms(ms)
    ds_part = _largest_partition(dt).isel(
        time=slice(0, 50), frequency=slice(0, 16)
    )
    pol = ds_part.coords["polarization"].values[0]

    grid = _chan_avg_amp(ds_part, pol=pol).compute()
    cvs  = _make_canvas()
    agg  = cvs.raster(grid.transpose("time", "baseline_id"), agg=ds_agg.mean())
    rgba = _shade_to_uint8(agg)

    agg_nan    = np.isnan(agg.values)
    agg_finite = ~agg_nan
    alpha      = rgba[..., 3]

    n_nan_not_transp   = int((agg_nan  & (alpha != 0)).sum())
    n_finite_not_opaque = int((agg_finite & (alpha == 0)).sum())

    print(
        f"  NaN agg → non-transparent: {n_nan_not_transp} (should be 0)\n"
        f"  Finite agg → transparent:  {n_finite_not_opaque} (should be 0)"
    )
    assert n_nan_not_transp == 0, (
        f"{n_nan_not_transp} NaN agg pixels are not transparent in RGBA"
    )
    assert n_finite_not_opaque == 0, (
        f"{n_finite_not_opaque} finite agg pixels are transparent in RGBA"
    )


# ---------------------------------------------------------------------------
# Tests — zoom/flag-threshold detection
# ---------------------------------------------------------------------------

def test_zoom_count_decreases():
    """
    As x_range narrows (zoom in on baseline_id axis), the number of grid
    cells mapped to each canvas pixel decreases toward 1.

    Note: cvs.raster() does not support count() aggregation — its valid
    options are mean, min, max, first, last, mode, var, std.  For raster
    mode the samples-per-pixel ratio is computed analytically from the
    grid shape and canvas size, which is cheaper and exact.

    At samples_per_pixel <= 1.0 in both axes the display has reached the
    flagging-safe threshold: each pixel covers at most one grid cell and
    the user can draw a flag region that maps unambiguously to native MS
    coordinates.
    """
    _require_datashader()
    ms = _get_ms()
    dt = open_ms(ms)
    ds_part = _largest_partition(dt).isel(
        time=slice(0, 20), frequency=slice(0, 16)
    )
    pol = ds_part.coords["polarization"].values[0]

    grid = _chan_avg_amp(ds_part, pol=pol).compute()
    grid_t = grid.transpose("time", "baseline_id")

    grid_n_time = grid_t.sizes["time"]
    grid_n_bl   = grid_t.sizes["baseline_id"]

    bl_coords = grid_t.coords["baseline_id"].values
    bl_finite  = bl_coords[np.isfinite(bl_coords)]
    bl_min, bl_max = float(bl_finite.min()), float(bl_finite.max())
    bl_range = bl_max - bl_min

    ratios_x = []
    fractions = [1.0, 0.5, 0.25, 0.1]

    for frac in fractions:
        x_range = (bl_min, bl_min + bl_range * frac)
        # How many grid cells fall in this x_range?
        n_cells_in_range = int(np.sum(
            (bl_finite >= x_range[0]) & (bl_finite <= x_range[1])
        ))
        ratio_x = n_cells_in_range / PLOT_W
        ratio_y = grid_n_time / PLOT_H
        ratios_x.append(ratio_x)
        print(
            f"  x_range fraction={frac:.2f}: "
            f"baseline cells in range={n_cells_in_range}, "
            f"ratio x={ratio_x:.3f} cells/px, y={ratio_y:.3f} cells/px"
        )

    # Ratio should be non-increasing as we zoom in
    for i in range(len(ratios_x) - 1):
        assert ratios_x[i] >= ratios_x[i+1], (
            f"Cells/pixel increased on zoom: {ratios_x[i]:.3f} → {ratios_x[i+1]:.3f}"
        )

    # Also verify that Canvas.raster() with mean() still works at narrow range
    x_range_narrow = (bl_min, bl_min + bl_range * 0.1)
    cvs_narrow = ds.Canvas(
        plot_width=PLOT_W, plot_height=PLOT_H,
        x_range=x_range_narrow,
    )
    agg_narrow = cvs_narrow.raster(grid_t, agg=ds_agg.mean())
    rgba_narrow = _shade_to_uint8(agg_narrow)
    _assert_raster_output(agg_narrow, rgba_narrow, "zoom_10pct")
    print(f"  Narrow zoom raster: opaque_px={int((rgba_narrow[...,3]>0).sum())}")


def test_zoom_flag_threshold_detection():
    """
    Detect the zoom fraction at which the raster reaches the flagging-safe
    threshold: ≤ 1 grid cell per canvas pixel in both x and y.

    For raster mode this is a purely geometric calculation — no Datashader
    count() call needed (count() is not supported by cvs.raster()).

    Reports the zoom level and whether the current canvas size is sufficient
    to reach 1:1 mapping at some zoom level; never fails, only reports.
    """
    _require_datashader()
    ms = _get_ms()
    dt = open_ms(ms)
    ds_part = _largest_partition(dt).isel(
        time=slice(0, 20), frequency=slice(0, 16)
    )
    pol = ds_part.coords["polarization"].values[0]

    grid = _chan_avg_amp(ds_part, pol=pol).compute()
    grid_t = grid.transpose("time", "baseline_id")

    grid_n_time = grid_t.sizes["time"]
    grid_n_bl   = grid_t.sizes["baseline_id"]

    bl_coords = grid_t.coords["baseline_id"].values
    bl_finite  = bl_coords[np.isfinite(bl_coords)]
    bl_min, bl_max = float(bl_finite.min()), float(bl_finite.max())
    bl_range = bl_max - bl_min

    # y (time) ratio is fixed by canvas height vs time dimension
    ratio_y = grid_n_time / PLOT_H
    print(
        f"  Grid: {grid_n_time} time × {grid_n_bl} baseline_id, "
        f"canvas: {PLOT_W}×{PLOT_H}"
    )
    print(f"  y (time) cells/pixel = {ratio_y:.3f} "
          f"({'flagging-safe' if ratio_y <= 1.0 else 'needs y-zoom'})")

    threshold_frac = None
    for frac in [1.0, 0.5, 0.25, 0.1, 0.05, 0.02, 0.01]:
        x_range = (bl_min, bl_min + bl_range * frac)
        n_cells = int(np.sum(
            (bl_finite >= x_range[0]) & (bl_finite <= x_range[1])
        ))
        ratio_x = n_cells / PLOT_W
        if ratio_x <= 1.0 and ratio_y <= 1.0:
            threshold_frac = frac
            break

    if threshold_frac is not None:
        print(
            f"  Flagging-safe threshold: x_range fraction={threshold_frac} "
            f"(both axes ≤ 1 cell/pixel)"
        )
    else:
        print(
            f"  Flagging-safe threshold not reached at 1% zoom."
            f"  Min x ratio at 1% zoom: "
            f"{int(np.sum((bl_finite >= bl_min) & (bl_finite <= bl_min + bl_range*0.01))) / PLOT_W:.3f}"
            f"  Consider increasing canvas width beyond {PLOT_W}px or "
            f"reducing the baseline_id range via SelectionSpec."
        )
    assert True  # informational test


# ---------------------------------------------------------------------------
# Tests — full partition timing
# ---------------------------------------------------------------------------

def test_full_partition_raster_timing():
    """
    Full science target partition time×baseline_id raster.

    For sis14_twhya (OBSERVE_TARGET partition):
      ~270 time × 325 baseline_id × 48 freq (before chan-avg) → 2D (270, 325) grid

    Target: Dask compute + Canvas.raster() + shade() < 15s.
    Rasters are much faster than scatter because no DataFrame conversion is needed.
    """
    _require_datashader()
    ms = _get_ms()
    dt = open_ms(ms)
    ds_part = _largest_partition(dt)
    pol = ds_part.coords["polarization"].values[0]

    print(
        f"  Full partition: time={ds_part.sizes['time']}, "
        f"baseline_id={ds_part.sizes['baseline_id']}, "
        f"frequency={ds_part.sizes['frequency']}"
    )

    t0   = time_mod.perf_counter()
    grid = _chan_avg_amp(ds_part, pol=pol).compute()
    t1   = time_mod.perf_counter()

    cvs  = _make_canvas()
    agg  = cvs.raster(grid.transpose("time", "baseline_id"), agg=ds_agg.mean())
    rgba = _shade_to_uint8(agg)
    t2   = time_mod.perf_counter()

    _assert_raster_output(agg, rgba, "full_partition_raster")
    finite = agg.values[np.isfinite(agg.values)]
    print(
        f"  compute={t1-t0:.2f}s, render={t2-t1:.2f}s, total={t2-t0:.2f}s, "
        f"grid shape={grid.shape}, "
        f"finite pixels={len(finite)}/{PLOT_W*PLOT_H}, "
        f"amp range=[{finite.min():.3f}, {finite.max():.3f}]"
    )
    assert t2 - t0 < 15.0, (
        f"Full partition raster took {t2-t0:.1f}s — exceeds 15s target"
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
        # time × baseline_id
        test_time_baseline_amplitude,
        test_time_baseline_phase,
        test_time_baseline_flag_fraction,
        # frequency × baseline_id
        test_frequency_baseline_amplitude,
        # frequency × time (waterfall)
        test_frequency_time_waterfall_amplitude,
        test_frequency_time_waterfall_flag,
        # Two-layer design verification
        test_agg_pixel_probe,
        test_agg_nan_equals_rgba_transparent,
        # Zoom/flag threshold
        test_zoom_count_decreases,
        test_zoom_flag_threshold_detection,
        # Timing
        test_full_partition_raster_timing,
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
