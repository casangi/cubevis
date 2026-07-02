"""
visibility_plot.py
==================
Abstract base class for ``VisibilityRaster`` and ``VisibilityScatter``.

Owns all infrastructure that is common to both plot types:

* Bokeh ``Model`` properties (``vr_id``, ``canvas_width``, ``canvas_height``,
  ``status_text``, ``is_rendering``)
* ``CommMgr``/``Comm`` channel setup with ``squash_queue=True``
* ``_state_source`` — a single-row ``ColumnDataSource`` holding all
  axis-dependent values read by ``CustomJS`` callbacks at call time, so
  that axis changes after page-load work without re-creating any JS
* Figure construction: tick formatters, info Div, hover tool, rerender
  trigger, axes-changed p2j handler
* ``update_axes()`` / ``_handle_update_axes()`` j2p handler
* ``rerender()`` public API skeleton
* ``_viewport_selection()`` — maps viewport extents to ``SelectionSpec``
* ``_format_probe()`` — formats a probe result dict as HTML

Subclass responsibilities
-------------------------
``_build_glyphs(fig)``
    Add plot-type-specific glyphs to the Bokeh figure.  Called once from
    ``_build()`` after the figure is created and sources are initialised.

``_render(selection, **kw)``
    Query the backend and update ``_image_source.data``.  Called by
    ``_build()``, ``rerender()``, and ``update_axes()``.

``_do_viewport_rerender(x0, x1, y0, y1)`` → dict
    Handle a pan/zoom viewport change.  Returns the ``p2j`` response dict
    ``{image, x0, x1, y0, y1}``.  Called from ``_handle_rerender()``.

``_handle_probe(message)`` → dict
    Handle a hover probe request.  Returns ``{label: str}``.

``_state_data_extra()`` → dict
    Extra ``_state_source`` fields specific to the subclass (e.g.
    ``agg_n_x/y`` for raster, per-layer alpha for scatter).  Merged into
    the base ``_state_data()`` result.

Package location
----------------
``cubevis/cubevis/toolbox/visplot/visibility_plot.py``
"""

from __future__ import annotations

import abc
import logging
import time
from typing import Optional, TYPE_CHECKING
from uuid import uuid4

import numpy as np

from bokeh.model import Model
from bokeh.core.properties import String, Int, Bool
from bokeh.models import (
    BoxSelectTool, ColumnDataSource, CustomJS, CustomJSTickFormatter,
    Div, HoverTool,
)
from bokeh.plotting import figure, show as bk_show
from bokeh.layouts import column

if TYPE_CHECKING:
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


# ---------------------------------------------------------------------------
# Module-level helpers (shared by raster and scatter)
# ---------------------------------------------------------------------------

def _img_to_uint32(img) -> np.ndarray:
    """Convert a Datashader Image or PIL RGBA Image to Bokeh uint32 array.

    ``datashader.tf.shade()`` returns a Datashader ``Image`` — an
    ``xr.DataArray`` subclass with dtype ``uint32`` already packed as
    0xAARRGGBB.  ``np.array(img)`` gives ``(H, W) uint32`` directly.

    Also handles a PIL ``Image`` (mode ``"RGBA"``, uint8) for unit tests.
    """
    arr = np.array(img)
    if arr.dtype == np.uint32:
        return arr
    if arr.ndim == 3 and arr.shape[2] == 4:
        h, w = arr.shape[:2]
        out  = np.empty((h, w), dtype=np.uint32)
        view = out.view(np.uint8).reshape(h, w, 4)
        view[..., 0] = arr[..., 2]   # B
        view[..., 1] = arr[..., 1]   # G
        view[..., 2] = arr[..., 0]   # R
        view[..., 3] = arr[..., 3]   # A
        return out
    raise ValueError(
        f"_img_to_uint32: unrecognised input — shape={arr.shape}, "
        f"dtype={arr.dtype}"
    )


def _axis_label(axis: "Axis") -> str:
    """Format a display label with optional unit suffix."""
    return f"{axis.label}" + (f" [{axis.unit}]" if axis.unit else "")


# ---------------------------------------------------------------------------
# Base class
# ---------------------------------------------------------------------------

class VisibilityPlot(Model):
    """Abstract Bokeh Model base for visibility plot components.

    Concrete subclasses: ``VisibilityRaster``, ``VisibilityScatter``.

    Parameters
    ----------
    backend : VisibilityReader
        Opened reader.  In local sessions this is a
        ``LocalVisibilityReader`` wrapping an ``MSv2Backend`` or
        ``MSv4Backend``; in remote sessions it is a
        ``RemoteReductionContext`` that satisfies the same protocol.
    selection : SelectionSpec
        Data selection.
    y_dim : Axis
        Native axis for the plot y dimension.
    x_dim : Axis
        Native axis for the plot x dimension.
    width, height : int
        Canvas dimensions in pixels.
    title : str | None
        Figure title; ``None`` → subclass generates from axes.
    comm_mgr :
        ``CommMgr`` from the active ``BokehAppContext``.  Auto-retrieved
        when ``None``.
    """

    # Bokeh Model properties (synced to JavaScript via Bokeh serialisation)
    vr_id         = String(default="",   help="Unique ID for this instance")
    canvas_width  = Int(default=900,     help="Canvas width in pixels")
    canvas_height = Int(default=600,     help="Canvas height in pixels")
    status_text   = String(default="",   help="Status / hover label text")
    is_rendering  = Bool(default=False,  help="True while a re-render is in flight")

    def __init__(
        self,
        backend: "VisibilityReader",
        selection: "SelectionSpec",
        y_dim: "Axis",
        x_dim: "Axis",
        width: int  = 900,
        height: int = 600,
        title: Optional[str] = None,
        comm_mgr=None,
        **kwargs,
    ) -> None:
        kwargs.setdefault("vr_id",         str(uuid4())[:8])
        kwargs.setdefault("canvas_width",  width)
        kwargs.setdefault("canvas_height", height)
        super().__init__(**kwargs)

        # Auto-wrap a bare XArrayReader so that self._backend always
        # satisfies the VisibilityReader protocol.  This lets callers pass
        # an MSv2Backend or MSv4Backend directly (the common case in tests
        # and notebooks) without requiring an explicit LocalVisibilityReader
        # construction step.  The wrapper is lightweight — pure delegation
        # with no caching — so there is no runtime cost beyond one extra
        # call frame per query.  Inline imports avoid circular-import risk
        # since LocalVisibilityReader → reader, but reader ↛ visibility_plot.
        from .data.reader import XArrayReader
        from .local_visibility_reader import LocalVisibilityReader
        if isinstance(backend, XArrayReader):
            backend = LocalVisibilityReader(backend)
        self._backend   = backend
        self._selection = selection
        self._y_dim     = y_dim
        self._x_dim     = x_dim
        self._width     = width
        self._height    = height
        self._title     = title   # None → subclass auto-generates

        # Coordinate extents — set by _render()
        self._x_range: tuple[float, float] = (0.0, 1.0)
        self._y_range: tuple[float, float] = (0.0, 1.0)

        # CommMgr / Comm
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
                    description=self._comm_description(),
                    squash_queue=True,
                )
            except Exception as exc:
                log.warning("%s: could not open Comm: %s",
                            type(self).__name__, exc)

        # Per-instance message IDs — uuid strings registered on the Comm
        # and passed into CustomJS args so JS calls comm.send(msg_id, ...).
        # Using uuids (rather than fixed strings like "vr_probe") means
        # multiple VisibilityPlot instances on the same page each have
        # unique routing, matching the cubevis convention established in
        # _interactive_clean_ui.py.
        self._msg_probe    = str(uuid4())
        self._msg_rerender = str(uuid4())
        self._msg_axes     = str(uuid4())
        self._msg_select   = str(uuid4())

        # Set by register_select_callback(); None until VisibilityPlotter wires it.
        self._select_callback = None

        # Bokeh sources — created in _build()
        self._image_source: Optional[ColumnDataSource] = None
        self._state_source: Optional[ColumnDataSource] = None
        self._fig      = None
        self._info_div: Optional[Div] = None
        self._layout   = None

        self._build()
        if self._comm is not None:
            self._register_comm_handlers()

    # ------------------------------------------------------------------
    # Abstract interface — subclasses must implement
    # ------------------------------------------------------------------

    @abc.abstractmethod
    def _comm_description(self) -> str:
        """Short description string for the Comm channel."""

    @abc.abstractmethod
    def _effective_title(self) -> str:
        """Return the figure title string (auto-generated or user-set)."""

    @abc.abstractmethod
    def _render(self, selection: "SelectionSpec", **kwargs) -> None:
        """Query the backend and update ``_image_source.data``.

        Must also update ``self._x_range``, ``self._y_range``, and call
        ``self._update_state_source()`` at the end.
        """

    @abc.abstractmethod
    def _build_glyphs(self) -> None:
        """Add plot-type-specific glyphs to ``self._fig``.

        Called once from ``_build()`` after the figure and sources exist.
        ``_image_source`` and ``_state_source`` are guaranteed to be set.
        """

    @abc.abstractmethod
    def _do_viewport_rerender(
        self, x0: float, x1: float, y0: float, y1: float
    ) -> dict:
        """Handle a pan/zoom viewport change; return p2j response dict.

        Returns
        -------
        dict
            ``{image: ndarray, x0, x1, y0, y1}``
        """

    @abc.abstractmethod
    def _handle_probe(self, message: dict) -> dict:
        """Handle a ``'vr_probe'`` j2p message; return ``{label: str}``."""

    def _state_data_extra(self) -> dict:
        """Subclass-specific ``_state_source`` fields.

        Override to add extra fields (e.g. ``agg_n_x/y`` for raster,
        per-layer alpha for scatter).  Default returns empty dict.
        """
        return {}

    def _register_extra_comm_handlers(self) -> None:
        """Register additional j2p handlers beyond the base set.

        Called from ``_register_comm_handlers()``.  Default is a no-op.
        """

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def figure(self):
        """The raw Bokeh ``Figure`` object."""
        return self._fig

    @property
    def layout(self):
        """Bokeh column layout (figure + info Div) for embedding."""
        return self._layout

    def register_select_callback(self, callback) -> None:
        """Register a Python callable to receive box-select events.

        Called by ``VisibilityPlotter`` after constructing both widget
        instances so that box-close events from either figure are routed
        to the plotter's ``FlagDB`` accumulation and overlay re-render
        path rather than being handled inside the widget itself.

        The callback receives a single dict::

            {
                "panel":  "raster" | "scatter",   # which figure fired
                "x0": float, "x1": float,          # data-space extents
                "y0": float, "y1": float,
                "tool":  "box_select",             # always for now
            }

        The ``panel`` key is set by ``VisibilityPlotter`` when it calls
        this method, not by the widget itself — the widget only knows its
        own geometry.

        Parameters
        ----------
        callback : async callable
            An ``async def`` function accepting one dict argument and
            returning a dict (or ``None``), conforming to the
            ``CommMgr`` handler signature.

        Notes
        -----
        * Calling this method a second time replaces the previous
          callback — only one handler per widget is supported.
        * The box-select ``CustomJS`` is wired during ``_build()`` and
          always fires the j2p message when a box closes; whether a
          Python handler is registered only controls what happens on the
          Python side.  Calling this method before ``_build()`` is not
          necessary — the message ID is fixed at construction time.
        """
        self._select_callback = callback
        if self._comm is not None:
            self._comm.register(self._msg_select, callback)

    def show(self) -> None:
        """Open the figure in a browser tab / render inline in a notebook."""
        bk_show(self._layout)

    def update_axes(
        self,
        y_dim: Optional["Axis"] = None,
        x_dim: Optional["Axis"] = None,
        title: Optional[str] = None,
    ) -> None:
        """Change the plot axes and re-render in place.

        Sends ``'vr_axes_changed'`` p2j so the browser updates the Bokeh
        Figure's axis label text (which cannot be driven via CDS alone).

        Parameters
        ----------
        y_dim, x_dim : Axis | None
            New axis.  ``None`` keeps the current value.
        title : str | None
            New figure title.  ``None`` keeps or auto-generates.
        """
        changed = False
        if y_dim is not None and y_dim != self._y_dim:
            self._y_dim = y_dim;  changed = True
        if x_dim is not None and x_dim != self._x_dim:
            self._x_dim = x_dim;  changed = True
        if title is not None:
            self._title = title;  changed = True

        if not changed:
            return

        self._render(self._selection)
        self._notify_axes_changed()

    def rerender(
        self,
        x_range: Optional[tuple[float, float]] = None,
        y_range: Optional[tuple[float, float]] = None,
        new_selection: Optional["SelectionSpec"] = None,
    ) -> None:
        """Re-render without changing axes.

        Viewport change (pan/zoom)
            Pass ``x_range`` and/or ``y_range``.

        Selection change
            Pass ``new_selection``.  Does NOT permanently replace
            ``self._selection``.

        No args
            Full re-render with the existing selection.
        """
        if new_selection is not None:
            self._render(new_selection)
        elif x_range is not None or y_range is not None:
            xr = x_range or self._x_range
            yr = y_range or self._y_range
            resp = self._do_viewport_rerender(xr[0], xr[1], yr[0], yr[1])
            self._image_source.data = {
                "image": [resp["image"]],
                "x":     [resp["x0"]],
                "y":     [resp["y0"]],
                "dw":    [resp["x1"] - resp["x0"]],
                "dh":    [resp["y1"] - resp["y0"]],
            }
        else:
            self._render(self._selection)

    # ------------------------------------------------------------------
    # Internal: _state_source
    # ------------------------------------------------------------------

    def _state_data(self) -> dict:
        """Build the full ``_state_source.data`` dict.

        Base fields (shared by raster and scatter) are merged with
        subclass-specific fields from ``_state_data_extra()``.
        """
        from .axes import Axis
        x0, x1 = self._x_range
        y0, y1 = self._y_range
        base = {
            "full_x0":   [float(x0)],
            "full_x1":   [float(x1)],
            "full_y0":   [float(y0)],
            "full_y1":   [float(y1)],
            "y_is_time": [int(self._y_dim == Axis.TIME)],
            "x_is_time": [int(self._x_dim == Axis.TIME)],
            "x_label":   [_axis_label(self._x_dim)],
            "y_label":   [_axis_label(self._y_dim)],
        }
        base.update(self._state_data_extra())
        return base

    def _update_state_source(self) -> None:
        """Push current state into ``_state_source`` (no-op before build)."""
        if self._state_source is not None:
            self._state_source.data = self._state_data()

    # ------------------------------------------------------------------
    # Internal: build
    # ------------------------------------------------------------------

    def _build(self) -> None:
        if not HAS_DATASHADER:
            raise ImportError(
                "datashader is required for VisibilityPlot subclasses.\n"
                "Install: pip install datashader"
            )

        # Initial data query (sets _x_range, _y_range, _image_source)
        self._render(self._selection)

        x0, x1 = self._x_range
        y0, y1 = self._y_range

        # State source — created after _render() so _state_data() has ranges
        self._state_source = ColumnDataSource(data=self._state_data())

        # Figure
        self._fig = figure(
            title         = self._effective_title(),
            width         = self._width,
            height        = self._height,
            x_range       = (x0, x1),
            y_range       = (y0, y1),
            x_axis_label  = _axis_label(self._x_dim),
            y_axis_label  = _axis_label(self._y_dim),
            tools         = "pan,wheel_zoom,box_zoom,reset,save",
            active_scroll = "wheel_zoom",
        )

        # Subclass adds its glyphs (image_rgba, scatter, etc.)
        self._build_glyphs()

        # Shared tick formatters reading from _state_source
        _time_fmt_code = """
const is_time = state.data[axis_key][0];
if (!is_time) return tick.toFixed(4);
const t0      = state.data[t0_key][0];
const elapsed = tick - t0;
if (Math.abs(elapsed) < 60)
    return elapsed.toFixed(1) + ' s';
const m    = Math.floor(Math.abs(elapsed) / 60);
const s    = Math.round(Math.abs(elapsed) % 60);
const sign = elapsed < 0 ? '-' : '';
return sign + m + 'm ' + s.toString().padStart(2, '0') + 's';
"""
        self._fig.yaxis.formatter = CustomJSTickFormatter(
            args={"state": self._state_source,
                  "axis_key": "y_is_time", "t0_key": "full_y0"},
            code=_time_fmt_code,
        )
        self._fig.xaxis.formatter = CustomJSTickFormatter(
            args={"state": self._state_source,
                  "axis_key": "x_is_time", "t0_key": "full_x0"},
            code=_time_fmt_code,
        )

        # Info / hover status div
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

        # Wiring
        self._add_hover_tool()
        if self._comm is not None:
            self._add_rerender_trigger()
            self._add_axes_changed_handler()
            self._add_box_select_tool()

    # ------------------------------------------------------------------
    # Internal: hover tool
    # ------------------------------------------------------------------

    def _add_hover_tool(self) -> None:
        if self._comm is not None:
            self._add_comm_hover()
        else:
            self._add_static_hover()

    def _add_static_hover(self) -> None:
        self._fig.add_tools(HoverTool(
            tooltips=[("x", "$x{0.5g}"), ("y", "$y{0.5g}")],
        ))

    def _add_comm_hover(self) -> None:
        """HoverTool sending the probe message via the Comm channel."""
        info_div  = self._info_div
        comm      = self._comm
        msg_probe = self._msg_probe
        hover_js = CustomJS(
            args={"info_div": info_div, "comm": comm},
            code=f"""
const now = Date.now();
if (window._cvLastProbe && (now - window._cvLastProbe) < 120) return;
window._cvLastProbe = now;
const x = cb_data.geometry.x;
const y = cb_data.geometry.y;
if (x == null || y == null) return;
comm.send('{msg_probe}', {{x: x, y: y}}, function(resp) {{
    if (resp && resp.label) info_div.text = resp.label;
}});
""",
        )
        self._fig.add_tools(HoverTool(
            callback  = hover_js,
            tooltips  = None,
            renderers = self._fig.renderers,
        ))

    # ------------------------------------------------------------------
    # Internal: pan/zoom rerender trigger
    # ------------------------------------------------------------------

    def _add_rerender_trigger(self) -> None:
        """Attach range callbacks for pan/zoom re-render via Comm."""
        image_source = self._image_source
        state_source = self._state_source
        comm         = self._comm

        msg_rerender = self._msg_rerender
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
    comm.send('{msg_rerender}',
        {{x0: x0, x1: x1, y0: y0, y1: y1}},
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
    # Internal: axes-changed p2j handler (pre-wired at page-load)
    # ------------------------------------------------------------------

    def _add_axes_changed_handler(self) -> None:
        """Pre-wire a p2j 'vr_axes_changed' listener in the browser.

        Sets ``x_axis_label``, ``y_axis_label``, and ``title`` on the
        Bokeh Figure — properties that can't be driven from a CDS.
        """
        vr_id = self.vr_id
        comm  = self._comm
        fig   = self._fig

        init_source = ColumnDataSource(data={"_init": [1]})
        msg_axes = self._msg_axes
        init_js = CustomJS(
            args={"fig": fig, "comm": comm},
            code=f"""
comm.register('{msg_axes}', function(msg) {{
    if (msg.x_label != null) fig.xaxis[0].axis_label = msg.x_label;
    if (msg.y_label != null) fig.yaxis[0].axis_label = msg.y_label;
    if (msg.title   != null) fig.title.text           = msg.title;
}});
""",
        )
        init_source.js_on_change("data", init_js)
        self._axes_init_source = init_source

    def _add_box_select_tool(self) -> None:
        """Add a BoxSelectTool wired to fire a j2p select message on box-close.

        The tool sends ``{x0, x1, y0, y1, tool: "box_select"}`` to
        Python via the widget's own ``Comm`` channel using
        ``self._msg_select`` as the routing UUID.  The Python handler
        registered by ``register_select_callback()`` receives this dict.

        The BoxSelectTool is added to the figure's tool list but is **not**
        set as the active tool — ``VisibilityPlotter`` activates it by
        setting ``fig.toolbar.active_drag`` when the Box Select toolbar
        button is pressed.

        The tool uses ``select_every_mousemove=False`` so the j2p message
        fires exactly once when the mouse button is released, not on every
        mouse-move event during the drag.  This matches the AIPS TVFLG
        interaction model: drag to define the region, release to flag.
        """
        comm       = self._comm
        msg_select = self._msg_select

        select_js = CustomJS(
            args={"comm": comm},
            code=f"""
const geom = cb_data['geometry'];
if (!geom) return;
const x0 = geom.x0, x1 = geom.x1;
const y0 = geom.y0, y1 = geom.y1;
// Ignore degenerate (click, not drag) selections
if (Math.abs(x1 - x0) < 1e-12 && Math.abs(y1 - y0) < 1e-12) return;
comm.send('{msg_select}',
    {{x0: x0, x1: x1, y0: y0, y1: y1, tool: 'box_select'}},
    function(resp) {{
        // Python handler returns null in the preview (FlagDB accumulation
        // only); a non-null response will carry a re-rendered overlay image
        // once the full flag overlay pipeline is wired (Phase 1 F-9/F-10).
        if (!resp || resp.image == null) return;
        // Future: update image_source with the flagged overlay composite.
    }}
);
""",
        )

        box_tool = BoxSelectTool(select_every_mousemove=False)
        self._fig.add_tools(box_tool)
        box_tool.js_on_event("selectiongeometry", select_js)

    def _notify_axes_changed(self) -> None:
        """Send 'vr_axes_changed' p2j after an axis update."""
        if self._comm is None:
            return
        try:
            self._comm.send_p2j(self._msg_axes, {
                "x_label": _axis_label(self._x_dim),
                "y_label": _axis_label(self._y_dim),
                "title":   self._effective_title(),
            })
        except Exception as exc:
            log.warning("_notify_axes_changed: %s", exc)

    # ------------------------------------------------------------------
    # Internal: CommMgr handler registration
    # ------------------------------------------------------------------

    def _register_comm_handlers(self) -> None:
        self._comm.register(self._msg_probe,    self._handle_probe)
        self._comm.register(self._msg_rerender, self._handle_rerender)
        # The axes-changed message ID is registered as a p2j listener in JS
        # (_add_axes_changed_handler) — no Python-side registration needed.
        # update_axes message IDs are registered by each subclass via
        # _register_extra_comm_handlers() so raster/scatter can handle
        # their own extended fields.
        self._register_extra_comm_handlers()
        # Select callback: only if pre-registered before _build() ran.
        # The normal path is VisibilityPlotter calling
        # register_select_callback() after construction, which registers
        # directly on self._comm at that point.
        if self._select_callback is not None:
            self._comm.register(self._msg_select, self._select_callback)

    # ------------------------------------------------------------------
    # j2p handlers (base)
    # ------------------------------------------------------------------

    def _handle_rerender(self, message: dict) -> dict:
        """Route pan/zoom rerender to subclass ``_do_viewport_rerender``."""
        x0 = float(message["x0"])
        x1 = float(message["x1"])
        y0 = float(message["y0"])
        y1 = float(message["y1"])
        return self._do_viewport_rerender(x0, x1, y0, y1)

    def _parse_axis(self, message: dict, key: str) -> "Optional[Axis]":
        """Parse an Axis enum member from a j2p message dict by key.

        Helper for subclass ``_handle_update_axes_*`` handlers so they
        don't each need to duplicate the try/except KeyError pattern.
        """
        from .axes import Axis
        name = message.get(key)
        if name is None:
            return None
        try:
            return Axis[name]
        except KeyError:
            log.warning("%s._parse_axis: unknown Axis %r",
                        type(self).__name__, name)
            return None

    # ------------------------------------------------------------------
    # Internal: viewport selection narrowing
    # ------------------------------------------------------------------

    def _viewport_selection(
        self,
        x_range: Optional[tuple[float, float]],
        y_range: Optional[tuple[float, float]],
    ) -> "SelectionSpec":
        """Return base selection tightened to viewport extents."""
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
    # Formatting (shared; subclasses may extend)
    # ------------------------------------------------------------------

    def _format_probe(self, info: dict, quantity_label: str = "") -> str:
        """Format a probe result dict as an HTML status-bar string."""
        parts: list[str] = []

        val = info.get("value")
        lbl = quantity_label or "Value"
        if val is not None:
            parts.append(f"<b>{lbl}:</b> {val:.6g}")
        else:
            parts.append(f"<b>{lbl}:</b> <i>empty</i>")

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

        n = info.get("n_scatter_samples")
        if n is not None:
            parts.append(f"<b>N:</b> {n}")

        return " &nbsp;|&nbsp; ".join(parts)
