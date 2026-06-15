"""
test_10_bokeh_display.py — Render scatter and raster plots as Bokeh figures.

Produces a self-contained HTML file containing a representative cross-section
of msvis plot types.  Open the HTML in any browser to inspect the results.

Output: msvis_plots.html  (in the current working directory)

This test is the bridge between the xarray-ms/Datashader pipeline tests
(test_01 through test_09) and the final VisibilityPlotter application.
It exercises the complete path:

    xarray-ms DataTree
        → derived quantities (amp, phase, uvdist, flag fraction)
        → flat DataFrame or 2D grid
        → Datashader Canvas (float64 agg DataArray)
        → uint8 RGBA image
        → Bokeh ImageRGBA or Image glyph
        → HTML figure

Plots produced (one panel per row in the output HTML):
  Row 1 — Scatter: Amplitude vs Time (XX and YY overlaid, different colours)
  Row 2 — Scatter: Amplitude vs UV-distance (metres)
  Row 3 — Scatter: Phase vs Time (XX polarization)
  Row 4 — Scatter: UV-coverage (U vs V with conjugate baselines)
  Row 5 — Raster:  Time × Baseline (channel-averaged amplitude)
  Row 6 — Raster:  Frequency × Time waterfall (single baseline, amplitude)
  Row 7 — Raster:  Time × Baseline (flag fraction overlay)

Each panel includes:
  - Axis labels with units
  - A title identifying the plot type and data selection
  - HoverTool showing the aggregated float64 value at the cursor position
    (from the agg DataArray, not from the uint8 RGBA — the two-layer design)
  - Basic Bokeh toolbar (pan, wheel zoom, reset, save)

The flag overlay in Row 7 demonstrates compositing: two Datashader images
are stacked in the same Bokeh figure — data amplitude underneath, flag
regions on top in a distinct colour (red with partial transparency).

Usage:
    MS=sis14_twhya_calibrated_flagged.ms python test_10_bokeh_display.py

    # Then open the output:
    open msvis_plots.html          # macOS
    xdg-open msvis_plots.html      # Linux
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

try:
    import bokeh
    from bokeh.plotting import figure, output_file, save
    from bokeh.layouts import column, row, gridplot
    from bokeh.models import (
        ColorBar, LinearColorMapper, HoverTool,
        ColumnDataSource, BasicTicker, PrintfTickFormatter,
        Div, Range1d,
    )
    from bokeh.palettes import Viridis256, Plasma256, RdYlGn11
    from bokeh.io import export_png
    HAS_BOKEH = True
except ImportError:
    HAS_BOKEH = False

C_MS = 299_792_458.0
PLOT_W = 600     # canvas and figure width
PLOT_H = 400     # canvas and figure height
OUTPUT_HTML = "msvis_plots.html"


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------

def _require_all():
    if not HAS_DATASHADER:
        raise RuntimeError("datashader not installed: pip install datashader")
    if not HAS_BOKEH:
        raise RuntimeError("bokeh not installed: pip install bokeh")


# ---------------------------------------------------------------------------
# xarray-ms helpers (shared with earlier tests)
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


# ---------------------------------------------------------------------------
# Datashader helpers
# ---------------------------------------------------------------------------

def _shade_to_uint8(agg):
    """Convert Datashader Image to (H, W, 4) uint8 RGBA."""
    img = ds_tf.shade(agg)
    if hasattr(img, "data"):
        arr32 = np.asarray(img.data)
    elif hasattr(img, "values"):
        arr32 = img.values
    else:
        arr32 = np.asarray(img)
    return arr32.view(np.uint8).reshape(arr32.shape + (4,))


def _shade_with_palette(agg, palette=Viridis256):
    """Shade with a specific palette; returns uint8 RGBA."""
    img = ds_tf.shade(agg, cmap=palette)
    if hasattr(img, "data"):
        arr32 = np.asarray(img.data)
    elif hasattr(img, "values"):
        arr32 = img.values
    else:
        arr32 = np.asarray(img)
    return arr32.view(np.uint8).reshape(arr32.shape + (4,))


def _stack_scatter(x_da, y_da):
    """Stack two DataArrays into a flat DataFrame, dropping NaN."""
    x_bc = x_da.broadcast_like(y_da)
    stacked = xr.Dataset({"x": x_bc, "y": y_da}).stack(
        sample=list(y_da.dims)
    )
    return stacked.to_dataframe()[["x", "y"]].dropna()


def _scatter_agg(df, reduction=None):
    """Run Datashader scatter aggregation, return (agg, x_range, y_range)."""
    if reduction is None:
        reduction = ds_agg.mean("y")
    cvs = ds.Canvas(plot_width=PLOT_W, plot_height=PLOT_H)
    agg = cvs.points(df, "x", "y", agg=reduction)
    return agg


def _raster_agg(grid_2d, y_dim, x_dim):
    """Run Datashader raster aggregation on a 2D DataArray."""
    if grid_2d.dims != (y_dim, x_dim):
        grid_2d = grid_2d.transpose(y_dim, x_dim)
    cvs = ds.Canvas(plot_width=PLOT_W, plot_height=PLOT_H)
    return cvs.raster(grid_2d, agg=ds_agg.mean())


# ---------------------------------------------------------------------------
# Bokeh figure builder
# ---------------------------------------------------------------------------

def _rgba_to_bokeh_image(rgba, x_range, y_range):
    """
    Convert (H, W, 4) uint8 RGBA array to a Bokeh-compatible uint32 array.

    Bokeh's image_rgba glyph expects a 2D array of uint32 packed ARGB values.
    Datashader produces RGBA uint8; we repack as uint32 with correct byte order.
    """
    # Repack uint8 RGBA → uint32 (each pixel: R|G|B|A packed as 0xAARRGGBB
    # in little-endian which is what Bokeh expects as RGBA uint32)
    r = rgba[..., 0].astype(np.uint32)
    g = rgba[..., 1].astype(np.uint32)
    b = rgba[..., 2].astype(np.uint32)
    a = rgba[..., 3].astype(np.uint32)
    packed = (a << 24) | (r << 16) | (g << 8) | b
    return packed


def _make_figure(title, x_label, y_label, x_range, y_range,
                 tooltips=None, width=PLOT_W, height=PLOT_H):
    """Create a Bokeh figure with standard toolbar and hover tool."""
    tools = "pan,wheel_zoom,reset,save"
    p = figure(
        title=title,
        width=width, height=height,
        x_range=Range1d(*x_range),
        y_range=Range1d(*y_range),
        tools=tools,
        toolbar_location="above",
    )
    p.xaxis.axis_label = x_label
    p.yaxis.axis_label = y_label
    p.title.text_font_size = "11pt"
    p.background_fill_color = "#1a1a2e"  # dark background for astronomy plots
    p.border_fill_color = "#16213e"
    p.axis.axis_label_text_color = "#e0e0e0"
    p.axis.major_label_text_color = "#e0e0e0"
    p.axis.axis_line_color = "#444466"
    p.axis.major_tick_line_color = "#444466"
    p.grid.grid_line_color = "#2a2a4a"
    p.title.text_color = "#e0e0e0"

    if tooltips:
        hover = HoverTool(tooltips=tooltips)
        p.add_tools(hover)
    return p


def _add_image_rgba(fig, rgba, x_range, y_range):
    """Add a Datashader uint8 RGBA image as a Bokeh image_rgba glyph."""
    packed = _rgba_to_bokeh_image(rgba, x_range, y_range)
    dw = x_range[1] - x_range[0]
    dh = y_range[1] - y_range[0]
    fig.image_rgba(
        image=[packed],
        x=x_range[0], y=y_range[0],
        dw=dw, dh=dh,
    )


def _agg_x_range(agg):
    """Extract (min, max) of the x coordinate from a Datashader agg DataArray."""
    x_dim = agg.dims[1]
    x_vals = agg.coords[x_dim].values
    return (float(x_vals.min()), float(x_vals.max()))


def _agg_y_range(agg):
    """Extract (min, max) of the y coordinate from a Datashader agg DataArray."""
    y_dim = agg.dims[0]
    y_vals = agg.coords[y_dim].values
    return (float(y_vals.min()), float(y_vals.max()))


# ---------------------------------------------------------------------------
# Individual plot builders
# ---------------------------------------------------------------------------

def build_scatter_amp_vs_time(ds_part):
    """
    Row 1: Amplitude vs Time scatter, XX and YY overlaid.

    Two separate Datashader passes (one per polarization) are composited
    using ds_tf.stack() — XX in blue-green, YY in orange-red.
    """
    print("  Building: amp vs time (XX+YY overlay)...")
    vis  = ds_part["VISIBILITY"]
    flag = _flag_mask(ds_part)
    pols = ds_part.coords["polarization"].values
    times = ds_part.coords["time"].values

    palettes = [
        list(reversed(["#00ffff", "#00cccc", "#009999", "#006666"])),
        list(reversed(["#ff6600", "#cc5500", "#993300", "#661100"])),
    ]

    # Use first polarization for range calculation
    pol0 = pols[0]
    amp0 = _amp(vis).where(~flag).sel(polarization=pol0)
    time_bc0 = ds_part.coords["time"].broadcast_like(amp0)
    df0 = _stack_scatter(time_bc0, amp0)
    agg0 = _scatter_agg(df0)

    x_range = _agg_x_range(agg0)
    # Extend y_range slightly for visibility
    finite0 = df0["y"].values[np.isfinite(df0["y"].values)]
    y_range = (max(0, finite0.min() * 0.9), finite0.max() * 1.1)

    # Format time as offset from first integration (seconds)
    t0 = float(times[0])
    x_label = f"Time (MJD seconds, offset from {t0:.1f})"

    tooltips = [("amp", "@image"), ("time", "$x"), ("baseline", "$y")]
    p = _make_figure(
        title=f"Amplitude vs Time — {' + '.join(str(pol) for pol in pols)} "
              f"({ds_part.sizes['time']} integrations, "
              f"{ds_part.sizes['baseline_id']} baselines, "
              f"{ds_part.sizes['frequency']} channels)",
        x_label="Time (MJD seconds)",
        y_label="Amplitude (Jy)",
        x_range=x_range,
        y_range=y_range,
    )

    # Build and composite images for each polarization
    images = []
    for pol, palette in zip(pols, palettes):
        amp_pol = _amp(vis).where(~flag).sel(polarization=pol)
        time_bc = ds_part.coords["time"].broadcast_like(amp_pol)
        df = _stack_scatter(time_bc, amp_pol)
        agg = _scatter_agg(df)
        rgba = _shade_with_palette(agg, palette=palette)
        images.append(rgba)
        _add_image_rgba(p, rgba, x_range, y_range)

    # Legend annotation
    for pol, palette in zip(pols, palettes):
        p.rect(x=0, y=0, width=0, height=0,
               color=palette[0], legend_label=str(pol))
    p.legend.label_text_color = "#e0e0e0"
    p.legend.background_fill_color = "#1a1a2e"
    p.legend.border_line_color = "#444466"

    print(f"    samples: {len(df0):,} (per pol), x_range={x_range}, y_range={y_range}")
    return p


def build_scatter_amp_vs_uvdist(ds_part):
    """
    Row 2: Amplitude vs UV-distance (metres), XX polarization.
    """
    print("  Building: amp vs uvdist...")
    vis  = ds_part["VISIBILITY"]
    flag = _flag_mask(ds_part)
    pol  = ds_part.coords["polarization"].values[0]

    amp_pol = _amp(vis).where(~flag).sel(polarization=pol)
    uvw     = ds_part["UVW"]
    u = uvw.sel(uvw_label="u")
    v = uvw.sel(uvw_label="v")
    uvdist  = np.sqrt(u**2 + v**2)
    uvdist_bc = uvdist.broadcast_like(amp_pol)

    df  = _stack_scatter(uvdist_bc, amp_pol)
    agg = _scatter_agg(df)
    rgba = _shade_with_palette(agg, palette=Viridis256)

    x_range = _agg_x_range(agg)
    finite = df["y"].values[np.isfinite(df["y"].values)]
    y_range = (max(0, finite.min() * 0.9), finite.max() * 1.1)

    p = _make_figure(
        title=f"Amplitude vs UV-Distance — {pol} polarization",
        x_label="UV-Distance (metres)",
        y_label="Amplitude (Jy)",
        x_range=x_range,
        y_range=y_range,
    )
    _add_image_rgba(p, rgba, x_range, y_range)
    print(f"    samples: {len(df):,}, uvdist range: {x_range}")
    return p


def build_scatter_phase_vs_time(ds_part):
    """
    Row 3: Phase vs Time, XX polarization.
    """
    print("  Building: phase vs time...")
    vis  = ds_part["VISIBILITY"]
    flag = _flag_mask(ds_part)
    pol  = ds_part.coords["polarization"].values[0]

    phase_pol = _phase_deg(vis).where(~flag).sel(polarization=pol)
    time_bc   = ds_part.coords["time"].broadcast_like(phase_pol)

    df  = _stack_scatter(time_bc, phase_pol)
    agg = _scatter_agg(df)
    rgba = _shade_with_palette(agg, palette=Plasma256)

    x_range = _agg_x_range(agg)
    y_range = (-185, 185)

    p = _make_figure(
        title=f"Phase vs Time — {pol} polarization",
        x_label="Time (MJD seconds)",
        y_label="Phase (degrees)",
        x_range=x_range,
        y_range=y_range,
    )
    _add_image_rgba(p, rgba, x_range, y_range)
    print(f"    samples: {len(df):,}")
    return p


def build_scatter_uv_coverage(ds_part):
    """
    Row 4: UV-coverage (U vs V) with conjugate baselines.
    """
    print("  Building: UV coverage...")
    import pandas as pd

    uvw = ds_part["UVW"]
    u   = uvw.sel(uvw_label="u").compute().values.ravel()
    v   = uvw.sel(uvw_label="v").compute().values.ravel()
    finite = np.isfinite(u) & np.isfinite(v)
    u, v = u[finite], v[finite]
    u_full = np.concatenate([u, -u])
    v_full = np.concatenate([v, -v])
    df = pd.DataFrame({"x": u_full, "y": v_full})

    cvs  = ds.Canvas(plot_width=PLOT_W, plot_height=PLOT_H)
    agg  = cvs.points(df, "x", "y", agg=ds_agg.count())
    rgba = _shade_with_palette(agg, palette=Viridis256)

    x_range = (float(u_full.min()) * 1.05, float(u_full.max()) * 1.05)
    y_range = (float(v_full.min()) * 1.05, float(v_full.max()) * 1.05)

    p = _make_figure(
        title="UV-Coverage (with conjugate baselines)",
        x_label="U (metres)",
        y_label="V (metres)",
        x_range=x_range,
        y_range=y_range,
    )
    _add_image_rgba(p, rgba, x_range, y_range)
    print(f"    points: {len(df):,} (including conjugates)")
    return p


def build_raster_time_baseline_amp(ds_part):
    """
    Row 5: Time × Baseline raster, channel-averaged amplitude.

    The msview analog — the primary 2D diagnostic plot.
    Includes a HoverTool that reads the float64 agg value.
    """
    print("  Building: time × baseline raster (amplitude)...")
    vis  = ds_part["VISIBILITY"]
    flag = _flag_mask(ds_part)
    pol  = ds_part.coords["polarization"].values[0]

    amp_pol  = _amp(vis).where(~flag).sel(polarization=pol)
    chan_avg  = amp_pol.mean(dim="frequency", skipna=True).compute()
    grid     = chan_avg.transpose("time", "baseline_id")

    agg  = _raster_agg(grid, y_dim="time", x_dim="baseline_id")
    rgba = _shade_with_palette(agg, palette=Viridis256)

    x_range = _agg_x_range(agg)
    y_range = _agg_y_range(agg)

    # Store agg values for hover — map canvas pixel → float64 value
    # Bokeh image_rgba doesn't natively support float hover, but we can
    # overlay a transparent image glyph carrying the float data
    finite_vals = agg.values[np.isfinite(agg.values)]
    vmin, vmax = float(finite_vals.min()), float(finite_vals.max())

    tooltips = [
        ("baseline_id", "$x{0.0}"),
        ("time (MJD s)", "$y{0.0f}"),
        ("amp (Jy)", "@image{0.000}"),
    ]
    p = _make_figure(
        title=f"Time × Baseline — channel-averaged amplitude, {pol} "
              f"(amp range: [{vmin:.2f}, {vmax:.2f}] Jy)",
        x_label="Baseline ID",
        y_label="Time (MJD seconds)",
        x_range=x_range,
        y_range=y_range,
        tooltips=tooltips,
    )
    _add_image_rgba(p, rgba, x_range, y_range)

    # Second image overlay: the float64 agg as a transparent Bokeh Image
    # glyph for HoverTool to read (the two-layer design in action)
    agg_display = agg.values.copy()
    agg_display[np.isnan(agg_display)] = np.nan  # keep NaN for transparency
    dw = x_range[1] - x_range[0]
    dh = y_range[1] - y_range[0]
    # Add a transparent image carrying the float values for hover
    mapper = LinearColorMapper(
        palette=Viridis256, low=vmin, high=vmax, nan_color=(0, 0, 0, 0)
    )
    p.image(
        image=[agg_display],
        x=x_range[0], y=y_range[0],
        dw=dw, dh=dh,
        color_mapper=mapper,
        alpha=0.0,  # invisible — only here for the HoverTool
    )
    # Add colorbar
    color_bar = ColorBar(
        color_mapper=mapper,
        ticker=BasicTicker(),
        label_standoff=8,
        border_line_color=None,
        location=(0, 0),
        title="Amp (Jy)",
        title_text_color="#e0e0e0",
        major_label_text_color="#e0e0e0",
    )
    p.add_layout(color_bar, "right")
    print(f"    grid: {grid.shape}, amp range: [{vmin:.3f}, {vmax:.3f}] Jy")
    return p


def build_raster_waterfall(ds_part):
    """
    Row 6: Frequency × Time waterfall for a single non-padded baseline.
    """
    print("  Building: frequency × time waterfall...")
    eit_t0   = ds_part["EFFECTIVE_INTEGRATION_TIME"].isel(time=0).compute()
    valid_bl = int(np.where(np.isfinite(eit_t0.values))[0][0])
    pol      = ds_part.coords["polarization"].values[0]

    vis_bl   = ds_part["VISIBILITY"].isel(baseline_id=valid_bl)
    flag_bl  = _flag_mask(ds_part).isel(baseline_id=valid_bl)
    amp_bl   = _amp(vis_bl).where(~flag_bl).sel(polarization=pol).compute()
    # amp_bl dims: (time, frequency)

    agg  = _raster_agg(amp_bl, y_dim="time", x_dim="frequency")
    rgba = _shade_with_palette(agg, palette=Plasma256)

    freqs_GHz = ds_part.coords["frequency"].values / 1e9
    x_range   = (float(freqs_GHz.min()), float(freqs_GHz.max()))
    y_range   = _agg_y_range(agg)

    finite_vals = agg.values[np.isfinite(agg.values)]
    vmin, vmax  = float(finite_vals.min()), float(finite_vals.max())

    ant1 = ds_part.coords["baseline_antenna1_name"].values[valid_bl]
    ant2 = ds_part.coords["baseline_antenna2_name"].values[valid_bl]

    p = _make_figure(
        title=f"RFI Waterfall — baseline {ant1}–{ant2}, {pol} "
              f"(amp range: [{vmin:.2f}, {vmax:.2f}] Jy)",
        x_label="Frequency (GHz)",
        y_label="Time (MJD seconds)",
        x_range=x_range,
        y_range=y_range,
    )
    _add_image_rgba(p, rgba, x_range, y_range)

    mapper = LinearColorMapper(
        palette=Plasma256, low=vmin, high=vmax, nan_color=(0, 0, 0, 0)
    )
    color_bar = ColorBar(
        color_mapper=mapper,
        ticker=BasicTicker(),
        label_standoff=8,
        border_line_color=None,
        location=(0, 0),
        title="Amp (Jy)",
        title_text_color="#e0e0e0",
        major_label_text_color="#e0e0e0",
    )
    p.add_layout(color_bar, "right")
    print(f"    baseline: {ant1}–{ant2}, grid: {amp_bl.shape}")
    return p


def build_raster_flag_overlay(ds_part):
    """
    Row 7: Time × Baseline with flag fraction overlay.

    Two Datashader images composited:
    - Background: channel-averaged amplitude (Viridis)
    - Overlay: flag fraction > 0 regions in semi-transparent red
      (demonstrates the two-layer compositing path for flag visualisation)
    """
    print("  Building: time × baseline with flag overlay...")
    vis  = ds_part["VISIBILITY"]
    flag = _flag_mask(ds_part)
    pol  = ds_part.coords["polarization"].values[0]
    eit  = ds_part["EFFECTIVE_INTEGRATION_TIME"]

    # Layer 1: amplitude background
    amp_pol  = _amp(vis).where(~flag).sel(polarization=pol)
    chan_avg  = amp_pol.mean(dim="frequency", skipna=True).compute()
    grid_amp = chan_avg.transpose("time", "baseline_id")

    agg_amp  = _raster_agg(grid_amp, y_dim="time", x_dim="baseline_id")
    rgba_amp = _shade_with_palette(agg_amp, palette=Viridis256)

    # Layer 2: flag fraction overlay
    flag_frac = flag.mean(dim=["frequency", "polarization"]).where(
        np.isfinite(eit)
    ).compute()
    grid_flag = flag_frac.transpose("time", "baseline_id")
    agg_flag  = _raster_agg(grid_flag, y_dim="time", x_dim="baseline_id")

    # Create red overlay: only where flag fraction > 0
    flag_vals = agg_flag.values.copy()
    flag_mask_2d = np.isfinite(flag_vals) & (flag_vals > 0)

    # Build manual red RGBA overlay
    flag_rgba = np.zeros((PLOT_H, PLOT_W, 4), dtype=np.uint8)
    # Datashader canvas maps data coords to pixel coords in the agg array
    # We reuse the agg_flag array to find which pixels have flags
    # (the agg and rgba arrays have the same H×W layout)
    if flag_mask_2d.any():
        flag_rgba[flag_mask_2d, 0] = 220   # R
        flag_rgba[flag_mask_2d, 1] = 50    # G
        flag_rgba[flag_mask_2d, 2] = 50    # B
        flag_rgba[flag_mask_2d, 3] = 180   # A (semi-transparent)

    x_range = _agg_x_range(agg_amp)
    y_range = _agg_y_range(agg_amp)

    finite_vals = agg_amp.values[np.isfinite(agg_amp.values)]
    vmin, vmax  = float(finite_vals.min()), float(finite_vals.max())
    n_flagged   = int(flag_mask_2d.sum())

    p = _make_figure(
        title=f"Time × Baseline — amplitude + flag overlay "
              f"({n_flagged} flagged pixels shown in red)",
        x_label="Baseline ID",
        y_label="Time (MJD seconds)",
        x_range=x_range,
        y_range=y_range,
    )

    # Add amplitude background first
    _add_image_rgba(p, rgba_amp, x_range, y_range)

    # Add flag overlay on top
    if flag_mask_2d.any():
        _add_image_rgba(p, flag_rgba, x_range, y_range)

    mapper = LinearColorMapper(
        palette=Viridis256, low=vmin, high=vmax, nan_color=(0, 0, 0, 0)
    )
    color_bar = ColorBar(
        color_mapper=mapper,
        ticker=BasicTicker(),
        label_standoff=8,
        border_line_color=None,
        location=(0, 0),
        title="Amp (Jy)",
        title_text_color="#e0e0e0",
        major_label_text_color="#e0e0e0",
    )
    p.add_layout(color_bar, "right")
    n_flag_frac = float(np.nanmean(flag_frac.values))
    print(
        f"    grid: {grid_amp.shape}, "
        f"overall flag fraction: {n_flag_frac:.4f}, "
        f"flagged canvas pixels: {n_flagged}"
    )
    return p


# ---------------------------------------------------------------------------
# Main test
# ---------------------------------------------------------------------------

def test_bokeh_display():
    """
    Build all seven plot panels and save to msvis_plots.html.
    """
    _require_all()

    ms = _get_ms()
    print(f"\nOpening MS: {ms}")
    dt = open_ms(ms)
    ds_part = _largest_partition(dt)

    pols = ds_part.coords["polarization"].values
    print(
        f"Science target partition: "
        f"{ds_part.sizes['time']} integrations × "
        f"{ds_part.sizes['baseline_id']} baselines × "
        f"{ds_part.sizes['frequency']} channels × "
        f"{len(pols)} polarizations ({', '.join(str(p) for p in pols)})"
    )

    output_file(OUTPUT_HTML, title="msvis xarray-ms Datashader test plots")

    t0 = time_mod.perf_counter()

    header = Div(
        text=f"""
        <div style="background:#16213e; color:#e0e0e0; padding:12px;
                    font-family:monospace; font-size:12px;">
        <b>msvis test_10 — xarray-ms → Datashader → Bokeh</b><br>
        MS: {os.path.basename(ms)}<br>
        Partition: OBSERVE_TARGET,
        {ds_part.sizes['time']} integrations ×
        {ds_part.sizes['baseline_id']} baselines ×
        {ds_part.sizes['frequency']} channels ×
        {len(pols)} pols ({', '.join(str(p) for p in pols)})<br>
        Datashader {ds.__version__} &nbsp;|&nbsp;
        Bokeh {bokeh.__version__} &nbsp;|&nbsp;
        Canvas {PLOT_W}×{PLOT_H}
        </div>
        """,
        width=PLOT_W * 2 + 20,
    )

    # Build all panels
    p1 = build_scatter_amp_vs_time(ds_part)
    p2 = build_scatter_amp_vs_uvdist(ds_part)
    p3 = build_scatter_phase_vs_time(ds_part)
    p4 = build_scatter_uv_coverage(ds_part)
    p5 = build_raster_time_baseline_amp(ds_part)
    p6 = build_raster_waterfall(ds_part)
    p7 = build_raster_flag_overlay(ds_part)

    t_build = time_mod.perf_counter() - t0

    # Layout: 2-column grid, scatter on left column, raster on right
    scatter_col = Div(
        text='<div style="color:#aaa;font-family:monospace;padding:4px">'
             '<b>Scatter plots</b> (all baselines/channels stacked)</div>',
        width=PLOT_W,
    )
    raster_col = Div(
        text='<div style="color:#aaa;font-family:monospace;padding:4px">'
             '<b>Raster plots</b> (2D grid, channel or time averaged)</div>',
        width=PLOT_W,
    )

    layout = column(
        header,
        row(scatter_col, raster_col),
        row(p1, p5),
        row(p2, p6),
        row(p3, p7),
        row(p4),
    )

    save(layout)
    t_total = time_mod.perf_counter() - t0

    print(f"\n  Build time: {t_build:.2f}s, total (incl. save): {t_total:.2f}s")
    print(f"  Output: {os.path.abspath(OUTPUT_HTML)}")

    # Assertions
    assert os.path.exists(OUTPUT_HTML), "HTML output file not created"
    file_size_kb = os.path.getsize(OUTPUT_HTML) / 1024
    assert file_size_kb > 10, f"HTML file suspiciously small: {file_size_kb:.1f} KB"
    print(f"  HTML file size: {file_size_kb:.0f} KB")
    print(f"\n  Open with: open {OUTPUT_HTML}")


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
    warnings.filterwarnings(
        "ignore",
        message="omp_set_nested",
    )

    print(f"\n--- test_bokeh_display ---")
    try:
        test_bokeh_display()
        print("  PASS")
    except Exception as exc:
        import traceback
        print(f"  FAIL: {exc}")
        traceback.print_exc()
