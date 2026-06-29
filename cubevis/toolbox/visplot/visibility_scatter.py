"""
visibility_scatter.py
=====================
Datashader scatter plot for visibility data with multiple overlaid layers.

Each layer is a ``ScatterLayer`` specifying a y-axis quantity, polarization,
colour map, and alpha value.  All layers share the same x-axis and
``SelectionSpec``.

Rendering pipeline
------------------
For each layer::

    query_columns(x_axis, [(y_axis, pol)], selection)
        -> DataFrame with columns "x", "y"

    Canvas.points(df, "x", "y", agg=mean("y"))
        -> canvas-resolution float64 agg (H × W)

    tf.shade(agg, cmap=layer.cmap, alpha=int(layer.alpha * 255))
        -> Datashader Image (uint32, RGBA)

Then composite all layer images::

    tf.stack(*images, how="over")
        -> single composite Datashader Image

The composite is pushed to a single Bokeh ``image_rgba`` glyph via
``_image_source``.  This avoids painter's-order artefacts and lets
Datashader handle alpha compositing correctly in float space.

Alpha control
-------------
Each layer's ``alpha`` (0.0–1.0) is stored in ``_state_source`` as
``layer_alpha_N`` for JS widgets to read.  Changing alpha only re-runs
the shade + stack step (no re-query), so it is fast.

Axis switching
--------------
``update_axes(x_dim=, layers=)`` re-queries all layers and composites.
Passing new ``layers`` replaces the layer list entirely.

Package location
----------------
``cubevis/cubevis/toolbox/visplot/visibility_scatter.py``
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from typing import Optional, TYPE_CHECKING
from uuid import uuid4

import numpy as np

from bokeh.models import ColumnDataSource

from .visibility_plot import VisibilityPlot, _img_to_uint32, _axis_label

if TYPE_CHECKING:
    import pandas as pd
    import xarray as xr
    from .reader import XArrayReader
    from .selection import SelectionSpec
    from .axes import Axis

log = logging.getLogger(__name__)

try:
    import datashader as ds
    import datashader.reductions as ds_agg
    import datashader.transfer_functions as tf
    HAS_DATASHADER = True
except ImportError:
    HAS_DATASHADER = False

# Default colour maps for successive layers
_LAYER_CMAPS = [
    # Plasma
    ["#0d0887","#46039f","#7201a8","#9c179e","#bd3786",
     "#d8576b","#ed7953","#fb9f3a","#fdcb26","#f0f921"],
    # Inferno
    ["#000004","#1b0c41","#4a0c4e","#781c6d","#a52c60",
     "#cf4446","#ed6925","#fb9b06","#f7d13d","#fcffa4"],
    # Viridis
    ["#440154","#482878","#3e4989","#31688e","#26828e",
     "#1f9e89","#35b779","#6ece58","#b5de2b","#fde725"],
    # Magma
    ["#000004","#180f3d","#440f76","#721f81","#9f2f7f",
     "#cd4071","#f1605d","#fd9668","#feca8d","#fcfdbf"],
]


# ---------------------------------------------------------------------------
# ScatterLayer dataclass
# ---------------------------------------------------------------------------

@dataclass
class ScatterLayer:
    """Specification for one scatter plot layer.

    Parameters
    ----------
    y_axis : Axis
        The y-axis quantity (e.g. ``Axis.AMPLITUDE``, ``Axis.PHASE``).
    polarization : str
        Correlation product label (e.g. ``"XX"``).
    cmap : list[str] | None
        Colour map hex strings.  ``None`` → assigned from ``_LAYER_CMAPS``
        cycle based on layer index.
    alpha : float
        Opacity in [0.0, 1.0].  0.0 = transparent (hidden), 1.0 = opaque.
    label : str
        Human-readable label for legend / toggle widgets.  Auto-generated
        from ``y_axis.label`` and ``polarization`` if empty.
    """
    y_axis:      "Axis"
    polarization: str        = "XX"
    cmap:         Optional[list] = None
    alpha:        float      = 1.0
    label:        str        = ""

    def __post_init__(self):
        if not self.label:
            self.label = f"{self.y_axis.label} {self.polarization}"


# ---------------------------------------------------------------------------
# VisibilityScatter
# ---------------------------------------------------------------------------

class VisibilityScatter(VisibilityPlot):
    """Multi-layer Datashader scatter plot for visibility data.

    Parameters
    ----------
    backend : XArrayReader
        Opened backend.
    selection : SelectionSpec
        Data selection.
    x_axis : Axis
        The x-axis (e.g. ``Axis.UVDIST``, ``Axis.TIME``).
    layers : list[ScatterLayer]
        One or more scatter layers.  Each specifies a y-axis quantity,
        polarization, colour map, and alpha.
    width, height : int
        Canvas dimensions in pixels.
    title : str | None
        Figure title; ``None`` → auto-generated.
    comm_mgr :
        ``CommMgr`` from the active ``BokehAppContext``.
    """

    def __init__(
        self,
        backend: "XArrayReader",
        selection: "SelectionSpec",
        x_axis: "Axis",
        layers: list[ScatterLayer],
        width: int  = 900,
        height: int = 600,
        title: Optional[str] = None,
        comm_mgr=None,
        color_mode: str = "global",
        **kwargs,
    ) -> None:
        if not layers:
            raise ValueError("VisibilityScatter: layers must be non-empty")

        # Assign default colour maps by layer index
        self._layers: list[ScatterLayer] = []
        for i, lyr in enumerate(layers):
            if lyr.cmap is None:
                lyr = ScatterLayer(
                    y_axis       = lyr.y_axis,
                    polarization = lyr.polarization,
                    cmap         = _LAYER_CMAPS[i % len(_LAYER_CMAPS)],
                    alpha        = lyr.alpha,
                    label        = lyr.label,
                )
            self._layers.append(lyr)

        # Cached DataFrames and canvas aggs — one per layer
        self._layer_dfs:  list[Optional["pd.DataFrame"]] = [None] * len(layers)
        self._layer_aggs: list[Optional["xr.DataArray"]] = [None] * len(layers)

        # Current viewport — updated on every pan/zoom rerender so that
        # set_alpha / set_color_mode re-composite over the correct region.
        self._current_viewport: Optional[tuple[float,float,float,float]] = None

        # x_axis is stored as y_dim placeholder; base class uses _x_dim / _y_dim
        # for viewport narrowing.  For scatter, _y_dim is unused but must be set.
        # We use the first layer's y_axis as the canonical _y_dim for the base.
        self._color_mode       = color_mode
        self._msg_update_axes  = str(uuid4())
        self._msg_set_alpha    = str(uuid4())
        self._msg_color_mode   = str(uuid4())

        super().__init__(
            backend   = backend,
            selection = selection,
            y_dim     = layers[0].y_axis,
            x_dim     = x_axis,
            width     = width,
            height    = height,
            title     = title,
            comm_mgr  = comm_mgr,
            **kwargs,
        )

    # ------------------------------------------------------------------
    # Public API extensions
    # ------------------------------------------------------------------

    @property
    def layers(self) -> list[ScatterLayer]:
        """The current scatter layer list."""
        return list(self._layers)

    def set_alpha(self, layer_index: int, alpha: float) -> None:
        """Set the alpha for a single layer and re-composite.

        Does NOT re-query the backend — only re-runs shade + stack.

        Parameters
        ----------
        layer_index : int
            Zero-based index into ``self.layers``.
        alpha : float
            New opacity in [0.0, 1.0].
        """
        if not (0 <= layer_index < len(self._layers)):
            raise IndexError(f"layer_index {layer_index} out of range")
        lyr = self._layers[layer_index]
        self._layers[layer_index] = ScatterLayer(
            y_axis       = lyr.y_axis,
            polarization = lyr.polarization,
            cmap         = lyr.cmap,
            alpha        = max(0.0, min(1.0, alpha)),
            label        = lyr.label,
        )
        self._composite_and_push()
        self._update_state_source()

    def update_axes(
        self,
        x_dim: Optional["Axis"] = None,
        layers: Optional[list[ScatterLayer]] = None,
        title: Optional[str] = None,
    ) -> None:
        """Change the x-axis or layer list and re-render.

        Parameters
        ----------
        x_dim : Axis | None
            New x-axis.  ``None`` keeps the current value.
        layers : list[ScatterLayer] | None
            Replacement layer list.  ``None`` keeps existing layers.
        title : str | None
            New figure title.
        """
        changed = False
        if x_dim is not None and x_dim != self._x_dim:
            self._x_dim = x_dim;  changed = True
        if layers is not None:
            self._layers = list(layers)
            self._layer_dfs  = [None] * len(layers)
            self._layer_aggs = [None] * len(layers)
            changed = True
        if title is not None:
            self._title = title;  changed = True

        if not changed:
            return

        self._render(self._selection)
        self._notify_axes_changed()

    # ------------------------------------------------------------------
    # VisibilityPlot abstract interface
    # ------------------------------------------------------------------

    def _comm_description(self) -> str:
        return "visibility scatter"

    def _effective_title(self) -> str:
        if self._title:
            return self._title
        labels = ", ".join(lyr.label for lyr in self._layers)
        return f"{labels}  vs  {self._x_dim.label}"

    def _state_data_extra(self) -> dict:
        """Add per-layer alpha values to _state_source."""
        extra: dict = {}
        for i, lyr in enumerate(self._layers):
            extra[f"layer_alpha_{i}"] = [lyr.alpha]
            extra[f"layer_label_{i}"] = [lyr.label]
        extra["n_layers"]   = [len(self._layers)]
        extra["color_mode"] = [self._color_mode]
        return extra

    def _build_glyphs(self) -> None:
        """Add the single composite image_rgba glyph."""
        self._fig.image_rgba(
            source = self._image_source,
            image  = "image",
            x = "x", y = "y", dw = "dw", dh = "dh",
        )

    def _render(self, selection: "SelectionSpec", **kwargs) -> None:
        """Query all layers and push the composite image."""
        t0 = time.perf_counter()
        self._query_all_layers(selection)
        self._current_viewport = None   # reset — new data covers full range
        self._composite_and_push()
        log.debug("VisibilityScatter._render: %.3fs", time.perf_counter() - t0)
        self._update_state_source()

    def _do_viewport_rerender(
        self, x0: float, x1: float, y0: float, y1: float
    ) -> dict:
        """Re-composite cached DataFrames over the new viewport."""
        # Normalise — Bokeh box-zoom can produce start > end
        x0, x1 = min(x0, x1), max(x0, x1)
        y0, y1 = min(y0, y1), max(y0, y1)
        self._current_viewport = (x0, x1, y0, y1)
        img32 = self._shade_all_layers(x_range=(x0, x1), y_range=(y0, y1))
        return {
            "image": img32,
            "x0": x0, "x1": x1,
            "y0": y0, "y1": y1,
        }

    def _handle_probe(self, message: dict) -> dict:
        """Probe the composite canvas agg at hover coordinates."""
        x = float(message.get("x", 0.0))
        y = float(message.get("y", 0.0))

        x0, x1 = self._x_range
        y0, y1 = self._y_range
        if not (x0 <= x <= x1 and y0 <= y <= y1):
            return {"label": "<i>out of range</i>"}

        # Use the first non-None layer agg for the probe
        canvas_agg = next(
            (a for a in self._layer_aggs if a is not None), None
        )
        if canvas_agg is None:
            return {"label": "<i>no data</i>"}

        # Convert data-space to canvas pixel indices
        x_coords = canvas_agg.coords[canvas_agg.dims[1]].values
        y_coords = canvas_agg.coords[canvas_agg.dims[0]].values
        px = max(0, min(int(np.argmin(np.abs(x_coords - x))),
                        canvas_agg.shape[1] - 1))
        py = max(0, min(int(np.argmin(np.abs(y_coords - y))),
                        canvas_agg.shape[0] - 1))

        # Probe the first non-empty layer at this pixel
        for i, (agg, df, lyr) in enumerate(
            zip(self._layer_aggs, self._layer_dfs, self._layers)
        ):
            if agg is None or df is None:
                continue
            try:
                info  = self._backend.probe_scatter_pixel(
                    agg, px, py, self._selection, df
                )
                label = self._format_probe(info, lyr.y_axis.label)
                return {"label": label}
            except Exception as exc:
                log.warning("probe_scatter_pixel layer %d failed: %s", i, exc)

        return {"label": "<i>empty</i>"}

    def _register_extra_comm_handlers(self) -> None:
        self._comm.register(self._msg_set_alpha,   self._handle_set_alpha)
        self._comm.register(self._msg_color_mode,  self._handle_set_color_mode)
        self._comm.register(self._msg_update_axes, self._handle_update_axes_scatter)

    # ------------------------------------------------------------------
    # Scatter-specific internals
    # ------------------------------------------------------------------

    def _query_all_layers(self, selection: "SelectionSpec") -> None:
        """Query backend for all layers; update _layer_dfs."""
        # Build combined y-axes list for a single query_columns call
        y_axes = [(lyr.y_axis, lyr.polarization) for lyr in self._layers]
        result = self._backend.query_columns(
            self._x_dim, y_axes, selection
        )

        x0_all, x1_all, y0_all, y1_all = [], [], [], []
        for i, lyr in enumerate(self._layers):
            key = (lyr.y_axis, lyr.polarization)
            df  = result.get(key)
            self._layer_dfs[i] = df
            if df is not None and len(df) > 0:
                x0_all.append(float(df["x"].min()))
                x1_all.append(float(df["x"].max()))
                y0_all.append(float(df["y"].min()))
                y1_all.append(float(df["y"].max()))

        if x0_all:
            self._x_range = (min(x0_all), max(x1_all))
            self._y_range = (min(y0_all), max(y1_all))
        else:
            self._x_range = (0.0, 1.0)
            self._y_range = (0.0, 1.0)

    def _shade_all_layers(
        self,
        x_range: Optional[tuple[float, float]] = None,
        y_range: Optional[tuple[float, float]] = None,
    ) -> np.ndarray:
        """Run cvs.points + shade for each layer; stack and return uint32.

        Parameters
        ----------
        x_range, y_range :
            Viewport extents.  ``None`` → use full data range.
        """
        xr = x_range or self._x_range
        yr = y_range or self._y_range
        # Normalise so x0 < x1 and y0 < y1 — Bokeh's box-zoom can produce
        # inverted ranges when the figure y-axis is flipped for image display.
        x0, x1 = (min(xr), max(xr))
        y0, y1 = (min(yr), max(yr))

        if x0 == x1 or y0 == y1:
            return np.zeros((self._height, self._width), dtype=np.uint32)

        # Global amplitude scale — anchor span to the full data y_range so
        # the same amplitude value always maps to the same colour regardless
        # of zoom level.  This makes it possible to visually track features
        # across pan/zoom without the colour shifting under the user's feet.
        span_arg = None
        if self._color_mode == "global":
            global_y0, global_y1 = self._y_range
            span_arg = [global_y0, global_y1]

        # Compute total points in viewport across all layers to determine
        # a single canvas size shared by all layers — this ensures the
        # Porter-Duff compositing loop always gets arrays of the same shape.
        total_in_view = sum(
            int(((df["x"] >= x0) & (df["x"] <= x1) &
                 (df["y"] >= y0) & (df["y"] <= y1)).sum())
            for lyr, df in zip(self._layers, self._layer_dfs)
            if df is not None and len(df) > 0 and lyr.alpha > 0.0
        )
        pts_per_px = total_in_view / (self._width * self._height)
        if pts_per_px < 0.01 and total_in_view > 0:
            scale = max(0.05, math.sqrt(
                total_in_view / (self._width * self._height * 0.01)
            ))
            shared_w = max(10, int(self._width  * scale))
            shared_h = max(10, int(self._height * scale))
        else:
            shared_w, shared_h = self._width, self._height

        shaded_images = []
        for i, (lyr, df) in enumerate(zip(self._layers, self._layer_dfs)):
            if df is None or len(df) == 0 or lyr.alpha == 0.0:
                continue
            try:
                # Count points in viewport for this layer's auto-alpha.
                n_in_view = int(
                    ((df["x"] >= x0) & (df["x"] <= x1) &
                     (df["y"] >= y0) & (df["y"] <= y1)).sum()
                )
                if n_in_view == 0:
                    continue

                cvs_layer = ds.Canvas(
                    plot_width  = shared_w,
                    plot_height = shared_h,
                    x_range     = (x0, x1),
                    y_range     = (y0, y1),
                )
                agg = cvs_layer.points(df, "x", "y", ds_agg.mean("y"))
                self._layer_aggs[i] = agg

                # Auto-scale opacity by overplot ratio
                # Use shared canvas pixels so alpha is consistent across layers
                canvas_pixels = shared_w * shared_h
                ratio        = max(1.0, n_in_view / canvas_pixels)
                auto_alpha   = int(255.0 / math.log1p(ratio))
                auto_alpha   = max(80, min(255, auto_alpha))
                layer_alpha  = max(0, min(255, int(auto_alpha * lyr.alpha)))

                # Colour mapping controlled by color_mode:
                #   "global" → linear + span=[full_y0, full_y1]
                #       Stable colours across all zoom levels — a 50 Jy point
                #       always maps to the same colour.  Best for flagging.
                #   "local" → linear + span=[viewport_y_min, viewport_y_max]
                #       Full Plasma palette spans whatever amplitudes are
                #       visible in the current viewport.  Colours change on
                #       zoom (expected).  Best for exploring structure.
                if self._color_mode == "local":
                    visible_y = df.loc[
                        (df["x"] >= x0) & (df["x"] <= x1) &
                        (df["y"] >= y0) & (df["y"] <= y1),
                        "y"
                    ]
                    if len(visible_y) > 0:
                        local_span = [float(visible_y.min()),
                                      float(visible_y.max())]
                    else:
                        local_span = [float(self._y_range[0]),
                                      float(self._y_range[1])]
                    shade_kwargs = dict(cmap=lyr.cmap, how="linear",
                                       span=local_span)
                else:  # "global"
                    shade_kwargs = dict(cmap=lyr.cmap, how="linear")
                    if span_arg is not None:
                        shade_kwargs["span"] = span_arg
                img     = tf.shade(agg, **shade_kwargs)
                img_arr = np.array(img, dtype=np.uint32)
                if layer_alpha > 0:
                    nonempty = (img_arr >> 24) > 0
                    img_arr[nonempty] = (
                        (img_arr[nonempty] & 0x00FFFFFF)
                        | (np.uint32(layer_alpha) << np.uint32(24))
                    )
                shaded_images.append(img_arr)
            except Exception as exc:
                log.warning("shade layer %d failed: %s", i, exc)

        if not shaded_images:
            return np.zeros((self._height, self._width), dtype=np.uint32)

        if len(shaded_images) == 1:
            return shaded_images[0]

        # Porter-Duff "over" compositing in numpy on uint32 ARGB arrays.
        # For each pixel: result = src + dst * (1 - src_alpha/255).
        # This is equivalent to tf.stack(..., how="over") but works on
        # plain ndarray so we don't need Datashader Image objects.
        composite = shaded_images[0].copy()
        for layer_arr in shaded_images[1:]:
            src_a = ((layer_arr >> 24) & 0xFF).astype(np.float32) / 255.0
            dst_a = ((composite  >> 24) & 0xFF).astype(np.float32) / 255.0
            out_a = src_a + dst_a * (1.0 - src_a)

            # Blend each channel
            for shift in (16, 8, 0):   # R, G, B
                src_c = ((layer_arr >> shift) & 0xFF).astype(np.float32)
                dst_c = ((composite  >> shift) & 0xFF).astype(np.float32)
                with np.errstate(invalid="ignore", divide="ignore"):
                    out_c = np.where(
                        out_a > 0,
                        (src_c * src_a + dst_c * dst_a * (1.0 - src_a)) / out_a,
                        0.0,
                    )
                mask = np.uint32(0xFF) << np.uint32(shift)
                composite = (composite & ~mask) | \
                            (out_c.astype(np.uint32) << np.uint32(shift))

            out_a_u8 = np.clip(out_a * 255, 0, 255).astype(np.uint32)
            composite = (composite & 0x00FFFFFF) | (out_a_u8 << 24)

        return composite

    def _composite_and_push(
        self,
        x_range: Optional[tuple[float, float]] = None,
        y_range: Optional[tuple[float, float]] = None,
    ) -> None:
        """Shade + stack all layers; push result into _image_source.

        Uses _current_viewport when set (i.e. user has panned/zoomed) so
        that set_alpha / set_color_mode re-composite over the correct region
        rather than the full data range.
        """
        if x_range is None and y_range is None and self._current_viewport:
            vx0, vx1, vy0, vy1 = self._current_viewport
            xr = (vx0, vx1)
            yr = (vy0, vy1)
        else:
            xr = x_range or self._x_range
            yr = y_range or self._y_range
        x0, x1 = xr
        y0, y1 = yr

        img32    = self._shade_all_layers(xr, yr)
        new_data = {
            "image": [img32],
            "x":     [x0],
            "y":     [y0],
            "dw":    [x1 - x0],
            "dh":    [y1 - y0],
        }
        if self._image_source is None:
            self._image_source = ColumnDataSource(data=new_data)
        else:
            self._image_source.data = new_data

    # ------------------------------------------------------------------
    # j2p handlers (scatter-specific)
    # ------------------------------------------------------------------

    def set_color_mode(self, mode: str) -> None:
        """Toggle colour mode and re-composite without re-querying the backend.

        Parameters
        ----------
        mode : ``"global"`` | ``"local"``
            ``"global"`` (default) — ``linear`` shading with ``span`` anchored
            to the full data y_range.  A 50 Jy point always maps to the same
            colour regardless of zoom level.  Recommended for flagging.
            ``"local"`` — ``linear`` shading with ``span`` derived from the
            amplitude range of the data visible in the current viewport.  The
            full Plasma palette spans whatever is on screen, so zooming into
            a narrow amplitude range uses the full colour range for that
            region.  Colours change on zoom (expected).  Best for exploring
            structure and low-contrast features within a region.
        """
        if mode not in ("global", "local"):
            raise ValueError(
                f"color_mode must be 'global' or 'local', got {mode!r}"
            )
        self._color_mode = mode
        self._composite_and_push()
        self._update_state_source()

    def _image_response(self, status: str = "ok", **extra) -> dict:
        """Build a j2p response dict containing the current image and viewport.

        All j2p handlers that update the composite image use this so the JS
        callback can correctly reposition the image glyph at the current
        viewport extents rather than the full data range.
        """
        src = self._image_source.data
        return {
            "status": status,
            "image":  src["image"][0],
            "x0":     src["x"][0],
            "x1":     src["x"][0] + src["dw"][0],
            "y0":     src["y"][0],
            "y1":     src["y"][0] + src["dh"][0],
            **extra,
        }

    def _handle_set_color_mode(self, message: dict) -> dict:
        """Handle j2p message to toggle colour mode: {mode: "global"|"local"}.

        Returns the new composite image so the JS callback can update
        image_source.data directly — Python-side model property changes
        don't propagate to the browser in static HTML mode.
        """
        mode = message.get("mode", "global")
        try:
            self.set_color_mode(mode)
        except ValueError as exc:
            return {"status": "error", "message": str(exc)}
        return self._image_response("ok", color_mode=self._color_mode)

    def _handle_set_alpha(self, message: dict) -> dict:
        """Handle j2p 'vs_set_alpha': {layer_index: int, alpha: float}."""
        idx   = int(message.get("layer_index", 0))
        alpha = float(message.get("alpha", 1.0))
        try:
            self.set_alpha(idx, alpha)
        except (IndexError, ValueError) as exc:
            return {"status": "error", "message": str(exc)}
        return self._image_response("ok")

    def _handle_update_axes_scatter(self, message: dict) -> dict:
        """Handle j2p 'vs_update_axes' with scatter-specific fields."""
        from .axes import Axis
        new_layers = None
        if "layers" in message:
            try:
                new_layers = [
                    ScatterLayer(
                        y_axis       = Axis[entry["y_axis"]],
                        polarization = entry.get("polarization", "XX"),
                        alpha        = float(entry.get("alpha", 1.0)),
                        label        = entry.get("label", ""),
                    )
                    for entry in message["layers"]
                ]
            except Exception as exc:
                log.warning("_handle_update_axes_scatter: bad layers: %s", exc)

        self.update_axes(
            x_dim  = self._parse_axis(message, "x_dim"),
            layers = new_layers,
            title  = message.get("title"),
        )
        return {"status": "ok"}
