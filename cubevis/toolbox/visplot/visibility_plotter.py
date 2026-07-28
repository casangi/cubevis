"""visibility_plotter.py
========================
``VisibilityPlotter`` — astronomer-facing combined raster + scatter
visibility inspection and flagging tool.

This is a cubevis *application* class in the sense of
``cubevis-app-startup.md``: it owns a ``BokehAppContext``, a ``CommMgr``,
and an async server coroutine, and exposes a ``show()`` entry point for
Jupyter notebook use.

Astronomer-facing API
---------------------
The constructor accepts only strings, numbers, and lists — no internal
objects.  The same call works in the preview and in the full release;
no API changes are required later.

::

    from cubevis.toolbox.visplot import VisibilityPlotter

    plotter = VisibilityPlotter(
        ms          = "sis14_twhya_calibrated_flagged.ms",
        field       = "0637-752",
        spw         = "0,1,2,3",
        correlation = "XX,YY",
        datacolumn  = "data",
        mode        = "both",
        layout      = "side",
        enable_flagging = True,   # False -> quick-look, no Flag/Unflag tools
    )
    plotter.show()

Preview scope
-------------
* Display mode toggle: Both / Raster only / Scatter only (CustomJS)
* ``enable_flagging`` (default True): adds the FlagTool / Unflag drag
  tools to both panels' toolbars, replacing box-select. Set False for a
  quick-look instance with no flagging workflow at all — only
  pan/wheel-zoom/box-zoom/reset/save remain.
* Layout toggle: Side by Side / Over Under — dual container approach,
  both Bokeh row/column containers always in the document, one hidden
* Collapsible sidebar with ⟨/⟩ toggle button
* Dark-mode sidebar widget styling via InlineStyleSheet
* Shared toolbar: pan, zoom, reset applied to both figures simultaneously;
  individual figure toolbars hidden (toolbar_location=None)
* Cursor tracking: raster._info_div and scatter._info_div surfaced via
  raster.layout / scatter.layout
* Session-scoped layout preference memory (ColumnDataSource JSON store)
* Sidebar: data selection (field, SPW, correlation, data column),
  raster axis controls, scatter axis controls, colormap controls
* Toolbar: Plot ▶, Reload ↺, ⟨Sidebar, display mode, layout, presets,
  pan/zoom/reset. Flag ⚑ / Unflag are per-figure drag tools (see
  ``enable_flagging`` above), not top-level buttons.
* Flag/Unflag box → FlagDB accumulation + red overlay re-render stub
* Linked x-axis Range1d when both panels share x dimension
* Status bar Div

Absent from the preview (Phase 2+):
* Writing flags to disk
* Iteration (Prev/Next), Locate, Save plot, Copy flagdata
* Averaging controls
* Calibration sidebar section

Package location
----------------
``cubevis/cubevis/toolbox/visplot/visibility_plotter.py``
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional
from uuid import uuid4

import websockets

from bokeh.events import MouseEnter, MouseLeave
from bokeh.layouts import column, row
from bokeh.models import (
    Button, CheckboxGroup, ColumnDataSource, CustomJS, Div,
    InlineStyleSheet, MultiSelect, RadioButtonGroup, Select,
    TextInput, Toggle,
)

from cubevis.bokeh import BokehInit
from cubevis.bokeh.models import BokehAppContext, Showable, EvTextInput, Tip
from bokeh.models import Tooltip
from bokeh.models.dom import HTML as BokehHTML
from cubevis.bokeh.transport import CommMgr
from cubevis import exe

from .axes import Axis
from .selection import SelectionSpec
from .visibility_raster import VisibilityRaster
from .visibility_scatter import VisibilityScatter, ScatterLayer, _LAYER_CMAPS
from .visibility_plot import _axis_label
from .flag_db import FlagDB
from .reduction_context import (
    FlagDelta,
    NullReductionContext,
    ObservationMetadata,
    ReductionBackend,
    ReductionContext,
)
from .local_visibility_reader import LocalVisibilityReader

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants / defaults
# ---------------------------------------------------------------------------

_PANEL_WIDTH_SIDE  = 500    # each panel in side-by-side mode
_PANEL_WIDTH_FULL  = 1020   # single-panel or over/under mode
_PANEL_HEIGHT      = 550
_PANEL_HEIGHT_OVER = 280    # each panel height in over/under mode
_SIDEBAR_WIDTH     = 260
_SIDEBAR_WIDTH_COL = 268    # column width including padding

# Preset definitions: (raster_y, raster_x, raster_qty, scatter_x, scatter_y, layout)
_PRESETS = {
    "vplot": (
        Axis.BASELINE, Axis.TIME,    Axis.AMPLITUDE,
        Axis.TIME,     Axis.AMPLITUDE,
        "side",
    ),
    "radplot": (
        # Raster: Amplitude vs Baseline (x=TIME is native; UVDIST is scatter-only)
        # Scatter: Amplitude vs UVDIST (the defining radplot axis)
        Axis.BASELINE, Axis.TIME,    Axis.AMPLITUDE,
        Axis.UVDIST,   Axis.AMPLITUDE,
        "side",
    ),
    "waterfall": (
        Axis.TIME,     Axis.CHANNEL, Axis.AMPLITUDE,
        Axis.TIME,     Axis.AMPLITUDE,
        "over",
    ),
}

_RASTER_Y_OPTIONS   = [("TIME",      "Time"),
                       ("BASELINE",  "Baseline")]
_RASTER_X_OPTIONS   = [("CHANNEL",  "Channel"),
                       ("TIME",     "Time")]
_RASTER_QTY_OPTIONS = [("AMPLITUDE", "Amplitude"),
                       ("PHASE",     "Phase")]
_SCATTER_X_OPTIONS  = [("UVDIST",    "UV Distance"),
                       ("TIME",      "Time"),
                       ("FREQUENCY", "Frequency")]
_SCATTER_Y_OPTIONS  = [("AMPLITUDE", "Amplitude"),
                       ("PHASE",     "Phase")]

# Dark-mode CSS applied to all sidebar input widgets via InlineStyleSheet.
# Overrides Bokeh's default light component styles so widgets blend with
# the #1e1e2e sidebar background.
_DARK_WIDGET_CSS = """
:host { --bokeh-base-font: system-ui, sans-serif; }
.bk-input {
    background:   #313244 !important;
    color:        #cdd6f4 !important;
    border-color: #45475a !important;
}
select.bk-input option {
    background: #313244;
    color:      #cdd6f4;
}
.bk-input-group label,
.bk-label,
label {
    color: #cdd6f4 !important;
}
.bk-btn {
    background: #313244 !important;
    color:      #cdd6f4 !important;
    border-color: #45475a !important;
}
.bk-btn:hover {
    background: #45475a !important;
}
"""


# ---------------------------------------------------------------------------
# MSSelection string parsers (preview-grade — full parser in Phase 2)
# ---------------------------------------------------------------------------

def _parse_spw_string(spw_str: str, meta: ObservationMetadata) -> list[int]:
    if not spw_str or spw_str.strip() == "":
        return [s.spw_id for s in meta.spws]
    result = []
    for tok in spw_str.split(","):
        tok = tok.strip()
        if tok.isdigit():
            result.append(int(tok))
    return result or [s.spw_id for s in meta.spws]


def _parse_correlation_string(corr_str: str,
                               meta: ObservationMetadata) -> list[str]:
    if not corr_str or corr_str.strip() == "":
        if meta.spws:
            return list(meta.spws[0].polarizations)
        return ["XX", "YY"]
    return [c.strip().upper() for c in corr_str.split(",") if c.strip()]


def _parse_field_string(field_str: str,
                         meta: ObservationMetadata) -> Optional[str]:
    if not field_str or field_str.strip() == "":
        return None
    if field_str.strip().isdigit():
        idx = int(field_str.strip())
        if 0 <= idx < len(meta.fields):
            return meta.fields[idx].name
    return field_str.strip() or None


# ---------------------------------------------------------------------------
# Backend probes and open_ms / open_ps factory functions
# ---------------------------------------------------------------------------

def _probe_casatasks() -> bool:
    try:
        import casatasks  # noqa: F401
        return True
    except ImportError:
        return False


def _probe_radps() -> bool:
    try:
        import radps  # noqa: F401
        return True
    except ImportError:
        pass
    try:
        import astroviper  # noqa: F401
        return True
    except ImportError:
        return False


def _make_casa6_context(path: str) -> ReductionContext:
    raise NotImplementedError(
        "Casa6ReductionContext is not yet implemented. "
        "Pass backend='null' to suppress this error and use display-only mode."
    )


def _make_radps_context(path: str) -> ReductionContext:
    raise NotImplementedError(
        "RadpsReductionContext is not yet implemented. "
        "Pass backend='null' to suppress this error and use display-only mode."
    )


def _make_remote_context(path: str, endpoint: str) -> ReductionContext:
    raise NotImplementedError(
        f"RemoteReductionContext is not yet implemented (preview release). "
        f"endpoint={endpoint!r} path={path!r}"
    )


def _resolve_context_msv2(path, backend, remote_endpoint):
    if backend == ReductionBackend.NULL:
        return NullReductionContext()
    if backend == ReductionBackend.REMOTE:
        if not remote_endpoint:
            raise ValueError("backend='remote' requires remote_endpoint.")
        return _make_remote_context(path, remote_endpoint)
    if backend == ReductionBackend.CASA6:
        if not _probe_casatasks():
            raise RuntimeError("backend='casa6' requested but casatasks not importable.")
        return _make_casa6_context(path)
    if backend == ReductionBackend.RADPS:
        if not _probe_radps():
            raise RuntimeError("backend='radps' requested but RADPS not available.")
        return _make_radps_context(path)
    # AUTO
    if _probe_casatasks():
        try:
            return _make_casa6_context(path)
        except NotImplementedError:
            log.debug("open_ms (auto): Casa6ReductionContext not implemented; trying RADPS")
    if _probe_radps():
        try:
            return _make_radps_context(path)
        except NotImplementedError:
            log.debug("open_ms (auto): RadpsReductionContext not implemented; using Null")
    return NullReductionContext()


def _resolve_context_msv4(path, backend, remote_endpoint):
    if backend == ReductionBackend.CASA6:
        raise ValueError("backend='casa6' is not valid for MSv4/PS data.")
    if backend == ReductionBackend.NULL:
        return NullReductionContext()
    if backend == ReductionBackend.REMOTE:
        if not remote_endpoint:
            raise ValueError("backend='remote' requires remote_endpoint.")
        return _make_remote_context(path, remote_endpoint)
    if backend == ReductionBackend.RADPS:
        if not _probe_radps():
            raise RuntimeError("backend='radps' requested but RADPS not available.")
        return _make_radps_context(path)
    # AUTO
    if _probe_radps():
        try:
            return _make_radps_context(path)
        except NotImplementedError:
            log.debug("open_ps (auto): RadpsReductionContext not implemented; using Null")
    return NullReductionContext()


def open_ms(path, *, backend=ReductionBackend.AUTO, remote_endpoint=None):
    """Open an MSv2 measurement set; return (metadata, reader, context)."""
    from .data.msv2_backend import MSv2Backend
    backend = ReductionBackend(backend)
    b = MSv2Backend(path)
    b.open()
    reader = LocalVisibilityReader(b)
    meta = ObservationMetadata.from_backend_metadata(
        reader.metadata(), source_path=path
    )
    context = _resolve_context_msv2(path, backend, remote_endpoint)
    log.debug("open_ms: fields=%d spws=%d context=%s",
              len(meta.fields), len(meta.spws), type(context).__name__)
    return meta, reader, context


def open_ps(path, *, backend=ReductionBackend.AUTO, remote_endpoint=None):
    """Open an MSv4 / Processing Set; return (metadata, reader, context)."""
    from .data.msv4_backend import MSv4Backend
    backend = ReductionBackend(backend)
    b = MSv4Backend(path)
    b.open()
    reader = LocalVisibilityReader(b)
    meta = ObservationMetadata.from_backend_metadata(
        reader.metadata(), source_path=path
    )
    context = _resolve_context_msv4(path, backend, remote_endpoint)
    log.debug("open_ps: fields=%d spws=%d context=%s",
              len(meta.fields), len(meta.spws), type(context).__name__)
    return meta, reader, context


def _make_scatter_layers(
    y_axis: "Axis",
    polarizations: list[str],
    scaling_alpha: float = 50.0,
) -> list[ScatterLayer]:
    """Build one ``ScatterLayer`` per polarisation with assigned cmaps.

    ``VisibilityScatter.__init__`` assigns cmaps when ``lyr.cmap is None``,
    but ``update_axes`` with a fresh layer list does not.  This helper
    always assigns cmaps explicitly so the shade step never receives
    ``cmap=None`` regardless of which code path constructs the layers.
    """
    return [
        ScatterLayer(
            y_axis        = y_axis,
            polarization  = pol,
            cmap          = _LAYER_CMAPS[i % len(_LAYER_CMAPS)],
            scaling_alpha = scaling_alpha,
        )
        for i, pol in enumerate(polarizations)
    ]


# ---------------------------------------------------------------------------
# VisibilityPlotter
# ---------------------------------------------------------------------------

class VisibilityPlotter:
    """Combined raster + scatter visibility inspection and flagging tool.

    Parameters
    ----------
    ms : str | None
        Path to an MSv2 measurement set.  Exactly one of ``ms`` or ``ps``
        must be supplied.
    ps : str | None
        Path to an MSv4 / Processing Set Zarr store.
    backend : str
        Reduction backend: ``"auto"``, ``"casa6"``, ``"radps"``,
        ``"remote"``, or ``"null"``.  Default ``"auto"``.
    remote_endpoint : str | None
        Required only when ``backend="remote"``.
    field : str
        Field name or integer index string.  Default: first field.
    spw : str
        Comma-separated SPW indices (``"0,1,2,3"``).  Default: all.
    antenna : str
        MSSelection antenna string.  (Stored; not yet wired in preview.)
    scan : str
        MSSelection scan string.  (Stored; not yet wired.)
    timerange : str
        MSSelection time-range string.  (Stored; not wired.)
    uvrange : str
        UV range string.  (Stored; not wired.)
    correlation : str
        Comma-separated correlation labels (``"XX,YY"``).  Default: all.
    datacolumn : str
        Visibility column: ``"data"``, ``"corrected"``, or ``"model"``.
    mode : str
        Initial display mode: ``"both"``, ``"raster"``, or ``"scatter"``.
    layout : str
        Initial layout: ``"side"`` (side by side) or ``"over"`` (over/under).
    preset : str | None
        Named preset: ``"vplot"``, ``"radplot"``, ``"waterfall"``, or ``None``.
    time_range : tuple[float, float] | list[float] | None
        ``(start, end)`` as MJD floats.
    freq_range : tuple[float, float] | list[float] | None
        ``(start, end)`` in Hz.
    uvdist_range : tuple[float, float] | list[float] | None
        ``(min, max)`` in metres.
    """

    def __init__(
        self,
        *,
        ms:               Optional[str] = None,
        ps:               Optional[str] = None,
        backend:          str           = "auto",
        remote_endpoint:  Optional[str] = None,
        field:            str           = "",
        spw:              str           = "",
        antenna:          str           = "",
        scan:             str           = "",
        timerange:        str           = "",
        uvrange:          str           = "",
        correlation:      str           = "",
        datacolumn:       str           = "data",
        mode:             str           = "both",
        layout:           str           = "side",
        preset:           Optional[str] = None,
        time_range:       tuple[float, float] | list[float] | None = None,
        freq_range:       tuple[float, float] | list[float] | None = None,
        uvdist_range:     tuple[float, float] | list[float] | None = None,
        enable_flagging:  bool           = True,
    ) -> None:

        # ------------------------------------------------------------------ #
        # Validate                                                             #
        # ------------------------------------------------------------------ #
        if ms is not None and ps is not None:
            raise ValueError("Supply exactly one of ms= or ps=, not both.")
        if ms is None and ps is None:
            raise ValueError("One of ms= or ps= must be supplied.")

        # ------------------------------------------------------------------ #
        # Store arguments                                                      #
        # ------------------------------------------------------------------ #
        self._ms_path       = ms
        self._ps_path       = ps
        self._source_path   = ms or ps
        self._field_str     = field
        self._spw_str       = spw
        self._antenna_str   = antenna
        self._scan_str      = scan
        self._timerange_str = timerange
        self._uvrange_str   = uvrange
        self._corr_str      = correlation
        self._datacolumn    = datacolumn.upper()
        self._mode          = mode.lower()
        self._layout        = layout.lower()
        self._preset        = preset.lower() if preset else None
        self._time_range    = time_range
        self._freq_range    = freq_range
        self._uvdist_range  = uvdist_range
        self._enable_flagging = enable_flagging

        if self._mode not in ("both", "raster", "scatter"):
            raise ValueError(f"mode must be 'both', 'raster', or 'scatter'; got {mode!r}")
        if self._layout not in ("side", "over"):
            raise ValueError(f"layout must be 'side' or 'over'; got {layout!r}")

        # ------------------------------------------------------------------ #
        # Open data source                                                     #
        # ------------------------------------------------------------------ #
        if ms is not None:
            self._meta, self._reader, self._context = open_ms(
                ms, backend=backend, remote_endpoint=remote_endpoint
            )
        else:
            self._meta, self._reader, self._context = open_ps(
                ps, backend=backend, remote_endpoint=remote_endpoint
            )

        self._selection = self._build_selection()
        # Record initial selection for raster_axes_changed comparison
        self._last_raster_selection = self._selection

        # ------------------------------------------------------------------ #
        # Preset axes                                                          #
        # ------------------------------------------------------------------ #
        raster_y   = Axis.TIME
        raster_x   = Axis.CHANNEL
        raster_qty = Axis.AMPLITUDE
        scatter_x  = Axis.UVDIST
        scatter_y  = Axis.AMPLITUDE

        if self._preset and self._preset in _PRESETS:
            ry, rx, rq, sx, sy, pl = _PRESETS[self._preset]
            raster_y, raster_x, raster_qty = ry, rx, rq
            scatter_x, scatter_y           = sx, sy
            self._layout                   = pl
            self._mode                     = "both"

        self._raster_y   = raster_y
        self._raster_x   = raster_x
        self._raster_qty = raster_qty
        self._scatter_x  = scatter_x
        self._scatter_y  = scatter_y

        # ------------------------------------------------------------------ #
        # FlagDB + hotkey scope                                                #
        # ------------------------------------------------------------------ #
        self._flag_db         = FlagDB()
        self._hotkey_scope_id = str(uuid4())

        # ------------------------------------------------------------------ #
        # Communication infrastructure                                         #
        # ------------------------------------------------------------------ #
        def _shutdown_handler(reason, description):
            self._stop()
            BokehInit.clear_app_context(self._app_context)

        def _connection_closed_handler(reason, description):
            #
            # A connection ended but the session is still alive -- the laptop
            # slept, the network blipped, or the browser tab was reloaded.
            #
            # Do NOT call self._stop() here. It resolves _result_future, which
            # exits the `async with websockets.serve( ... )` block in
            # _task_server() and closes the listening socket that the frontend
            # is about to reconnect to.
            #
            log.debug(
                "VisibilityPlotter: connection lost (%s); awaiting reconnection",
                description,
            )

        def _reconnect_handler(generation):
            log.debug(
                "VisibilityPlotter: frontend reconnected (generation=%d)", generation
            )

        self._app_context = BokehAppContext(
            comm_mgr  = CommMgr(on_shutdown=_shutdown_handler),
            app_state = {
                "name":        "VisibilityPlotter",
                "initialized": True,
                "source_path": self._source_path,
                "mode":        self._mode,
                "layout":      self._layout,
            },
            title = f"VisibilityPlotter — {os.path.basename(self._source_path)}",
        )
        self._comm_mgr = self._app_context.comm_mgr

        # ------------------------------------------------------------------ #
        # Reconnection behaviour                                              #
        # ------------------------------------------------------------------ #
        # These fire for transient disconnects and are distinct from
        # on_shutdown, which ends the session for good.
        self._comm_mgr.set_connection_closed_callback(_connection_closed_handler)
        self._comm_mgr.set_reconnect_callback(_reconnect_handler)

        # Requests that were in flight when a connection dropped are replayed
        # once the frontend returns. All p2j traffic here is idempotent GUI
        # state (raster/scatter payloads, widget values), so redelivery is
        # safe. Set to False if that ever stops being true.
        self._comm_mgr.resend_inflight_on_reconnect = True

        # Wait indefinitely for the frontend to come back. Set to a number of
        # seconds to have the session shut itself down after an outage of that
        # length -- useful if a closed browser tab should not leave the Python
        # side running forever.
        self._comm_mgr.reconnect_timeout = None

        self._result_future = None

        self._pipe = {"control": None}
        self._pipe["control"] = self._comm_mgr.open(
            squash_queue=True,
            description="visibility plotter control",
        )

        # Message IDs — must be created before registering handlers
        self._ids = {
            "plot": str(uuid4()),
            "done": str(uuid4()),
        }
        self._pipe["control"].register(self._ids["plot"], self._handle_plot)
        self._pipe["control"].register(self._ids["done"], self._handle_done)

        # ------------------------------------------------------------------ #
        # Construct display widgets                                            #
        # ------------------------------------------------------------------ #
        pols = self._selection.correlation or ["XX"]
        first_pol = pols[0]

        # Shared cursor ColumnDataSource for linked cursor across figures.
        # Created before widgets and passed via cursor_source= so the hover
        # CustomJS has the reference at _build() time.
        import math
        self._cursor_source = ColumnDataSource(
            data={"x": [math.nan], "y": [math.nan], "fig": [""]}
        )

        # Use stretch_width so figures fill available space responsively.
        # Fixed pixel widths are not set — Bokeh's CSS flexbox handles sizing.
        self._raster = VisibilityRaster(
            backend       = self._reader,
            selection     = self._selection,
            y_dim         = self._raster_y,
            x_dim         = self._raster_x,
            quantity      = self._raster_qty,
            polarization  = first_pol,
            width         = _PANEL_WIDTH_SIDE,
            height        = _PANEL_HEIGHT,
            comm_mgr      = self._comm_mgr,
            cursor_source = self._cursor_source,
            enable_flagging = self._enable_flagging,
        )

        # One scatter layer per polarisation — multi-layer compositing
        # naturally boosts density and visibility vs a single layer.
        self._scatter = VisibilityScatter(
            backend       = self._reader,
            selection     = self._selection,
            x_axis        = self._scatter_x,
            layers        = _make_scatter_layers(self._scatter_y, pols),
            width         = _PANEL_WIDTH_SIDE,
            height        = _PANEL_HEIGHT,
            comm_mgr      = self._comm_mgr,
            cursor_source = self._cursor_source,
            enable_flagging = self._enable_flagging,
        )

        # Use stretch_width so figures expand to fill available space when
        # the sidebar is collapsed or layout changes.
        self._raster.figure.sizing_mode  = "stretch_width"
        self._scatter.figure.sizing_mode = "stretch_width"

        # Keep the Bokeh toolbar visible on each figure — it provides all
        # standard tools (pan, wheel zoom, box zoom, reset, save, etc.) with
        # correct visual feedback.  Tool synchronisation is done via JS
        # on_change callbacks so selecting a tool on one figure activates
        # the matching tool on the other.
        self._raster.figure.toolbar_location  = "right"
        self._scatter.figure.toolbar_location = "right"

        # Apply dark mode styling at construction time so figures match the
        # dark sidebar/status bar without the user needing to press the button.
        for fig in (self._raster.figure, self._scatter.figure):
            fig.background_fill_color = "black"
            fig.border_fill_color     = "#1e1e2e"
            if fig.title:
                fig.title.text_color  = "#cdd6f4"
            _lc = "#cdd6f4"
            _gc = "#45475a"
            for ax in (*fig.below, *fig.left, *fig.right, *fig.above):
                if hasattr(ax, "axis_label_text_color"):
                    ax.axis_label_text_color      = _lc
                    ax.major_label_text_color     = _lc
                    ax.axis_line_color            = _lc
                    ax.major_tick_line_color      = _lc
                    ax.minor_tick_line_color      = _lc
            for g in fig.center:
                if hasattr(g, "grid_line_color"):
                    g.grid_line_color = _gc

        # ------------------------------------------------------------------ #
        # Synchronise Bokeh toolbars between the two figures.              #
        # js_on_change on the toolbar model property fires when the user   #
        # activates a tool via the Bokeh UI.                               #
        # ------------------------------------------------------------------ #
        _sync_drag_code = """
const t = cb_obj.active_drag;
if (!t) { other.active_drag = null; return; }
for (const dt of other.tools) {
    if (dt.type === t.type) { other.active_drag = dt; return; }
}
"""
        _sync_scroll_code = """
const t = cb_obj.active_scroll;
if (!t) { other.active_scroll = null; return; }
for (const dt of other.tools) {
    if (dt.type === t.type) { other.active_scroll = dt; return; }
}
"""
        r_tb = self._raster.figure.toolbar
        s_tb = self._scatter.figure.toolbar

        r_tb.js_on_change("active_drag",
            CustomJS(args={"other": s_tb}, code=_sync_drag_code))
        s_tb.js_on_change("active_drag",
            CustomJS(args={"other": r_tb}, code=_sync_drag_code))
        r_tb.js_on_change("active_scroll",
            CustomJS(args={"other": s_tb}, code=_sync_scroll_code))
        s_tb.js_on_change("active_scroll",
            CustomJS(args={"other": r_tb}, code=_sync_scroll_code))

        # ------------------------------------------------------------------ #
        # Wire flag/unflag callbacks. Registering this is harmless even     #
        # when enable_flagging=False — with no FlagTool present on either   #
        # figure's toolbar, nothing in the browser ever sends _msg_select,  #
        # so these closures simply never fire.                             #
        # ------------------------------------------------------------------ #
        async def _raster_select(msg, context=None, self=self):
            return await self._handle_box_select(msg, panel="raster")

        async def _scatter_select(msg, context=None, self=self):
            return await self._handle_box_select(msg, panel="scatter")

        self._raster.register_select_callback(_raster_select)
        self._scatter.register_select_callback(_scatter_select)

        # ------------------------------------------------------------------ #
        # Build layout                                                         #
        # ------------------------------------------------------------------ #
        inner_layout = self._build_layout()
        self._app_context.ui = inner_layout

    # ====================================================================== #
    # Public entry point                                                       #
    # ====================================================================== #

    def show(self) -> "Showable":
        """Display the plotter in a Jupyter notebook cell."""
        from bokeh.io.state import curstate
        if not curstate().notebook:
            from bokeh.io import output_notebook
            output_notebook()

        app_id  = uuid4()
        context = exe.Context(exe.Mode.THREAD)
        app_context, task = self(context, app_id)

        future_holder = [None]

        def startup():
            future_holder[0] = context.execute(task, app_id)

        def get_future():
            if future_holder[0] is None:
                raise RuntimeError("VisibilityPlotter has not been launched yet")
            return future_holder[0]

        return Showable(
            app_context, startup, get_future,
            name="visibility-plotter-jpy",
        )

    # ====================================================================== #
    # cubevis application protocol                                             #
    # ====================================================================== #

    def __call__(self, exec_context, task_id=None):
        self._open_channels()
        return self._app_context, exe.Task(self._task_server)

    async def _task_server(self):
        self._result_future = asyncio.Future()
        if self._comm_mgr.address:
            #
            # The serve() context manager must outlive any individual
            # connection. process_messages() is the *per-connection* handler
            # and returns every time a socket drops; keeping the listener open
            # across those returns is what makes reconnection possible. Only
            # _stop() -- via _shutdown_handler -- may resolve _result_future.
            #
            # The keepalive parameters let the server notice a dead peer in
            # ~30s instead of waiting out TCP timeouts. The frontend runs its
            # own application-level __ping__/__pong__ heartbeat, because
            # browsers cannot send WebSocket ping frames from JavaScript.
            #
            async with websockets.serve(
                self._comm_mgr.process_messages,
                self._comm_mgr.address[0],
                self._comm_mgr.address[1],
                ping_interval = 20,
                ping_timeout  = 10,
                close_timeout = 5,
            ):
                await self._result_future
        else:
            await self._comm_mgr.process_messages()
        return self.result()

    def _stop(self, _=None):
        if self._result_future is not None and not self._result_future.done():
            self._result_future.set_result(None)

    def result(self):
        if self._result_future is None or not self._result_future.done():
            return None
        return self._result_future.result()

    def _open_channels(self):
        # Channels opened eagerly in __init__; this is a no-op guard.
        pass

    # ====================================================================== #
    # j2p handlers                                                             #
    # ====================================================================== #

    async def _handle_plot(self, msg, context=None):
        # Reload ↺ (as opposed to Plot ▶ or a preset) starts over — the
        # pending FlagDB is preview-scope, in-memory, never written
        # anywhere, so "reload" discarding it is the correct behaviour
        # rather than something to preserve across a fresh render.
        did_reload = bool(msg.get("reload", False))
        if did_reload:
            self._flag_db = FlagDB()
            log.debug("_handle_plot: reload requested — FlagDB cleared")

        if "field"       in msg: self._field_str   = msg["field"]
        if "spw"         in msg: self._spw_str      = msg["spw"]
        if "correlation" in msg: self._corr_str     = msg["correlation"]
        if "datacolumn"  in msg: self._datacolumn   = msg["datacolumn"].upper()
        for attr, key in (("_raster_y",   "raster_y"),
                          ("_raster_x",   "raster_x"),
                          ("_raster_qty", "raster_qty"),
                          ("_scatter_x",  "scatter_x"),
                          ("_scatter_y",  "scatter_y")):
            if key in msg:
                try:
                    setattr(self, attr, Axis[msg[key]])
                except KeyError:
                    log.warning("_handle_plot: unknown Axis %r for %s",
                                msg[key], key)

        self._selection = self._build_selection()
        pols      = self._selection.correlation or ["XX"]
        first_pol = pols[0]

        # Update both widgets' selection so field/SPW/correlation changes
        # take effect on the next render.
        self._raster._selection  = self._selection
        self._scatter._selection = self._selection

        # Force re-render by resetting stored dims then calling update_axes.
        # Forcing _y_dim/_x_dim to None makes update_axes always detect
        # a change and run _render, even if the new value equals the old one.
        # Force raster re-render only when raster axes or selection content
        # actually changed — not just because a new SelectionSpec object
        # was created (which always happens at top of _handle_plot).
        raster_axes_changed = (
            self._raster_y   != self._raster._y_dim   or
            self._raster_x   != self._raster._x_dim   or
            self._raster_qty != self._raster._quantity or
            self._selection.field_names  != getattr(self._last_raster_selection, 'field_names', None)  or
            self._selection.spw          != getattr(self._last_raster_selection, 'spw', None)           or
            self._selection.correlation  != getattr(self._last_raster_selection, 'correlation', None)   or
            self._selection.data_column  != getattr(self._last_raster_selection, 'data_column', None)
        )

        try:
            if raster_axes_changed:
                self._raster._y_dim    = None
                self._raster._x_dim    = None
                self._raster._quantity = None
                self._raster.update_axes(
                    y_dim        = self._raster_y,
                    x_dim        = self._raster_x,
                    quantity     = self._raster_qty,
                    polarization = first_pol,
                )
                self._last_raster_selection = self._selection
        except Exception as exc:
            log.error("_handle_plot: raster update_axes failed: %s", exc,
                      exc_info=True)
            text = f"⚠ Raster error: {exc}"
            self._notify(text)
            return {"status": "error", "status_text": text,
                    "notify_text": text, "notify_color": "#f38ba8"}

        try:
            layers = _make_scatter_layers(self._scatter_y, pols)
            log.debug("_handle_plot: scatter update_axes x=%s layers=%s",
                      self._scatter_x, [(l.y_axis, l.polarization) for l in layers])
            # Setting _x_dim to None forces update_axes to always re-render.
            self._scatter._x_dim  = None
            self._scatter.update_axes(
                x_dim  = self._scatter_x,
                layers = layers,
            )
            log.debug("_handle_plot: scatter _layer_aggs after render: %s",
                      [a is not None for a in self._scatter._layer_aggs])
        except Exception as exc:
            log.error("_handle_plot: scatter update_axes failed: %s", exc,
                      exc_info=True)
            text = f"⚠ Scatter error: {exc}"
            self._notify(text)
            return {"status": "error", "status_text": text,
                    "notify_text": text, "notify_color": "#f38ba8"}

        self._notify("")   # clear any previous warning
        rx0, rx1 = self._raster._x_range
        ry0, ry1 = self._raster._y_range
        sx0, sx1 = self._scatter._x_range
        sy0, sy1 = self._scatter._y_range

        r_img_data = self._raster._image_source.data
        s_img_data = self._scatter._image_source.data

        return {
            "status":       "ok",
            "status_text":  self._status_text(),
            "notify_text":  "",
            # Always send the raster image to keep the hover tool renderer
            # active (required for cursor tracking). When raster_axes_changed=False
            # the image is unchanged and we skip the range reset to avoid
            # triggering a spurious viewport rerender via x_range.js_on_change.
            "raster_image":   r_img_data["image"][0],
            "raster_x0":      float(rx0) if raster_axes_changed else None,
            "raster_x1":      float(rx1) if raster_axes_changed else None,
            "raster_y0":      float(ry0) if raster_axes_changed else None,
            "raster_y1":      float(ry1) if raster_axes_changed else None,
            "raster_x_label": _axis_label(self._raster_x) if raster_axes_changed else None,
            "raster_y_label": _axis_label(self._raster_y) if raster_axes_changed else None,
            "raster_title":   self._raster._effective_title() if raster_axes_changed else None,
            # Scatter image + axes
            "scatter_image": s_img_data["image"][0],
            "scatter_x0":    float(s_img_data["x"][0]),
            "scatter_x1":    float(s_img_data["x"][0]) + float(s_img_data["dw"][0]),
            "scatter_y0":    float(s_img_data["y"][0]),
            "scatter_y1":    float(s_img_data["y"][0]) + float(s_img_data["dh"][0]),
            "scatter_x_label": _axis_label(self._scatter_x),
            "scatter_y_label": _axis_label(self._scatter_y),
            "scatter_title":   self._scatter._effective_title(),
            # _state_source (full_x0/x1/y0/y1, agg_n_x/agg_n_y, ...) has no
            # other path to the browser without a Bokeh server — without
            # this, FlagTool's zoom-to-1:1 math (flag_tool.ts) keeps using
            # whatever full_x0/agg_n_x were current as of the last time
            # this was sent (page load, or the last axes change that
            # happened to trigger it), silently wrong after any axis
            # change. Raster is gated the same way its image/range fields
            # already are; scatter always re-renders so is unconditional.
            "raster_state":  self._raster._state_data()  if raster_axes_changed else None,
            "scatter_state": self._scatter._state_data(),
        }

    async def _handle_done(self, msg, context=None):
        self._stop()
        return {"result": "stopped"}

    async def _handle_box_select(self, msg: dict, panel: str) -> Optional[dict]:
        x0 = float(msg.get("x0", 0.0))
        x1 = float(msg.get("x1", 0.0))
        y0 = float(msg.get("y0", 0.0))
        y1 = float(msg.get("y1", 0.0))
        # FlagTool(flag=True) vs. FlagTool(flag=False) ("Unflag") — both
        # send through the same _msg_select channel and are distinguished
        # here. Default True only covers messages from something older
        # that didn't set the field.
        flag = bool(msg.get("flag", True))
        verb = "flag" if flag else "unflag"

        # No Bokeh server here, so self._notify()'s Python-side
        # `_notify_div.text = ...` assignment never reaches the browser
        # on its own — it's still called (keeps Python-side state
        # consistent, e.g. for a later full-page re-render), but the
        # thing that actually updates the browser is this handler's
        # *return value*: FlagTool's comm.send() callback (flag_tool.ts)
        # applies notify_text/notify_color/status_text directly to the
        # live notify_div/status_div models, the same p2j response
        # mechanism doPlot already uses for resp.status_text.
        colour_warn = "#f38ba8"
        colour_ok   = "#a6e3a1"

        # The box now draws at any zoom level (better UX feedback than a
        # silent no-op), but flagging is still only semantically valid at
        # or past 1:1 pixel resolution — averaged/decimated bins aren't
        # individual visibilities. Below that, tell the user why nothing
        # happened instead of just doing nothing.
        if not bool(msg.get("at_pixel_res", False)):
            text = (f"⚠ Zoom to ≥1:1 pixel resolution before you can {verb} "
                    f"({panel}) — nothing {verb}ged.")
            self._notify(text, colour=colour_warn)
            return {"notify_text": text, "notify_color": colour_warn}

        if panel == "raster":
            delta = FlagDelta(
                flag       = flag,
                time_range = (min(x0, x1), max(x0, x1))
                             if self._raster_x == Axis.TIME else None,
                freq_range = (min(x0, x1), max(x0, x1))
                             if self._raster_x in (Axis.CHANNEL, Axis.FREQUENCY)
                             else None,
                source  = f"raster_box_{verb}",
                comment = f"raster {verb} box x=[{x0:.4g},{x1:.4g}] y=[{y0:.4g},{y1:.4g}]",
            )
        else:
            delta = FlagDelta(
                flag       = flag,
                time_range = (min(x0, x1), max(x0, x1))
                             if self._scatter_x == Axis.TIME else None,
                freq_range = (min(x0, x1), max(x0, x1))
                             if self._scatter_x in (Axis.CHANNEL, Axis.FREQUENCY)
                             else None,
                source  = f"scatter_box_{verb}",
                comment = f"scatter {verb} box x=[{x0:.4g},{x1:.4g}] y=[{y0:.4g},{y1:.4g}]",
            )

        self._flag_db.append(delta)
        count = self._flag_db.pending_count
        log.debug(
            "%s round trip delivered to Python: panel=%s "
            "x=[%.4g,%.4g] y=[%.4g,%.4g] count=%d",
            verb.capitalize(), panel, x0, x1, y0, y1, count,
        )
        self._render_flag_overlay()
        self._update_status_bar()
        text = (f"✓ {verb.capitalize()}ged box recorded ({panel}) — "
                f"preview only, stored — not yet committed. "
                f"Flag count: {count}.")
        self._notify(text, colour=colour_ok)
        return {
            "notify_text":  text,
            "notify_color": colour_ok,
            "status_text":  self._status_text(),
        }

    # ====================================================================== #
    # Flag overlay rendering (stub — Phase 1 F-9/F-10)                        #
    # ====================================================================== #

    def _render_flag_overlay(self) -> None:
        pending = self._flag_db.overlay_deltas()
        log.debug("_render_flag_overlay: %d pending delta(s) — stub", len(pending))
        # TODO Phase 1: query backend for flagged rows, shade red, composite.

    # ====================================================================== #
    # SelectionSpec construction                                               #
    # ====================================================================== #

    def _build_selection(self) -> SelectionSpec:
        field_name = _parse_field_string(self._field_str, self._meta)
        spw_ids    = _parse_spw_string(self._spw_str, self._meta)
        corrs      = _parse_correlation_string(self._corr_str, self._meta)
        return SelectionSpec(
            field_names = [field_name] if field_name else None,
            spw         = spw_ids or None,
            correlation = corrs or None,
            data_column = self._datacolumn,
            time_range  = self._time_range,
            freq_range  = self._freq_range,
        )

    def _notify(self, text: str, colour: str = "#f38ba8") -> None:
        """Show a transient notification in the status bar.

        Parameters
        ----------
        text : str
            HTML message to display.  Empty string clears the notification.
        colour : str
            CSS colour for the text.  Default is red (#f38ba8) for errors
            and warnings.  Use ``#a6e3a1`` (green) for success messages.
        """
        if hasattr(self, "_notify_div") and self._notify_div is not None:
            self._notify_div.styles = dict(
                self._notify_div.styles,
                color=colour,
            )
            self._notify_div.text = text

    # ====================================================================== #
    # Status bar                                                               #
    # ====================================================================== #

    def _status_text(self) -> str:
        fname        = os.path.basename(self._source_path)
        mode_label   = {"both": "Both", "raster": "Raster only",
                        "scatter": "Scatter only"}.get(self._mode, self._mode)
        layout_label = {"side": "Side by Side",
                        "over": "Over / Under"}.get(self._layout, self._layout)
        field   = self._field_str or "all"
        spw     = self._spw_str or "all"
        col     = self._datacolumn
        count = self._flag_db.pending_count
        flag_note = (f"  |  <b>Flag count:</b> {count}"
                     if count > 0 else "")
        return (
            f"<b>{fname}</b>  |  Mode: {mode_label}  |  "
            f"Layout: {layout_label}<br>"
            f"Field: {field}  |  SPW: {spw}  |  Col: {col}{flag_note}"
        )

    def _update_status_bar(self) -> None:
        if hasattr(self, "_status_div") and self._status_div is not None:
            self._status_div.text = self._status_text()

    # ====================================================================== #
    # Layout construction                                                      #
    # ====================================================================== #

    def _build_layout(self):
        """Build the full layout.

        Build order is significant — each step creates attributes referenced
        by later steps:

        1. ``_pref_source``       — needed by toolbar layout_js and plot area
        2. ``_build_status_bar``  — creates ``_status_div`` (toolbar plot_js)
        3. ``_build_sidebar``     — creates all ``_*_select`` / ``_corr_cbg``
        4. ``_build_toolbar``     — references all of the above
        5. ``_build_plot_area``   — references ``_pref_source``; returns
                                    (side_container, over_container)
        """
        self._pref_source = ColumnDataSource(data={"prefs": ["{}"]})

        # Inject page-level dark background and tool sync via add_init_script.
        # This runs at app init time in JS before anything else renders.
        self._app_context.add_init_script(
            code="""
document.body.style.background            = '#181825';
document.documentElement.style.background = '#181825';
""",
            description="dark page background",
        )

        # CSS injection for light-mode sidebar widget overrides.
        # When document.body gets class "cv-light", these rules activate.
        _css_div = Div(
            text="""<style>
.cv-light .cv-sidebar { background: #f8f8f0 !important; border-right: 1px solid #ccc !important; }
.cv-light .cv-sidebar .bk-input { background: #ffffff !important; color: #222222 !important; border-color: #aaa !important; }
.cv-light .cv-sidebar select.bk-input option { background: #fff; color: #222; }
.cv-light .cv-sidebar .bk-input-group label,
.cv-light .cv-sidebar .bk-label,
.cv-light .cv-sidebar label { color: #222222 !important; }
.cv-light .cv-sidebar .bk-btn { background: #f0f0f0 !important; color: #222 !important; border-color: #aaa !important; }
</style>""",
            width=0, height=0,
            styles={"display": "none"},
        )

        status_bar               = self._build_status_bar()

        # FlagTool/Unflag instances are built earlier (during __init__, via
        # each panel's own VisibilityPlot._build() -> _add_flag_tools()),
        # before _notify_div/_status_div exist — so wire them here, now
        # that both divs are available. None when enable_flagging=False.
        for panel in (self._raster, self._scatter):
            for tool in (getattr(panel, "_flag_tool", None),
                         getattr(panel, "_unflag_tool", None)):
                if tool is not None:
                    tool.notify_div = self._notify_div
                    tool.status_div = self._status_div

        sidebar_col, toggle_btn  = self._build_sidebar()
        toolbar                  = self._build_toolbar(toggle_btn)
        side_container, over_container = self._build_plot_area()

        # Both containers always in the document; only one visible.
        side_container.visible = (self._layout == "side")
        over_container.visible = (self._layout == "over")

        # stretch_width lets containers fill the browser window as it resizes
        # and correctly reclaims space when the sidebar is collapsed.
        plot_area = column(
            side_container, over_container,
            sizing_mode="stretch_width",
        )
        body = row(
            sidebar_col, plot_area,
            sizing_mode="stretch_width",
        )
        return column(_css_div, toolbar, body, status_bar,
                      sizing_mode="stretch_width")

    def _style_cmap_column(self, cmap_col, dark_stylesheet) -> list:
        """Apply dark InlineStyleSheet to all input widgets in a colormap column.

        ``colormap_controls()`` returns a Bokeh ``column`` whose children
        are ``Select``, ``TextInput``, ``Div``, ``row``, and ``column``
        instances.  This method walks the tree, applies ``dark_stylesheet``
        to every input widget, and returns a flat list of those widgets
        so the dark/light JS callback can update their ``stylesheets[0].css``.
        """
        from bokeh.models import Select, TextInput, Div
        from bokeh.layouts import column as bk_column, row as bk_row

        styled = []

        def _walk(node):
            if isinstance(node, (Select, TextInput)):
                node.stylesheets = [dark_stylesheet]
                styled.append(node)
            elif isinstance(node, Div):
                # Style equation label Div text colour
                if node.text and not node.text.startswith("<span"):
                    node.text = (
                        f"<span style='color:#a6adc8;font-size:11px'>"
                        f"{node.text}</span>"
                    )
            # Walk children of layout containers
            children = getattr(node, "children", None)
            if children:
                for child in children:
                    _walk(child)

        _walk(cmap_col)
        return styled

    # ---------------------------------------------------------------------- #
    # Sidebar                                                                  #
    # ---------------------------------------------------------------------- #

    def _dark(self):
        """Return an InlineStyleSheet applying the dark widget theme."""
        return InlineStyleSheet(css=_DARK_WIDGET_CSS)

    def _build_sidebar(self):
        """Build sidebar column + collapse toggle button.

        Returns
        -------
        sidebar_col : Bokeh column
            The full sidebar, collapsible via ``visible`` toggle.
        toggle_btn : Button
            The ⟨ / ⟩ toggle button — placed in the toolbar row by
            ``_build_toolbar`` so it is always visible.
        """
        meta = self._meta
        dark = self._dark()

        # ---- Source path -------------------------------------------------- #
        path_div = Div(
            text=(
                f"<b style='color:#cdd6f4'>Source:</b> "
                f"<span style='font-family:monospace;font-size:11px;"
                f"color:#a6e3a1'>"
                f"{os.path.basename(self._source_path)}</span>"
            ),
            width=_SIDEBAR_WIDTH,
        )

        # ---- Data column -------------------------------------------------- #
        col_options = list(meta.data_columns) or ["DATA"]
        self._col_select = Select(
            title       = "Data column",
            value       = self._datacolumn if self._datacolumn in col_options
                          else col_options[0],
            options     = col_options,
            width       = _SIDEBAR_WIDTH,
            stylesheets = [dark],
        )

        # ---- Field --------------------------------------------------------- #
        # Include an "All fields" sentinel so the widget can represent the
        # initial state (field_names=None) without auto-selecting a specific field.
        field_options = [("", "All fields")] + [(f.name, f.name) for f in meta.fields]
        current_field = _parse_field_string(self._field_str, meta) or ""
        self._field_select = Select(
            title       = "Field",
            value       = current_field if current_field in [v for v, _ in field_options]
                          else "",
            options     = field_options,
            width       = _SIDEBAR_WIDTH,
            stylesheets = [dark],
        )

        # ---- SPW ----------------------------------------------------------- #
        spw_options = [
            (str(s.spw_id),
             f"SPW {s.spw_id}  ({s.centre_freq_hz/1e9:.3f} GHz)"
             if s.centre_freq_hz else f"SPW {s.spw_id}")
            for s in meta.spws
        ]
        if not spw_options:
            spw_options = [("0", "SPW 0")]
        selected_spws = [str(i) for i in _parse_spw_string(self._spw_str, meta)]
        self._spw_select = MultiSelect(
            title       = "SPW",
            value       = selected_spws,
            options     = spw_options,
            size        = min(len(spw_options), 5),
            width       = _SIDEBAR_WIDTH,
            stylesheets = [dark],
        )

        # ---- Correlations -------------------------------------------------- #
        all_corrs = (list(meta.spws[0].polarizations)
                     if meta.spws else ["XX", "YY"])
        sel_corrs = _parse_correlation_string(self._corr_str, meta)
        self._corr_cbg = CheckboxGroup(
            labels      = all_corrs,
            active      = [i for i, c in enumerate(all_corrs) if c in sel_corrs],
            width       = _SIDEBAR_WIDTH,
            stylesheets = [dark],
        )
        corr_label = Div(
            text  = "<span style='color:#cdd6f4;font-weight:bold'>Correlation</span>",
            width = _SIDEBAR_WIDTH,
        )

        # ---- Stub inputs for unwired selections ---------------------------- #
        # ---- Context-sensitive hint text from MS metadata ------------------- #
        import datetime
        def _mjd_to_iso(mjd_s: float) -> str:
            try:
                dt = datetime.datetime(1858, 11, 17) + datetime.timedelta(seconds=mjd_s)
                return dt.strftime("%Y/%m/%d/%H:%M:%S")
            except Exception:
                return str(mjd_s)

        scan_ids = sorted({s.scan_id for s in meta.scans})
        ant_names = sorted({a.name for a in meta.antennas})
        t0, t1 = meta.time_range

        field_names_str  = ", ".join(f.name for f in meta.fields)
        spw_ids_str      = ", ".join(str(s.spw_id) for s in meta.spws)
        corr_str         = ", ".join(meta.spws[0].polarizations) if meta.spws else "XX, YY"

        scan_hint    = (
            f"<b>Scan</b> — MSSelection integer list  "
            f"| Valid: {', '.join(str(s) for s in scan_ids[:8])}"
            + (f" … {scan_ids[-1]} ({len(scan_ids)} total)" if len(scan_ids) > 8 else "")
            + "  | e.g. <tt>1,3,7</tt>  or  <tt>1~7</tt>"
        )
        ant_hint     = (
            f"<b>Antenna</b> — name or index, MSSelection syntax  "
            f"| Antennas: {', '.join(ant_names[:6])}"
            + (f" … ({len(ant_names)} total)" if len(ant_names) > 6 else "")
            + "  | e.g. <tt>DA41</tt>  or  <tt>DA41&DV01</tt>  or  <tt>!DA42</tt>"
        )
        time_hint    = (
            f"<b>Time range</b> — YYYY/MM/DD/HH:MM:SS~YYYY/MM/DD/HH:MM:SS  "
            f"| Obs: {_mjd_to_iso(t0)} – {_mjd_to_iso(t1)}  "
            f"| e.g. <tt>{_mjd_to_iso(t0)}~{_mjd_to_iso(t1)}</tt>"
        )
        uvrange_hint = (
            "<b>UV range</b> — distance or wavelengths  "
            "| e.g. <tt>0~50klambda</tt>  or  <tt>100~300m</tt>  or  <tt>&gt;200m</tt>"
        )
        field_hint   = (
            f"<b>Field</b> — name or index  "
            f"| Fields: {field_names_str}  "
            "| e.g. <tt>TW Hya</tt>  or  <tt>0,2</tt>  or leave blank for all"
        )
        spw_hint     = (
            f"<b>SPW</b> — spectral window index (multi-select)  "
            f"| SPWs: {spw_ids_str}  "
            "| Select one or more; leave all unselected for all SPWs"
        )
        corr_hint    = (
            f"<b>Correlation</b> — tick to include  "
            f"| Available: {corr_str}  "
            "| Untick to exclude a polarisation from the display"
        )

        # Populate the pre-built hint divs (created in _build_status_bar)
        self._hint_field.text   = field_hint
        self._hint_spw.text     = spw_hint
        self._hint_corr.text    = corr_hint
        self._hint_scan.text    = scan_hint
        self._hint_antenna.text = ant_hint
        self._hint_time.text    = time_hint
        self._hint_uvrange.text = uvrange_hint

        def _focus_blur(widget, hint_div):
            """Wire MouseEnter→show hint (full width), MouseLeave→show
            status+notify row. Hints need the full status-bar width (some
            run long, e.g. the antenna-selection syntax hint), so both
            halves of the status row hide together rather than just
            _status_div — otherwise the red flag-notification half would
            keep showing alongside a hint, cramping it."""
            show = CustomJS(
                args={"hint": hint_div, "status_row": self._status_row},
                code="status_row.visible = false; hint.visible = true;",
            )
            hide = CustomJS(
                args={"hint": hint_div, "status_row": self._status_row},
                code="hint.visible = false; status_row.visible = true;",
            )
            widget.js_on_event(MouseEnter, show)
            widget.js_on_event(MouseLeave, hide)

        def _stub_input(title, placeholder, hint_div):
            inp = EvTextInput(
                title       = title,
                value       = placeholder,
                width       = _SIDEBAR_WIDTH,
                stylesheets = [dark],
            )
            _focus_blur(inp, hint_div)
            return inp

        scan_inp    = _stub_input("Scan",       self._scan_str,      self._hint_scan)
        antenna_inp = _stub_input("Antenna",    self._antenna_str,   self._hint_antenna)
        time_inp    = _stub_input("Time range", self._timerange_str, self._hint_time)
        uv_inp      = _stub_input("UV range",   self._uvrange_str,   self._hint_uvrange)

        # Wire focus/blur on the already-created select/checkbox widgets too
        _focus_blur(self._field_select, self._hint_field)
        _focus_blur(self._spw_select,   self._hint_spw)
        _focus_blur(self._corr_cbg,     self._hint_corr)

        # ---- Raster axis controls ----------------------------------------- #
        self._ry_select = Select(
            title="Raster Y axis", value=self._raster_y.name,
            options=[(k, v) for k, v in _RASTER_Y_OPTIONS],
            width=_SIDEBAR_WIDTH, stylesheets=[dark],
        )
        self._rx_select = Select(
            title="Raster X axis", value=self._raster_x.name,
            options=[(k, v) for k, v in _RASTER_X_OPTIONS],
            width=_SIDEBAR_WIDTH, stylesheets=[dark],
        )
        self._rq_select = Select(
            title="Raster quantity", value=self._raster_qty.name,
            options=[(k, v) for k, v in _RASTER_QTY_OPTIONS],
            width=_SIDEBAR_WIDTH, stylesheets=[dark],
        )
        raster_cmap = self._raster.colormap_controls()
        self._raster_cmap_widgets = self._style_cmap_column(raster_cmap, dark)

        self._raster_axis_section = column(
            Div(text="<span style='color:#89b4fa;font-weight:bold'>"
                     "── Raster ──</span>", width=_SIDEBAR_WIDTH),
            self._ry_select, self._rx_select, self._rq_select,
            raster_cmap,
            visible=(self._mode != "scatter"),
        )

        # ---- Scatter axis controls ---------------------------------------- #
        self._sx_select = Select(
            title="Scatter X axis", value=self._scatter_x.name,
            options=[(k, v) for k, v in _SCATTER_X_OPTIONS],
            width=_SIDEBAR_WIDTH, stylesheets=[dark],
        )
        self._sy_select = Select(
            title="Scatter Y axis", value=self._scatter_y.name,
            options=[(k, v) for k, v in _SCATTER_Y_OPTIONS],
            width=_SIDEBAR_WIDTH, stylesheets=[dark],
        )
        scatter_cmap = self._scatter.colormap_controls(layer_index=0)
        self._scatter_cmap_widgets = self._style_cmap_column(scatter_cmap, dark)

        self._scatter_axis_section = column(
            Div(text="<span style='color:#89b4fa;font-weight:bold'>"
                     "── Scatter ──</span>", width=_SIDEBAR_WIDTH),
            self._sx_select, self._sy_select,
            scatter_cmap,
            visible=(self._mode != "raster"),
        )

        # ---- Assemble sidebar column --------------------------------------- #
        # css_classes enables light-mode switching via a CSS class toggle
        # in the dark/light JS callback.
        self._sidebar_col = column(
            path_div,
            Div(text="<span style='color:#cdd6f4;font-weight:bold'>"
                     "Data</span>", width=_SIDEBAR_WIDTH),
            self._col_select, self._field_select, self._spw_select,
            corr_label, self._corr_cbg,
            scan_inp, antenna_inp, time_inp, uv_inp,
            Div(text="<span style='color:#cdd6f4;font-weight:bold'>"
                     "Axes</span>", width=_SIDEBAR_WIDTH),
            self._raster_axis_section,
            self._scatter_axis_section,
            width       = _SIDEBAR_WIDTH_COL,
            visible     = True,
            css_classes = ["cv-sidebar"],
            styles      = {
                "background":    "#1e1e2e",
                "padding":       "8px",
                "border-right":  "1px solid #45475a",
                "overflow-y":    "auto",
                "max-height":    f"{_PANEL_HEIGHT + 60}px",
            },
        )

        # ---- Collapse toggle button --------------------------------------- #
        toggle_btn = Button(
            label       = "⟨",
            button_type = "default",
            width       = 28,
            styles      = {"font-size": "14px", "padding": "0 4px"},
        )
        # Store on self so _build_plot_area can patch side_container /
        # over_container into the args after they are created.
        self._sidebar_toggle_js = CustomJS(
            args={
                "sidebar": self._sidebar_col,
                "btn":     toggle_btn,
                "side_container": None,   # patched in _build_plot_area
                "over_container": None,   # patched in _build_plot_area
            },
            code="""
const collapsing = sidebar.visible;
sidebar.visible  = !collapsing;
btn.label        = collapsing ? '⟩' : '⟨';
// sizing_mode="stretch_width" on figures and containers handles the
// reflow automatically — no explicit width manipulation needed.
""",
        )
        toggle_btn.js_on_click(self._sidebar_toggle_js)

        return self._sidebar_col, toggle_btn

    # ---------------------------------------------------------------------- #
    # Toolbar                                                                  #
    # ---------------------------------------------------------------------- #

    def _build_toolbar(self, sidebar_toggle_btn):
        """Build the toolbar row.

        Parameters
        ----------
        sidebar_toggle_btn : Button
            The ⟨/⟩ collapse button returned by ``_build_sidebar``.
        """
        ctrl        = self._pipe["control"]
        ids         = self._ids
        raster_fig  = self._raster.figure
        scatter_fig = self._scatter.figure

        side_w   = _PANEL_WIDTH_SIDE
        full_w   = _PANEL_WIDTH_FULL
        panel_h  = _PANEL_HEIGHT
        over_h   = _PANEL_HEIGHT_OVER

        # ---- Plot ▶ and Reload ↺ ----------------------------------------- #
        plot_btn   = Button(label="Plot ▶",   button_type="success", width=80)
        reload_btn = Button(label="Reload ↺", button_type="default", width=80)

        # Shared plot-send logic used by Plot ▶, Reload ↺, and all presets.
        _do_plot_js = """
function doPlot(reload) {
    const corr = corr_cbg.labels.filter((_, i) => corr_cbg.active.includes(i));
    ctrl.send(ids['plot'], {
        field:       field_sel.value,
        spw:         spw_sel.value.join(','),
        correlation: corr.join(','),
        datacolumn:  col_sel.value,
        raster_y:    ry_sel.value,
        raster_x:    rx_sel.value,
        raster_qty:  rq_sel.value,
        scatter_x:   sx_sel.value,
        scatter_y:   sy_sel.value,
        reload:      !!reload,
    }, function(resp) {
        if (!resp) return;
        if (resp.status_text && status_div)
            status_div.text = resp.status_text;
        // No Bokeh server — resp.notify_text must be applied explicitly
        // the same way FlagTool's own comm.send() callback does, or a
        // Python-side self._notify("") clear never reaches the browser.
        if (resp.notify_text != null && notify_div) {
            notify_div.text = resp.notify_text;
            if (resp.notify_color != null) {
                notify_div.styles = {...notify_div.styles, color: resp.notify_color};
            }
        }
        // Same story for _state_source (full_x0/agg_n_x/...) — without
        // this, FlagTool keeps computing its 1:1 zoom target from
        // whatever full_x0/agg_n_x were current as of the last time this
        // ran, silently stale after any axis change.
        if (resp.raster_state  != null) { r_state.data = resp.raster_state; }
        if (resp.scatter_state != null) { s_state.data = resp.scatter_state; }

        // Update raster image + axes.
        try {
            if (resp.raster_image != null) {
                r_img_src.data['image'] = [resp.raster_image];
            }
            if (resp.raster_x0 != null) {
                r_img_src.data['x']  = [resp.raster_x0];
                r_img_src.data['y']  = [resp.raster_y0];
                r_img_src.data['dw'] = [resp.raster_x1 - resp.raster_x0];
                r_img_src.data['dh'] = [resp.raster_y1 - resp.raster_y0];
                r_fig.x_range.start = resp.raster_x0; r_fig.x_range.end = resp.raster_x1;
                r_fig.y_range.start = resp.raster_y0; r_fig.y_range.end = resp.raster_y1;
                r_fig.x_range.reset_start = resp.raster_x0; r_fig.x_range.reset_end = resp.raster_x1;
                r_fig.y_range.reset_start = resp.raster_y0; r_fig.y_range.reset_end = resp.raster_y1;
            }
            // Single emit *after* image and x/y/dw/dh are both settled — a
            // ColumnDataSource.data mutation is a plain dict write and does
            // not itself notify the renderer (no Bokeh server here to sync
            // that automatically), so emitting between the two blocks above
            // redrew the glyph with the new image but the still-stale
            // x/y/dw/dh box from the previous axes, positioning the correct
            // pixels outside the new viewport (all black) even though
            // r_fig's ranges/labels/title (driven by their own property
            // setters, not this CDS) updated correctly. Always emit here —
            // raster_image is always sent, even when axes are unchanged, to
            // keep the hover renderer active.
            if (resp.raster_image != null) {
                r_img_src.change.emit();
            }
            if (resp.raster_x_label != null) r_fig.below[0].axis_label = resp.raster_x_label;
            if (resp.raster_y_label != null) r_fig.left[0].axis_label  = resp.raster_y_label;
            if (resp.raster_title   != null) r_fig.title.text           = resp.raster_title;
        } catch(e) { console.warn('raster update failed:', e); }

        // Update scatter image + axes.
        try {
            if (resp.scatter_image != null) {
                s_img_src.data['image'] = [resp.scatter_image];
                s_img_src.data['x']     = [resp.scatter_x0];
                s_img_src.data['y']     = [resp.scatter_y0];
                s_img_src.data['dw']    = [resp.scatter_x1 - resp.scatter_x0];
                s_img_src.data['dh']    = [resp.scatter_y1 - resp.scatter_y0];
                s_img_src.change.emit();
                s_fig.x_range.start = resp.scatter_x0; s_fig.x_range.end = resp.scatter_x1;
                s_fig.y_range.start = resp.scatter_y0; s_fig.y_range.end = resp.scatter_y1;
                // Update reset bounds so the ResetTool returns to new data extents
                s_fig.x_range.reset_start = resp.scatter_x0; s_fig.x_range.reset_end = resp.scatter_x1;
                s_fig.y_range.reset_start = resp.scatter_y0; s_fig.y_range.reset_end = resp.scatter_y1;
            }
            if (resp.scatter_x_label != null) s_fig.below[0].axis_label = resp.scatter_x_label;
            if (resp.scatter_y_label != null) s_fig.left[0].axis_label  = resp.scatter_y_label;
            if (resp.scatter_title   != null) s_fig.title.text           = resp.scatter_title;
        } catch(e) { console.warn('scatter update failed:', e); }
    });
}
"""
        _plot_js_args = {
            "ctrl":       ctrl,
            "ids":        ids,
            "field_sel":  self._field_select,
            "spw_sel":    self._spw_select,
            "corr_cbg":   self._corr_cbg,
            "col_sel":    self._col_select,
            "ry_sel":     self._ry_select,
            "rx_sel":     self._rx_select,
            "rq_sel":     self._rq_select,
            "sx_sel":     self._sx_select,
            "sy_sel":     self._sy_select,
            "status_div": self._status_div,
            "notify_div": self._notify_div,
            "r_fig":      raster_fig,
            "s_fig":      scatter_fig,
            "r_img_src":  self._raster._image_source,
            "s_img_src":  self._scatter._image_source,
            "r_state":    self._raster._state_source,
            "s_state":    self._scatter._state_source,
        }

        plot_js = CustomJS(
            args=_plot_js_args,
            code=_do_plot_js + "doPlot(false);",
        )
        reload_js = CustomJS(
            args=_plot_js_args,
            code=_do_plot_js + "doPlot(true);",
        )
        plot_btn.js_on_click(plot_js)
        reload_btn.js_on_click(reload_js)

        # Store for use in _preset_js callbacks
        self._do_plot_js   = _do_plot_js
        self._plot_js_args = _plot_js_args

        # ---- Display mode ------------------------------------------------- #
        mode_rbg = RadioButtonGroup(
            labels = ["Both", "Raster only", "Scatter only"],
            active = {"both": 0, "raster": 1, "scatter": 2}.get(self._mode, 0),
            width  = 260,
        )
        self._mode_rbg = mode_rbg

        mode_js = CustomJS(
            args={
                "raster_fig":     raster_fig,
                "scatter_fig":    scatter_fig,
                "raster_layout":  self._raster.layout,
                "scatter_layout": self._scatter.layout,
                "layout_rbg":     None,
                "raster_sec":     self._raster_axis_section,
                "scatter_sec":    self._scatter_axis_section,
                "side_w":         side_w,
                "full_w":         full_w,
                "panel_h":        panel_h,
            },
            code="""
const mode    = cb_obj.active;
const both    = (mode === 0);
const raster  = (mode !== 2);
const scatter = (mode !== 1);

// Toggle the whole layout column (fig + info_div) not just the figure,
// so the info_div is also hidden when a panel is not active.
raster_layout.visible  = raster;
scatter_layout.visible = scatter;

if (both) {
    raster_fig.width   = side_w;
    scatter_fig.width  = side_w;
    raster_fig.height  = panel_h;
    scatter_fig.height = panel_h;
    if (layout_rbg) layout_rbg.disabled = false;
} else {
    const vfig = raster ? raster_fig : scatter_fig;
    vfig.width  = full_w;
    vfig.height = panel_h;
    if (layout_rbg) layout_rbg.disabled = true;
}

raster_sec.visible  = raster;
scatter_sec.visible = scatter;
""",
        )
        mode_rbg.js_on_change("active", mode_js)

        # ---- Layout toggle (dual-container approach) ---------------------- #
        layout_rbg = RadioButtonGroup(
            labels   = ["Side by Side", "Over / Under"],
            active   = 0 if self._layout == "side" else 1,
            disabled = (self._mode != "both"),
            width    = 220,
        )
        self._layout_rbg = layout_rbg
        mode_js.args["layout_rbg"] = layout_rbg   # patch back-reference

        layout_js = CustomJS(
            args={
                "raster_fig":       raster_fig,
                "scatter_fig":      scatter_fig,
                "side_container":   None,    # patched in after _build_plot_area
                "over_container":   None,    # patched in after _build_plot_area
                "side_w":           side_w,
                "full_w":           full_w,
                "panel_h":          panel_h,
                "over_h":           over_h,
                "pref_src":         self._pref_source,
                "ry_sel":           self._ry_select,
                "rx_sel":           self._rx_select,
                "sx_sel":           self._sx_select,
                "sy_sel":           self._sy_select,
            },
            code="""
const over = (cb_obj.active === 1);

// Switch container visibility
side_container.visible = !over;
over_container.visible =  over;

// Resize figures to fit new container
if (over) {
    raster_fig.width   = full_w;
    scatter_fig.width  = full_w;
    raster_fig.height  = over_h;
    scatter_fig.height = over_h;
} else {
    raster_fig.width   = side_w;
    scatter_fig.width  = side_w;
    raster_fig.height  = panel_h;
    scatter_fig.height = panel_h;
}

// Persist preference
const key   = [ry_sel.value, rx_sel.value, 'AMPLITUDE',
               sx_sel.value, sy_sel.value].join(':');
const prefs = JSON.parse(pref_src.data['prefs'][0]);
prefs[key]  = over ? 'over' : 'side';
pref_src.data = {prefs: [JSON.stringify(prefs)]};
""",
        )
        layout_rbg.js_on_change("active", layout_js)

        # Store layout_js so _build_plot_area can patch container refs
        self._layout_js = layout_js

        # ---- Preset buttons ----------------------------------------------- #
        vplot_btn     = Button(label="vplot",     button_type="default", width=70)
        radplot_btn   = Button(label="radplot",   button_type="default", width=70)
        waterfall_btn = Button(label="Waterfall", button_type="default", width=80)

        def _preset_js(preset_name: str) -> CustomJS:
            ry, rx, rq, sx, sy, pl = _PRESETS[preset_name]
            active_layout = 0 if pl == "side" else 1
            args = {
                **self._plot_js_args,
                "mode_rbg":        mode_rbg,
                "layout_rbg":      layout_rbg,
                "active_layout":   active_layout,
                "raster_fig":      raster_fig,
                "scatter_fig":     scatter_fig,
                "raster_layout":   self._raster.layout,
                "scatter_layout":  self._scatter.layout,
                "side_container":  None,
                "over_container":  None,
                "side_w":          side_w,
                "full_w":          full_w,
                "panel_h":         panel_h,
                "over_h":          over_h,
            }
            return CustomJS(
                args=args,
                code=self._do_plot_js + f"""
mode_rbg.active     = 0;
layout_rbg.active   = active_layout;
layout_rbg.disabled = false;
raster_layout.visible  = true;
scatter_layout.visible = true;

ry_sel.value = '{ry.name}';
rx_sel.value = '{rx.name}';
rq_sel.value = '{rq.name}';
sx_sel.value = '{sx.name}';
sy_sel.value = '{sy.name}';

const over = (active_layout === 1);
side_container.visible = !over;
over_container.visible =  over;
if (over) {{
    raster_fig.width   = full_w;  scatter_fig.width  = full_w;
    raster_fig.height  = over_h;  scatter_fig.height = over_h;
}} else {{
    raster_fig.width   = side_w;  scatter_fig.width  = side_w;
    raster_fig.height  = panel_h; scatter_fig.height = panel_h;
}}

doPlot();
""",
            )

        self._preset_js_objects = [
            _preset_js("vplot"),
            _preset_js("radplot"),
            _preset_js("waterfall"),
        ]
        vplot_btn.js_on_click(self._preset_js_objects[0])
        radplot_btn.js_on_click(self._preset_js_objects[1])
        waterfall_btn.js_on_click(self._preset_js_objects[2])

        # ---- Dark / Light mode toggle ------------------------------------- #
        dark_btn = Toggle(
            label       = "☀ Light",
            active      = False,        # False = currently dark
            button_type = "default",
            width       = 78,
        )
        dark_btn.js_on_change("active", CustomJS(
            args={
                "rf":           raster_fig,
                "sf":           scatter_fig,
                "r_info":       self._raster._info_div,
                "s_info":       self._scatter._info_div,
                "sidebar":      self._sidebar_col,
                "status_div":   self._status_div,
                "notify_div":   self._notify_div,
                "widgets":      [self._col_select, self._field_select,
                                 self._spw_select, self._corr_cbg,
                                 self._ry_select, self._rx_select,
                                 self._rq_select, self._sx_select,
                                 self._sy_select]
                                + self._raster_cmap_widgets
                                + self._scatter_cmap_widgets,
            },
            code="""
const light     = cb_obj.active;
const bg_fig    = light ? 'white'   : 'black';
const bg_border = light ? '#ffffff' : '#1e1e2e';
const label_c   = light ? '#222222' : '#cdd6f4';
const grid_c    = light ? '#cccccc' : '#45475a';
const page_bg   = light ? '#ffffff' : '#181825';
const info_bg   = light ? '#ffffff' : '#1e1e2e';
const info_c    = light ? '#222222' : '#cdd6f4';
const status_c  = light ? '#155724' : '#a6e3a1';
const title_c   = light ? '#222222' : '#cdd6f4';
const dark_css = `:host { --bokeh-base-font: system-ui, sans-serif; }
.bk-input { background: #313244 !important; color: #cdd6f4 !important; border-color: #45475a !important; }
select.bk-input option { background: #313244; color: #cdd6f4; }
.bk-input-group label, .bk-label, label { color: #cdd6f4 !important; }
.bk-btn { background: #313244 !important; color: #cdd6f4 !important; border-color: #45475a !important; }
.bk-btn:hover { background: #45475a !important; }`;
const light_css = `:host { }
.bk-input { background: #ffffff !important; color: #222222 !important; border-color: #aaa !important; }
select.bk-input option { background: #fff; color: #222; }
.bk-input-group label, .bk-label, label { color: #222222 !important; }
.bk-btn { background: #f0f0f0 !important; color: #222 !important; border-color: #aaa !important; }`;
const widget_css = light ? light_css : dark_css;

// Page background
for (const el of [document.body, document.documentElement]) {
    try { el.style.background = page_bg; } catch(e) {}
}
for (const sel of ['.bk-root', '[data-root-id]', '.bk']) {
    try {
        document.querySelectorAll(sel).forEach(
            el => el.style.background = page_bg
        );
    } catch(e) {}
}

// Figures
for (const fig of [rf, sf]) {
    fig.background_fill_color = bg_fig;
    fig.border_fill_color     = bg_border;
    if (fig.title) fig.title.text_color = title_c;
    for (const ax of [...fig.below, ...fig.left, ...fig.right, ...fig.above]) {
        if (ax.axis_label_text_color  !== undefined) ax.axis_label_text_color  = label_c;
        if (ax.major_label_text_color !== undefined) ax.major_label_text_color = label_c;
        if (ax.axis_line_color        !== undefined) ax.axis_line_color        = label_c;
        if (ax.major_tick_line_color  !== undefined) ax.major_tick_line_color  = label_c;
        if (ax.minor_tick_line_color  !== undefined) ax.minor_tick_line_color  = label_c;
    }
    for (const g of fig.center) {
        if (g.grid_line_color !== undefined) g.grid_line_color = grid_c;
    }
}

// Info divs and status bar
function recolor_div(div, bg, fg) {
    try {
        const s = Object.assign({}, div.styles);
        s['background'] = bg;
        s['color']      = fg;
        div.styles = s;
    } catch(e) {}
}
recolor_div(r_info,     info_bg, info_c);
recolor_div(s_info,     info_bg, info_c);
recolor_div(status_div, page_bg, status_c);
recolor_div(notify_div, page_bg, light ? '#b02a37' : '#f38ba8');

// Sidebar container background
try {
    const s = Object.assign({}, sidebar.styles);
    s['background']   = light ? '#f8f8f0' : '#1e1e2e';
    s['border-right'] = light ? '1px solid #ccc' : '1px solid #45475a';
    sidebar.styles = s;
} catch(e) {}

// Sidebar widgets — update InlineStyleSheet CSS directly on each widget.
// This is the only reliable way since InlineStyleSheet overrides all other CSS.
try {
    for (const w of widgets) {
        if (w.stylesheets && w.stylesheets.length > 0) {
            w.stylesheets[0].css = widget_css;
        }
    }
} catch(e) { console.warn('widget stylesheet update failed:', e); }

cb_obj.label = light ? '🌙 Dark' : '☀ Light';
""",
        ))

        # ---- Flag ⚑ / Unflag toolbar buttons removed -----------------------
        # Flagging now lives directly on each figure's own toolbar via the
        # FlagTool / FlagTool(flag=False) drag tools added in
        # VisibilityPlot._add_flag_tools() (see visibility_plot.py), rather
        # than as top-level stub buttons here. Pending-flag count is still
        # surfaced in the status bar via _update_status_bar()/_status_text().
        # When enable_flagging=False no such tools exist on either figure
        # and there is nothing here to disable/hide — this row simply has
        # no flagging-related controls in that case.
        def _tt(html: str, position: str = "bottom") -> Tooltip:
            return Tooltip(content=BokehHTML(html), position=position)

        # ---- Separators --------------------------------------------------- #
        def _sep():
            return Div(text="&nbsp;|&nbsp;", width=14,
                       styles={"line-height": "32px", "color": "#45475a"})

        return row(
            Tip(sidebar_toggle_btn,
                tooltip=_tt("Show / hide the plot configuration panel", "right")),
            _sep(),
            Tip(plot_btn,   tooltip=_tt("Replot both panels using the current configuration")),
            Tip(reload_btn, tooltip=_tt("Reload data and replot (clears any pending flags)")),
            _sep(),
            Tip(mode_rbg,   tooltip=_tt("Show both panels, raster only, or scatter only")),
            _sep(),
            Tip(layout_rbg, tooltip=_tt("Arrange panels side by side or one above the other")),
            _sep(),
            Tip(vplot_btn,     tooltip=_tt("Preset: Baseline vs Time (raster) + Amplitude vs Time (scatter)")),
            Tip(radplot_btn,   tooltip=_tt("Preset: Baseline vs Time (raster) + Amplitude vs UV Distance (scatter)")),
            Tip(waterfall_btn, tooltip=_tt("Preset: Amplitude vs Channel waterfall (over/under layout)")),
            _sep(),
            Tip(dark_btn, tooltip=_tt("Toggle between dark and light background")),
        )

    # ---------------------------------------------------------------------- #
    # Plot area                                                                #
    # ---------------------------------------------------------------------- #

    def _build_plot_area(self):
        """Build both layout containers with linked cursor spans."""
        if self._raster_x == self._scatter_x:
            self._scatter.figure.x_range = self._raster.figure.x_range

        raster_layout  = self._raster.layout
        scatter_layout = self._scatter.layout

        raster_layout.sizing_mode  = "stretch_width"
        scatter_layout.sizing_mode = "stretch_width"

        # ---- Linked cursor Spans ----------------------------------------- #
        from bokeh.models import Span

        def _make_span(dim, color="#f38ba8"):
            return Span(location=float("nan"), dimension=dim,
                        line_color=color, line_width=1,
                        line_alpha=0.85, line_dash="dashed")

        # Both figures always get both a vertical and horizontal span.
        # The JS callback decides which to show based on current axis labels,
        # so span visibility correctly updates after preset/axis changes.
        r_vspan = _make_span("height")   # raster vertical (x-axis link)
        r_hspan = _make_span("width")    # raster horizontal (y-axis link)
        s_vspan = _make_span("height")   # scatter vertical (x-axis link)
        s_hspan = _make_span("width")    # scatter horizontal (y-axis link)

        self._raster.figure.add_layout(r_vspan)
        self._raster.figure.add_layout(r_hspan)
        self._scatter.figure.add_layout(s_vspan)
        self._scatter.figure.add_layout(s_hspan)

        cursor_src = self._cursor_source
        raster_fig  = self._raster.figure
        scatter_fig = self._scatter.figure

        self._cursor_source.js_on_change("data", CustomJS(
            args={
                "r_vspan":    r_vspan,
                "r_hspan":    r_hspan,
                "s_vspan":    s_vspan,
                "s_hspan":    s_hspan,
                "cursor_src": cursor_src,
                "r_fig":      raster_fig,
                "s_fig":      scatter_fig,
                "r_id":       self._raster.vr_id,
                "s_id":       self._scatter.vr_id,
            },
            code="""
const x      = cursor_src.data['x'][0];
const y      = cursor_src.data['y'][0];
const fig_id = cursor_src.data['fig'] ? cursor_src.data['fig'][0] : '';

const from_raster  = (fig_id === r_id);
const from_scatter = (fig_id === s_id);

const r_x_label = r_fig.below.length ? r_fig.below[0].axis_label : '';
const r_y_label = r_fig.left.length  ? r_fig.left[0].axis_label  : '';
const s_x_label = s_fig.below.length ? s_fig.below[0].axis_label : '';
const s_y_label = s_fig.left.length  ? s_fig.left[0].axis_label  : '';

// Reset all spans
r_vspan.location = NaN;
r_hspan.location = NaN;
s_vspan.location = NaN;
s_hspan.location = NaN;

if (from_raster && x != null && !isNaN(x)) {
    // Cursor is in the raster: x=raster_x_coord, y=raster_y_coord
    r_vspan.location = x;   // always show vertical span on raster at x

    // Show horizontal span on raster at y (shows which row cursor is on)
    if (y != null && !isNaN(y)) r_hspan.location = y;

    // Scatter vertical span: if raster_x matches scatter_x
    if (r_x_label && r_x_label === s_x_label) s_vspan.location = x;
    // Scatter vertical span: if raster_y matches scatter_x (x coord = y of raster)
    else if (r_y_label && r_y_label === s_x_label && y != null && !isNaN(y))
        s_vspan.location = y;

    // Scatter horizontal span: if raster_x matches scatter_y
    if (r_x_label && r_x_label === s_y_label) s_hspan.location = x;
    // Scatter horizontal span: if raster_y matches scatter_y
    else if (r_y_label && r_y_label === s_y_label && y != null && !isNaN(y))
        s_hspan.location = y;

} else if (from_scatter && x != null && !isNaN(x)) {
    // Cursor is in the scatter: x=scatter_x_coord, y=scatter_y_coord
    s_vspan.location = x;   // always show vertical span on scatter at x

    // Show horizontal span on scatter at y
    if (y != null && !isNaN(y)) s_hspan.location = y;

    // Raster vertical span: if scatter_x matches raster_x
    if (s_x_label && s_x_label === r_x_label) r_vspan.location = x;
    // Raster horizontal span: if scatter_x matches raster_y
    else if (s_x_label && s_x_label === r_y_label) r_hspan.location = x;

    // Raster vertical span: if scatter_y matches raster_x
    if (s_y_label && s_y_label === r_x_label && y != null && !isNaN(y))
        r_vspan.location = y;
    // Raster horizontal span: if scatter_y matches raster_y
    else if (s_y_label && s_y_label === r_y_label && y != null && !isNaN(y))
        r_hspan.location = y;
}
""",
        ))

        side_container = row(
            raster_layout, scatter_layout,
            sizing_mode = "stretch_width",
            visible     = (self._layout == "side"),
        )
        over_container = column(
            raster_layout, scatter_layout,
            sizing_mode = "stretch_width",
            visible     = (self._layout == "over"),
        )

        self._layout_js.args["side_container"] = side_container
        self._layout_js.args["over_container"] = over_container
        for pjs in self._preset_js_objects:
            pjs.args["side_container"] = side_container
            pjs.args["over_container"] = over_container
        self._sidebar_toggle_js.args["side_container"] = side_container
        self._sidebar_toggle_js.args["over_container"] = over_container

        return side_container, over_container

    # ---------------------------------------------------------------------- #
    # Status bar                                                               #
    # ---------------------------------------------------------------------- #

    def _build_status_bar(self):
        _hint_style = {
            "font-size":   "12px",
            "font-family": "monospace",
            "padding":     "4px 10px",
            "background":  "#181825",
            "color":       "#89dceb",     # cyan — distinct from status green
            "border-top":  "1px solid #45475a",
        }
        _status_style = {
            "font-size":   "12px",
            "font-family": "monospace",
            "padding":     "4px 10px",
            "background":  "#181825",
            "color":       "#a6e3a1",
            "border-top":  "1px solid #45475a",
        }

        self._status_div = Div(
            text        = self._status_text(),
            sizing_mode = "stretch_width",
            visible     = True,
            styles      = _status_style,
        )
        self._notify_div = Div(
            text        = "",
            sizing_mode = "stretch_width",
            styles      = {
                "font-size":    "12px",
                "font-family":  "monospace",
                "padding":      "4px 10px",
                "background":   "#181825",
                "color":        "#f38ba8",
                "min-height":   "18px",
                "text-align":   "right",
                "border-top":   "1px solid #45475a",
                "border-left":  "1px solid #45475a",
            },
        )
        # Shared status-bar line: dataset/config summary (green) on the
        # left half, flagging feedback (red, via _notify()) on the right
        # half. Both halves hide together during a sidebar-field hint
        # (see _focus_blur in _build_sidebar), which needs the full row
        # width for itself.
        self._status_row = row(
            self._status_div, self._notify_div,
            sizing_mode="stretch_width",
        )

        # Pre-built hint divs — one per sidebar widget that benefits from help.
        # All start hidden; focus/blur JS toggles visibility.
        def _hint(html):
            return Div(text=html, sizing_mode="stretch_width",
                       visible=False, styles=_hint_style)

        self._hint_field    = _hint("")   # filled in _build_sidebar with MS data
        self._hint_spw      = _hint("")
        self._hint_corr     = _hint("")
        self._hint_scan     = _hint("")
        self._hint_antenna  = _hint("")
        self._hint_time     = _hint("")
        self._hint_uvrange  = _hint("")

        return column(
            self._status_row,
            self._hint_field,
            self._hint_spw,
            self._hint_corr,
            self._hint_scan,
            self._hint_antenna,
            self._hint_time,
            self._hint_uvrange,
            sizing_mode="stretch_width",
        )
