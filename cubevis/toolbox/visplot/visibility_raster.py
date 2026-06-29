"""
visibility_raster.py
====================
Datashader-rendered raster plot for visibility data.

Derives from ``VisibilityPlot`` which owns all CommMgr/Comm wiring,
``_state_source`` management, figure construction, tick formatters, hover
tool, and pan/zoom rerender trigger.

Raster-specific additions
--------------------------
* ``query_raster`` backend call → 2D float64 agg
* Two-level pan/zoom: Level-1 Datashader ``Canvas.raster()`` resample
  (fast, no backend query) vs Level-2 backend re-query when zoomed past
  agg resolution and ``is_decimated=True``
* ``interpolate='nearest'`` when upsampling (canvas pixel < one agg cell)
  to show crisp block boundaries rather than bilinear blur
* ``_state_source`` extra fields: ``agg_n_x``, ``agg_n_y`` for the 1:1
  zoom button
* ``update_axes(quantity=, polarization=)`` extends the base signature
* ``_data_to_pixel()`` converts hover data-space coordinates to agg indices

Package location
----------------
``cubevis/cubevis/toolbox/visplot/visibility_raster.py``
"""

from __future__ import annotations

import logging
import time
from typing import Optional, TYPE_CHECKING
from uuid import uuid4

import numpy as np

from bokeh.models import ColumnDataSource

from .visibility_plot import VisibilityPlot, _img_to_uint32, _axis_label

if TYPE_CHECKING:
    import xarray as xr
    from .visibility_reader import VisibilityReader
    from .selection import SelectionSpec
    from .axes import Axis

log = logging.getLogger(__name__)

try:
    import datashader as ds
    import datashader.transfer_functions as tf
    HAS_DATASHADER = True
except ImportError:
    HAS_DATASHADER = False

_DEFAULT_CMAP = [
    "#0d0887", "#46039f", "#7201a8", "#9c179e", "#bd3786",
    "#d8576b", "#ed7953", "#fb9f3a", "#fdcb26", "#f0f921",
]


def _auto_title(quantity: "Axis", y_dim: "Axis", x_dim: "Axis",
                polarization: str) -> str:
    return (
        f"{quantity.label}  "
        f"[{y_dim.label} vs {x_dim.label}]"
        f"  pol={polarization}"
    )


class VisibilityRaster(VisibilityPlot):
    """Datashader raster plot for a single visibility quantity.

    Parameters
    ----------
    backend : VisibilityReader
        Opened reader (``LocalVisibilityReader`` wrapping an
        ``MSv2Backend`` or ``MSv4Backend``, or a
        ``RemoteReductionContext`` for remote sessions).
    selection : SelectionSpec
        Data selection.
    y_dim : Axis
        Native axis for the raster y dimension (rows).
    x_dim : Axis
        Native axis for the raster x dimension (columns).
    quantity : Axis
        Derived quantity rendered as colour (AMPLITUDE, PHASE, etc.).
    polarization : str
        Correlation product label, e.g. ``"XX"``.
    width, height : int
        Canvas dimensions in pixels.
    title : str | None
        Figure title; ``None`` → auto-generated.
    comm_mgr :
        ``CommMgr`` from the active ``BokehAppContext``.
    cmap : list[str] | None
        Colour map hex strings.  Defaults to Plasma.
    max_cells : int
        Agg cell budget for ``query_raster`` decimation.
    """

    def __init__(
        self,
        backend: "VisibilityReader",
        selection: "SelectionSpec",
        y_dim: "Axis",
        x_dim: "Axis",
        quantity: "Axis",
        polarization: str = "XX",
        width: int  = 900,
        height: int = 600,
        title: Optional[str] = None,
        comm_mgr=None,
        cmap: Optional[list] = None,
        max_cells: int = 2_000_000,
        color_mode: str = "global",
        **kwargs,
    ) -> None:
        self._quantity     = quantity
        self._polarization = polarization
        self._cmap         = cmap or _DEFAULT_CMAP
        self._max_cells    = max_cells
        self._color_mode   = color_mode

        # Raster-specific state (set by _render)
        self._agg:          Optional["xr.DataArray"] = None
        self._is_decimated: bool                     = False

        # Per-instance uuid for the update_axes message
        self._msg_update_axes = str(uuid4())
        self._msg_color_mode  = str(uuid4())

        super().__init__(
            backend   = backend,
            selection = selection,
            y_dim     = y_dim,
            x_dim     = x_dim,
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
    def agg(self) -> Optional["xr.DataArray"]:
        """Cached float64 2D DataArray from the last ``query_raster`` call."""
        return self._agg

    def update_axes(
        self,
        y_dim: Optional["Axis"]  = None,
        x_dim: Optional["Axis"]  = None,
        quantity: Optional["Axis"] = None,
        polarization: Optional[str] = None,
        title: Optional[str] = None,
    ) -> None:
        """Change axes, quantity, or polarization and re-render in place.

        Extends the base ``update_axes`` with ``quantity`` and
        ``polarization`` parameters specific to raster mode.
        """
        changed = False
        if quantity is not None and quantity != self._quantity:
            self._quantity = quantity;  changed = True
        if polarization is not None and polarization != self._polarization:
            self._polarization = polarization;  changed = True

        # Delegate y_dim / x_dim / title changes to base; it calls _render
        # and _notify_axes_changed.  If only quantity/polarization changed
        # we must trigger those ourselves.
        if y_dim is not None or x_dim is not None or title is not None or changed:
            if y_dim is not None and y_dim != self._y_dim:
                self._y_dim = y_dim
            if x_dim is not None and x_dim != self._x_dim:
                self._x_dim = x_dim
            if title is not None:
                self._title = title
            self._render(self._selection)
            self._notify_axes_changed()

    # ------------------------------------------------------------------
    # VisibilityPlot abstract interface
    # ------------------------------------------------------------------

    def _comm_description(self) -> str:
        return "visibility raster"

    def _effective_title(self) -> str:
        return self._title or _auto_title(
            self._quantity, self._y_dim, self._x_dim, self._polarization
        )

    def set_color_mode(self, mode: str) -> None:
        """Toggle colour mode and re-render.

        Parameters
        ----------
        mode : ``"global"`` | ``"local"``
            ``"global"`` — span = full data y_range; colours stable on zoom.
            ``"local"`` — Datashader normalises to viewport; reveals detail.
        """
        if mode not in ("global", "local"):
            raise ValueError(f"color_mode must be 'global' or 'local', got {mode!r}")
        self._color_mode = mode
        self._render(self._selection)

    def _state_data_extra(self) -> dict:
        """Add agg shape and color_mode to _state_source."""
        agg = self._agg
        n_x = agg.shape[1] if agg is not None and agg.ndim == 2 else 1
        n_y = agg.shape[0] if agg is not None and agg.ndim == 2 else 1
        return {"agg_n_x": [n_x], "agg_n_y": [n_y], "color_mode": [self._color_mode]}

    def _build_glyphs(self) -> None:
        """Add the single image_rgba glyph for the raster."""
        self._fig.image_rgba(
            source = self._image_source,
            image  = "image",
            x = "x", y = "y", dw = "dw", dh = "dh",
        )

    def _render(
        self,
        selection: "SelectionSpec",
        max_cells: Optional[int] = None,
    ) -> None:
        """Run query_raster → shade → update _image_source."""
        t0     = time.perf_counter()
        budget = max_cells if max_cells is not None else self._max_cells

        agg, x_range, y_range, is_decimated = self._backend.query_raster(
            y_dim        = self._y_dim,
            x_dim        = self._x_dim,
            quantity     = self._quantity,
            selection    = selection,
            polarization = self._polarization,
            max_cells    = budget,
        )
        log.debug(
            "query_raster: agg=%s  x=%s  y=%s  decimated=%s  (%.3fs)",
            agg.shape, x_range, y_range, is_decimated,
            time.perf_counter() - t0,
        )

        self._agg          = agg
        self._x_range      = x_range
        self._y_range      = y_range
        self._is_decimated = is_decimated

        x0, x1 = x_range
        y0, y1 = y_range

        _degenerate = (
            agg.shape[0] < 2
            or agg.shape[1] < 2
            or not np.isfinite(agg.values).any()
            or x0 == x1
            or y0 == y1
        )

        if _degenerate:
            log.debug("_render: degenerate agg — blank image")
            img32 = np.zeros((self._height, self._width), dtype=np.uint32)
        else:
            cvs    = ds.Canvas(
                plot_width  = self._width,
                plot_height = self._height,
                x_range     = (x0, x1),
                y_range     = (y0, y1),
            )
            ds_agg = cvs.raster(agg)
            shade_kw = dict(cmap=self._cmap, how="linear")
            if self._color_mode == "global":
                shade_kw["span"] = [float(y0), float(y1)]
            shaded = tf.shade(ds_agg, **shade_kw)
            img32  = _img_to_uint32(shaded)

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

        # Keep _state_source in sync (no-op before _state_source exists)
        self._update_state_source()

    def _do_viewport_rerender(
        self, x0: float, x1: float, y0: float, y1: float
    ) -> dict:
        """Two-level pan/zoom: Level-1 Datashader resample or Level-2 re-query."""
        agg = self._agg
        if agg is not None and agg.shape[0] >= 2 and agg.shape[1] >= 2:
            agg_cell_w = (self._x_range[1] - self._x_range[0]) / agg.shape[1]
            agg_cell_h = (self._y_range[1] - self._y_range[0]) / agg.shape[0]
            needs_requery = (
                self._is_decimated
                and (x1 - x0) / self._width  < agg_cell_w
                and (y1 - y0) / self._height < agg_cell_h
            )
        else:
            needs_requery = False

        if needs_requery:
            log.debug("_do_viewport_rerender: Level-2 re-query")
            sub_sel = self._viewport_selection((x0, x1), (y0, y1))
            self._render(sub_sel, max_cells=self._max_cells * 4)
            img32 = self._shade_viewport(self._x_range, self._y_range)
            out_x0, out_x1 = self._x_range
            out_y0, out_y1 = self._y_range
        else:
            log.debug("_do_viewport_rerender: Level-1 resample")
            img32 = self._shade_viewport((x0, x1), (y0, y1))
            out_x0, out_x1 = x0, x1
            out_y0, out_y1 = y0, y1

        return {
            "image": img32,
            "x0": out_x0, "x1": out_x1,
            "y0": out_y0, "y1": out_y1,
        }

    def _handle_probe(self, message: dict) -> dict:
        x = float(message.get("x", 0.0))
        y = float(message.get("y", 0.0))

        x0, x1 = self._x_range
        y0, y1 = self._y_range
        if not (x0 <= x <= x1 and y0 <= y <= y1):
            return {"label": "<i>out of range</i>"}

        px, py = self._data_to_pixel(x, y)
        if px is None:
            return {"label": "<i>out of range</i>"}
        try:
            info  = self._backend.probe_raster_pixel(
                self._agg, px, py, self._selection
            )
            label = self._format_probe(info, self._quantity.label)
        except Exception as exc:
            log.warning("probe_raster_pixel failed: %s", exc)
            label = f"<span style='color:#f38ba8'>probe error: {exc}</span>"
        return {"label": label}

    # ------------------------------------------------------------------
    # Raster-specific internals
    # ------------------------------------------------------------------

    def _shade_viewport(
        self,
        x_range: tuple[float, float],
        y_range: tuple[float, float],
    ) -> np.ndarray:
        """Resample cached agg; use nearest-neighbour when upsampling."""
        agg = self._agg
        x0, x1 = x_range
        y0, y1 = y_range

        if (
            agg is None
            or agg.shape[0] < 2
            or agg.shape[1] < 2
            or x0 == x1
            or y0 == y1
        ):
            return np.zeros((self._height, self._width), dtype=np.uint32)

        agg_cell_w = (self._x_range[1] - self._x_range[0]) / agg.shape[1]
        agg_cell_h = (self._y_range[1] - self._y_range[0]) / agg.shape[0]
        upsampling  = (
            (x1 - x0) / self._width  < agg_cell_w
            or (y1 - y0) / self._height < agg_cell_h
        )
        interpolate = "nearest" if upsampling else "linear"

        cvs    = ds.Canvas(
            plot_width  = self._width,
            plot_height = self._height,
            x_range     = (x0, x1),
            y_range     = (y0, y1),
        )
        ds_agg = cvs.raster(agg, interpolate=interpolate)
        shade_kw = dict(cmap=self._cmap, how="linear")
        if self._color_mode == "global":
            shade_kw["span"] = [float(self._y_range[0]), float(self._y_range[1])]
        shaded = tf.shade(ds_agg, **shade_kw)
        return _img_to_uint32(shaded)

    def _data_to_pixel(
        self, x: float, y: float
    ) -> tuple[Optional[int], Optional[int]]:
        """Map data-space (x, y) to agg grid indices (px, py)."""
        if self._agg is None:
            return None, None
        agg        = self._agg
        y_dim_name = agg.dims[0]
        x_dim_name = agg.dims[1]
        if x_dim_name not in agg.coords or y_dim_name not in agg.coords:
            return None, None
        x_coords = agg.coords[x_dim_name].values
        y_coords = agg.coords[y_dim_name].values
        if len(x_coords) == 0 or len(y_coords) == 0:
            return None, None
        px = int(np.argmin(np.abs(x_coords - x)))
        py = int(np.argmin(np.abs(y_coords - y)))
        h, w = agg.shape
        return max(0, min(px, w - 1)), max(0, min(py, h - 1))

    def _register_extra_comm_handlers(self) -> None:
        self._comm.register(self._msg_update_axes, self._handle_update_axes_raster)
        self._comm.register(self._msg_color_mode,  self._handle_set_color_mode_raster)

    def _handle_set_color_mode_raster(self, message: dict) -> dict:
        """Handle j2p message to toggle colour mode: {mode: "global"|"local"}."""
        mode = message.get("mode", "global")
        try:
            self.set_color_mode(mode)
        except ValueError as exc:
            return {"status": "error", "message": str(exc)}
        # Return the new image so JS can update image_source directly
        src = self._image_source.data
        return {
            "status":     "ok",
            "color_mode": self._color_mode,
            "image":      src["image"][0],
            "x0":         src["x"][0],
            "x1":         src["x"][0] + src["dw"][0],
            "y0":         src["y"][0],
            "y1":         src["y"][0] + src["dh"][0],
        }

    def _handle_update_axes_raster(self, message: dict) -> dict:
        """Handle j2p 'vr_update_axes' with raster-specific fields."""
        self.update_axes(
            y_dim        = self._parse_axis(message, "y_dim"),
            x_dim        = self._parse_axis(message, "x_dim"),
            quantity     = self._parse_axis(message, "quantity"),
            polarization = message.get("polarization"),
            title        = message.get("title"),
        )
        return {"status": "ok"}
