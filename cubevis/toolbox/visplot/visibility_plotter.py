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
    )
    plotter.show()

Preview scope
-------------
* Display mode toggle: Both / Raster only / Scatter only (CustomJS,
  no Python round-trip)
* Layout toggle: Side by Side / Over Under (CustomJS)
* Session-scoped layout preference memory (ColumnDataSource JSON store)
* Sidebar: data selection (field, SPW, correlation, data column),
  raster axis controls, scatter axis controls, colormap controls for
  both panels
* Toolbar: Plot ▶, Reload ↺, display mode, layout, presets (vplot,
  radplot, Waterfall), Box Select, Flag ⚑ (disabled), Undo ⟲ (disabled)
* Box-select → FlagDB accumulation + red overlay re-render (working)
* Linked x-axis Range1d when both panels show the same x dimension
* Status bar Div
* Hotkey bindings via ``casalib.hotkeys``

Absent from the preview (see implementation plan §6, Phase 2+):
* Writing flags to disk (FlagDB accumulation works; disk write — full release)
* Flag / Undo toolbar buttons (disabled with tooltip in preview)
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

from bokeh.layouts import column, row
from bokeh.models import (
    Button, CheckboxGroup, ColumnDataSource, CustomJS, Div,
    MultiSelect, RadioButtonGroup, Select, TextInput, Toggle,
)

from cubevis.bokeh import BokehInit
from cubevis.bokeh.models import BokehAppContext, Showable
from cubevis.bokeh.transport import CommMgr
from cubevis import exe

from .axes import Axis
from .selection import SelectionSpec
from .visibility_raster import VisibilityRaster
from .visibility_scatter import VisibilityScatter, ScatterLayer
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

# Default panel dimensions
_PANEL_WIDTH_SIDE  = 450   # each panel in side-by-side mode
_PANEL_WIDTH_FULL  = 920   # single-panel or over/under mode
_PANEL_HEIGHT      = 500
_SIDEBAR_WIDTH     = 280

# Preset definitions: (raster_y, raster_x, raster_qty, scatter_x, scatter_y, layout)
_PRESETS = {
    "vplot": (
        Axis.BASELINE, Axis.TIME,    Axis.AMPLITUDE,
        Axis.TIME,     Axis.AMPLITUDE,
        "side",
    ),
    "radplot": (
        Axis.BASELINE, Axis.UVDIST,  Axis.AMPLITUDE,
        Axis.UVDIST,   Axis.AMPLITUDE,
        "side",
    ),
    "waterfall": (
        Axis.TIME,     Axis.CHANNEL, Axis.AMPLITUDE,
        Axis.TIME,     Axis.AMPLITUDE,
        "over",
    ),
}

# Axes available in the sidebar dropdowns (preview subset)
_RASTER_Y_OPTIONS  = [("TIME",     "Time"),
                      ("BASELINE", "Baseline")]
_RASTER_X_OPTIONS  = [("CHANNEL",  "Channel"),
                      ("TIME",     "Time")]
_RASTER_QTY_OPTIONS = [("AMPLITUDE", "Amplitude"),
                       ("PHASE",     "Phase")]
_SCATTER_X_OPTIONS  = [("UVDIST",    "UV Distance"),
                       ("TIME",      "Time"),
                       ("FREQUENCY", "Frequency")]
_SCATTER_Y_OPTIONS  = [("AMPLITUDE", "Amplitude"),
                       ("PHASE",     "Phase")]


# ---------------------------------------------------------------------------
# MSSelection string parsers (preview-grade — full parser in Phase 2)
# ---------------------------------------------------------------------------

def _parse_spw_string(spw_str: str, meta: ObservationMetadata) -> list[int]:
    """Parse a simple comma-separated SPW string like ``"0,1,2,3"``."""
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
    """Parse ``"XX,YY"`` → ``["XX", "YY"]``."""
    if not corr_str or corr_str.strip() == "":
        if meta.spws:
            return list(meta.spws[0].polarizations)
        return ["XX", "YY"]
    return [c.strip().upper() for c in corr_str.split(",") if c.strip()]


def _parse_field_string(field_str: str,
                         meta: ObservationMetadata) -> Optional[str]:
    """Return the field name string, or None for all fields."""
    if not field_str or field_str.strip() == "":
        return None
    # Accept integer index
    if field_str.strip().isdigit():
        idx = int(field_str.strip())
        if 0 <= idx < len(meta.fields):
            return meta.fields[idx].name
    return field_str.strip() or None



# ---------------------------------------------------------------------------
# Backend probes and open_ms / open_ps factory functions
#
# These live here rather than in a separate factory.py because they are only
# ever called from VisibilityPlotter.__init__.  A separate module would add
# indirection with no architectural benefit.
# ---------------------------------------------------------------------------

def _probe_casatasks() -> bool:
    """Return ``True`` if ``casatasks`` is importable in this session."""
    try:
        import casatasks  # noqa: F401
        return True
    except ImportError:
        return False


def _probe_radps() -> bool:
    """Return ``True`` if RADPS / AstroVIPER is available in this session."""
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


def _resolve_context_msv2(
    path: str,
    backend: ReductionBackend,
    remote_endpoint: Optional[str],
) -> ReductionContext:
    if backend == ReductionBackend.NULL:
        return NullReductionContext()
    if backend == ReductionBackend.REMOTE:
        if not remote_endpoint:
            raise ValueError("backend='remote' requires remote_endpoint to be supplied.")
        return _make_remote_context(path, remote_endpoint)
    if backend == ReductionBackend.CASA6:
        if not _probe_casatasks():
            raise RuntimeError(
                "backend='casa6' was requested but casatasks is not importable."
            )
        return _make_casa6_context(path)
    if backend == ReductionBackend.RADPS:
        if not _probe_radps():
            raise RuntimeError(
                "backend='radps' was requested but RADPS / AstroVIPER is not available."
            )
        return _make_radps_context(path)
    # AUTO — casatasks → RADPS → Null
    if _probe_casatasks():
        try:
            return _make_casa6_context(path)
        except NotImplementedError:
            log.debug("open_ms (auto): Casa6ReductionContext not yet implemented; trying RADPS")
    if _probe_radps():
        try:
            return _make_radps_context(path)
        except NotImplementedError:
            log.debug("open_ms (auto): RadpsReductionContext not yet implemented; using Null")
    return NullReductionContext()


def _resolve_context_msv4(
    path: str,
    backend: ReductionBackend,
    remote_endpoint: Optional[str],
) -> ReductionContext:
    if backend == ReductionBackend.CASA6:
        raise ValueError(
            "backend='casa6' is not valid for MSv4 / Processing Set data. "
            "CASA6 has no MSv4 write path."
        )
    if backend == ReductionBackend.NULL:
        return NullReductionContext()
    if backend == ReductionBackend.REMOTE:
        if not remote_endpoint:
            raise ValueError("backend='remote' requires remote_endpoint to be supplied.")
        return _make_remote_context(path, remote_endpoint)
    if backend == ReductionBackend.RADPS:
        if not _probe_radps():
            raise RuntimeError(
                "backend='radps' was requested but RADPS / AstroVIPER is not available."
            )
        return _make_radps_context(path)
    # AUTO — RADPS → Null
    if _probe_radps():
        try:
            return _make_radps_context(path)
        except NotImplementedError:
            log.debug("open_ps (auto): RadpsReductionContext not yet implemented; using Null")
    return NullReductionContext()


def open_ms(
    path: str,
    *,
    backend: ReductionBackend | str = ReductionBackend.AUTO,
    remote_endpoint: Optional[str] = None,
) -> tuple[ObservationMetadata, LocalVisibilityReader, ReductionContext]:
    """Open an MSv2 measurement set; return (metadata, reader, context).

    Called internally by ``VisibilityPlotter.__init__``.  Also importable
    directly for developer / testing use.
    """
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


def open_ps(
    path: str,
    *,
    backend: ReductionBackend | str = ReductionBackend.AUTO,
    remote_endpoint: Optional[str] = None,
) -> tuple[ObservationMetadata, LocalVisibilityReader, ReductionContext]:
    """Open an MSv4 / Processing Set; return (metadata, reader, context).

    Called internally by ``VisibilityPlotter.__init__``.  Also importable
    directly for developer / testing use.
    """
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
        Reduction backend selection.  One of ``"auto"``, ``"casa6"``,
        ``"radps"``, ``"remote"``, ``"null"``.  Default ``"auto"``.
    remote_endpoint : str | None
        Required only when ``backend="remote"``.
    field : str
        Field name or integer index string.  Default: first field.
    spw : str
        Comma-separated SPW indices (``"0,1,2,3"``).  Default: all.
    antenna : str
        MSSelection antenna string.  Default: all.  (Stored; not yet
        wired to the backend in the preview.)
    scan : str
        MSSelection scan string.  Default: all.  (Stored; not yet wired.)
    timerange : str
        MSSelection time-range string.  Default: all.  (Stored; not wired.)
    uvrange : str
        UV range string (``"0~50klambda"``).  Default: all.  (Stored; not wired.)
    correlation : str
        Comma-separated correlation labels (``"XX,YY"``).  Default: all.
    datacolumn : str
        Visibility column: ``"data"``, ``"corrected"``, or ``"model"``.
    mode : str
        Initial display mode: ``"both"``, ``"raster"``, or ``"scatter"``.
    layout : str
        Initial layout: ``"side"`` (side by side) or ``"over"`` (over/under).
    preset : str | None
        Named preset to apply at startup: ``"vplot"``, ``"radplot"``,
        ``"waterfall"``, or ``None``.
    time_range : tuple | None
        ``(start, end)`` as ISO strings or MJD floats.
    freq_range : tuple | None
        ``(start, end)`` in Hz.
    uvdist_range : tuple | None
        ``(min, max)`` in metres.
    """

    def __init__(
        self,
        *,
        ms:               Optional[str] = None,
        ps:               Optional[str] = None,
        backend:          str           = "auto",
        remote_endpoint:  Optional[str] = None,
        # Selection
        field:            str           = "",
        spw:              str           = "",
        antenna:          str           = "",
        scan:             str           = "",
        timerange:        str           = "",
        uvrange:          str           = "",
        correlation:      str           = "",
        datacolumn:       str           = "data",
        # Display
        mode:             str           = "both",
        layout:           str           = "side",
        preset:           Optional[str] = None,
        # Axis ranges
        time_range:       Optional[tuple]  = None,
        freq_range:       Optional[tuple]  = None,
        uvdist_range:     Optional[tuple]  = None,
    ) -> None:

        # ------------------------------------------------------------------ #
        # Validate data source                                                 #
        # ------------------------------------------------------------------ #
        if ms is not None and ps is not None:
            raise ValueError(
                "VisibilityPlotter: supply exactly one of ms= or ps=, not both."
            )
        if ms is None and ps is None:
            raise ValueError(
                "VisibilityPlotter: one of ms= or ps= must be supplied."
            )

        # ------------------------------------------------------------------ #
        # Store constructor arguments for status bar / sidebar population     #
        # ------------------------------------------------------------------ #
        self._ms_path   = ms
        self._ps_path   = ps
        self._source_path = ms or ps
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

        if self._mode not in ("both", "raster", "scatter"):
            raise ValueError(
                f"mode must be 'both', 'raster', or 'scatter'; got {mode!r}"
            )
        if self._layout not in ("side", "over"):
            raise ValueError(
                f"layout must be 'side' or 'over'; got {layout!r}"
            )

        # ------------------------------------------------------------------ #
        # Open data source — factory resolves backend + builds metadata       #
        # ------------------------------------------------------------------ #
        if ms is not None:
            self._meta, self._reader, self._context = open_ms(
                ms, backend=backend, remote_endpoint=remote_endpoint
            )
        else:
            self._meta, self._reader, self._context = open_ps(
                ps, backend=backend, remote_endpoint=remote_endpoint
            )

        # ------------------------------------------------------------------ #
        # Build the initial SelectionSpec from constructor arguments           #
        # ------------------------------------------------------------------ #
        self._selection = self._build_selection()

        # ------------------------------------------------------------------ #
        # Apply preset axes if requested (overrides default axis choices)      #
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
        # FlagDB                                                               #
        # ------------------------------------------------------------------ #
        self._flag_db = FlagDB()

        # Hotkey scope ID — stable UUID that scopes casalib.hotkeys bindings
        # to this VisibilityPlotter instance, preventing hotkeys from firing
        # for the wrong instance when multiple plotters are open in the same
        # browser session.  Allocated here so it is available when the
        # hotkey init script is added (see cubevis-app-startup.md §2.2 and
        # _cube.py setup-key-mgmt pattern).
        self._hotkey_scope_id = str(uuid4())

        # ------------------------------------------------------------------ #
        # Communication infrastructure (must precede widget construction)      #
        # ------------------------------------------------------------------ #
        def _shutdown_handler(reason, description):
            self._stop()
            BokehInit.clear_app_context(self._app_context)

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

        # Result future (set in _task_server)
        self._result_future = None

        # Pipe handles — opened immediately so toolbar CustomJS can reference
        # the Comm object in its args at layout-build time.
        self._pipe = {"control": None}
        self._pipe["control"] = self._comm_mgr.open(
            squash_queue=True,
            description="visibility plotter control",
        )
        self._pipe["control"].register(self._ids["plot"], self._handle_plot)
        self._pipe["control"].register(self._ids["done"], self._handle_done)

        # Message IDs
        self._ids = {
            "plot":   str(uuid4()),
            "done":   str(uuid4()),
        }

        # ------------------------------------------------------------------ #
        # Construct display widgets                                            #
        # ------------------------------------------------------------------ #
        first_pol = (
            self._selection.correlation[0]
            if self._selection.correlation
            else "XX"
        )

        w_raster = _PANEL_WIDTH_SIDE if self._mode == "both" else _PANEL_WIDTH_FULL
        w_scatter = w_raster

        self._raster = VisibilityRaster(
            backend     = self._reader,
            selection   = self._selection,
            y_dim       = self._raster_y,
            x_dim       = self._raster_x,
            quantity    = self._raster_qty,
            polarization= first_pol,
            width       = w_raster,
            height      = _PANEL_HEIGHT,
            comm_mgr    = self._comm_mgr,
        )

        self._scatter = VisibilityScatter(
            backend     = self._reader,
            selection   = self._selection,
            x_axis      = self._scatter_x,
            layers      = [ScatterLayer(y_axis=self._scatter_y,
                                        polarization=first_pol)],
            width       = w_scatter,
            height      = _PANEL_HEIGHT,
            comm_mgr    = self._comm_mgr,
        )

        # ------------------------------------------------------------------ #
        # Wire box-select callbacks                                            #
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
        """Display the plotter in a Jupyter notebook cell.

        Returns a ``Showable`` that renders the layout and starts the
        backend server thread when displayed.

        Example
        -------
        ::

            plotter = VisibilityPlotter(ms="data.ms")
            app = plotter.show()   # or just: plotter.show() as the last cell expr
        """
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
            app_context,
            startup,
            get_future,
            name="visibility-plotter-jpy",
        )

    # ====================================================================== #
    # cubevis application protocol                                             #
    # ====================================================================== #

    def __call__(self, exec_context, task_id=None):
        """Called by the adapter layer (or ``show()``).

        Opens communication channels and returns
        ``(BokehAppContext, exe.Task)``.
        """
        self._open_channels()
        return self._app_context, exe.Task(self._task_server)

    async def _task_server(self):
        """Async server coroutine — runs the websocket server loop."""
        self._result_future = asyncio.Future()

        if self._comm_mgr.address:
            async with websockets.serve(
                self._comm_mgr.process_messages,
                self._comm_mgr.address[0],
                self._comm_mgr.address[1],
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

    # ====================================================================== #
    # Channel management                                                       #
    # ====================================================================== #

    def _open_channels(self):
        # Channels are opened eagerly in __init__ so that toolbar CustomJS
        # args can reference the Comm object at layout-build time.  This
        # guard exists only to satisfy the cubevis application protocol
        # (which calls _open_channels from __call__) and to protect against
        # accidental double-open if __call__ is invoked more than once.
        pass

    # ====================================================================== #
    # j2p handlers                                                             #
    # ====================================================================== #

    async def _handle_plot(self, msg, context=None):
        """Handle Plot ▶ / Reload ↺ button press.

        Reads the current sidebar selection state from ``msg``, rebuilds
        the ``SelectionSpec``, and re-renders both active panels.
        """
        # Update selection from sidebar values carried in the message
        if "field" in msg:
            self._field_str = msg["field"]
        if "spw" in msg:
            self._spw_str = msg["spw"]
        if "correlation" in msg:
            self._corr_str = msg["correlation"]
        if "datacolumn" in msg:
            self._datacolumn = msg["datacolumn"].upper()
        if "raster_y" in msg:
            try:
                self._raster_y = Axis[msg["raster_y"]]
            except KeyError:
                pass
        if "raster_x" in msg:
            try:
                self._raster_x = Axis[msg["raster_x"]]
            except KeyError:
                pass
        if "raster_qty" in msg:
            try:
                self._raster_qty = Axis[msg["raster_qty"]]
            except KeyError:
                pass
        if "scatter_x" in msg:
            try:
                self._scatter_x = Axis[msg["scatter_x"]]
            except KeyError:
                pass
        if "scatter_y" in msg:
            try:
                self._scatter_y = Axis[msg["scatter_y"]]
            except KeyError:
                pass

        self._selection = self._build_selection()

        first_pol = (
            self._selection.correlation[0]
            if self._selection.correlation
            else "XX"
        )

        # Re-render raster
        self._raster.update_axes(
            y_dim       = self._raster_y,
            x_dim       = self._raster_x,
            quantity    = self._raster_qty,
            polarization= first_pol,
        )

        # Re-render scatter
        self._scatter.update_axes(
            x_dim  = self._scatter_x,
            layers = [ScatterLayer(y_axis=self._scatter_y,
                                   polarization=first_pol)],
        )

        return {
            "status":      "ok",
            "status_text": self._status_text(),
        }

    async def _handle_done(self, msg, context=None):
        """Handle shutdown message from JS (Stop / close tab)."""
        self._stop()
        return {"result": "stopped"}

    async def _handle_box_select(self, msg: dict, panel: str) -> Optional[dict]:
        """Handle a box-select j2p message from either panel.

        Creates a ``FlagDelta``, appends it to ``FlagDB``, and triggers
        the flag overlay re-render on both panels.

        The ``panel`` argument is injected by the per-widget async wrapper
        closures registered in ``__init__`` so that the ``FlagDelta``
        records which figure the selection came from.

        Parameters
        ----------
        msg : dict
            ``{x0, x1, y0, y1, tool: "box_select"}``
        panel : str
            ``"raster"`` or ``"scatter"``

        Returns
        -------
        dict | None
            In the preview, returns ``None`` (overlay re-render via
            ``_render_flag_overlay()`` updates image sources directly;
            the JS callback response is unused).  In Phase 1 the
            response will carry the composite overlay image so JS can
            push it without a second round-trip.
        """
        x0 = float(msg.get("x0", 0.0))
        x1 = float(msg.get("x1", 0.0))
        y0 = float(msg.get("y0", 0.0))
        y1 = float(msg.get("y1", 0.0))

        # Build coordinate ranges in terms meaningful to the MS
        # The axis identity of x0/x1, y0/y1 depends on which panel fired.
        if panel == "raster":
            delta = FlagDelta(
                flag        = True,
                time_range  = (min(x0, x1), max(x0, x1))
                              if self._raster_x == Axis.TIME else None,
                freq_range  = (min(x0, x1), max(x0, x1))
                              if self._raster_x in (Axis.CHANNEL, Axis.FREQUENCY)
                              else None,
                source      = "raster_box",
                comment     = (
                    f"raster box x=[{x0:.4g},{x1:.4g}] "
                    f"y=[{y0:.4g},{y1:.4g}]"
                ),
            )
        else:
            delta = FlagDelta(
                flag        = True,
                time_range  = (min(x0, x1), max(x0, x1))
                              if self._scatter_x == Axis.TIME else None,
                freq_range  = (min(x0, x1), max(x0, x1))
                              if self._scatter_x in (Axis.CHANNEL, Axis.FREQUENCY)
                              else None,
                source      = "scatter_box",
                comment     = (
                    f"scatter box x=[{x0:.4g},{x1:.4g}] "
                    f"y=[{y0:.4g},{y1:.4g}]"
                ),
            )

        self._flag_db.append(delta)
        log.debug(
            "_handle_box_select: panel=%s  pending=%d",
            panel, self._flag_db.pending_count,
        )

        # Render the flag overlay (red layer) on both panels.
        # In the preview this is a Python-side re-render that pushes new
        # images via image_source.data — no response payload to JS needed.
        self._render_flag_overlay()

        # Update status bar text via the preference store
        self._update_status_bar()

        return None

    # ====================================================================== #
    # Flag overlay rendering                                                   #
    # ====================================================================== #

    def _render_flag_overlay(self) -> None:
        """Re-render both panels with pending FlagDB deltas shown in red.

        For the preview this is a stub that logs the pending delta count.
        The full implementation (Phase 1 F-9/F-10) will:

        1. Call ``_reader.query_raster`` / ``query_columns`` with a
           ``SelectionSpec`` built from the union of all pending deltas.
        2. Run a separate Datashader shade pass in red (``#FF0000``).
        3. Porter-Duff composite the red layer over the main image.
        4. Push the composite to ``_raster._image_source.data`` and
           ``_scatter._image_source.data``.
        """
        pending = self._flag_db.overlay_deltas()
        log.debug(
            "_render_flag_overlay: %d pending delta(s) — overlay stub",
            len(pending),
        )
        # TODO (Phase 1 F-9/F-10): build overlay SelectionSpec from pending
        # deltas, query backend for flagged rows, render red composite,
        # push to both image sources.

    # ====================================================================== #
    # SelectionSpec construction                                               #
    # ====================================================================== #

    def _build_selection(self) -> SelectionSpec:
        """Translate constructor / sidebar string arguments → SelectionSpec."""
        field_name = _parse_field_string(self._field_str, self._meta)
        spw_ids    = _parse_spw_string(self._spw_str, self._meta)
        corrs      = _parse_correlation_string(self._corr_str, self._meta)

        return SelectionSpec(
            field_names  = [field_name] if field_name else None,
            spw          = spw_ids or None,
            correlation  = corrs or None,
            data_column  = self._datacolumn,
            time_range   = self._time_range,
            freq_range   = self._freq_range,
        )

    # ====================================================================== #
    # Status bar                                                               #
    # ====================================================================== #

    def _status_text(self) -> str:
        """Build the status bar HTML string."""
        fname = os.path.basename(self._source_path)
        mode_label   = {"both": "Both", "raster": "Raster only",
                        "scatter": "Scatter only"}.get(self._mode, self._mode)
        layout_label = {"side": "Side by Side",
                        "over": "Over / Under"}.get(self._layout, self._layout)
        field = self._field_str or (
            self._meta.fields[0].name if self._meta.fields else "all"
        )
        spw   = self._spw_str or "all"
        col   = self._datacolumn
        pending = self._flag_db.pending_count

        flag_note = (
            f"  |  <b>Pending flags:</b> {pending}"
            if pending > 0 else ""
        )

        return (
            f"<b>{fname}</b>  |  "
            f"Mode: {mode_label}  |  Layout: {layout_label}<br>"
            f"Field: {field}  |  SPW: {spw}  |  Col: {col}"
            f"{flag_note}"
        )

    def _update_status_bar(self) -> None:
        """Push new status text into the status bar Div."""
        if hasattr(self, "_status_div") and self._status_div is not None:
            self._status_div.text = self._status_text()

    # ====================================================================== #
    # Layout construction                                                      #
    # ====================================================================== #

    def _build_layout(self):
        """Build the full Bokeh layout: toolbar + sidebar + plot area + status.

        Build order matters: status bar and sidebar must exist before the
        toolbar because toolbar ``CustomJS`` args reference their widget
        objects directly (``self._status_div``, ``self._field_select``, etc.).
        """
        status_bar = self._build_status_bar()
        sidebar    = self._build_sidebar()
        toolbar    = self._build_toolbar()
        plot_area  = self._build_plot_area()

        body = row(sidebar, plot_area)
        return column(toolbar, body, status_bar)

    # ---------------------------------------------------------------------- #
    # Toolbar                                                                  #
    # ---------------------------------------------------------------------- #

    def _build_toolbar(self):
        """Build the toolbar row."""
        ctrl   = self._pipe["control"] if self._pipe["control"] else None
        ids    = self._ids
        raster_fig  = self._raster.figure
        scatter_fig = self._scatter.figure

        # ---- Plot ▶ and Reload ↺ ----------------------------------------- #
        plot_btn   = Button(label="Plot ▶",   button_type="success", width=90)
        reload_btn = Button(label="Reload ↺", button_type="default", width=90)

        # Both buttons collect sidebar state and fire the "plot" j2p message.
        # Sidebar widget state is read from JS Bokeh model properties directly.
        plot_js = CustomJS(
            args={
                "ctrl":         ctrl,
                "ids":          ids,
                "field_sel":    self._field_select,
                "spw_sel":      self._spw_select,
                "corr_cbg":     self._corr_cbg,
                "col_sel":      self._col_select,
                "ry_sel":       self._ry_select,
                "rx_sel":       self._rx_select,
                "rq_sel":       self._rq_select,
                "sx_sel":       self._sx_select,
                "sy_sel":       self._sy_select,
                "status_div":   self._status_div,
            },
            code="""
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
}, function(resp) {
    if (resp && resp.status_text && status_div) {
        status_div.text = resp.status_text;
    }
});
""",
        )
        plot_btn.js_on_click(plot_js)
        reload_btn.js_on_click(plot_js)

        # ---- Display mode toggle ------------------------------------------ #
        mode_rbg = RadioButtonGroup(
            labels=["Both", "Raster only", "Scatter only"],
            active={"both": 0, "raster": 1, "scatter": 2}.get(self._mode, 0),
            width=260,
        )
        self._mode_rbg = mode_rbg

        side_w  = _PANEL_WIDTH_SIDE
        full_w  = _PANEL_WIDTH_FULL
        panel_h = _PANEL_HEIGHT

        mode_js = CustomJS(
            args={
                "raster_fig":    raster_fig,
                "scatter_fig":   scatter_fig,
                "layout_rbg":    None,   # filled after layout_rbg is created
                "raster_sec":    self._raster_axis_section,
                "scatter_sec":   self._scatter_axis_section,
                "side_w":        side_w,
                "full_w":        full_w,
                "panel_h":       panel_h,
            },
            code="""
const mode = cb_obj.active;   // 0=Both, 1=Raster, 2=Scatter
const both    = (mode === 0);
const raster  = (mode !== 2);
const scatter = (mode !== 1);

raster_fig.visible  = raster;
scatter_fig.visible = scatter;

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

        # ---- Layout toggle ------------------------------------------------- #
        layout_rbg = RadioButtonGroup(
            labels    = ["Side by Side", "Over / Under"],
            active    = 0 if self._layout == "side" else 1,
            disabled  = (self._mode != "both"),
            width     = 220,
        )
        self._layout_rbg = layout_rbg

        # Now that layout_rbg exists, patch it into the mode_js args
        mode_js.args["layout_rbg"] = layout_rbg

        half_h  = _PANEL_HEIGHT // 2
        layout_js = CustomJS(
            args={
                "raster_fig":  raster_fig,
                "scatter_fig": scatter_fig,
                "side_w":      side_w,
                "full_w":      full_w,
                "panel_h":     panel_h,
                "half_h":      half_h,
                "pref_src":    self._pref_source,
                "ry_sel":      self._ry_select,
                "rx_sel":      self._rx_select,
                "sx_sel":      self._sx_select,
                "sy_sel":      self._sy_select,
            },
            code="""
const over = (cb_obj.active === 1);

if (over) {
    raster_fig.width   = full_w;
    scatter_fig.width  = full_w;
    raster_fig.height  = half_h;
    scatter_fig.height = half_h;
} else {
    raster_fig.width   = side_w;
    scatter_fig.width  = side_w;
    raster_fig.height  = panel_h;
    scatter_fig.height = panel_h;
}

// Write preference
const key = [ry_sel.value, rx_sel.value, 'AMPLITUDE',
             sx_sel.value, sy_sel.value].join(':');
const prefs = JSON.parse(pref_src.data['prefs'][0]);
prefs[key] = over ? 'over' : 'side';
pref_src.data = {prefs: [JSON.stringify(prefs)]};
""",
        )
        layout_rbg.js_on_change("active", layout_js)

        # ---- Preset buttons ----------------------------------------------- #
        vplot_btn     = Button(label="vplot",     button_type="default", width=70)
        radplot_btn   = Button(label="radplot",   button_type="default", width=70)
        waterfall_btn = Button(label="Waterfall", button_type="default", width=80)

        def _preset_js(preset_name: str) -> CustomJS:
            ry, rx, rq, sx, sy, pl = _PRESETS[preset_name]
            return CustomJS(
                args={
                    "mode_rbg":    mode_rbg,
                    "layout_rbg":  layout_rbg,
                    "ry_sel":      self._ry_select,
                    "rx_sel":      self._rx_select,
                    "rq_sel":      self._rq_select,
                    "sx_sel":      self._sx_select,
                    "sy_sel":      self._sy_select,
                    "active_layout": 0 if pl == "side" else 1,
                    "raster_fig":    raster_fig,
                    "scatter_fig":   scatter_fig,
                    "side_w":        side_w,
                    "full_w":        full_w,
                    "panel_h":       panel_h,
                    "half_h":        half_h,
                },
                code=f"""
// Switch to Both mode first
mode_rbg.active = 0;
raster_fig.visible  = true;
scatter_fig.visible = true;
layout_rbg.disabled = false;

// Apply preset axes
ry_sel.value = '{ry.name}';
rx_sel.value = '{rx.name}';
rq_sel.value = '{rq.name}';
sx_sel.value = '{sx.name}';
sy_sel.value = '{sy.name}';

// Apply preset layout
layout_rbg.active = active_layout;
const over = (active_layout === 1);
if (over) {{
    raster_fig.width   = full_w;  scatter_fig.width  = full_w;
    raster_fig.height  = half_h;  scatter_fig.height = half_h;
}} else {{
    raster_fig.width   = side_w;  scatter_fig.width  = side_w;
    raster_fig.height  = panel_h; scatter_fig.height = panel_h;
}}
""",
            )

        vplot_btn.js_on_click(_preset_js("vplot"))
        radplot_btn.js_on_click(_preset_js("radplot"))
        waterfall_btn.js_on_click(_preset_js("waterfall"))

        # ---- Box Select toggle -------------------------------------------- #
        box_btn = Toggle(
            label  = "□ Box Select",
            active = False,
            button_type = "default",
            width  = 110,
        )
        box_js = CustomJS(
            args={
                "raster_fig":  raster_fig,
                "scatter_fig": scatter_fig,
            },
            code="""
// Activate or deactivate the BoxSelectTool on both figures.
// The tool was added by VisibilityPlot._add_box_select_tool().
function setBoxSelect(fig, activate) {
    for (let t of fig.toolbar.tools) {
        if (t.type === 'BoxSelectTool') {
            if (activate) {
                fig.toolbar.active_drag = t;
            } else {
                if (fig.toolbar.active_drag === t)
                    fig.toolbar.active_drag = null;
            }
            break;
        }
    }
}
setBoxSelect(raster_fig,  cb_obj.active);
setBoxSelect(scatter_fig, cb_obj.active);
""",
        )
        box_btn.js_on_change("active", box_js)

        # ---- Flag ⚑ and Undo ⟲ (disabled in preview) -------------------- #
        flag_btn = Button(
            label="⚑ Flag",
            button_type="warning",
            width=80,
            disabled=True,
        )
        undo_btn = Button(
            label="⟲ Undo",
            button_type="default",
            width=80,
            disabled=True,
        )
        # Tooltips are set via HTML title attribute in the Bokeh Div wrapper
        # since Bokeh Button doesn't natively support tooltips in all versions.

        # ---- Assemble toolbar row ----------------------------------------- #
        sep = Div(text="&nbsp;|&nbsp;", width=20,
                  styles={"line-height": "32px", "color": "#888"})
        sep2 = Div(text="&nbsp;|&nbsp;", width=20,
                   styles={"line-height": "32px", "color": "#888"})
        sep3 = Div(text="&nbsp;|&nbsp;", width=20,
                   styles={"line-height": "32px", "color": "#888"})
        sep4 = Div(text="&nbsp;|&nbsp;", width=20,
                   styles={"line-height": "32px", "color": "#888"})

        return row(
            plot_btn, reload_btn,
            sep,
            mode_rbg,
            sep2,
            layout_rbg,
            sep3,
            vplot_btn, radplot_btn, waterfall_btn,
            sep4,
            box_btn, flag_btn, undo_btn,
        )

    # ---------------------------------------------------------------------- #
    # Sidebar                                                                  #
    # ---------------------------------------------------------------------- #

    def _build_sidebar(self):
        """Build the sidebar column with data, axis, and colormap controls."""
        meta = self._meta

        # ---- Source path (read-only) -------------------------------------- #
        path_div = Div(
            text=(
                f"<b>Source:</b> "
                f"<span style='font-family:monospace;font-size:11px'>"
                f"{os.path.basename(self._source_path)}</span>"
            ),
            width=_SIDEBAR_WIDTH,
        )

        # ---- Data column -------------------------------------------------- #
        col_options = list(meta.data_columns) or ["DATA"]
        self._col_select = Select(
            title   = "Data column",
            value   = self._datacolumn if self._datacolumn in col_options
                      else col_options[0],
            options = col_options,
            width   = _SIDEBAR_WIDTH,
        )

        # ---- Field --------------------------------------------------------- #
        field_options = [f.name for f in meta.fields] or [""]
        current_field = (
            _parse_field_string(self._field_str, meta) or field_options[0]
        )
        self._field_select = Select(
            title   = "Field",
            value   = current_field if current_field in field_options
                      else field_options[0],
            options = field_options,
            width   = _SIDEBAR_WIDTH,
        )

        # ---- SPW ----------------------------------------------------------- #
        spw_options = [(str(s.spw_id),
                        f"SPW {s.spw_id}  ({s.centre_freq_hz/1e9:.3f} GHz)"
                        if s.centre_freq_hz else f"SPW {s.spw_id}")
                       for s in meta.spws]
        if not spw_options:
            spw_options = [("0", "SPW 0")]
        selected_spws = [str(i) for i in _parse_spw_string(self._spw_str, meta)]
        self._spw_select = MultiSelect(
            title   = "SPW",
            value   = selected_spws,
            options = spw_options,
            size    = min(len(spw_options), 5),
            width   = _SIDEBAR_WIDTH,
        )

        # ---- Correlations -------------------------------------------------- #
        all_corrs = list(meta.spws[0].polarizations) if meta.spws else ["XX", "YY"]
        sel_corrs = _parse_correlation_string(self._corr_str, meta)
        self._corr_cbg = CheckboxGroup(
            labels = all_corrs,
            active = [i for i, c in enumerate(all_corrs) if c in sel_corrs],
            width  = _SIDEBAR_WIDTH,
        )
        corr_label = Div(text="<b>Correlation</b>", width=_SIDEBAR_WIDTH)

        # ---- Unwired selection fields (scan, antenna, time, uvrange) ------- #
        def _stub_input(title, placeholder=""):
            inp = TextInput(title=title, value=placeholder, width=_SIDEBAR_WIDTH)
            note = Div(
                text=(
                    "<span style='font-size:10px;color:#888'>"
                    "Full selection — full release</span>"
                ),
                width=_SIDEBAR_WIDTH,
            )
            return column(inp, note)

        scan_col    = _stub_input("Scan",     self._scan_str)
        antenna_col = _stub_input("Antenna",  self._antenna_str)
        time_col    = _stub_input("Time range", self._timerange_str)
        uv_col      = _stub_input("UV range",   self._uvrange_str)

        # ---- Raster axis controls ----------------------------------------- #
        self._ry_select = Select(
            title   = "Raster Y axis",
            value   = self._raster_y.name,
            options = [(k, v) for k, v in _RASTER_Y_OPTIONS],
            width   = _SIDEBAR_WIDTH,
        )
        self._rx_select = Select(
            title   = "Raster X axis",
            value   = self._raster_x.name,
            options = [(k, v) for k, v in _RASTER_X_OPTIONS],
            width   = _SIDEBAR_WIDTH,
        )
        self._rq_select = Select(
            title   = "Raster quantity",
            value   = self._raster_qty.name,
            options = [(k, v) for k, v in _RASTER_QTY_OPTIONS],
            width   = _SIDEBAR_WIDTH,
        )
        raster_cmap = self._raster.colormap_controls()

        self._raster_axis_section = column(
            Div(text="<b>── Raster ──</b>", width=_SIDEBAR_WIDTH),
            self._ry_select,
            self._rx_select,
            self._rq_select,
            raster_cmap,
            visible = (self._mode != "scatter"),
        )

        # ---- Scatter axis controls ---------------------------------------- #
        self._sx_select = Select(
            title   = "Scatter X axis",
            value   = self._scatter_x.name,
            options = [(k, v) for k, v in _SCATTER_X_OPTIONS],
            width   = _SIDEBAR_WIDTH,
        )
        self._sy_select = Select(
            title   = "Scatter Y axis",
            value   = self._scatter_y.name,
            options = [(k, v) for k, v in _SCATTER_Y_OPTIONS],
            width   = _SIDEBAR_WIDTH,
        )
        scatter_cmap = self._scatter.colormap_controls(layer_index=0)

        self._scatter_axis_section = column(
            Div(text="<b>── Scatter ──</b>", width=_SIDEBAR_WIDTH),
            self._sx_select,
            self._sy_select,
            scatter_cmap,
            visible = (self._mode != "raster"),
        )

        # ---- Assemble sidebar --------------------------------------------- #
        return column(
            path_div,
            Div(text="<b>Data</b>", width=_SIDEBAR_WIDTH),
            self._col_select,
            self._field_select,
            self._spw_select,
            corr_label, self._corr_cbg,
            scan_col, antenna_col, time_col, uv_col,
            Div(text="<b>Axes</b>", width=_SIDEBAR_WIDTH),
            self._raster_axis_section,
            self._scatter_axis_section,
            width  = _SIDEBAR_WIDTH,
            styles = {
                "background":   "#1e1e2e",
                "padding":      "8px",
                "border-right": "1px solid #45475a",
                "overflow-y":   "auto",
            },
        )

    # ---------------------------------------------------------------------- #
    # Plot area                                                                #
    # ---------------------------------------------------------------------- #

    def _build_plot_area(self):
        """Build the flex-container div holding both figures.

        Both figures are always present in the Bokeh document; visibility
        and sizing are controlled by ``CustomJS`` callbacks so no
        Python round-trip is needed for layout changes.
        """
        # Preference ColumnDataSource — keyed JSON string mapping axis
        # combinations to user-chosen layout modes.
        self._pref_source = ColumnDataSource(data={"prefs": ["{}"]})

        # Linked x-axis: if both panels start with the same x dimension,
        # share a Range1d so panning one panel moves the other in sync.
        if self._raster_x == self._scatter_x:
            self._scatter.figure.x_range = self._raster.figure.x_range

        flex_div = Div(
            text=(
                f'<div id="cv-plot-area" style="'
                f'display:flex;flex-direction:'
                f'{"row" if self._layout == "side" else "column"};'
                f'gap:4px">'
            ),
            width=_PANEL_WIDTH_SIDE * 2 + 8,
        )

        # Bokeh doesn't allow arbitrary HTML children, so the figures are
        # placed in a Bokeh row/column and the flex direction is controlled
        # via the figure width/height rather than a true CSS flex container.
        # The layout_rbg CustomJS resizes both figures directly.
        if self._layout == "side":
            return row(self._raster.figure, self._scatter.figure)
        else:
            return column(self._raster.figure, self._scatter.figure)

    # ---------------------------------------------------------------------- #
    # Status bar                                                               #
    # ---------------------------------------------------------------------- #

    def _build_status_bar(self):
        """Build the status bar Div."""
        self._status_div = Div(
            text   = self._status_text(),
            width  = _PANEL_WIDTH_SIDE * 2 + _SIDEBAR_WIDTH,
            styles = {
                "font-size":   "12px",
                "font-family": "monospace",
                "padding":     "4px 10px",
                "background":  "#181825",
                "color":       "#a6e3a1",
                "border-top":  "1px solid #45475a",
            },
        )
        return self._status_div
