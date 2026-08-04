"""
visibility_scatter.py
=====================
Datashader scatter plot for visibility data with multiple overlaid layers.

Each layer is a ``ScatterLayer`` specifying a y-axis quantity, polarization,
color map, and alpha value.  All layers share the same x-axis and
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
from . import colormap_scaling as _cms

if TYPE_CHECKING:
    import pandas as pd
    import xarray as xr
    from .visibility_reader import VisibilityReader
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

_DEFAULT_SCALING = "eq_hist"

# Default color maps for successive layers
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
        Color map hex strings.  ``None`` → assigned from ``_LAYER_CMAPS``
        cycle based on layer index.
    alpha : float
        Opacity in [0.0, 1.0].  0.0 = transparent (hidden), 1.0 = opaque.
    label : str
        Human-readable label for legend / toggle widgets.  Auto-generated
        from ``y_axis.label`` and ``polarization`` if empty.
    scaling : str
        Value-to-color transfer function for this layer.  One of
        ``colormap_scaling.ALL_SCALINGS``.  Defaults to ``"eq_hist"``
        (histogram equalization), which resolves the low-amplitude
        saturation seen under linear scaling on real visibility data —
        see ``colormap_scaling`` module docstring for the full
        rationale.
    scaling_alpha : float
        Parameter for ``"log"`` and ``"power"`` scalings.
    scaling_gamma : float
        Parameter for ``"gamma"`` scaling.
    """
    y_axis:      "Axis"
    polarization: str        = "XX"
    cmap:         Optional[list] = None
    alpha:        float      = 1.0
    label:        str        = ""
    scaling:        str   = _DEFAULT_SCALING
    scaling_alpha:  float = 10.0
    scaling_gamma:  float = 1.0

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
    backend : VisibilityReader
        Opened reader (``LocalVisibilityReader`` wrapping an
        ``MSv2Backend`` or ``MSv4Backend``, or a
        ``RemoteReductionContext`` for remote sessions).
    selection : SelectionSpec
        Data selection.
    x_axis : Axis
        The x-axis (e.g. ``Axis.UVDIST``, ``Axis.TIME``).
    layers : list[ScatterLayer]
        One or more scatter layers.  Each specifies a y-axis quantity,
        polarization, color map, and alpha.
    width, height : int
        Canvas dimensions in pixels.
    title : str | None
        Figure title; ``None`` → auto-generated.
    comm_mgr :
        ``CommMgr`` from the active ``BokehAppContext``.
    """

    def __init__(
        self,
        backend: "VisibilityReader",
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

        # Assign default color maps by layer index
        self._layers: list[ScatterLayer] = []
        for i, lyr in enumerate(layers):
            if lyr.cmap is None:
                lyr = ScatterLayer(
                    y_axis        = lyr.y_axis,
                    polarization  = lyr.polarization,
                    cmap          = _LAYER_CMAPS[i % len(_LAYER_CMAPS)],
                    alpha         = lyr.alpha,
                    label         = lyr.label,
                    scaling       = lyr.scaling,
                    scaling_alpha = lyr.scaling_alpha,
                    scaling_gamma = lyr.scaling_gamma,
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
        self._msg_update_scaling = str(uuid4())

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
            y_axis        = lyr.y_axis,
            polarization  = lyr.polarization,
            cmap          = lyr.cmap,
            alpha         = max(0.0, min(1.0, alpha)),
            label         = lyr.label,
            scaling       = lyr.scaling,
            scaling_alpha = lyr.scaling_alpha,
            scaling_gamma = lyr.scaling_gamma,
        )
        self._composite_and_push()
        self._update_state_source()

    def update_scaling(
        self,
        layer_index: int,
        scaling: Optional[str] = None,
        alpha: Optional[float] = None,
        gamma: Optional[float] = None,
    ) -> None:
        """Change one layer's value-to-color transfer function and re-composite.

        Does NOT re-query the backend — only re-runs shade + stack, mirroring
        ``set_alpha()``.

        Parameters
        ----------
        layer_index : int
            Zero-based index into ``self.layers``.
        scaling : str | None
            One of ``colormap_scaling.ALL_SCALINGS``.  ``None`` keeps the
            current value.
        alpha : float | None
            Parameter for ``"log"`` / ``"power"`` scalings.  ``None``
            keeps the current value.
        gamma : float | None
            Parameter for ``"gamma"`` scaling.  ``None`` keeps the
            current value.
        """
        if not (0 <= layer_index < len(self._layers)):
            raise IndexError(f"layer_index {layer_index} out of range")
        if scaling is not None and scaling not in _cms.ALL_SCALINGS:
            raise ValueError(
                f"scaling must be one of {_cms.ALL_SCALINGS}, got {scaling!r}"
            )
        lyr = self._layers[layer_index]
        self._layers[layer_index] = ScatterLayer(
            y_axis        = lyr.y_axis,
            polarization  = lyr.polarization,
            cmap          = lyr.cmap,
            alpha         = lyr.alpha,
            label         = lyr.label,
            scaling       = scaling if scaling is not None else lyr.scaling,
            scaling_alpha = alpha if alpha is not None else lyr.scaling_alpha,
            scaling_gamma = gamma if gamma is not None else lyr.scaling_gamma,
        )
        self._composite_and_push()
        self._update_state_source()

    def histogram(
        self, layer_index: int, bins: int = 254,
    ) -> tuple[np.ndarray, np.ndarray]:
        """Return ``(counts, bin_edges)`` for one layer's cached agg values.

        Used by ``colormap_controls()`` for an eventual histogram display
        alongside the scaling controls.  Returns empty arrays if the
        layer hasn't been rendered yet.
        """
        if not (0 <= layer_index < len(self._layer_aggs)):
            raise IndexError(f"layer_index {layer_index} out of range")
        agg = self._layer_aggs[layer_index]
        if agg is None:
            return np.array([]), np.array([])
        finite = agg.values[np.isfinite(agg.values)]
        if finite.size == 0:
            return np.array([]), np.array([])
        counts, edges = np.histogram(finite, bins=bins)
        return counts, edges

    def colormap_controls(self, layer_index: int = 0):
        """Return a Bokeh widget column for one layer's scaling controls.

        ``VisibilityPlotter``'s sidebar embeds one of these per layer —
        see Phase 0 CM-4 in the implementation plan. Mirrors
        ``VisibilityRaster.colormap_controls()``; see that docstring for
        the full rationale. Scaling is per-layer here since each layer
        has its own colormap and quantity.

        Parameters
        ----------
        layer_index : int
            Which layer's controls to build.  Defaults to the first
            layer.
        """
        from bokeh.layouts import column, row
        from bokeh.models import Select, TextInput, Div

        if not (0 <= layer_index < len(self._layers)):
            raise IndexError(f"layer_index {layer_index} out of range")
        lyr = self._layers[layer_index]

        equation = Div(text=_cms.scaling_equation_label(lyr.scaling))
        scaling_select = Select(
            title=f"Color scaling — {lyr.label}",
            value=lyr.scaling,
            options=list(_cms.ALL_SCALINGS),
        )
        alpha_input = TextInput(
            title="alpha", value=str(lyr.scaling_alpha),
            visible=lyr.scaling in ("log", "power"),
        )
        gamma_input = TextInput(
            title="gamma", value=str(lyr.scaling_gamma),
            visible=lyr.scaling == "gamma",
        )

        def _on_scaling_change(attr, old, new):
            alpha_input.visible = new in ("log", "power")
            gamma_input.visible = new == "gamma"
            equation.text = _cms.scaling_equation_label(new)
            self.update_scaling(layer_index, scaling=new)

        def _on_alpha_change(attr, old, new):
            try:
                self.update_scaling(layer_index, alpha=float(new))
            except ValueError:
                pass

        def _on_gamma_change(attr, old, new):
            try:
                self.update_scaling(layer_index, gamma=float(new))
            except ValueError:
                pass

        scaling_select.on_change("value", _on_scaling_change)
        alpha_input.on_change("value", _on_alpha_change)
        gamma_input.on_change("value", _on_gamma_change)

        return column(
            scaling_select,
            equation,
            row(alpha_input, gamma_input),
        )

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

        # A never-yet-rendered (defer_initial_render=True) panel must
        # render on its first update_axes() call regardless of what else
        # changed — same rationale as VisibilityRaster's identical guard.
        # Scatter has no single self._agg; "never rendered" here means
        # every layer's DataFrame is still None (set that way by
        # _render(defer=True), and by the layers-replacement branch above,
        # which is why this check must come after it).
        if all(df is None for df in self._layer_dfs):
            changed = True

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
        """Add per-layer alpha/scaling values plus agg_n_x/agg_n_y."""
        extra: dict = {}
        for i, lyr in enumerate(self._layers):
            extra[f"layer_alpha_{i}"] = [lyr.alpha]
            extra[f"layer_label_{i}"] = [lyr.label]
            extra[f"layer_scaling_{i}"]       = [lyr.scaling]
            extra[f"layer_scaling_alpha_{i}"] = [lyr.scaling_alpha]
            extra[f"layer_scaling_gamma_{i}"] = [lyr.scaling_gamma]
        extra["n_layers"]   = [len(self._layers)]
        extra["color_mode"] = [self._color_mode]

        # agg_n_x/agg_n_y: canvas resolution at the *full* data extent —
        # same field names as VisibilityRaster so FlagTool's existing
        # zoom-to-1:1 math (flag_tool.ts) works unchanged here too. Unlike
        # raster, scatter points are exact (not binned/decimated), so this
        # isn't about resolving averaged data — it's the same
        # sparse-data canvas-shrink logic _shade_all_layers uses
        # (_compute_canvas_size), which means "1:1" for scatter
        # effectively means "zoomed in enough that the full-extent view's
        # overplot-driven canvas shrink no longer applies" — a reasonable
        # proxy for "not looking at an overplotted, ambiguous cluster."
        full_x0, full_x1 = self._x_range
        full_y0, full_y1 = self._y_range
        agg_n_x, agg_n_y = self._compute_canvas_size(
            full_x0, full_x1, full_y0, full_y1)
        extra["agg_n_x"] = [agg_n_x]
        extra["agg_n_y"] = [agg_n_y]

        return extra

    def _build_glyphs(self) -> None:
        """Add the single composite image_rgba glyph."""
        self._fig.image_rgba(
            source = self._image_source,
            image  = "image",
            x = "x", y = "y", dw = "dw", dh = "dh",
        )

    def _render(self, selection: "SelectionSpec", defer: bool = False, **kwargs) -> None:
        """Query all layers and push the composite image.

        Parameters
        ----------
        defer : bool
            If ``True``, skip the backend query entirely and leave all
            layers empty with the same placeholder ``(0.0, 1.0)`` ranges
            ``_query_all_layers`` already uses for genuinely empty data —
            same purpose and pattern as ``VisibilityRaster._render``'s
            ``defer``; see decision 11 in the grid/iteration design notes.
        """
        t0 = time.perf_counter()
        if defer:
            self._layer_dfs = [None] * len(self._layers)
            self._x_range    = (0.0, 1.0)
            self._y_range    = (0.0, 1.0)
        else:
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
        self._comm.register(self._msg_update_scaling, self._handle_update_scaling)

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

    def _compute_canvas_size(
        self, x0: float, x1: float, y0: float, y1: float,
    ) -> tuple[int, int]:
        """Canvas pixel dimensions used to render the given range.

        Normally just ``(self._width, self._height)``, but for sparse
        data (few points relative to canvas area) a smaller canvas is
        used to visually boost apparent point density — see the
        ``pts_per_px`` scaling below. Factored out of ``_shade_all_layers``
        so ``_state_data_extra`` can also compute this for the *full*
        data extent (see ``agg_n_x``/``agg_n_y`` there), independent of
        whatever sub-range is currently in view.
        """
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
        return shared_w, shared_h

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
        # the same amplitude value always maps to the same color regardless
        # of zoom level.  This makes it possible to visually track features
        # across pan/zoom without the color shifting under the user's feet.
        span_arg = None
        if self._color_mode == "global":
            global_y0, global_y1 = self._y_range
            span_arg = [global_y0, global_y1]

        # Compute total points in viewport across all layers to determine
        # a single canvas size shared by all layers — this ensures the
        # Porter-Duff compositing loop always gets arrays of the same shape.
        shared_w, shared_h = self._compute_canvas_size(x0, x1, y0, y1)

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

                # Color mapping controlled by color_mode:
                #   "global" → span=[full_y0, full_y1]
                #       Stable colors across all zoom levels — a 50 Jy point
                #       always maps to the same color.  Best for flagging.
                #   "local" → span=[viewport_y_min, viewport_y_max]
                #       Full palette spans whatever amplitudes are visible
                #       in the current viewport.  Colors change on zoom
                #       (expected).  Best for exploring structure.
                #
                # Within either color_mode, lyr.scaling selects the actual
                # value-to-color transform (Phase 0 CM-1).  "eq_hist" is
                # the default — see colormap_scaling module docstring for
                # why linear scaling saturates on real visibility data.
                #
                # Unlike VisibilityRaster (where the y-axis is often a
                # different quantity than the rendered color, e.g. TIME
                # vs AMPLITUDE), here the y-axis IS the rendered quantity,
                # so df["y"] directly gives the reference value array
                # needed for "global" eq_hist equalization — no separate
                # full-data cache is needed.
                if self._color_mode == "local":
                    visible_y = df.loc[
                        (df["x"] >= x0) & (df["x"] <= x1) &
                        (df["y"] >= y0) & (df["y"] <= y1),
                        "y"
                    ]
                    if len(visible_y) > 0:
                        span = [float(visible_y.min()), float(visible_y.max())]
                        eq_hist_reference = visible_y.to_numpy()
                    else:
                        span = [float(self._y_range[0]), float(self._y_range[1])]
                        eq_hist_reference = None
                else:  # "global"
                    span = span_arg
                    eq_hist_reference = df["y"].to_numpy()

                if lyr.scaling in _cms.DATASHADER_HOW:
                    shade_kwargs = dict(
                        cmap=lyr.cmap,
                        how=_cms.DATASHADER_HOW[lyr.scaling],
                    )
                    if span is not None:
                        shade_kwargs["span"] = span
                    img = tf.shade(agg, **shade_kwargs)
                elif lyr.scaling == "eq_hist":
                    transformed = _cms.equalize_histogram(
                        agg.values, reference=eq_hist_reference,
                    )
                    scaled_agg = agg.copy(data=transformed)
                    img = tf.shade(
                        scaled_agg, cmap=lyr.cmap, how="linear", span=[0.0, 1.0]
                    )
                else:
                    transformed = _cms.apply_explicit_scaling(
                        agg.values,
                        lyr.scaling,
                        alpha=lyr.scaling_alpha,
                        gamma=lyr.scaling_gamma,
                        vmin=span[0] if span is not None else None,
                        vmax=span[1] if span is not None else None,
                    )
                    scaled_agg = agg.copy(data=transformed)
                    img = tf.shade(
                        scaled_agg, cmap=lyr.cmap, how="linear", span=[0.0, 1.0]
                    )
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
        """Toggle color mode and re-composite without re-querying the backend.

        Parameters
        ----------
        mode : ``"global"`` | ``"local"``
            ``"global"`` (default) — ``linear`` shading with ``span`` anchored
            to the full data y_range.  A 50 Jy point always maps to the same
            color regardless of zoom level.  Recommended for flagging.
            ``"local"`` — ``linear`` shading with ``span`` derived from the
            amplitude range of the data visible in the current viewport.  The
            full Plasma palette spans whatever is on screen, so zooming into
            a narrow amplitude range uses the full color range for that
            region.  Colors change on zoom (expected).  Best for exploring
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
        """Handle j2p message to toggle color mode: {mode: "global"|"local"}.

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

    def _handle_update_scaling(self, message: dict) -> dict:
        """Handle j2p 'vs_update_scaling': {layer_index, scaling, alpha, gamma}.

        All fields except layer_index are optional; omitted fields keep
        their current per-layer value. Uses _image_response so JS can
        update image_source directly, mirroring _handle_set_alpha.
        """
        idx = int(message.get("layer_index", 0))
        try:
            self.update_scaling(
                idx,
                scaling = message.get("scaling"),
                alpha   = message.get("alpha"),
                gamma   = message.get("gamma"),
            )
        except (IndexError, ValueError) as exc:
            return {"status": "error", "message": str(exc)}
        lyr = self._layers[idx]
        return self._image_response(
            "ok",
            layer_index   = idx,
            scaling       = lyr.scaling,
            scaling_alpha = lyr.scaling_alpha,
            scaling_gamma = lyr.scaling_gamma,
        )

    def _handle_update_axes_scatter(self, message: dict) -> dict:
        """Handle j2p 'vs_update_axes' with scatter-specific fields."""
        from .axes import Axis
        new_layers = None
        if "layers" in message:
            try:
                new_layers = [
                    ScatterLayer(
                        y_axis        = Axis[entry["y_axis"]],
                        polarization  = entry.get("polarization", "XX"),
                        alpha         = float(entry.get("alpha", 1.0)),
                        label         = entry.get("label", ""),
                        scaling       = entry.get("scaling", _DEFAULT_SCALING),
                        scaling_alpha = float(entry.get("scaling_alpha", 10.0)),
                        scaling_gamma = float(entry.get("scaling_gamma", 1.0)),
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
