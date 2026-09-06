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
) -> ScatterLayerRender:
    """Bin + shade one layer.

    Ported from ``VisibilityScatter._shade_all_layers``'s per-layer
    body (pre-redesign) -- see that method for the line-by-line
    precedent this mirrors. Stops short of alpha-channel collapse and
    cross-layer compositing -- see this module's docstring.
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
    if lyr.alpha == 0.0:
        return _empty_render(canvas_h, canvas_w, "hidden (alpha=0)")

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

    return ScatterLayerRender(
        image=img_arr, n_in_view=n_in_view, skip_reason=None,
        peak_value=peak_value, hist_counts=hist_counts, hist_edges=hist_edges,
        mapping_x=mapping_x, mapping_u=mapping_u,
    )
