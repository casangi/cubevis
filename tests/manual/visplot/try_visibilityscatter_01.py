"""
try_visibilityscatter_01.py
===========================
Minimal standalone demo: open a VisibilityScatter figure in a browser tab
showing UVDIST vs AMPLITUDE for all available polarizations, with the hover
info bar updating as the cursor moves.

Usage
-----
::

    MS=sis14_twhya_calibrated_flagged.ms python try_visibilityscatter_01.py

What it shows
-------------
* One ``ScatterLayer`` per polarization, each with a distinct colour map
* The composite image is produced by ``datashader.tf.stack()`` — overlapping
  data points are alpha-composited correctly in float space rather than by
  painter's order
* Moving the cursor over the plot shows the quantity value and sample count
  at that pixel in the info bar below the figure
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

MS_PATH = os.environ.get("MS", "sis14_twhya_calibrated_flagged.ms")

if not os.path.isdir(MS_PATH):
    print(
        f"\nERROR: MS not found at {MS_PATH!r}\n"
        "Set the MS environment variable or download from:\n"
        "  https://casa.nrao.edu/download/devel/casavis/data/"
        "sis14_twhya_calibrated_flagged.ms.tar.gz\n"
    )
    sys.exit(1)

from cubevis.bokeh.models import BokehAppContext
from cubevis.bokeh.transport import CommMgr

from cubevis.toolbox.visplot.data.msv2_backend import MSv2Backend
from cubevis.toolbox.visplot.axes import Axis
from cubevis.toolbox.visplot.selection import SelectionSpec
from cubevis.toolbox.visplot.visibility_scatter import VisibilityScatter, ScatterLayer


async def main():

    # ------------------------------------------------------------------
    # 1. Open the backend
    # ------------------------------------------------------------------
    log.info("Opening MS: %s", MS_PATH)
    backend = MSv2Backend(MS_PATH)
    backend.open()

    meta   = backend.metadata()
    t0, t1 = meta["time_range"]
    pols   = meta["correlation_labels"]
    log.info("  time range   : %.1f – %.1f  (MJD s, span=%.0f s)",
             t0, t1, t1 - t0)
    log.info("  correlations : %s", pols)

    sel = SelectionSpec(channel_range=(0, 48))

    # ------------------------------------------------------------------
    # 2. CommMgr + BokehAppContext
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
        title    = "VisibilityScatter demo — sis14",
    )
    comm_mgr = app_context.comm_mgr

    # ------------------------------------------------------------------
    # 3. Build VisibilityScatter — one layer per polarization
    # ------------------------------------------------------------------
    layers = [
        ScatterLayer(y_axis=Axis.AMPLITUDE, polarization=pol)
        for pol in pols
    ]
    log.info("Building VisibilityScatter (%d layer(s): %s) …",
             len(layers), [lyr.label for lyr in layers])

    vs = VisibilityScatter(
        backend   = backend,
        selection = sel,
        x_axis    = Axis.UVDIST,
        layers    = layers,
        width     = 1100,
        height    = 700,
        comm_mgr  = comm_mgr,
    )
    log.info("  x_range : (%.4g, %.4g)  [m]", *vs._x_range)
    log.info("  y_range : (%.4g, %.4g)  [Jy]", *vs._y_range)
    for i, (lyr, df) in enumerate(zip(vs._layers, vs._layer_dfs)):
        n = len(df) if df is not None else 0
        log.info("  layer %d  %-20s  %d points", i, lyr.label, n)

    # ------------------------------------------------------------------
    # 4. Visual styling
    # ------------------------------------------------------------------
    fig = vs.figure

    fig.background_fill_color = "black"
    fig.border_fill_color     = "#1e1e2e"

    _label_color = "#cdd6f4"
    _grid_color  = "#45475a"

    for axis in (fig.xaxis, fig.yaxis):
        axis.axis_label_text_color      = _label_color
        axis.axis_label_text_font_style = "normal"
        axis.major_label_text_color     = _label_color
        axis.axis_line_color            = _label_color
        axis.major_tick_line_color      = _label_color
        axis.minor_tick_line_color      = _label_color

    fig.xgrid.grid_line_color = _grid_color
    fig.ygrid.grid_line_color = _grid_color

    # ------------------------------------------------------------------
    # 5. Dark / light background toggle (diagnostic aid)
    # ------------------------------------------------------------------
    from bokeh.models import Button, CustomJS
    from bokeh.layouts import column as bk_column, row as bk_row

    btn_bg = Button(
        label       = "Light BG",
        button_type = "default",
        width       = 90,
        styles      = {
            "background": "#313244",
            "color":      "#cdd6f4",
            "border":     "1px solid #45475a",
            "font-size":  "12px",
        },
    )
    btn_bg.js_on_click(CustomJS(
        args={"fig": fig, "btn": btn_bg},
        code="""
const is_dark = fig.background_fill_color === 'black';
const label_c = is_dark ? '#333333' : '#cdd6f4';
const grid_c  = is_dark ? '#cccccc' : '#45475a';
fig.background_fill_color = is_dark ? 'white'   : 'black';
fig.border_fill_color     = is_dark ? '#f0f0f0' : '#1e1e2e';
for (const axis of [fig.below[0], fig.left[0]]) {
    if (!axis) continue;
    axis.axis_label_text_color  = label_c;
    axis.major_label_text_color = label_c;
    axis.axis_line_color        = label_c;
    axis.major_tick_line_color  = label_c;
    axis.minor_tick_line_color  = label_c;
}
for (const g of fig.center) {
    if (g.grid_line_color !== undefined) g.grid_line_color = grid_c;
}
btn.label = is_dark ? 'Dark BG' : 'Light BG';
""",
    ))

    # ------------------------------------------------------------------
    # 6. Open browser tab
    # ------------------------------------------------------------------
    app_context.ui = bk_column(bk_row(btn_bg), vs.layout)
    log.info("Saving HTML and opening browser tab …")
    app_context.show()

    # ------------------------------------------------------------------
    # 7. CommMgr server loop
    # ------------------------------------------------------------------
    log.info(
        "CommMgr on %s transport.  "
        "Hover over the plot to inspect pixels.  "
        "Ctrl-C to stop.",
        comm_mgr.transport_type,
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
