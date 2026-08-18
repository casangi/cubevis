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
* ``enable_flagging`` — when True (default), adds the ``FlagTool`` /
  ``FlagTool(flag=False)`` ("Unflag") drag tools to the figure toolbar in
  place of a plain box-select. When False, no flag/unflag tooling is
  added at all — the figure only gets pan/wheel-zoom/box-zoom/reset/save,
  useful for astronomers who just want to inspect data with no
  flagging workflow in the way.
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

``_panel_spec()`` → PanelSpec
    Describe the panel: ranges, labels, aggregation resolution, and one
    ``ColorBand`` per value-to-color mapping.  ``_state_data()`` is
    derived from this, so the Bokeh chrome and the matplotlib export
    chrome read one description rather than two — see ``panel_spec``.
    Must be cheap; it runs on every ``_state_source`` push.

``_shade_for_export(viewport)`` → ndarray | None
    Re-shade from cached state at a given viewport, without re-querying
    the backend.  Feeds ``render_result()``.

Package location
----------------
``cubevis/cubevis/toolbox/visplot/visibility_plot.py``
"""

from __future__ import annotations

import abc
import logging
import math
import time
from typing import Optional, TYPE_CHECKING
from uuid import uuid4

import numpy as np

from bokeh.model import Model
from bokeh.core.properties import String, Int, Bool
from bokeh.models import (
    ColumnDataSource, CustomJS, CustomJSTickFormatter,
    Div, HoverTool,
)
from bokeh.plotting import figure, show as bk_show
from bokeh.layouts import column

from cubevis.bokeh.tools._flag_tool import FlagTool

from .tick_format import TICK_FORMATTER_JS

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
    """Convert a Datashader Image or PIL RGBA Image to a Bokeh uint32 array.

    Returns ``(H, W) uint32`` whose bytes are **R, G, B, A in memory
    order** — the layout ``figure.image_rgba`` hands to canvas
    ``ImageData``, and the layout ``matplotlib.imshow`` reads back via a
    ``uint8`` view.  The result is always C-contiguous, so
    ``out.view(np.uint8).reshape(H, W, 4)`` is safe on it.

    Reason about this in memory order, not numerically.  On a
    little-endian machine the same bytes read as ``0xAABBGGRR`` and on
    big-endian as ``0xRRGGBBAA``; neither is a useful thing to assert.

    ``datashader.tf.shade()`` returns a Datashader ``Image`` — an
    ``xr.DataArray`` subclass already in this layout — so
    ``np.asarray(img)`` gives the ``(H, W) uint32`` directly.  A PIL
    ``Image`` in mode ``"RGBA"`` (uint8, used by unit tests) is likewise
    already RGBA, so packing it is a plain copy.

    NOTE: the uint32 branch returns a *view* of its input when that input
    is already contiguous, where this function previously always copied.
    No current caller mutates the result in place — raster's ``_render``
    and ``_shade_viewport`` only push it into a ``ColumnDataSource``, and
    ``VisibilityScatter._shade_all_layers`` does its in-place alpha
    rewrite on a separate ``np.array(img, dtype=np.uint32)`` that still
    copies — but a future one must copy first.

    HISTORY: the PIL branch used to write B, G, R, A, producing
    BGRA-in-memory — R and B transposed relative to what ``image_rgba``
    consumes.  Test-only, so nothing user-visible was wrong, but any test
    asserting colour through this path was asserting a mirrored answer,
    and the old docstring's "0xAARRGGBB" described that same wrong
    layout.  Datashader's actual output was verified empirically
    (2026-08): shading ``cmap=["#FF0000", "#0000FF"]`` yields bytes
    ``[255, 0, 0, 255]`` and ``[0, 0, 255, 255]``.
    """
    arr = np.asarray(img)
    if arr.dtype == np.uint32:
        # No-op when already contiguous; present so callers may rely on
        # the uint8 view above unconditionally.
        return np.ascontiguousarray(arr)
    if arr.ndim == 3 and arr.shape[2] == 4 and arr.dtype == np.uint8:
        h, w = arr.shape[:2]
        out  = np.empty((h, w), dtype=np.uint32)
        out.view(np.uint8).reshape(h, w, 4)[:] = arr   # PIL is already RGBA
        return out
    raise ValueError(
        f"_img_to_uint32: unrecognised input — shape={arr.shape}, "
        f"dtype={arr.dtype}"
    )


def _json_num(v):
    """Coerce a backend probe value to a JSON-safe number, or ``None``.

    Backend ``probe_*_pixel`` results carry numpy scalars, and a
    non-finite float is not representable in JSON: ``json.dumps`` emits a
    bare ``NaN``/``Infinity`` token that the browser's ``JSON.parse``
    rejects, taking down the whole p2j response rather than just the one
    field.  Every numeric value placed under a probe envelope's
    ``"probe"`` key goes through here first.
    """
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _axis_label(axis: "Axis") -> str:
    """Format a display label for a bare ``Axis``, with unit suffix.

    DEPRECATED for plot code.  A bare ``Axis`` cannot know whether the
    backend actually plotted it — ``Axis.CHANNEL`` is routinely resolved
    as frequency — so labelling from the enum is how an axis came to read
    "Channel" with ticks in Hz.  Plot classes must use
    ``self._x_info`` / ``self._y_info``, which carry the axis that was
    really plotted.

    Retained only for callers that legitimately have no selection context
    (e.g. populating a dropdown of choices, where the label describes the
    option rather than a rendered axis).
    """
    from .axes import AxisInfo
    return AxisInfo.direct(axis).display_label()


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
    headless : bool
        Build the data substrate but no browser chrome.  ``_render()``
        runs and ``_state_source`` is populated -- so ``render_result()``
        and therefore the PNG export path work normally -- but no
        ``figure``, glyphs, tick formatters, hover, or flag tools are
        created, and ``self._fig`` is ``None``.

        For a pipeline export this skips work whose only consumer is a
        browser that will never exist.  ``comm_mgr`` is independently
        optional (see below), so a headless panel is also comm-free.

    comm_mgr :
        ``CommMgr`` from the active ``BokehAppContext``.  Auto-retrieved
        when ``None``.
    enable_flagging : bool
        Whether to add the ``FlagTool`` / ``FlagTool(flag=False)``
        ("Unflag") drag tools to this figure's toolbar.  Defaults to
        ``True``.  Set ``False`` to build a plot with no flagging
        workflow at all — e.g. a quick-look tool for astronomers who
        only want to inspect data.  When ``False``, ``register_select_callback``
        can still be called but nothing in the browser will ever trigger
        it, since no select/flag gesture tool is present.
    compact_toolbar : bool
        Whether the figure's toolbar auto-hides until the mouse is over
        the plot (``bokeh.models.Toolbar.autohide``).  Defaults to
        ``True``.  Purely a client-side Bokeh behavior — no server-side
        state, no JS beyond what Bokeh already provides.
    defer_initial_render : bool
        If ``True``, construct the figure without querying the backend —
        a blank placeholder image with sane (non-degenerate) axis ranges
        is used instead, reusing each subclass's existing degenerate/empty
        fallback path. The first real ``update_axes()`` or ``rerender()``
        call (neither of which ever defers) performs the actual query.
        Defaults to ``False``. Used to construct a panel object — e.g. a
        slot's inactive kind — without paying its query/render cost until
        it actually becomes active; see decision 11 in the grid/iteration
        design notes.
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
        headless=False,
        cursor_source=None,
        enable_flagging: bool = True,
        compact_toolbar: bool = True,
        defer_initial_render: bool = False,
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
        # Seeded direct; _render() re-resolves against the backend once
        # the subclass has finished wiring itself up.
        from .axes import AxisInfo as _AxisInfo
        self._x_info = _AxisInfo.direct(x_dim)
        self._y_info = _AxisInfo.direct(y_dim)
        self._width     = width
        self._height    = height
        self._title     = title   # None → subclass auto-generates
        self._enable_flagging = enable_flagging
        self._compact_toolbar = compact_toolbar
        self._defer_initial_render = defer_initial_render

        # Coordinate extents — set by _render()
        self._x_range: tuple[float, float] = (0.0, 1.0)
        self._y_range: tuple[float, float] = (0.0, 1.0)

        # CommMgr / Comm
        # Set before _build() runs; see the early return there.
        self._headless = bool(headless)

        if comm_mgr is None and not self._headless:
            # Convenience for standalone use: adopt an existing app
            # context's CommMgr when one is already around.
            #
            # Gated on headless because BokehInit.get_app_context()
            # *creates* a context when none exists -- it logs "creating a
            # BokehAppContext due to a BokehInit.get_app_context() call".
            # A pipeline export would otherwise manufacture browser
            # plumbing and open Comms against it for a figure that is
            # never built.
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

        # Separate, dedicated Comm for flag/unflag traffic. The main
        # self._comm above uses squash_queue=True (appropriate for
        # high-frequency hover/probe traffic, where only the latest
        # matters) — sharing it for flagging would risk a rapid second
        # flag-box message silently squashing/replacing an earlier one
        # still waiting in the queue before Python ever sees it. Each
        # comm is queued independently, so this also means flag/unflag
        # round-trips never wait behind probe/rerender traffic either.
        self._flag_comm = None
        if enable_flagging and self._comm_mgr is not None:
            try:
                self._flag_comm = self._comm_mgr.open(
                    description=f"{self._comm_description()} flagging",
                    squash_queue=False,
                )
            except Exception as exc:
                log.warning("%s: could not open flagging Comm: %s",
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

        self._select_callback    = None
        self._cursor_source_ref  = cursor_source  # shared CDS for linked cursor

        # Bokeh sources — created in _build()
        self._image_source: Optional[ColumnDataSource] = None
        self._state_source: Optional[ColumnDataSource] = None
        self._fig      = None
        self._info_div: Optional[Div] = None
        self._layout   = None

        # FlagTool / Unflag instances — created in _build() only when
        # enable_flagging=True.  None otherwise.
        self._flag_tool   = None
        self._unflag_tool = None

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

    @abc.abstractmethod
    def _panel_spec(self) -> "PanelSpec":
        """Describe this panel: geometry, labels, and colour bands.

        Must be cheap — plain attribute reads and values already computed
        during ``_render()``.  It is called on every ``_state_source``
        push, so anything expensive here (a re-query, a re-shade) is a
        per-interaction cost.  The pixels are deliberately not part of
        this; see ``render_result()``.

        Replaces the old ``_state_data_extra()`` hook, which returned
        raw Bokeh column dicts.  Returning a structured spec instead is
        what lets the matplotlib export path consume the same
        description the JS chrome does.
        """

    def _shade_for_export(
        self, viewport: "Optional[tuple[float, float, float, float]]" = None
    ) -> "Optional[np.ndarray]":
        """Shade this panel at *viewport* and return ``(H, W) uint32``.

        Must not re-query the backend — it operates on whatever the last
        ``_render()`` cached, which is what makes an export from the GUI
        instant and guarantees it shows the data the user is looking at
        rather than a fresh query that might differ.

        Default returns ``None`` (nothing renderable).  Subclasses
        override.
        """
        return None

    def render_result(
        self, viewport: "Optional[tuple[float, float, float, float]] " = None
    ):
        """Return a ``RenderedPanel``: this panel's spec plus its pixels.

        The unit the export compositor consumes, produced identically by
        a headless render and by the GUI's Export button.  ``viewport``
        is ``(x0, x1, y0, y1)``; ``None`` means full extent.  The GUI
        path must supply it — with no Bokeh server, a pan or zoom done in
        the browser never reaches Python, so the Python-side ranges are
        stale the moment the user touches the plot.
        """
        from .panel_spec import RenderedPanel
        spec = self._panel_spec()
        if spec.status != "ok":
            return RenderedPanel(spec=spec, image=None, viewport=viewport)
        # Mappings are attached here rather than in _panel_spec because
        # building one costs a histogram plus a few hundred interpolation
        # samples: negligible beside a shade, but far too much to pay on
        # every _state_source push.
        spec = spec.with_bands(self._bands_with_mappings(spec, viewport))
        return RenderedPanel(
            spec     = spec,
            image    = self._shade_for_export(viewport),
            viewport = viewport,
        )

    def _bands_with_mappings(self, spec, viewport=None):
        """Return *spec*'s bands with ``mapping``/``peak_density`` filled.

        Default is a no-op; subclasses override.  The mapping must be
        built from the same reference array the shade uses, or the
        colorbar will label the image with a curve the image was not
        drawn with — which is worse than no colorbar at all.
        """
        return spec.bands

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
    def flagging_enabled(self) -> bool:
        """Whether this instance was built with the Flag/Unflag drag tools."""
        return self._enable_flagging

    @property
    def layout(self):
        """Bokeh column layout (figure + info Div) for embedding."""
        return self._layout

    def show(self) -> None:
        """Open the figure in a browser tab / render inline in a notebook."""
        bk_show(self._layout)

    def register_select_callback(self, callback) -> None:
        """Register a Python callable to receive box-select events.

        Called by ``VisibilityPlotter`` after constructing both widget
        instances.  The callback receives::

            {"x0": float, "x1": float, "y0": float, "y1": float,
             "flag": bool, "panel": "raster"|"scatter",
             "at_pixel_res": bool,
             "tool": "flag_box"|"unflag_box"}

        The box is now sent regardless of zoom level (``at_pixel_res``
        tells the callback whether the box is actually at/past 1:1 pixel
        resolution) — the callback is responsible for deciding whether to
        record a flag and for reporting back to the user either way (see
        ``VisibilityPlotter._handle_box_select`` / ``_notify``), rather
        than the browser silently dropping below-resolution boxes.

        Parameters
        ----------
        callback : async callable
            ``async def`` function accepting one dict argument.

        Note
        ----
        If this instance was built with ``enable_flagging=False`` the
        callback is still registered on the Comm channel, but nothing in
        the browser will ever send ``_msg_select`` — no flag/unflag tool
        is present on the figure — so the callback simply never fires.
        """
        self._select_callback = callback
        if self._flag_comm is not None:
            self._flag_comm.register(self._msg_select, callback)

    def _add_flag_tools(self) -> None:
        """Add the Flag / Unflag drag tools that fire a j2p on box-close.

        Replaces the previous plain ``BoxSelectTool``-based flagging stub.
        Only called from ``_build()`` when ``enable_flagging=True``.

        Both instances share this panel's dedicated flagging ``Comm``
        (``self._flag_comm`` — separate from the panel's main ``_comm``,
        see ``__init__``) and ``_msg_select`` —
        a single Python handler (registered via
        ``register_select_callback``) distinguishes flag vs. unflag using
        the ``flag`` field in the j2p payload, exactly as
        ``VisibilityPlotter._handle_box_select`` already expects.
        Box-zoom (``tools="pan,wheel_zoom,box_zoom,reset,save"`` in
        ``_build()``) is unaffected — it's a separate tool and stays on
        every figure regardless of ``enable_flagging``.
        """
        # Cosmetic only (informational field in the j2p payload) — derives
        # "raster"/"scatter" from the subclass's "visibility raster" /
        # "visibility scatter" comm description rather than requiring a
        # dedicated abstract method.
        panel_name = self._comm_description().rsplit(" ", 1)[-1]

        common = dict(
            comm         = self._flag_comm,
            msg_id       = self._msg_select,
            panel        = panel_name,
            image_source = self._image_source,
            state_source = self._state_source,
        )
        self._flag_tool   = FlagTool(flag=True,  **common)
        self._unflag_tool = FlagTool(flag=False, **common)
        self._fig.add_tools(self._flag_tool, self._unflag_tool)

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

        # A never-yet-rendered (defer_initial_render=True) panel must
        # render on its first update_axes() call even if the caller passes
        # its own current axes back unchanged — e.g. "this slot's inactive
        # kind just became active." See decision 11 in the grid/iteration
        # design notes; self._agg is None only in that pre-first-render
        # state, never after (including on legitimately empty selections —
        # see each subclass's own degenerate-agg fallback).
        if self._agg is None:
            changed = True

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

        Derived from ``_panel_spec()`` rather than assembled here, so the
        JS chrome and the matplotlib export chrome read one description
        instead of two.  See ``panel_spec`` for why that matters; the
        short version is that a field added for the browser becomes
        visible to the exporter for free, and neither can drift without
        the other noticing.

        ``PanelSpec.to_state_data()`` emits exactly the historical key
        set, so this is a pure refactor from the browser's point of view.
        """
        return self._panel_spec().to_state_data()

    def _axis_flags(self) -> tuple[bool, bool]:
        """``(x_is_time, y_is_time)`` — shared by both subclasses' specs."""
        from .axes import Axis
        return (self._x_dim == Axis.TIME, self._y_dim == Axis.TIME)

    # ------------------------------------------------------------------
    # Axis resolution
    # ------------------------------------------------------------------

    _axis_info_warned: set = set()
    """Reader class names already warned about missing ``axis_info``."""

    def _refresh_axis_info(self, selection=None) -> None:
        """Re-resolve ``_x_info`` / ``_y_info`` from the backend.

        Called at the top of each subclass's ``_render()``, which is the
        one place that knows both the current axes and the current
        selection, and which every axis- or selection-changing path
        already funnels through.  Resolving here rather than inside
        ``_panel_spec()`` matters: the backend counts partitions to
        decide whether ``Axis.CHANNEL`` is unique, and ``_panel_spec()``
        runs on every ``_state_source`` push.

        Falls back to a direct, un-substituted resolution when the
        backend does not implement ``axis_info`` — a remote reduction
        context satisfies the reader protocol structurally and may not
        have been updated.  The fallback loses only the CHANNEL
        substitution, which is what the old behaviour was anyway.
        """
        from .axes import AxisInfo
        sel = selection if selection is not None else self._selection
        resolve = getattr(self._backend, "axis_info", None)
        if resolve is None:
            # Log once per reader class.  This fallback produces the
            # pre-AxisInfo behaviour, which looks correct -- the plot
            # renders, the label just names the requested axis rather
            # than the plotted one.  It stayed hidden exactly that way
            # when LocalVisibilityReader (which narrows XArrayReader to
            # the display protocol) was not given the new method.  A
            # fallback indistinguishable from success is worse than none.
            cls = type(self._backend).__name__
            if cls not in VisibilityPlot._axis_info_warned:
                VisibilityPlot._axis_info_warned.add(cls)
                log.warning(
                    "%s does not implement axis_info(); axis labels will "
                    "name the requested axis rather than the one actually "
                    "plotted (Axis.CHANNEL will read 'Channel' even when "
                    "frequency is on the axis)", cls,
                )
            self._x_info = AxisInfo.direct(self._x_dim)
            self._y_info = AxisInfo.direct(self._y_dim)
            self._sync_axis_labels()
            return
        try:
            self._x_info = resolve(self._x_dim, sel, self._QUERY_PATH)
            self._y_info = resolve(self._y_dim, sel, self._QUERY_PATH)
        except Exception as exc:
            log.warning("axis_info failed (%s); using unresolved labels", exc)
            self._x_info = AxisInfo.direct(self._x_dim)
            self._y_info = AxisInfo.direct(self._y_dim)
        self._sync_axis_labels()

    def apply_refresh(self, level) -> None:
        """Recompute only as much as *level* requires.

        The point of the ladder (see ``refresh.py``) is that a caller can
        ask for the minimum and get it.  A palette change is a
        ``SHADE``: it alters no rows, no aggregation and no extent, so
        re-querying 30 million points for it is pure waste.

        ``CHROME`` does nothing here -- chrome is either browser-side or
        already applied by the caller.  ``SHADE`` and ``AGGREGATE``
        delegate to ``_reshade()``, which subclasses implement over their
        cached state.  ``QUERY`` is the only level that reaches the
        backend.
        """
        from .refresh import RefreshLevel
        if level <= RefreshLevel.CHROME:
            return
        if level <= RefreshLevel.AGGREGATE:
            self._reshade()
            return
        self._render(self._selection)
        self._update_state_source()

    def _reshade(self) -> None:
        """Re-shade from cached state; no backend query.

        Default is a no-op so a subclass without a cheap path degrades to
        "nothing visibly happens" rather than to a silent full re-query.
        Both concrete subclasses override.
        """
        log.debug("%s has no _reshade(); ignoring refresh",
                  type(self).__name__)

    _QUERY_PATH = "columns"
    """Which backend query path this plot uses; see ``axis_info``.

    Capability is per-path: ``query_columns`` returns a real channel
    index while ``query_raster`` does not, so a plot must say which one
    it will call or the resolved label will not match the values drawn.
    """

    _theme: str = "dark"
    """Theme the panel's palette was resolved for; set by VisibilityPlotter."""

    def _theme_hint(self) -> str:
        """Theme the pixels were shaded for, for ``PanelSpec.theme``.

        Set by ``VisibilityPlotter`` alongside the palette, so a
        ``RenderedPanel`` carries the background its ramps were
        conditioned against and ``export_png`` can default to it.
        """
        return getattr(self, "_theme", "dark")

    def _sync_axis_labels(self) -> None:
        """Push the resolved labels onto the live Bokeh figure.

        ``_build()`` sets ``x_axis_label``/``y_axis_label`` once, from the
        seeded direct resolution, before the backend has been consulted.
        Re-resolving in ``_refresh_axis_info`` updates only the Python
        object, so without this the figure title keeps saying "Channel"
        while the status bar correctly says "Frequency".  Axis labels are
        plain Bokeh properties, so assigning them propagates to the
        browser through the normal document patch.
        """
        fig = getattr(self, "_fig", None)
        if fig is None:
            return                      # headless, or pre-_build
        try:
            spec = self._panel_spec()
            fig.xaxis.axis_label = spec.axis_label("x")
            fig.yaxis.axis_label = spec.axis_label("y")
        except Exception as exc:        # pragma: no cover
            log.debug("could not sync axis labels: %s", exc)

    @property
    def x_label(self) -> str:
        """Display label for the x axis actually plotted."""
        return self._x_info.display_label()

    @property
    def y_label(self) -> str:
        """Display label for the y axis actually plotted."""
        return self._y_info.display_label()

    def axis_notes(self) -> list[str]:
        """Substitution notes for either axis, for status/export display."""
        return [i.note for i in (self._x_info, self._y_info) if i.note]

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

        # Initial data query (sets _x_range, _y_range, _image_source) —
        # skipped when defer_initial_render=True; see that parameter's
        # docstring and decision 11 in the grid/iteration design notes.
        self._render(self._selection, defer=self._defer_initial_render)

        x0, x1 = self._x_range
        y0, y1 = self._y_range

        # State source — created after _render() so _state_data() has ranges
        self._state_source = ColumnDataSource(data=self._state_data())

        if self._headless:
            # Everything below builds browser chrome: a figure, glyphs,
            # tick formatters, hover, and the flag tools.  None of it
            # contributes a pixel to the data area -- Datashader has
            # already produced the RGBA array -- so an export path pays
            # for it and uses none of it.
            #
            # The cut is here rather than earlier because _render() and
            # _state_source are the substrate render_result() reads:
            # _render fills _x_range/_y_range/_image_source, and the
            # state dict is derived from _panel_spec().  Both are pure
            # Python and need no browser.
            self._fig = None
            return

        # Figure
        self._fig = figure(
            title         = self._effective_title(),
            width         = self._width,
            height        = self._height,
            x_range       = (x0, x1),
            y_range       = (y0, y1),
            x_axis_label  = self._panel_spec().axis_label("x"),
            y_axis_label  = self._panel_spec().axis_label("y"),
            tools         = "pan,wheel_zoom,box_zoom,reset,save",
            active_scroll = "wheel_zoom",
        )
        # Client-side only (bokeh.models.Toolbar.autohide) — no server
        # round-trip, no JS beyond what Bokeh already generates for it.
        self._fig.toolbar.autohide = self._compact_toolbar

        # Subclass adds its glyphs (image_rgba, scatter, etc.)
        self._build_glyphs()

        # Shared tick formatters reading from _state_source
        # Tick formatting lives in tick_format so the matplotlib export
        # chrome can produce byte-identical labels from Python.  Do not
        # inline a copy back here -- test_tick_format asserts this module
        # does not define its own, because a local copy would let the
        # browser and exported PNGs drift while the parity tests kept
        # passing against the shared string.
        self._fig.yaxis.formatter = CustomJSTickFormatter(
            args={"state": self._state_source,
                  "axis_key": "y_is_time", "t0_key": "full_y0",
                  "scale_key": "y_scale"},
            code=TICK_FORMATTER_JS,
        )
        self._fig.xaxis.formatter = CustomJSTickFormatter(
            args={"state": self._state_source,
                  "axis_key": "x_is_time", "t0_key": "full_x0",
                  "scale_key": "x_scale"},
            code=TICK_FORMATTER_JS,
        )

        # Info / hover status div
        self._info_div = Div(
            text        = "<i>Hover over the plot to inspect a pixel</i>",
            width       = self._width,
            sizing_mode = "stretch_width",
            styles      = {
                "font-size":   "12px",
                "font-family": "monospace",
                "padding":     "4px 8px",
                "background":  "#1e1e2e",
                "color":       "#cdd6f4",
                "border-top":  "1px solid #45475a",
            },
        )

        self._layout = column(
            self._fig, self._info_div,
            sizing_mode="stretch_width",
        )

        # Wiring
        self._add_hover_tool()
        if self._comm is not None:
            self._add_rerender_trigger()
            self._add_axes_changed_handler()
            if self._enable_flagging:
                self._add_flag_tools()

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
        info_div    = self._info_div
        comm        = self._comm
        msg_probe   = self._msg_probe
        vr_id       = self.vr_id   # unique per-figure ID
        hover_js = CustomJS(
            args={"info_div": info_div, "comm": comm,
                  "cursor_src": self._cursor_source_ref,
                  "fig_id": vr_id},
            code=f"""
const now = Date.now();
if (window._cvLastProbe && (now - window._cvLastProbe) < 120) return;
window._cvLastProbe = now;
const x = cb_data.geometry.x;
const y = cb_data.geometry.y;
if (x == null || y == null) return;
if (cursor_src) cursor_src.data = {{x: [x], y: [y], fig: [fig_id]}};
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
        """No-op placeholder — previously used js_on_change on _state_source.

        In cubevis's file-serve + websocket architecture Bokeh model mutations
        from Python handlers are not propagated to the browser.  Axis label,
        title, and viewport updates after a Plot press are instead returned in
        the handler response payload and applied by the JS ``doPlot`` callback
        in ``VisibilityPlotter``.  See ``_notify_axes_changed``.
        """
        pass

    def _notify_axes_changed(self) -> None:
        """Called by ``update_axes`` after a successful re-render.

        In cubevis's file-serve + websocket architecture, Bokeh model
        mutations from Python handlers are not propagated to the browser.
        Axis label, title, and viewport updates are therefore returned in
        the ``_handle_plot`` response payload and applied by the JS
        ``doPlot`` callback in ``VisibilityPlotter``.

        This method calls ``_update_state_source`` to keep ``_state_source``
        consistent (tick formatters and other JS that read from it will
        reflect the new axis state on the next pan/zoom rerender).
        """
        self._update_state_source()

    # ------------------------------------------------------------------
    # Internal: CommMgr handler registration
    # ------------------------------------------------------------------

    def _register_comm_handlers(self) -> None:
        self._comm.register(self._msg_probe,    self._handle_probe)
        self._comm.register(self._msg_rerender, self._handle_rerender)
        self._register_extra_comm_handlers()
        if self._select_callback is not None and self._flag_comm is not None:
            self._flag_comm.register(self._msg_select, self._select_callback)

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

    # Separator between status-bar fields.  Exposed as a class attribute
    # rather than inlined so subclasses that need to splice their own
    # sections into a formatted probe string (VisibilityScatter builds a
    # per-layer value section) can split on it without hard-coding the
    # literal in two places.
    _PROBE_SEP = " &nbsp;|&nbsp; "

    def _probe_envelope(self, status: str, label: str, **extra) -> dict:
        """Wrap a probe answer as ``{"label": ..., "probe": {...}}``.

        ``label`` is the HTML the status bar renders and remains the only
        key the browser reads — the ``CustomJS`` hover callback in
        ``_add_comm_hover`` does ``if (resp.label) info_div.text =
        resp.label`` — so ``"probe"`` is inert client-side.  It exists so
        Python callers and tests can assert on *structure* rather than on
        presentation markup.  Tests that matched on ``"empty"`` or on an
        em dash have twice broken purely because the label format
        changed, which is a bad reason for a test to fail.

        Parameters
        ----------
        status : str
            One of ``"ok"``, ``"out_of_range"``, ``"no_data"``,
            ``"error"``.  This is the fault-tolerance lever: a caller
            asking "was there data here" reads ``status`` and the
            per-value ``None``s, never the label.
        label : str
            The status-bar HTML, unchanged from what this method's
            callers produced before the envelope existed.
        **extra
            Merged into the ``"probe"`` dict.  Every numeric value must
            have gone through ``_json_num`` first — see its docstring for
            why a stray NaN breaks the entire p2j response and not just
            one field.
        """
        return {"label": label, "probe": {"status": status, **extra}}

    def _format_coord(self, value, info) -> str:
        """Render one axis coordinate for the status bar.

        The single place a coordinate becomes text, so the axis, the
        probe, and the export cannot disagree.  Three behaviours, in
        order:

        * **Time** goes through ``tick_format.format_tick`` — the same
          function the axis formatter and the matplotlib export use, so
          the probe reads ``6m 20s`` where the axis reads ``6m 20s``.
          It previously printed raw MJD seconds (``1.35331e+09``) beside
          an axis showing elapsed time.
        * **Dimensioned** values get an SI prefix: ``372.7640 GHz``, not
          ``3.72764e+11``.  ``si_scale`` is Python-only and needs no
          JS counterpart; see its docstring.
        * **Index and dimensionless** values print plainly.

        The label comes from *info*, so a substituted axis reports what
        was actually plotted rather than what was asked for.
        """
        from .tick_format import format_tick, si_scale
        from .axes import Axis

        label = info.label
        if info.axis is Axis.TIME:
            origin = (self._y_range[0] if info is self._y_info
                      else self._x_range[0])
            return f"<b>{label}:</b> {format_tick(float(value), True, origin)}"
        scaled, unit = si_scale(float(value), info.unit)
        text = f"{scaled:.6g}" if not unit else f"{scaled:.6g} {unit}"
        return f"<b>{label}:</b> {text}"

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
            parts.append(self._format_coord(xc, self._x_info))
        if yc is not None:
            parts.append(self._format_coord(yc, self._y_info))

        # Only when neither axis already reports frequency -- on a
        # time-vs-baseline raster the cell's frequency span is genuinely
        # extra information, but beside a Frequency axis it was printing
        # the same number twice under two names.
        from .axes import Axis
        shows_freq = any(i.axis is Axis.FREQUENCY
                         for i in (self._x_info, self._y_info))
        fg = info.get("freq_range_ghz")
        if fg is not None and not shows_freq:
            lo, hi = float(fg[0]), float(fg[1])
            # 6 significant figures cannot resolve one channel width
            # (~15.6 MHz on a 372 GHz centre is the 7th digit), so a real
            # range rendered as "372.764-372.764".  Widen, and collapse to
            # a single value when the ends are genuinely equal.
            if lo == hi:
                parts.append(f"<b>Freq:</b> {lo:.9g} GHz")
            else:
                parts.append(f"<b>Freq:</b> {lo:.9g}\u2013{hi:.9g} GHz")

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

        return self._PROBE_SEP.join(parts)
