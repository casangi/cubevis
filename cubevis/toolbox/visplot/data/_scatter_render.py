"""_scatter_render.py
=====================
Shared scatter binning + shading pipeline for ``MSv2Backend`` and
``MSv4Backend``.

Relocated here (2026-09) from ``VisibilityScatter._shade_all_layers`` /
``histogram`` / ``_bands_with_mappings`` almost verbatim -- see
``ScatterRenderResult``'s docstring in ``reader.py`` for the full
rationale. In short: query_columns() was shipping up to ~30M raw rows
over the wire for a remote session, because binning and shading lived
widget-side and needed the raw DataFrame to do it. Moving both here
means only a small, bounded per-layer result crosses a process or wire
boundary -- for local and remote sessions alike, since
``LocalVisibilityReader`` and ``VisplotRemoteBackend`` both just
delegate to this same backend method either way.

Deliberately a standalone module rather than folded into
``msv2_backend.py``/``msv4_backend.py`` directly, and NOT duplicated
between them the way ``_decimate_agg`` is: this pipeline is roughly 5x
``_decimate_agg``'s size, touches no backend-specific partition/Dask
internals (pure numpy/pandas/Datashader operating on an already-built
DataFrame), and has much higher drift risk as two independently-edited
copies. Flagging this as a deliberate deviation from the
``_decimate_agg`` precedent, not an oversight -- happy to duplicate
instead if consistency with that precedent matters more than the drift
risk.

What stays client-side (``VisibilityScatter``), and why
---------------------------------------------------------
* **Per-pixel alpha collapse** (``layer_alpha = auto_alpha *
  lyr.alpha``) -- needs only ``ScatterLayerRender.n_in_view`` and the
  canvas pixel count, both tiny. Keeping this client-side is what
  keeps ``VisibilityScatter.set_alpha()`` a free, no-requery operation,
  exactly as it is today.
* **Porter-Duff compositing across layers** -- pure image-space math
  over the returned RGBA arrays; needs no raw data.

Package location
-----------------
``cubevis/cubevis/toolbox/visplot/data/_scatter_render.py``
"""

from __future__ import annotations

import math
from typing import Optional

import numpy as np
import pandas as pd

try:
    import datashader as ds
    import datashader.reductions as ds_agg
    import datashader.transfer_functions as tf
    HAS_DATASHADER = True
except ImportError:
    HAS_DATASHADER = False

from .. import colormap_scaling as _cms
from .reader import ScatterLayerSpec, ScatterLayerRender

# Mirrors VisibilityScatter._MIN_ALPHA -- see that module's docstring
# for the measured rationale (Datashader's min_alpha=40 default makes a
# single-point pixel nearly invisible; 90 roughly doubles sparse
# visibility without flattening dense-region contrast).
_MIN_ALPHA = 90


def compute_canvas_size(
    dataframes: dict, layers: list[ScatterLayerSpec],
    x0: float, x1: float, y0: float, y1: float,
    width: int, height: int,
) -> tuple[int, int]:
    """Adaptive canvas size for sparse data.

    Ported verbatim from ``VisibilityScatter._compute_canvas_size`` --
    see that method's (pre-redesign) docstring for the ``pts_per_px``
    rationale. Excludes hidden (``alpha <= 0``) layers from the count,
    matching the original exactly.
    """
    total_in_view = 0
    for lyr in layers:
        if lyr.alpha <= 0.0:
            continue
        df = dataframes.get((lyr.y_axis, lyr.polarization))
        if df is None or len(df) == 0:
            continue
        total_in_view += int(
            ((df["x"] >= x0) & (df["x"] <= x1) &
             (df["y"] >= y0) & (df["y"] <= y1)).sum()
        )
    pts_per_px = total_in_view / (width * height)
    if pts_per_px < 0.01 and total_in_view > 0:
        scale = max(0.05, math.sqrt(
            total_in_view / (width * height * 0.01)
        ))
        return max(10, int(width * scale)), max(10, int(height * scale))
    return width, height


def _id_grid_size(
    canvas_w: int, canvas_h: int, max_cells: int,
) -> tuple[int, int]:
    """(width, height) for the coarse identity grid, bounded to at most
    ``max_cells`` total cells while matching the *display canvas's*
    screen aspect ratio.

    BUG FIX (2026-09): the first version of this function computed
    aspect ratio from the data's own (x1-x0)/(y1-y0) spans -- which is
    wrong whenever x and y are different physical quantities (e.g. Time
    in seconds vs. Amplitude in Jy, ratio in the thousands), since
    their raw numeric spans have nothing to do with the canvas's actual
    screen-pixel geometry. Confirmed in practice: a ~5400s Time span
    against a ~130 Jy Amplitude span produced a ~341x9 grid instead of
    anything resembling the roughly-square display canvas, making each
    coarse cell ~2.6 screen-px wide but ~106 screen-px tall -- a hover
    probe's "search a little further for a barely-missed point"
    tolerance (see _handle_probe -- REMOVED for the id grid specifically
    as of this same fix, see that method) then computed its search
    radius from the *smallest* bin dimension, letting a "miss" search
    hundreds of screen pixels in the tall direction and report data
    from a completely different, visually unrelated region as though it
    were "nearby". Using the canvas's own screen aspect ratio instead
    keeps id grid cells roughly square in screen space, matching what a
    user actually sees, regardless of what physical units x and y are
    in.
    """
    aspect = max(canvas_w, 1) / max(canvas_h, 1)
    h = max(1, int(round(math.sqrt(max_cells / max(aspect, 1e-9)))))
    w = max(1, int(round(max_cells / h)))
    return w, h


def _empty_render(canvas_h: int, canvas_w: int, reason: str) -> ScatterLayerRender:
    return ScatterLayerRender(
        image=np.zeros((canvas_h, canvas_w), dtype=np.uint32),
        n_in_view=0, skip_reason=reason, peak_value=None,
        hist_counts=None, hist_edges=None, mapping_x=None, mapping_u=None,
    )


def render_layer(
    df: Optional[pd.DataFrame], lyr: ScatterLayerSpec,
    x0: float, x1: float, y0: float, y1: float,
    canvas_w: int, canvas_h: int,
    color_mode: str, full_y_range: tuple[float, float],
    probe_grid_max_cells: int = 3072,
) -> ScatterLayerRender:
    """Bin + shade one layer.

    Ported from ``VisibilityScatter._shade_all_layers``'s per-layer
    body (pre-redesign) -- see that method for the line-by-line
    precedent this mirrors. Stops short of alpha-channel collapse and
    cross-layer compositing -- see this module's docstring.

    CORRECTION (2026-09, post-chunk-1): does NOT skip shading when
    ``lyr.alpha == 0.0``, unlike the very first version of this
    function. A hidden layer still needs a real cached image: toggling
    visibility back on happens via ``VisibilityScatter.set_alpha()``,
    which by design makes no backend call and can only work with
    whatever image is already cached -- there is nothing to un-hide if
    the backend never bothered to shade it. ``compute_canvas_size``
    still excludes ``alpha <= 0`` layers from its density estimate
    (that's a free, canvas-sizing-only decision with no such
    asymmetry). The widget now derives "hidden" purely from its own
    live ``lyr.alpha`` at composite time -- see
    ``VisibilityScatter._collapse_and_composite``.

    ADDITION (2026-09, hover-probe redesign piece 2): also computes a
    second, much coarser per-bin native-coordinate-range grid (see
    ``ScatterLayerRender.id_grid_*``'s docstring) via a SEPARATE
    ``Canvas.points()`` call at ``_id_grid_size(..., probe_grid_max_cells)``
    resolution, using Datashader's ``summary()`` to compute all six
    min/max reductions (time, baseline_id, frequency) in one aggregation
    pass rather than six separate ones. Requires ``df`` to carry
    "time"/"baseline_id"/"frequency" columns alongside "x"/"y" --
    conditional per-column, so a caller that only populates a subset
    (or none, e.g. during a transition) still gets a valid render, just
    with the corresponding ``id_grid_*`` fields left ``None``.
    """
    if not HAS_DATASHADER:
        raise ImportError(
            "datashader is required for VisibilityScatter's rendering "
            "path.\nInstall: pip install datashader"
        )
    if df is None:
        return _empty_render(canvas_h, canvas_w, "not queried")
    if len(df) == 0:
        return _empty_render(canvas_h, canvas_w, "query returned 0 rows")

    in_view = (
        (df["x"] >= x0) & (df["x"] <= x1) &
        (df["y"] >= y0) & (df["y"] <= y1)
    )
    n_in_view = int(in_view.sum())
    if n_in_view == 0:
        return _empty_render(
            canvas_h, canvas_w, f"0 of {len(df)} samples in viewport")

    cvs = ds.Canvas(
        plot_width=canvas_w, plot_height=canvas_h,
        x_range=(x0, x1), y_range=(y0, y1),
    )
    agg = cvs.points(df, "x", "y", ds_agg.mean("y"))

    # Reference population for eq_hist / colorbar / histogram is the
    # TRUE per-sample y-values, not the binned agg -- running here,
    # where the DataFrame still exists, is what makes that possible
    # without ever shipping them anywhere. Mirrors
    # VisibilityScatter._shade_all_layers' color_mode branch exactly.
    if color_mode == "local":
        visible_y = df.loc[in_view, "y"]
        if len(visible_y) > 0:
            span = [float(visible_y.min()), float(visible_y.max())]
            eq_reference = visible_y.to_numpy()
        else:
            span = [float(full_y_range[0]), float(full_y_range[1])]
            eq_reference = None
    else:  # "global"
        span = [float(full_y_range[0]), float(full_y_range[1])]
        eq_reference = df["y"].to_numpy()

    if lyr.scaling_vmin is not None and lyr.scaling_vmax is not None:
        span = [lyr.scaling_vmin, lyr.scaling_vmax]

    cmap = list(lyr.cmap)
    if lyr.scaling in _cms.DATASHADER_HOW:
        shade_kwargs = dict(
            cmap=cmap, how=_cms.DATASHADER_HOW[lyr.scaling],
            min_alpha=_MIN_ALPHA,
        )
        if span is not None:
            shade_kwargs["span"] = span
        img = tf.shade(agg, **shade_kwargs)
    elif lyr.scaling == "eq_hist":
        eq_ref = eq_reference
        if lyr.scaling_vmin is not None or lyr.scaling_vmax is not None:
            pool = eq_ref if eq_ref is not None else agg.values
            pool_finite = pool[np.isfinite(pool)]
            lo = lyr.scaling_vmin if lyr.scaling_vmin is not None else (
                float(pool_finite.min()) if pool_finite.size else None)
            hi = lyr.scaling_vmax if lyr.scaling_vmax is not None else (
                float(pool_finite.max()) if pool_finite.size else None)
            if lo is not None and hi is not None and hi > lo:
                in_band = pool_finite[(pool_finite >= lo) & (pool_finite <= hi)]
                if in_band.size > 0:
                    eq_ref = in_band
        transformed = _cms.equalize_histogram(agg.values, reference=eq_ref)
        scaled_agg = agg.copy(data=transformed)
        img = tf.shade(
            scaled_agg, cmap=cmap, how="linear",
            span=[0.0, 1.0], min_alpha=_MIN_ALPHA,
        )
    else:
        transformed = _cms.apply_explicit_scaling(
            agg.values, lyr.scaling, alpha=lyr.scaling_alpha,
            gamma=lyr.scaling_gamma,
            vmin=span[0] if span is not None else None,
            vmax=span[1] if span is not None else None,
        )
        scaled_agg = agg.copy(data=transformed)
        img = tf.shade(
            scaled_agg, cmap=cmap, how="linear",
            span=[0.0, 1.0], min_alpha=_MIN_ALPHA,
        )

    img_arr = np.array(img, dtype=np.uint32)

    finite_agg = agg.values[np.isfinite(agg.values)]
    peak_value = float(finite_agg.max()) if finite_agg.size else None

    hist_counts = hist_edges = None
    ref_for_hist = eq_reference if eq_reference is not None else agg.values
    ref_finite = np.asarray(ref_for_hist)
    ref_finite = ref_finite[np.isfinite(ref_finite)]
    if ref_finite.size:
        hist_counts, hist_edges = np.histogram(ref_finite, bins=254)

    mapping_x = mapping_u = None
    mapping = _cms.ScalarMapping.from_values(
        agg.values, lyr.scaling, reference=eq_reference,
        alpha=lyr.scaling_alpha, gamma=lyr.scaling_gamma,
        vmin=lyr.scaling_vmin, vmax=lyr.scaling_vmax,
    )
    if mapping is not None:
        mapping_x, mapping_u = mapping.curve

    # ---- hover-probe redesign piece 2: coarse identity grid -------- #
    # A second, separate (much coarser) Canvas.points() pass -- see
    # this function's docstring and _id_grid_size for why it can't just
    # reuse the display `agg` above. Conditional per native-coordinate
    # column: a caller (or an older DataFrame construction path mid
    # transition) that hasn't populated one of "time"/"baseline_id"/
    # "frequency" simply doesn't get that pair of id_grid_* fields,
    # rather than failing the whole render.
    id_grid_t_lo = id_grid_t_hi = None
    id_grid_bl_lo = id_grid_bl_hi = None
    id_grid_freq_lo = id_grid_freq_hi = None
    id_grid_value = None
    id_cols = [c for c in ("time", "baseline_id", "frequency") if c in df.columns]
    if id_cols or True:
        # "or True": id_grid_value (the coarse mean reading) only needs
        # x/y, which always exist -- so the coarse grid is still worth
        # computing even when none of the three identity columns made
        # it into df (e.g. mid-transition), just with only the value
        # field populated and all six range fields left None.
        id_w, id_h = _id_grid_size(canvas_w, canvas_h, probe_grid_max_cells)
        id_cvs = ds.Canvas(
            plot_width=id_w, plot_height=id_h,
            x_range=(x0, x1), y_range=(y0, y1),
        )
        summary_kwargs = {"val": ds_agg.mean("y")}
        if "time" in id_cols:
            summary_kwargs["t_lo"] = ds_agg.min("time")
            summary_kwargs["t_hi"] = ds_agg.max("time")
        if "baseline_id" in id_cols:
            summary_kwargs["bl_lo"] = ds_agg.min("baseline_id")
            summary_kwargs["bl_hi"] = ds_agg.max("baseline_id")
        if "frequency" in id_cols:
            summary_kwargs["f_lo"] = ds_agg.min("frequency")
            summary_kwargs["f_hi"] = ds_agg.max("frequency")
        # One aggregation pass computes all requested reductions
        # together (Datashader's ds.summary()), not one pass per
        # reduction -- seven reductions here cost the same single
        # vectorized pass over df as the one-reduction display agg
        # above, just a bigger (still tiny, ~id_w*id_h*7 float64s) output.
        id_agg = id_cvs.points(df, "x", "y", ds_agg.summary(**summary_kwargs))
        id_grid_value = id_agg["val"].values
        if "time" in id_cols:
            id_grid_t_lo = id_agg["t_lo"].values
            id_grid_t_hi = id_agg["t_hi"].values
        if "baseline_id" in id_cols:
            id_grid_bl_lo = id_agg["bl_lo"].values
            id_grid_bl_hi = id_agg["bl_hi"].values
        if "frequency" in id_cols:
            id_grid_freq_lo = id_agg["f_lo"].values
            id_grid_freq_hi = id_agg["f_hi"].values

    return ScatterLayerRender(
        image=img_arr, n_in_view=n_in_view, skip_reason=None,
        peak_value=peak_value, hist_counts=hist_counts, hist_edges=hist_edges,
        mapping_x=mapping_x, mapping_u=mapping_u,
        id_grid_t_lo=id_grid_t_lo, id_grid_t_hi=id_grid_t_hi,
        id_grid_bl_lo=id_grid_bl_lo, id_grid_bl_hi=id_grid_bl_hi,
        id_grid_freq_lo=id_grid_freq_lo, id_grid_freq_hi=id_grid_freq_hi,
        id_grid_x_range=(x0, x1), id_grid_y_range=(y0, y1),
        id_grid_value=id_grid_value,
    )
