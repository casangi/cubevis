"""
try_visibilityraster_01.py
==========================
Minimal standalone demo: open a VisibilityRaster figure in a browser tab
and update the pixel-metadata status bar as the cursor moves over the plot.

Usage
-----
::

    MS=sis14_twhya_calibrated_flagged.ms python try_visibilityraster_01.py

The MS path defaults to ``sis14_twhya_calibrated_flagged.ms`` in the current
directory if the ``MS`` environment variable is not set.

What it does
------------
1. Opens the measurement set with ``MSv2Backend``.
2. Creates a ``BokehAppContext`` that owns a ``CommMgr``.
3. Constructs a ``VisibilityRaster`` (TIME × BASELINE, AMPLITUDE, pol XX).
   The constructor calls ``comm_mgr.open(squash_queue=True)`` internally,
   so rapid cursor movements that arrive while a probe is still in flight
   are automatically dropped — only the last queued position is resolved.
4. Places the raster layout inside the ``BokehAppContext`` and calls
   ``app_context.show()`` to save an HTML file and open it in a browser tab.
5. Runs the CommMgr server loop so that hover-probe ``j2p`` messages from
   the browser are routed to ``VisibilityRaster._handle_probe`` and the
   formatted pixel label is returned as a ``p2j`` response, which the
   JavaScript callback writes into the info Div below the plot.

Transport
---------
Outside of Jupyter / Colab, ``CommMgr`` auto-detects ``'websocket'``
transport and allocates a local port via ``find_ws_address()``.
The server loop follows the same pattern as ``InteractiveCleanUI._task_server``:
``websockets.serve(comm_mgr.process_messages, host, port)``.
"""

import asyncio
import logging
import os
import sys

import websockets

logging.basicConfig(
    level  = logging.INFO,
    format = "%(levelname)s  %(name)s  %(message)s",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Locate the measurement set
# ---------------------------------------------------------------------------

MS_PATH = os.environ.get("MS", "sis14_twhya_calibrated_flagged.ms")

if not os.path.isdir(MS_PATH):
    print(
        f"\nERROR: MS not found at {MS_PATH!r}\n"
        "Set the MS environment variable or download from:\n"
        "  https://casa.nrao.edu/download/devel/casavis/data/"
        "sis14_twhya_calibrated_flagged.ms.tar.gz\n"
    )
    sys.exit(1)

# ---------------------------------------------------------------------------
# Imports
# ---------------------------------------------------------------------------

from cubevis.bokeh.models import BokehAppContext
from cubevis.bokeh.transport import CommMgr
from cubevis.bokeh import BokehInit

from cubevis.toolbox.visplot.data.msv2_backend import MSv2Backend
from cubevis.toolbox.visplot.axes import Axis
from cubevis.toolbox.visplot.selection import SelectionSpec
from cubevis.toolbox.visplot.visibility_raster import VisibilityRaster

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main():

    # ------------------------------------------------------------------
    # 1. Open the backend
    # ------------------------------------------------------------------
    log.info("Opening MS: %s", MS_PATH)
    backend = MSv2Backend(MS_PATH)
    backend.open()

    meta    = backend.metadata()
    t0, t1  = meta["time_range"]
    pols    = meta["correlation_labels"]
    log.info("  time range   : %.1f – %.1f  (MJD s)", t0, t1)
    log.info("  correlations : %s", pols)

    # Use the first 30 % of the observation for a quick initial render
    sel = SelectionSpec(
        time_range    = (t0, t0 + (t1 - t0) * 0.3),
        channel_range = (0, 48),
    )

    # ------------------------------------------------------------------
    # 2. Create CommMgr + BokehAppContext
    #    Mirrors the pattern in InteractiveCleanUI.__init__
    # ------------------------------------------------------------------
    result_future: asyncio.Future = asyncio.get_event_loop().create_future()

    def on_shutdown(reason, description=""):
        log.info("CommMgr shutdown: %s  %s", reason, description)
        if not result_future.done():
            result_future.set_result(None)

    def on_error(error):
        log.error("CommMgr error: %s", error)

    app_context = BokehAppContext(
        comm_mgr = CommMgr(on_shutdown=on_shutdown, on_error=on_error),
        title    = "VisibilityRaster demo — sis14",
    )
    comm_mgr = app_context.comm_mgr

    # ------------------------------------------------------------------
    # 3. Build the VisibilityRaster
    #    The constructor:
    #      - calls comm_mgr.open(description="visibility raster",
    #                            squash_queue=True)
    #      - runs query_raster → Datashader → Bokeh source
    #      - registers _handle_probe / _handle_rerender on the Comm
    # ------------------------------------------------------------------
    log.info("Building VisibilityRaster (TIME × BASELINE, %s AMPLITUDE) …", pols[0])
    vr = VisibilityRaster(
        backend      = backend,
        selection    = sel,
        y_dim        = Axis.TIME,
        x_dim        = Axis.BASELINE,
        quantity     = Axis.AMPLITUDE,
        polarization = pols[0],
        width        = 1000,
        height       = 650,
        comm_mgr     = comm_mgr,
    )

    # ------------------------------------------------------------------
    # Visual polish: black canvas + legible axis styling
    # ------------------------------------------------------------------
    fig = vr.figure

    # Black plot canvas so transparent (no-data) pixels read as gaps,
    # and the Plasma palette has maximum contrast.
    fig.background_fill_color = "black"
    fig.border_fill_color     = "#1e1e2e"   # match the info Div

    # Tick labels and axis labels in a light colour so they read clearly
    # against the dark border area.
    _label_color = "#cdd6f4"
    _grid_color  = "#45475a"

    for axis in (fig.xaxis, fig.yaxis):
        axis.axis_label_text_color = _label_color
        axis.axis_label_text_font_style = "normal"
        axis.major_label_text_color = _label_color
        axis.axis_line_color  = _label_color
        axis.major_tick_line_color = _label_color
        axis.minor_tick_line_color = _label_color

    fig.xgrid.grid_line_color = _grid_color
    fig.ygrid.grid_line_color = _grid_color

    # Y-axis (TIME): raw MJD seconds are unreadable at 1.353e9 scale.
    # Display as elapsed seconds from the start of the selection so the
    # tick labels are small, human-readable numbers (e.g. 0 … 3600 s).
    from bokeh.models import CustomJSTickFormatter
    t_start = float(vr._y_range[0])
    fig.yaxis.formatter = CustomJSTickFormatter(
        args={"t0": t_start},
        code="""
const elapsed = tick - t0;
if (Math.abs(elapsed) < 60)
    return elapsed.toFixed(1) + ' s';
const m = Math.floor(Math.abs(elapsed) / 60);
const s = Math.round(Math.abs(elapsed) % 60);
const sign = elapsed < 0 ? '-' : '';
return sign + m + 'm ' + s.toString().padStart(2,'0') + 's';
""",
    )
    fig.yaxis.axis_label = "Time offset from scan start"

    # X-axis (BASELINE): integer IDs are fine, just label clearly
    fig.xaxis.axis_label = "Baseline ID"
    log.info("  agg shape : %s", vr.agg.shape)
    log.info("  x_range   : (%.4g, %.4g)", *vr._x_range)
    log.info("  y_range   : (%.4g, %.4g)", *vr._y_range)

    # ------------------------------------------------------------------
    # 4. Place the layout in BokehAppContext and open a browser tab
    # ------------------------------------------------------------------
    app_context.ui = vr.layout
    log.info("Saving HTML and opening browser tab …")
    app_context.show()

    # ------------------------------------------------------------------
    # 5. Run the CommMgr server loop
    #    Mirrors InteractiveCleanUI._task_server:
    #      - WebSocket transport: comm_mgr.address is set by find_ws_address()
    #        inside CommMgr.registered(); serve with websockets.serve()
    #      - Jupyter/Colab transport: comm_mgr.address is None; call
    #        process_messages() directly (it uses CommsTransport internally)
    # ------------------------------------------------------------------
    log.info(
        "CommMgr listening on %s transport.",
        comm_mgr.transport_type,
    )
    log.info(
        "Move the cursor over the plot to see pixel metadata.  "
        "Press Ctrl-C to stop."
    )

    try:
        if comm_mgr.address:
            host, port = comm_mgr.address
            log.info("WebSocket server at %s:%d", host, port)
            async with websockets.serve(comm_mgr.process_messages, host, port):
                await result_future
        else:
            await comm_mgr.process_messages()

    except KeyboardInterrupt:
        log.info("Interrupted by user.")
    finally:
        if not result_future.done():
            result_future.cancel()
        await comm_mgr.shutdown()
        backend.close()
        log.info("Done.")


if __name__ == "__main__":
    asyncio.run(main())
