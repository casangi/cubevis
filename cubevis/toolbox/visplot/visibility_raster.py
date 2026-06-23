"""
visibility_raster.py
====================
Bokeh Model wrapping a Datashader-rendered visibility raster.

Axis switching after page-load
--------------------------------
Without Bokeh Server there is no property-sync channel after ``show()``
serialises the document to static HTML.  The only way to update the browser
is through the ``Comm``/``CommMgr`` j2p/p2j channel.

All JS values that depend on the current axes — tick formatter parameters,
1:1-zoom viewport dimensions, axis labels — are stored in a small Bokeh
``ColumnDataSource`` called ``_state_source``.  Every ``CustomJS`` callback
and formatter reads from this source at call time rather than from baked-in
closure literals.  When the Python side changes axes it:

1. Re-queries the backend with the new axes (``_render``).
2. Updates ``_state_source.data`` with the new agg shape, range extents,
   axis labels, and tick-formatter parameters.
3. Pushes the new RGBA image into ``_image_source.data``.
4. Sends a ``p2j`` ``'vr_axes_changed'`` message so the JS side can
   update the Bokeh Figure's ``x_axis_label`` / ``y_axis_label`` text
   (which cannot be driven by a ColumnDataSource directly).

``_state_source`` fields
------------------------
``agg_n_x``, ``agg_n_y``   int   agg cell counts — for 1:1 zoom
``full_x0``, ``full_x1``   float full data extents on x — for 1:1 zoom / clamp
``full_y0``, ``full_y1``   float full data extents on y
``t0``                     float y-axis time origin for tick formatter (MJD s)
``y_is_time``              int   1 if y_dim == TIME, else 0
``x_is_time``              int   1 if x_dim == TIME, else 0
``x_label``                str   x-axis label text
``y_label``                str   y-axis label text
``title``                  str   figure title

CommMgr / Comm integration
---------------------------
* ``comm_mgr.open(description=..., squash_queue=True)`` opens a ``Comm``.
* The ``Comm`` and ``_state_source`` / ``_image_source`` objects are passed
  directly into every ``CustomJS`` ``args`` dict so Bokeh serialises them as
  proper Model references.
* ``Comm.initialize()`` in TypeScript reconnects via
  ``document.get_model_by_name(comm_mgr_id)`` — correct inside Colab iframes.
* JS sends: ``comm.send(messageId, payload, callback)``.

Package location
----------------
``cubevis/cubevis/toolbox/visplot/visibility_raster.py``
"""

from __future__ import annotations

import logging
import time
from typing import Optional, TYPE_CHECKING

import numpy as np

from bokeh.model import Model
from bokeh.core.properties import String, Int, Bool
from bokeh.models import ColumnDataSource, CustomJS, CustomJSTickFormatter, Div, HoverTool
from bokeh.plotting import figure, show as bk_show
from bokeh.layouts import column

if TYPE_CHECKING:
    import xarray as xr
    from .reader import XArrayReader
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


# ---------------------------------------------------------------------------
# Image conversion helper
# ---------------------------------------------------------------------------

def _img_to_uint32(img) -> np.ndarray:
    """Extract a Bokeh-compatible uint32 RGBA array from a Datashader Image.

    ``datashader.tf.shade()`` returns a Datashader ``Image`` — an
    ``xr.DataArray`` subclass with dtype ``uint32`` already packed as
    0xAARRGGBB, exactly what Bokeh's ``image_rgba`` glyph expects.

    Also handles a PIL ``Image`` (mode ``"RGBA"``, uint8) from unit tests.
    """
    arr = np.array(img)
    if arr.dtype == np.uint32:
        return arr
    if arr.ndim == 3 and arr.shape[2] == 4:
        h, w = arr.shape[:2]
        out  = np.empty((h, w), dtype=np.uint32)
        view = out.view(np.uint8).reshape(h, w, 4)
        view[..., 0] = arr[..., 2]
        view[..., 1] = arr[..., 1]
        view[..., 2] = arr[..., 0]
        view[..., 3] = arr[..., 3]
        return out
    raise ValueError(
        f"_img_to_uint32: unrecognised input — shape={arr.shape}, "
        f"dtype={arr.dtype}"
    )


# ---------------------------------------------------------------------------
# Axis label helper
# ---------------------------------------------------------------------------

def _axis_label(axis: "Axis") -> str:
    """Format a display label with optional unit suffix."""
    return f"{axis.label}" + (f" [{axis.unit}]" if axis.unit else "")


def _auto_title(quantity: "Axis", y_dim: "Axis", x_dim: "Axis",
                polarization: str) -> str:
    return (
        f"{quantity.label}  "
        f"[{y_dim.label} vs {x_dim.label}]"
        f"  pol={polarization}"
    )


# ---------------------------------------------------------------------------
# VisibilityRaster
# ---------------------------------------------------------------------------

class VisibilityRaster(Model):
    """Bokeh Model for a Datashader-rendered visibility raster.

    Supports axis switching after page-load via the ``update_axes()`` method
    and the ``'vr_update_axes'`` Comm message.  All axis-dependent JS values
    are stored in ``_state_source`` (a ``ColumnDataSource``) so that
    ``CustomJS`` callbacks read them at call time rather than from
    construction-time literals.

    Parameters
    ----------
    backend : XArrayReader
        Opened backend (``MSv2Backend`` or ``MSv4Backend``).
    selection : SelectionSpec
        Data selection (SPW, field, scan, baselines, time range, ...).
    y_dim : Axis
        Native axis for the raster y dimension (rows).
    x_dim : Axis
        Native axis for the raster x dimension (columns).
    quantity : Axis
        Derived quantity rendered as colour.
    polarization : str
        Correlation product label, e.g. ``"XX"``.
    width : int
        Canvas width in pixels.
    height : int
        Canvas height in pixels.
    title : str | None
        Figure title; ``None`` → auto-generated.
    comm_mgr :
        ``CommMgr`` from the active ``BokehAppContext``.  Auto-retrieved
        from ``BokehInit.get_app_context().comm_mgr`` when ``None``.
    cmap : list[str] | None
        Colour map hex strings.  Defaults to Plasma.
    max_cells : int
        Maximum agg cell budget; see ``XArrayReader.query_raster``.
    """

    # Bokeh Model properties (synced to JavaScript)
    vr_id         = String(default="",   help="Unique ID for this instance")
    canvas_width  = Int(default=900,     help="Canvas width in pixels")
    canvas_height = Int(default=600,     help="Canvas height in pixels")
    status_text   = String(default="",   help="Status / hover label text")
    is_rendering  = Bool(default=False,  help="True while a re-render is in flight")

    def __init__(
        self,
        backend: "XArrayReader",
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
        **kwargs,
    ) -> None:
        import uuid
        kwargs.setdefault("vr_id",         str(uuid.uuid4())[:8])
        kwargs.setdefault("canvas_width",  width)
        kwargs.setdefault("canvas_height", height)
        super().__init__(**kwargs)

        self._backend      = backend
        self._selection    = selection
        self._y_dim        = y_dim
        self._x_dim        = x_dim
        self._quantity     = quantity
        self._polarization = polarization
        self._width        = width
        self._height       = height
        self._cmap         = cmap or _DEFAULT_CMAP
        self._max_cells    = max_cells
        self._title        = title  # None → auto

        # Resolve CommMgr
        if comm_mgr is None:
            try:
                from cubevis.bokeh import BokehInit
                ctx = BokehInit.get_app_context()
                comm_mgr = ctx.comm_mgr if ctx is not None else None
            except Exception:
                comm_mgr = None
        self._comm_mgr = comm_mgr

        self._comm = None
        if self._comm_mgr is not None:
            try:
                self._comm = self._comm_mgr.open(
                    description="visibility raster",
                    squash_queue=True,
                )
            except Exception as exc:
                log.warning("VisibilityRaster: could not open Comm: %s", exc)

        # State updated by _render() and _update_state_source()
        self._agg: Optional["xr.DataArray"] = None
        self._x_range: tuple[float, float]  = (0.0, 1.0)
        self._y_range: tuple[float, float]  = (0.0, 1.0)
        self._is_decimated: bool            = False

        # Bokeh sources — created in _build()
        self._image_source: Optional[ColumnDataSource] = None
        self._state_source: Optional[ColumnDataSource] = None
        self._fig   = None
        self._info_div: Optional[Div] = None
        self._layout = None

        self._build()
        if self._comm is not None:
            self._register_comm_handlers()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def figure(self):
        return self._fig

    @property
    def layout(self):
        return self._layout

    @property
    def agg(self) -> Optional["xr.DataArray"]:
        return self._agg

    def show(self) -> None:
        bk_show(self._layout)

    def update_axes(
        self,
        y_dim: Optional["Axis"] = None,
        x_dim: Optional["Axis"] = None,
        quantity: Optional["Axis"] = None,
        polarization: Optional[str] = None,
        title: Optional[str] = None,
    ) -> None:
        """Change the plot axes and re-render in place.

        Sends a ``'vr_axes_changed'`` p2j message to the browser so it can
        update the Bokeh Figure's axis label text (which cannot be driven by
        ``_state_source`` directly).

        Parameters
        ----------
        y_dim, x_dim : Axis | None
            New y/x dimension.  ``None`` keeps the current value.
        quantity : Axis | None
            New quantity (colour axis).  ``None`` keeps the current value.
        polarization : str | None
            New polarization label.  ``None`` keeps the current value.
        title : str | None
            New figure title.  ``None`` auto-generates from the new axes.
        """
        changed = False
        if y_dim is not None and y_dim != self._y_dim:
            self._y_dim = y_dim;  changed = True
        if x_dim is not None and x_dim != self._x_dim:
            self._x_dim = x_dim;  changed = True
        if quantity is not None and quantity != self._quantity:
            self._quantity = quantity;  changed = True
        if polarization is not None and polarization != self._polarization:
            self._polarization = polarization;  changed = True
        if title is not None:
            self._title = title;  changed = True

        if not changed:
            return

        # Re-query backend with new axes (_render also calls _update_state_source)
        self._render(self._selection)

        # Send p2j to update figure axis labels (can't be done via
        # _state_source alone — Bokeh axis label is not a CDS column)
        if self._comm is not None:
            try:
                effective_title = self._title or _auto_title(
                    self._quantity, self._y_dim, self._x_dim, self._polarization
                )
                self._comm.send_p2j("vr_axes_changed", {
                    "x_label": _axis_label(self._x_dim),
                    "y_label": _axis_label(self._y_dim),
                    "title":   effective_title,
                    "vr_id":   self.vr_id,
                })
            except Exception as exc:
                log.warning("update_axes: could not send p2j: %s", exc)

    def rerender(
        self,
        x_range: Optional[tuple[float, float]] = None,
        y_range: Optional[tuple[float, float]] = None,
        new_selection: Optional["SelectionSpec"] = None,
    ) -> None:
        """Re-render the raster without changing axes.

        Viewport change (pan/zoom)
            Pass ``x_range`` and/or ``y_range``.  Datashader resamples the
            cached agg; no backend query.

        Selection change
            Pass ``new_selection``.  Full backend re-query, but axes unchanged.
            Does NOT permanently replace ``self._selection``.

        No args
            Full re-render with the existing selection.
        """
        if new_selection is not None:
            self._render(new_selection)
        elif x_range is not None or y_range is not None:
            xr = x_range or self._x_range
            yr = y_range or self._y_range
            img32 = self._shade_viewport(xr, yr)
            self._image_source.data = {
                "image": [img32],
                "x":     [xr[0]],
                "y":     [yr[0]],
                "dw":    [xr[1] - xr[0]],
                "dh":    [yr[1] - yr[0]],
            }
        else:
            self._render(self._selection)

    # ------------------------------------------------------------------
    # Internal: state source
    # ------------------------------------------------------------------

    def _state_data(self) -> dict:
        """Build the ``_state_source.data`` dict from current Python state.

        All JS callbacks read from this dict at call time.  Updating it
        (via ``_update_state_source``) is sufficient to change axis-dependent
        behaviour for all pre-wired callbacks without re-creating any JS.
        """
        from .axes import Axis
        x0, x1 = self._x_range
        y0, y1 = self._y_range
        agg     = self._agg
        n_x     = agg.shape[1] if agg is not None and agg.ndim == 2 else 1
        n_y     = agg.shape[0] if agg is not None and agg.ndim == 2 else 1

        return {
            # 1:1 zoom parameters
            "agg_n_x":  [n_x],
            "agg_n_y":  [n_y],
            "full_x0":  [float(x0)],
            "full_x1":  [float(x1)],
            "full_y0":  [float(y0)],
            "full_y1":  [float(y1)],
            # Tick formatter parameters
            "t0":       [float(y0 if self._y_dim == Axis.TIME else x0)],
            "y_is_time":[int(self._y_dim == Axis.TIME)],
            "x_is_time":[int(self._x_dim == Axis.TIME)],
            # Axis labels (read by the 'vr_axes_changed' JS handler)
            "x_label":  [_axis_label(self._x_dim)],
            "y_label":  [_axis_label(self._y_dim)],
        }

    def _update_state_source(self) -> None:
        """Push current axis state into ``_state_source``."""
        if self._state_source is not None:
            self._state_source.data = self._state_data()

    # ------------------------------------------------------------------
    # Internal: build
    # ------------------------------------------------------------------

    def _build(self) -> None:
        if not HAS_DATASHADER:
            raise ImportError(
                "datashader is required for VisibilityRaster.\n"
                "Install: pip install datashader"
            )

        # Initial data query
        self._render(self._selection)

        x0, x1 = self._x_range
        y0, y1 = self._y_range

        # State source: all axis-dependent values read by JS at call time
        self._state_source = ColumnDataSource(data=self._state_data())

        # Figure
        effective_title = self._title or _auto_title(
            self._quantity, self._y_dim, self._x_dim, self._polarization
        )
        self._fig = figure(
            title         = effective_title,
            width         = self._width,
            height        = self._height,
            x_range       = (x0, x1),
            y_range       = (y0, y1),
            x_axis_label  = _axis_label(self._x_dim),
            y_axis_label  = _axis_label(self._y_dim),
            tools         = "pan,wheel_zoom,box_zoom,reset,save",
            active_scroll = "wheel_zoom",
        )

        self._fig.image_rgba(
            source = self._image_source,
            image  = "image",
            x = "x", y = "y", dw = "dw", dh = "dh",
        )

        # Tick formatter — reads t0 and y_is_time from _state_source at
        # call time so it works correctly after an axis change.
        self._fig.yaxis.formatter = CustomJSTickFormatter(
            args={"state": self._state_source},
            code="""
const y_is_time = state.data['y_is_time'][0];
if (!y_is_time) return tick.toFixed(4);
const t0      = state.data['full_y0'][0];
const elapsed = tick - t0;
if (Math.abs(elapsed) < 60)
    return elapsed.toFixed(1) + ' s';
const m    = Math.floor(Math.abs(elapsed) / 60);
const s    = Math.round(Math.abs(elapsed) % 60);
const sign = elapsed < 0 ? '-' : '';
return sign + m + 'm ' + s.toString().padStart(2, '0') + 's';
""",
        )

        # x-axis formatter (mirrors y for completeness)
        self._fig.xaxis.formatter = CustomJSTickFormatter(
            args={"state": self._state_source},
            code="""
const x_is_time = state.data['x_is_time'][0];
if (!x_is_time) return tick.toFixed(4);
const t0      = state.data['full_x0'][0];
const elapsed = tick - t0;
if (Math.abs(elapsed) < 60)
    return elapsed.toFixed(1) + ' s';
const m    = Math.floor(Math.abs(elapsed) / 60);
const s    = Math.round(Math.abs(elapsed) % 60);
const sign = elapsed < 0 ? '-' : '';
return sign + m + 'm ' + s.toString().padStart(2, '0') + 's';
""",
        )

        self._info_div = Div(
            text   = "<i>Hover over the plot to inspect a pixel</i>",
            width  = self._width,
            styles = {
                "font-size":   "12px",
                "font-family": "monospace",
                "padding":     "4px 8px",
                "background":  "#1e1e2e",
                "color":       "#cdd6f4",
                "border-top":  "1px solid #45475a",
            },
        )

        self._layout = column(self._fig, self._info_div)

        self._add_hover_tool()
        if self._comm is not None:
            self._add_rerender_trigger()
            self._add_axes_changed_handler()

    # ------------------------------------------------------------------
    # Internal: render pipeline
    # ------------------------------------------------------------------

    def _render(
        self,
        selection: "SelectionSpec",
        max_cells: Optional[int] = None,
    ) -> None:
        """Run query_raster → shade → update _image_source."""
        t0_perf = time.perf_counter()
        budget  = max_cells if max_cells is not None else self._max_cells

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
            time.perf_counter() - t0_perf,
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
            log.debug("_render: degenerate agg — returning blank image")
            img32 = np.zeros((self._height, self._width), dtype=np.uint32)
        else:
            cvs    = ds.Canvas(
                plot_width  = self._width,
                plot_height = self._height,
                x_range     = (x0, x1),
                y_range     = (y0, y1),
            )
            ds_agg = cvs.raster(agg)
            shaded = tf.shade(ds_agg, cmap=self._cmap, how="linear")
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

        # Keep _state_source in sync with the new agg shape and ranges.
        # Called here so every code path that calls _render() automatically
        # keeps the JS callbacks (1:1 zoom, tick formatter) up to date.
        # The no-op guard in _update_state_source handles the initial call
        # from _build() before _state_source exists.
        self._update_state_source()

    # ------------------------------------------------------------------
    # Internal: Datashader viewport resample
    # ------------------------------------------------------------------

    def _shade_viewport(
        self,
        x_range: tuple[float, float],
        y_range: tuple[float, float],
    ) -> np.ndarray:
        """Resample cached agg over a viewport; switch interpolation on upsample."""
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

        # nearest-neighbour when upsampling (each canvas px < one agg cell)
        agg_cell_w  = (self._x_range[1] - self._x_range[0]) / agg.shape[1]
        agg_cell_h  = (self._y_range[1] - self._y_range[0]) / agg.shape[0]
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
        shaded = tf.shade(ds_agg, cmap=self._cmap, how="linear")
        return _img_to_uint32(shaded)

    # ------------------------------------------------------------------
    # Internal: viewport selection narrowing
    # ------------------------------------------------------------------

    def _viewport_selection(
        self,
        x_range: Optional[tuple[float, float]],
        y_range: Optional[tuple[float, float]],
    ) -> "SelectionSpec":
        from .axes import Axis
        sel = self._selection.copy()

        def _apply(axis: "Axis", lo: float, hi: float) -> None:
            if axis == Axis.TIME:
                sel.time_range = (lo, hi)
            elif axis in (Axis.FREQUENCY, Axis.CHANNEL):
                sel.freq_range = (lo, hi)

        if x_range is not None:
            _apply(self._x_dim, *x_range)
        if y_range is not None:
            _apply(self._y_dim, *y_range)
        return sel

    # ------------------------------------------------------------------
    # Internal: data-space → pixel index
    # ------------------------------------------------------------------

    def _data_to_pixel(
        self, x: float, y: float
    ) -> tuple[Optional[int], Optional[int]]:
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

    # ------------------------------------------------------------------
    # Internal: hover tool
    # ------------------------------------------------------------------

    def _add_hover_tool(self) -> None:
        if self._comm is not None:
            self._add_comm_hover()
        else:
            self._add_static_hover()

    def _add_static_hover(self) -> None:
        """Static tooltip reading axis labels from _state_source."""
        # Tooltip text is generic ($x/$y); axis labels are updated by
        # the CustomJSTickFormatter on the axes themselves.
        hover = HoverTool(
            tooltips=[("x", "$x{0.5g}"), ("y", "$y{0.5g}")],
        )
        self._fig.add_tools(hover)

    def _add_comm_hover(self) -> None:
        """HoverTool sending 'vr_probe' via the Comm channel."""
        vr_id    = self.vr_id
        info_div = self._info_div
        comm     = self._comm

        hover_js = CustomJS(
            args={"info_div": info_div, "comm": comm},
            code=f"""
const now = Date.now();
if (window._cvLastProbe && (now - window._cvLastProbe) < 120) return;
window._cvLastProbe = now;
const x = cb_data.geometry.x;
const y = cb_data.geometry.y;
if (x == null || y == null) return;
comm.send('vr_probe', {{x: x, y: y, vr_id: '{vr_id}'}}, function(resp) {{
    if (resp && resp.label) info_div.text = resp.label;
}});
""",
        )
        hover = HoverTool(
            callback  = hover_js,
            tooltips  = None,
            renderers = self._fig.renderers,
        )
        self._fig.add_tools(hover)

    # ------------------------------------------------------------------
    # Internal: pan/zoom rerender trigger
    # ------------------------------------------------------------------

    def _add_rerender_trigger(self) -> None:
        """Attach range callbacks for pan/zoom re-render via Comm.

        The 1:1 zoom viewport computation reads ``agg_n_x``, ``agg_n_y``,
        and ``full_*`` from ``_state_source`` at call time so it remains
        correct after an axis change.
        """
        vr_id        = self.vr_id
        image_source = self._image_source
        state_source = self._state_source
        comm         = self._comm

        rerender_js = CustomJS(
            args={
                "image_source": image_source,
                "state":        state_source,
                "comm":         comm,
                "x_range":      self._fig.x_range,
                "y_range":      self._fig.y_range,
            },
            code=f"""
if (window._cvRerenderTimer) clearTimeout(window._cvRerenderTimer);
window._cvRerenderTimer = setTimeout(function() {{
    const x0 = x_range.start, x1 = x_range.end;
    const y0 = y_range.start, y1 = y_range.end;
    comm.send('vr_rerender',
        {{x0: x0, x1: x1, y0: y0, y1: y1, vr_id: '{vr_id}'}},
        function(resp) {{
            if (!resp || resp.image == null) return;
            image_source.data['image'] = [resp.image];
            image_source.data['x']     = [resp.x0];
            image_source.data['y']     = [resp.y0];
            image_source.data['dw']    = [resp.x1 - resp.x0];
            image_source.data['dh']    = [resp.y1 - resp.y0];
            image_source.change.emit();
        }}
    );
}}, 300);
""",
        )
        self._fig.x_range.js_on_change("end", rerender_js)
        self._fig.y_range.js_on_change("end", rerender_js)

    # ------------------------------------------------------------------
    # Internal: axes-changed JS handler (updates figure labels in browser)
    # ------------------------------------------------------------------

    def _add_axes_changed_handler(self) -> None:
        """Pre-wire a p2j 'vr_axes_changed' handler in the browser.

        When Python calls ``update_axes()``, it sends a ``'vr_axes_changed'``
        p2j message.  The pre-wired CustomJS handler updates the Bokeh
        Figure's ``x_axis_label``, ``y_axis_label``, and ``title`` text —
        operations that cannot be driven from ``_state_source`` alone because
        they are properties of the Figure model, not CDS columns.
        """
        vr_id = self.vr_id
        comm  = self._comm
        fig   = self._fig

        axes_js = CustomJS(
            args={"fig": fig, "comm": comm},
            code=f"""
comm.register('vr_axes_changed', function(msg) {{
    if (!msg || msg.vr_id !== '{vr_id}') return;
    if (msg.x_label != null) fig.xaxis[0].axis_label = msg.x_label;
    if (msg.y_label != null) fig.yaxis[0].axis_label = msg.y_label;
    if (msg.title   != null) fig.title.text           = msg.title;
}});
""",
        )
        # Execute at page-load via a dummy model trigger.
        # The cleanest hook available without Bokeh Server is a CustomJS
        # on a ColumnDataSource 'change' that fires once on load.
        # We use a one-row CDS with a sentinel column.
        init_source = ColumnDataSource(data={"_init": [1]})
        init_js = CustomJS(
            args={"fig": fig, "comm": comm},
            code=f"""
// Register the p2j axes-changed listener once at page load
comm.register('vr_axes_changed', function(msg) {{
    if (!msg || msg.vr_id !== '{vr_id}') return;
    if (msg.x_label != null) fig.xaxis[0].axis_label = msg.x_label;
    if (msg.y_label != null) fig.yaxis[0].axis_label = msg.y_label;
    if (msg.title   != null) fig.title.text           = msg.title;
}});
""",
        )
        init_source.js_on_change("data", init_js)
        # Trigger it immediately by touching the data once
        # (the source is already initialised, so we need to store it so
        # Bokeh serialises it into the document — the callback fires in JS
        # when the page loads and the CDS data is first set)
        self._axes_init_source = init_source

    # ------------------------------------------------------------------
    # Internal: CommMgr handler registration
    # ------------------------------------------------------------------

    def _register_comm_handlers(self) -> None:
        self._comm.register("vr_probe",       self._handle_probe)
        self._comm.register("vr_rerender",    self._handle_rerender)
        self._comm.register("vr_update_axes", self._handle_update_axes)

    # ------------------------------------------------------------------
    # j2p handlers
    # ------------------------------------------------------------------

    def _handle_probe(self, message: dict) -> dict:
        x = float(message.get("x", 0.0))
        y = float(message.get("y", 0.0))

        # Reject coordinates outside the current agg extents
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
            label = self._format_probe(info)
        except Exception as exc:
            log.warning("probe_raster_pixel failed: %s", exc)
            label = f"<span style='color:#f38ba8'>probe error: {exc}</span>"
        return {"label": label}

    def _handle_rerender(self, message: dict) -> dict:
        """Two-level pan/zoom re-render."""
        x0 = float(message["x0"])
        x1 = float(message["x1"])
        y0 = float(message["y0"])
        y1 = float(message["y1"])

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
            log.debug("_handle_rerender: Level-2 re-query")
            sub_sel = self._viewport_selection(
                x_range=(x0, x1), y_range=(y0, y1)
            )
            self._render(sub_sel, max_cells=self._max_cells * 4)
            img32 = self._shade_viewport(self._x_range, self._y_range)
            out_x0, out_x1 = self._x_range
            out_y0, out_y1 = self._y_range
        else:
            log.debug("_handle_rerender: Level-1 resample")
            img32 = self._shade_viewport((x0, x1), (y0, y1))
            out_x0, out_x1 = x0, x1
            out_y0, out_y1 = y0, y1

        return {
            "image": img32,
            "x0": out_x0, "x1": out_x1,
            "y0": out_y0, "y1": out_y1,
        }

    def _handle_update_axes(self, message: dict) -> dict:
        """Handle a j2p 'vr_update_axes' request from the GUI.

        The GUI can send this message to trigger an axis change from the
        browser side (e.g. from an axis-selector widget's CustomJS callback).

        Parameters
        ----------
        message : dict
            Any subset of ``{y_dim, x_dim, quantity, polarization, title}``
            as string names.  Unknown keys are ignored.
        """
        from .axes import Axis

        def _parse_axis(key: str) -> Optional["Axis"]:
            name = message.get(key)
            if name is None:
                return None
            try:
                return Axis[name]
            except KeyError:
                log.warning("_handle_update_axes: unknown Axis %r", name)
                return None

        self.update_axes(
            y_dim        = _parse_axis("y_dim"),
            x_dim        = _parse_axis("x_dim"),
            quantity     = _parse_axis("quantity"),
            polarization = message.get("polarization"),
            title        = message.get("title"),
        )
        return {"status": "ok"}

    # ------------------------------------------------------------------
    # Formatting
    # ------------------------------------------------------------------

    def _format_probe(self, info: dict) -> str:
        parts: list[str] = []

        val = info.get("value")
        if val is not None:
            parts.append(f"<b>{self._quantity.label}:</b> {val:.6g}")
        else:
            parts.append(f"<b>{self._quantity.label}:</b> <i>empty</i>")

        xc = info.get("x_centre")
        yc = info.get("y_centre")
        if xc is not None:
            parts.append(f"<b>{self._x_dim.label}:</b> {xc:.6g}")
        if yc is not None:
            parts.append(f"<b>{self._y_dim.label}:</b> {yc:.6g}")

        fg = info.get("freq_range_ghz")
        if fg is not None:
            parts.append(f"<b>Freq:</b> {fg[0]:.6g}–{fg[1]:.6g} GHz")

        for field in (info.get("field_names") or []):
            parts.append(f"<b>Field:</b> {field}")
            break

        scans = info.get("scan_names") or []
        if scans:
            parts.append(f"<b>Scan:</b> {', '.join(scans)}")

        pairs = info.get("antenna_pairs") or []
        if pairs:
            bl_str = "; ".join(f"{a}&{b}" for a, b in pairs[:3])
            if len(pairs) > 3:
                bl_str += f" (+{len(pairs)-3})"
            parts.append(f"<b>BL:</b> {bl_str}")

        return " &nbsp;|&nbsp; ".join(parts)
