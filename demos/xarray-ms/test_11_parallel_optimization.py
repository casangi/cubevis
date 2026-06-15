"""
test_11_parallel_optimization.py — Parallel access and optimization tests.

Tests the most promising parallelization strategies for the msvis scatter
pipeline, verifying both that arcae supports concurrent access and that
the optimized outputs are numerically identical to the serial baseline.

Each optimization strategy is timed against a serial baseline and produces
a side-by-side Bokeh HTML page comparing serial vs parallel outputs so the
correctness of the rendered images can be inspected visually.

Output: msvis_parallel_comparison.html

Strategies tested:

  1. FUSED DASK COMPUTE
     All derived quantities (amp_XX, amp_YY, phase_XX, uvdist) computed in
     a single dask.compute() call instead of four sequential .compute() calls.
     Measures whether VISIBILITY is read once or four times.

  2. DIRECT NUMPY RAVEL (no DataFrame MultiIndex)
     Replace xarray stack → to_dataframe() with direct .values.ravel() +
     np.isfinite() mask.  Avoids MultiIndex construction overhead.

  3. PARALLEL DATASHADER PASSES (ThreadPoolExecutor)
     After data is in memory, run four cvs.points() + shade() calls
     concurrently in threads.  Tests whether Datashader/numba releases the
     GIL during aggregation.

  4. COMBINED (fused compute + direct numpy + parallel Datashader)
     All three strategies together — expected to give the largest speedup.

  5. ARCAE CONCURRENT READ TEST
     Open the same MS in two independent open_datatree() calls and read
     VISIBILITY from both simultaneously in threads.  Directly tests whether
     arcae supports concurrent table reads without data corruption.

Each test:
  - Records wall-clock time for the full pipeline (read → DataFrame → Datashader → RGBA)
  - Asserts pixel-level identity between optimized and serial RGBA output
  - Adds its panel to the comparison HTML

Usage:
    MS=sis14_twhya_calibrated_flagged.ms python test_11_parallel_optimization.py
"""

import os
import time as time_mod
import warnings
import threading
import numpy as np
import pandas as pd
import xarray as xr
import xarray_ms  # noqa: F401
import dask
from concurrent.futures import ThreadPoolExecutor, as_completed

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
    from bokeh.models import Div, Range1d
    from bokeh.palettes import Viridis256, Plasma256, Inferno256
    HAS_BOKEH = True
except ImportError:
    HAS_BOKEH = False

C_MS = 299_792_458.0
PLOT_W = 500
PLOT_H = 350
OUTPUT_HTML = "msvis_parallel_comparison.html"

# Reduce channel count so tests run in reasonable time but still exercise
# the full baseline×time grid
N_CHAN = 48    # one SPW worth of channels (1/8th of 384)
N_TIME = 270   # all integrations in the science target


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------

def _require_all():
    if not HAS_DATASHADER:
        raise RuntimeError("pip install datashader")
    if not HAS_BOKEH:
        raise RuntimeError("pip install bokeh")


# ---------------------------------------------------------------------------
# MS helpers
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
        vis, dask="parallelized", output_dtypes=[float],
    )


# ---------------------------------------------------------------------------
# Datashader helpers
# ---------------------------------------------------------------------------

def _shade_rgba(agg, palette=Viridis256):
    img = ds_tf.shade(agg, cmap=palette)
    arr32 = np.asarray(img.data if hasattr(img, "data") else
                       img.values if hasattr(img, "values") else img)
    return arr32.view(np.uint8).reshape(arr32.shape + (4,))


def _scatter_from_df(df, palette=Viridis256):
    """DataFrame → Datashader aggregation → uint8 RGBA."""
    cvs = ds.Canvas(plot_width=PLOT_W, plot_height=PLOT_H)
    agg = cvs.points(df, "x", "y", agg=ds_agg.mean("y"))
    return agg, _shade_rgba(agg, palette)


def _rgba_x_range(agg):
    x_dim = agg.dims[1]
    v = agg.coords[x_dim].values
    return float(v.min()), float(v.max())


def _rgba_y_range(agg):
    y_dim = agg.dims[0]
    v = agg.coords[y_dim].values
    return float(v.min()), float(v.max())


# ---------------------------------------------------------------------------
# Bokeh helpers
# ---------------------------------------------------------------------------

def _bokeh_img_from_rgba(rgba, x_range, y_range):
    """uint8 RGBA → uint32 packed for Bokeh image_rgba."""
    r = rgba[..., 0].astype(np.uint32)
    g = rgba[..., 1].astype(np.uint32)
    b = rgba[..., 2].astype(np.uint32)
    a = rgba[..., 3].astype(np.uint32)
    return (a << 24) | (r << 16) | (g << 8) | b


def _make_figure(title, x_label, y_label, x_range, y_range):
    p = figure(
        title=title, width=PLOT_W, height=PLOT_H,
        x_range=Range1d(*x_range), y_range=Range1d(*y_range),
        tools="pan,wheel_zoom,reset,save", toolbar_location="above",
    )
    p.xaxis.axis_label = x_label
    p.yaxis.axis_label = y_label
    p.title.text_font_size = "10pt"
    p.background_fill_color = "#1a1a2e"
    p.border_fill_color = "#16213e"
    p.axis.axis_label_text_color = "#c0c0c0"
    p.axis.major_label_text_color = "#c0c0c0"
    p.grid.grid_line_color = "#2a2a4a"
    p.title.text_color = "#e0e0e0"
    return p


def _add_rgba(p, rgba, x_range, y_range):
    packed = _bokeh_img_from_rgba(rgba, x_range, y_range)
    dw = x_range[1] - x_range[0]
    dh = y_range[1] - y_range[0]
    p.image_rgba(image=[packed], x=x_range[0], y=y_range[0], dw=dw, dh=dh)


def _timing_label(label, t_serial, t_opt, match):
    colour = "#00ff88" if match else "#ff4444"
    speedup = t_serial / t_opt if t_opt > 0 else float("inf")
    status  = "✓ pixel-identical" if match else "✗ MISMATCH"
    return Div(
        text=f"""
        <div style="background:#16213e;color:#c0c0e0;padding:8px;
                    font-family:monospace;font-size:11px;border-left:3px solid {colour}">
        <b>{label}</b><br>
        Serial: {t_serial:.2f}s &nbsp;|&nbsp;
        Optimized: {t_opt:.2f}s &nbsp;|&nbsp;
        Speedup: <b>{speedup:.1f}×</b> &nbsp;|&nbsp;
        <span style="color:{colour}">{status}</span>
        </div>""",
        width=PLOT_W * 2 + 10,
    )


# ---------------------------------------------------------------------------
# SERIAL BASELINE
# The reference implementation from test_10 — xarray stack → to_dataframe
# → sequential Datashader passes.
# ---------------------------------------------------------------------------

def _serial_pipeline(ds_part):
    """
    Serial baseline: four sequential scatter plots.
    Returns (elapsed_seconds, list of (agg, rgba) tuples, list of DataFrames).
    """
    vis  = ds_part["VISIBILITY"]
    flag = _flag_mask(ds_part)
    pols = ds_part.coords["polarization"].values

    def _stack_df(x_da, y_da):
        x_bc = x_da.broadcast_like(y_da)
        stacked = xr.Dataset({"x": x_bc, "y": y_da}).stack(
            sample=list(y_da.dims)
        )
        return stacked.to_dataframe()[["x", "y"]].dropna()

    t0 = time_mod.perf_counter()

    # amp XX
    amp_xx = _amp(vis).where(~flag).sel(polarization=pols[0])
    time_bc = ds_part.coords["time"].broadcast_like(amp_xx)
    df_amp_xx = _stack_df(time_bc, amp_xx)
    agg_amp_xx, rgba_amp_xx = _scatter_from_df(df_amp_xx, Viridis256)

    # amp YY
    amp_yy = _amp(vis).where(~flag).sel(polarization=pols[1])
    time_bc2 = ds_part.coords["time"].broadcast_like(amp_yy)
    df_amp_yy = _stack_df(time_bc2, amp_yy)
    agg_amp_yy, rgba_amp_yy = _scatter_from_df(df_amp_yy, Inferno256)

    # phase XX
    phase_xx = _phase_deg(vis).where(~flag).sel(polarization=pols[0])
    time_bc3 = ds_part.coords["time"].broadcast_like(phase_xx)
    df_phase_xx = _stack_df(time_bc3, phase_xx)
    agg_phase_xx, rgba_phase_xx = _scatter_from_df(df_phase_xx, Plasma256)

    # uvdist vs amp XX
    uvw = ds_part["UVW"]
    uvdist = np.sqrt(uvw.sel(uvw_label="u")**2 + uvw.sel(uvw_label="v")**2)
    uvdist_bc = uvdist.broadcast_like(amp_xx)
    amp_xx2 = _amp(vis).where(~flag).sel(polarization=pols[0])
    df_uvdist = _stack_df(uvdist_bc, amp_xx2)
    agg_uvdist, rgba_uvdist = _scatter_from_df(df_uvdist, Viridis256)

    elapsed = time_mod.perf_counter() - t0

    results = [
        (agg_amp_xx,   rgba_amp_xx,   df_amp_xx,   "Amp XX vs Time"),
        (agg_amp_yy,   rgba_amp_yy,   df_amp_yy,   "Amp YY vs Time"),
        (agg_phase_xx, rgba_phase_xx, df_phase_xx, "Phase XX vs Time"),
        (agg_uvdist,   rgba_uvdist,   df_uvdist,   "Amp XX vs UVdist"),
    ]
    return elapsed, results


# ---------------------------------------------------------------------------
# OPTIMIZATION 1: FUSED DASK COMPUTE
# All lazy quantities computed in one dask.compute() call so VISIBILITY
# is read only once.
# ---------------------------------------------------------------------------

def _fused_pipeline(ds_part):
    """
    Optimization 1: single dask.compute() for all derived quantities.
    """
    vis  = ds_part["VISIBILITY"]
    flag = _flag_mask(ds_part)
    pols = ds_part.coords["polarization"].values
    uvw  = ds_part["UVW"]
    uvdist = np.sqrt(uvw.sel(uvw_label="u")**2 + uvw.sel(uvw_label="v")**2)

    # Build all lazy arrays
    amp_xx    = _amp(vis).where(~flag).sel(polarization=pols[0])
    amp_yy    = _amp(vis).where(~flag).sel(polarization=pols[1])
    phase_xx  = _phase_deg(vis).where(~flag).sel(polarization=pols[0])
    time_coord = ds_part.coords["time"]
    time_bc   = time_coord.broadcast_like(amp_xx)
    uvdist_bc = uvdist.broadcast_like(amp_xx)

    t0 = time_mod.perf_counter()

    # Single fused compute — VISIBILITY, FLAG read once
    (amp_xx_r, amp_yy_r, phase_xx_r,
     time_r, uvdist_r) = dask.compute(
        amp_xx, amp_yy, phase_xx, time_bc, uvdist_bc
    )

    # Sequential DataFrame + Datashader (not yet parallel)
    def _df(x_arr, y_arr):
        x_flat = x_arr.values.ravel()
        y_flat = y_arr.values.ravel()
        ok = np.isfinite(x_flat) & np.isfinite(y_flat)
        return pd.DataFrame({"x": x_flat[ok], "y": y_flat[ok]})

    df_amp_xx   = _df(time_r, amp_xx_r)
    df_amp_yy   = _df(time_r, amp_yy_r)
    df_phase_xx = _df(time_r, phase_xx_r)
    df_uvdist   = _df(uvdist_r, amp_xx_r)

    agg_amp_xx,   rgba_amp_xx   = _scatter_from_df(df_amp_xx,   Viridis256)
    agg_amp_yy,   rgba_amp_yy   = _scatter_from_df(df_amp_yy,   Inferno256)
    agg_phase_xx, rgba_phase_xx = _scatter_from_df(df_phase_xx, Plasma256)
    agg_uvdist,   rgba_uvdist   = _scatter_from_df(df_uvdist,   Viridis256)

    elapsed = time_mod.perf_counter() - t0

    results = [
        (agg_amp_xx,   rgba_amp_xx,   df_amp_xx,   "Amp XX vs Time"),
        (agg_amp_yy,   rgba_amp_yy,   df_amp_yy,   "Amp YY vs Time"),
        (agg_phase_xx, rgba_phase_xx, df_phase_xx, "Phase XX vs Time"),
        (agg_uvdist,   rgba_uvdist,   df_uvdist,   "Amp XX vs UVdist"),
    ]
    return elapsed, results


# ---------------------------------------------------------------------------
# OPTIMIZATION 2: DIRECT NUMPY RAVEL
# Replaces xarray stack → to_dataframe() with direct .values.ravel() +
# np.isfinite().  Avoids MultiIndex construction overhead.
# Also uses fused Dask compute.
# ---------------------------------------------------------------------------

def _numpy_ravel_pipeline(ds_part):
    """
    Optimization 2: fused compute + direct numpy ravel (no MultiIndex).
    """
    vis  = ds_part["VISIBILITY"]
    flag = _flag_mask(ds_part)
    pols = ds_part.coords["polarization"].values
    uvw  = ds_part["UVW"]
    uvdist = np.sqrt(uvw.sel(uvw_label="u")**2 + uvw.sel(uvw_label="v")**2)

    amp_xx   = _amp(vis).where(~flag).sel(polarization=pols[0])
    amp_yy   = _amp(vis).where(~flag).sel(polarization=pols[1])
    phase_xx = _phase_deg(vis).where(~flag).sel(polarization=pols[0])
    time_bc  = ds_part.coords["time"].broadcast_like(amp_xx)
    uvdist_bc = uvdist.broadcast_like(amp_xx)

    t0 = time_mod.perf_counter()

    (amp_xx_r, amp_yy_r, phase_xx_r,
     time_r, uvdist_r) = dask.compute(
        amp_xx, amp_yy, phase_xx, time_bc, uvdist_bc
    )

    def _ravel_df(x_arr, y_arr):
        x_flat = x_arr.values.ravel()
        y_flat = y_arr.values.ravel()
        ok = np.isfinite(x_flat) & np.isfinite(y_flat)
        # Pre-allocate to avoid pandas copy overhead
        return pd.DataFrame(
            {"x": x_flat[ok], "y": y_flat[ok]},
            copy=False,
        )

    df_amp_xx   = _ravel_df(time_r, amp_xx_r)
    df_amp_yy   = _ravel_df(time_r, amp_yy_r)
    df_phase_xx = _ravel_df(time_r, phase_xx_r)
    df_uvdist   = _ravel_df(uvdist_r, amp_xx_r)

    agg_amp_xx,   rgba_amp_xx   = _scatter_from_df(df_amp_xx,   Viridis256)
    agg_amp_yy,   rgba_amp_yy   = _scatter_from_df(df_amp_yy,   Inferno256)
    agg_phase_xx, rgba_phase_xx = _scatter_from_df(df_phase_xx, Plasma256)
    agg_uvdist,   rgba_uvdist   = _scatter_from_df(df_uvdist,   Viridis256)

    elapsed = time_mod.perf_counter() - t0

    results = [
        (agg_amp_xx,   rgba_amp_xx,   df_amp_xx,   "Amp XX vs Time"),
        (agg_amp_yy,   rgba_amp_yy,   df_amp_yy,   "Amp YY vs Time"),
        (agg_phase_xx, rgba_phase_xx, df_phase_xx, "Phase XX vs Time"),
        (agg_uvdist,   rgba_uvdist,   df_uvdist,   "Amp XX vs UVdist"),
    ]
    return elapsed, results


# ---------------------------------------------------------------------------
# OPTIMIZATION 3: PARALLEL DATASHADER PASSES
# After fused compute + ravel, run the four cvs.points() + shade() calls
# concurrently in a ThreadPoolExecutor.
# ---------------------------------------------------------------------------

def _parallel_datashader_pipeline(ds_part):
    """
    Optimization 3: fused compute + numpy ravel + parallel Datashader.
    """
    vis  = ds_part["VISIBILITY"]
    flag = _flag_mask(ds_part)
    pols = ds_part.coords["polarization"].values
    uvw  = ds_part["UVW"]
    uvdist = np.sqrt(uvw.sel(uvw_label="u")**2 + uvw.sel(uvw_label="v")**2)

    amp_xx   = _amp(vis).where(~flag).sel(polarization=pols[0])
    amp_yy   = _amp(vis).where(~flag).sel(polarization=pols[1])
    phase_xx = _phase_deg(vis).where(~flag).sel(polarization=pols[0])
    time_bc  = ds_part.coords["time"].broadcast_like(amp_xx)
    uvdist_bc = uvdist.broadcast_like(amp_xx)

    t0 = time_mod.perf_counter()

    (amp_xx_r, amp_yy_r, phase_xx_r,
     time_r, uvdist_r) = dask.compute(
        amp_xx, amp_yy, phase_xx, time_bc, uvdist_bc
    )

    def _ravel_df(x_arr, y_arr):
        x_flat = x_arr.values.ravel()
        y_flat = y_arr.values.ravel()
        ok = np.isfinite(x_flat) & np.isfinite(y_flat)
        return pd.DataFrame({"x": x_flat[ok], "y": y_flat[ok]}, copy=False)

    df_amp_xx   = _ravel_df(time_r, amp_xx_r)
    df_amp_yy   = _ravel_df(time_r, amp_yy_r)
    df_phase_xx = _ravel_df(time_r, phase_xx_r)
    df_uvdist   = _ravel_df(uvdist_r, amp_xx_r)

    # Parallel Datashader passes
    render_tasks = [
        (df_amp_xx,   Viridis256, "Amp XX vs Time"),
        (df_amp_yy,   Inferno256, "Amp YY vs Time"),
        (df_phase_xx, Plasma256,  "Phase XX vs Time"),
        (df_uvdist,   Viridis256, "Amp XX vs UVdist"),
    ]

    results_dict = {}
    with ThreadPoolExecutor(max_workers=4) as executor:
        future_to_label = {
            executor.submit(_scatter_from_df, df, pal): label
            for df, pal, label in render_tasks
        }
        for future in as_completed(future_to_label):
            label = future_to_label[future]
            results_dict[label] = future.result()

    elapsed = time_mod.perf_counter() - t0

    results = [
        (results_dict["Amp XX vs Time"][0],   results_dict["Amp XX vs Time"][1],
         df_amp_xx,   "Amp XX vs Time"),
        (results_dict["Amp YY vs Time"][0],   results_dict["Amp YY vs Time"][1],
         df_amp_yy,   "Amp YY vs Time"),
        (results_dict["Phase XX vs Time"][0], results_dict["Phase XX vs Time"][1],
         df_phase_xx, "Phase XX vs Time"),
        (results_dict["Amp XX vs UVdist"][0], results_dict["Amp XX vs UVdist"][1],
         df_uvdist,   "Amp XX vs UVdist"),
    ]
    return elapsed, results


# ---------------------------------------------------------------------------
# OPTIMIZATION 4: CONCURRENT ARCAE READ
# Open the MS twice independently and read VISIBILITY simultaneously
# in two threads.  Directly tests arcae concurrent read safety.
# ---------------------------------------------------------------------------

def test_arcae_concurrent_read():
    """
    Open the same MS in two independent DataTrees simultaneously in threads.

    Tests whether arcae's table reader supports concurrent access without
    data corruption, deadlock, or exceptions.

    Each thread reads VISIBILITY from the same partition and computes
    channel-averaged amplitude.  Results are compared for identity.
    """
    ms = _get_ms()

    results = {}
    errors  = {}

    def _read_partition(thread_id):
        try:
            dt = open_ms(ms)
            ds_part = _largest_partition(dt).isel(
                frequency=slice(0, N_CHAN)
            )
            vis  = ds_part["VISIBILITY"]
            flag = _flag_mask(ds_part)
            amp  = _amp(vis).where(~flag).sel(
                polarization=ds_part.coords["polarization"].values[0]
            )
            chan_avg = amp.mean(dim="frequency", skipna=True).compute()
            results[thread_id] = chan_avg.values
        except Exception as exc:
            errors[thread_id] = exc

    t0 = time_mod.perf_counter()
    threads = [threading.Thread(target=_read_partition, args=(i,))
               for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time_mod.perf_counter() - t0

    assert not errors, f"Thread errors: {errors}"
    assert 0 in results and 1 in results, "One or both threads returned no data"

    arr0, arr1 = results[0], results[1]
    max_diff = float(np.nanmax(np.abs(arr0 - arr1)))
    n_finite = int(np.isfinite(arr0).sum())

    print(
        f"  Concurrent read: elapsed={elapsed:.2f}s, "
        f"finite cells={n_finite}, "
        f"max |diff| between threads={max_diff:.2e}"
    )
    assert max_diff == 0.0, (
        f"arcae concurrent reads returned different values: max_diff={max_diff:.2e}"
    )
    return elapsed, max_diff


def test_arcae_concurrent_read_4threads():
    """
    Extend the concurrent read test to 4 simultaneous readers.
    All four results must be bit-identical.
    """
    ms = _get_ms()
    results = {}
    errors  = {}

    def _read(thread_id):
        try:
            dt = open_ms(ms)
            ds_part = _largest_partition(dt).isel(frequency=slice(0, N_CHAN))
            vis  = ds_part["VISIBILITY"]
            flag = _flag_mask(ds_part)
            amp  = _amp(vis).where(~flag).sel(
                polarization=ds_part.coords["polarization"].values[0]
            )
            results[thread_id] = amp.mean(dim="frequency", skipna=True).compute().values
        except Exception as exc:
            errors[thread_id] = exc

    t0 = time_mod.perf_counter()
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(_read, i): i for i in range(4)}
        for f in as_completed(futures):
            pass  # results collected via dict
    elapsed = time_mod.perf_counter() - t0

    assert not errors, f"Thread errors: {errors}"
    assert len(results) == 4, f"Only {len(results)}/4 threads returned data"

    ref = results[0]
    for i in range(1, 4):
        max_diff = float(np.nanmax(np.abs(ref - results[i])))
        assert max_diff == 0.0, (
            f"Thread {i} differs from thread 0: max_diff={max_diff:.2e}"
        )

    print(
        f"  4-thread concurrent read: elapsed={elapsed:.2f}s, "
        f"all results identical ✓"
    )
    return elapsed


# ---------------------------------------------------------------------------
# COMPARISON HELPER
# ---------------------------------------------------------------------------

def _compare_to_serial(serial_results, opt_results, tol_pixels=0):
    """
    Compare optimized RGBA outputs to serial baseline.

    Returns list of (label, match, n_diff_pixels) tuples.
    tol_pixels: allow this many pixels to differ (0 = strict identity).
    """
    comparisons = []
    for (s_agg, s_rgba, s_df, label), (o_agg, o_rgba, o_df, _) in zip(
        serial_results, opt_results
    ):
        diff = np.sum(s_rgba != o_rgba)
        # diff counts individual uint8 channel differences; pixels differ if
        # any of R,G,B,A differ
        pixel_diff = np.sum(np.any(s_rgba != o_rgba, axis=2))
        match = pixel_diff <= tol_pixels
        comparisons.append((label, match, int(pixel_diff)))
    return comparisons


# ---------------------------------------------------------------------------
# PLOT BUILDER
# ---------------------------------------------------------------------------

def _build_comparison_row(serial_results, opt_results, strategy_name,
                          t_serial, t_opt, comparisons):
    """
    Build a row of Bokeh figures: serial (left) vs optimized (right) for
    the first scatter plot (amp XX vs time) as representative.
    """
    s_agg, s_rgba, _, label = serial_results[0]
    o_agg, o_rgba, _, _     = opt_results[0]

    x_range = _rgba_x_range(s_agg)
    y_range = _rgba_y_range(s_agg)

    # Clamp y_range to sensible amplitude bounds
    finite = s_rgba[s_rgba[..., 3] > 0]
    y_range = (max(0, y_range[0]), y_range[1])

    label_overall, match_overall, _ = comparisons[0]

    p_serial = _make_figure(
        title=f"SERIAL — {label} ({t_serial:.1f}s total)",
        x_label="Time (MJD s)", y_label="Amplitude (Jy)",
        x_range=x_range, y_range=y_range,
    )
    _add_rgba(p_serial, s_rgba, x_range, y_range)

    colour = "#00cc66" if match_overall else "#ff4444"
    p_opt = _make_figure(
        title=f"{strategy_name} ({t_opt:.1f}s) — "
              f"{'✓ identical' if match_overall else '✗ differs'}",
        x_label="Time (MJD s)", y_label="Amplitude (Jy)",
        x_range=x_range, y_range=y_range,
    )
    p_opt.title.text_color = colour
    _add_rgba(p_opt, o_rgba, x_range, y_range)

    # Per-plot comparison details
    details = "<br>".join(
        f"{lbl}: {ndiff} pixel diffs — "
        f"{'✓' if m else '✗'}"
        for lbl, m, ndiff in comparisons
    )
    info = Div(
        text=f"""
        <div style="background:#16213e;color:#c0c0e0;padding:6px;
                    font-family:monospace;font-size:10px;">
        <b>{strategy_name}</b><br>
        Serial: {t_serial:.2f}s &nbsp;→&nbsp;
        Optimized: {t_opt:.2f}s &nbsp;|&nbsp;
        Speedup: <b>{t_serial/t_opt:.1f}×</b><br>
        {details}
        </div>""",
        width=PLOT_W * 2 + 10,
    )
    return info, row(p_serial, p_opt)


# ---------------------------------------------------------------------------
# MAIN TEST
# ---------------------------------------------------------------------------

def test_parallel_optimizations():
    """
    Run all four optimization strategies, compare to serial baseline,
    and produce the comparison HTML.
    """
    _require_all()

    ms = _get_ms()
    print(f"\nOpening MS: {ms}")
    dt = open_ms(ms)

    # Use a subset of channels to keep total time reasonable while still
    # exercising the full time×baseline grid
    ds_part = _largest_partition(dt).isel(frequency=slice(0, N_CHAN))
    pols    = ds_part.coords["polarization"].values
    n_samples_est = (ds_part.sizes["time"] * ds_part.sizes["baseline_id"] *
                     ds_part.sizes["frequency"])
    print(
        f"Partition slice: {ds_part.sizes['time']} time × "
        f"{ds_part.sizes['baseline_id']} baseline × "
        f"{ds_part.sizes['frequency']} freq = ~{n_samples_est/1e6:.1f}M samples/plot"
    )

    # --- Serial baseline ---
    print("\n[1/5] Running serial baseline...")
    t_serial, serial_results = _serial_pipeline(ds_part)
    print(f"  Serial: {t_serial:.2f}s")

    # --- Optimization 1: fused dask compute ---
    print("\n[2/5] Running fused Dask compute...")
    t_fused, fused_results = _fused_pipeline(ds_part)
    comp_fused = _compare_to_serial(serial_results, fused_results)
    print(f"  Fused: {t_fused:.2f}s  (speedup {t_serial/t_fused:.1f}×)")
    for label, match, ndiff in comp_fused:
        print(f"    {label}: {'✓ identical' if match else f'✗ {ndiff} pixel diffs'}")

    # --- Optimization 2: fused + numpy ravel ---
    print("\n[3/5] Running fused + numpy ravel...")
    t_ravel, ravel_results = _numpy_ravel_pipeline(ds_part)
    comp_ravel = _compare_to_serial(serial_results, ravel_results)
    print(f"  Ravel: {t_ravel:.2f}s  (speedup {t_serial/t_ravel:.1f}×)")
    for label, match, ndiff in comp_ravel:
        print(f"    {label}: {'✓ identical' if match else f'✗ {ndiff} pixel diffs'}")

    # --- Optimization 3: fused + ravel + parallel Datashader ---
    print("\n[4/5] Running fused + ravel + parallel Datashader...")
    t_par, par_results = _parallel_datashader_pipeline(ds_part)
    comp_par = _compare_to_serial(serial_results, par_results)
    print(f"  Parallel: {t_par:.2f}s  (speedup {t_serial/t_par:.1f}×)")
    for label, match, ndiff in comp_par:
        print(f"    {label}: {'✓ identical' if match else f'✗ {ndiff} pixel diffs'}")

    # --- Arcae concurrent read test ---
    print("\n[5/5] Testing arcae concurrent reads...")
    t_conc2, _ = test_arcae_concurrent_read()
    t_conc4    = test_arcae_concurrent_read_4threads()

    # --- Build HTML ---
    print("\nBuilding comparison HTML...")
    output_file(OUTPUT_HTML, title="msvis parallel optimization comparison")

    header = Div(
        text=f"""
        <div style="background:#16213e;color:#e0e0e0;padding:12px;
                    font-family:monospace;font-size:12px;">
        <b>msvis test_11 — Parallel Optimization Comparison</b><br>
        MS: {os.path.basename(ms)} &nbsp;|&nbsp;
        Partition slice: {ds_part.sizes['time']} time ×
        {ds_part.sizes['baseline_id']} baseline ×
        {ds_part.sizes['frequency']} freq
        ({n_samples_est/1e6:.1f}M samples/plot)<br>
        Datashader {ds.__version__} &nbsp;|&nbsp;
        Bokeh {bokeh.__version__} &nbsp;|&nbsp;
        Canvas {PLOT_W}×{PLOT_H}<br>
        arcae concurrent read (2 threads): {t_conc2:.2f}s ✓ &nbsp;|&nbsp;
        arcae concurrent read (4 threads): {t_conc4:.2f}s ✓
        </div>""",
        width=PLOT_W * 2 + 10,
    )

    col_labels = Div(
        text=f"""
        <div style="color:#888;font-family:monospace;font-size:10px;padding:4px">
        Left: SERIAL baseline ({t_serial:.1f}s) &nbsp;|&nbsp;
        Right: optimized strategy &nbsp;|&nbsp;
        Green title = pixel-identical to serial
        </div>""",
        width=PLOT_W * 2 + 10,
    )

    info1, row1 = _build_comparison_row(
        serial_results, fused_results,
        "OPT-1: Fused Dask compute", t_serial, t_fused, comp_fused
    )
    info2, row2 = _build_comparison_row(
        serial_results, ravel_results,
        "OPT-2: Fused + numpy ravel", t_serial, t_ravel, comp_ravel
    )
    info3, row3 = _build_comparison_row(
        serial_results, par_results,
        "OPT-3: Fused + ravel + parallel Datashader",
        t_serial, t_par, comp_par
    )

    # Summary table
    summary = Div(
        text=f"""
        <div style="background:#16213e;color:#e0e0e0;padding:10px;
                    font-family:monospace;font-size:11px;">
        <b>Performance summary</b><br><br>
        {'Strategy':<45} {'Time':>8} {'Speedup':>9} {'Identical':>12}<br>
        {'─'*76}<br>
        {'Serial (baseline)':<45} {t_serial:>7.2f}s {'1.0×':>9} {'—':>12}<br>
        {'OPT-1: Fused Dask compute':<45}
            {t_fused:>7.2f}s {f'{t_serial/t_fused:.1f}×':>9}
            {('✓' if all(m for _,m,_ in comp_fused) else '✗'):>12}<br>
        {'OPT-2: Fused + numpy ravel':<45}
            {t_ravel:>7.2f}s {f'{t_serial/t_ravel:.1f}×':>9}
            {('✓' if all(m for _,m,_ in comp_ravel) else '✗'):>12}<br>
        {'OPT-3: Fused + ravel + parallel DS':<45}
            {t_par:>7.2f}s {f'{t_serial/t_par:.1f}×':>9}
            {('✓' if all(m for _,m,_ in comp_par) else '✗'):>12}<br>
        <br>
        arcae 2-thread concurrent read: {t_conc2:.2f}s ✓<br>
        arcae 4-thread concurrent read: {t_conc4:.2f}s ✓
        </div>""",
        width=PLOT_W * 2 + 10,
    )

    layout = column(
        header, col_labels,
        info1, row1,
        info2, row2,
        info3, row3,
        summary,
    )
    save(layout)

    file_kb = os.path.getsize(OUTPUT_HTML) / 1024
    print(f"\n  Output: {os.path.abspath(OUTPUT_HTML)} ({file_kb:.0f} KB)")
    print(f"  Open with: open {OUTPUT_HTML}")

    # Final assertions
    assert os.path.exists(OUTPUT_HTML)
    # All optimizations must produce pixel-identical output
    for strategy, comparisons in [
        ("Fused",    comp_fused),
        ("Ravel",    comp_ravel),
        ("Parallel", comp_par),
    ]:
        for label, match, ndiff in comparisons:
            assert match, (
                f"{strategy} / {label}: {ndiff} pixels differ from serial baseline"
            )
    print("  All optimizations produce pixel-identical output ✓")


# ---------------------------------------------------------------------------
# Standalone runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    warnings.filterwarnings("ignore", message="omp_set_nested")
    warnings.filterwarnings(
        "ignore",
        message="The return type of `Dataset.dims` will be changed",
        category=FutureWarning,
    )

    tests = [
        ("arcae concurrent read (2 threads)", test_arcae_concurrent_read),
        ("arcae concurrent read (4 threads)", test_arcae_concurrent_read_4threads),
        ("parallel optimization comparison",  test_parallel_optimizations),
    ]
    passed = failed = 0
    for name, t in tests:
        try:
            print(f"\n--- {name} ---")
            t()
            print("  PASS")
            passed += 1
        except Exception as exc:
            import traceback
            print(f"  FAIL: {exc}")
            traceback.print_exc()
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
