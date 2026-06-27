"""
try_visibilityraster_02.py
==========================
Extends try_visibilityraster_01.py with pan/zoom re-render support.

When you pan or zoom the plot, the JS range callbacks fire a 'vr_rerender'
request via the Comm channel.  Python re-queries the backend over the new
viewport, re-runs Datashader at the same canvas resolution, and pushes the
new RGBA image back through the transport's serialize() path (ndarray →
Bokeh typed binary buffer) — no JSON integer list involved.

Usage
-----
::

    MS=sis14_twhya_calibrated_flagged.ms python try_visibilityraster_02.py

What to exercise
----------------
* Wheel-zoom in on a dense region — the re-render should sharpen within ~1s
* Pan left/right across the baseline axis
* Box-zoom to a specific time/baseline window
* Hover to confirm pixel metadata still updates after a re-render
* Zoom back out with the Reset tool to return to the full view

The 300 ms debounce on the JS side means rapid pan gestures fire only one
re-render request when the motion stops.  squash_queue=True on the Comm
means if a prior re-render hasn't returned yet, intermediate requests are
dropped and only the latest viewport is resolved.
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
from cubevis.bokeh import BokehInit


from cubevis.toolbox.visplot.data.msv2_backend import MSv2Backend
from cubevis.toolbox.visplot.axes import Axis
from cubevis.toolbox.visplot.selection import SelectionSpec
from cubevis.toolbox.visplot.visibility_raster import VisibilityRaster


async def main():

    # ------------------------------------------------------------------
    # 1. Open the backend — full time range so zooming out still works
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

    # Full time range, all channels — the initial render covers everything
    # so that zooming out after a zoom-in doesn't leave blank regions.
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
        title    = "VisibilityRaster pan/zoom demo — sis14",
    )
    comm_mgr = app_context.comm_mgr

    # ------------------------------------------------------------------
    # 3. Build VisibilityRaster
    #    The Comm is opened with squash_queue=True (done inside __init__)
    #    so both probe and rerender requests are squashed when in flight.
    # ------------------------------------------------------------------
    log.info("Building VisibilityRaster (full time range) …")
    vr = VisibilityRaster(
        backend      = backend,
        selection    = sel,
        y_dim        = Axis.TIME,
        x_dim        = Axis.BASELINE,
        quantity     = Axis.AMPLITUDE,
        polarization = pols[0],
        width        = 1100,
        height       = 700,
        comm_mgr     = comm_mgr,
    )
    log.info("  agg shape : %s", vr.agg.shape)
    log.info("  x_range   : (%.4g, %.4g)", *vr._x_range)
    log.info("  y_range   : (%.4g, %.4g)", *vr._y_range)

    # ------------------------------------------------------------------
    # 4. Visual styling
    # ------------------------------------------------------------------
    fig = vr.figure

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

    # Tick formatters and axis labels are now built into VisibilityRaster
    # via _state_source — no need to set them here.  They update
    # automatically when update_axes() is called.

    # ------------------------------------------------------------------
    # 1:1 zoom button — reads agg shape and data extents from
    # _state_source at click time so it remains correct after an axis
    # change via update_axes().
    # ------------------------------------------------------------------
    from bokeh.models import Button, CustomJS
    from bokeh.layouts import column as bk_column, row as bk_row

    btn_1to1 = Button(
        label       = "1:1 Zoom",
        button_type = "default",
        width       = 100,
        styles      = {
            "background": "#313244",
            "color":      "#cdd6f4",
            "border":     "1px solid #45475a",
            "font-size":  "12px",
        },
    )

    btn_1to1.js_on_click(CustomJS(
        args={
            "x_range": fig.x_range,
            "y_range": fig.y_range,
            "state":   vr._state_source,   # reads agg_n_x/y, full_* at click time
            "canvas_w": vr._width,
            "canvas_h": vr._height,
        },
        code="""
const d      = state.data;
const agg_n_x = d['agg_n_x'][0], agg_n_y = d['agg_n_y'][0];
const full_x0 = d['full_x0'][0], full_x1  = d['full_x1'][0];
const full_y0 = d['full_y0'][0], full_y1  = d['full_y1'][0];

const cell_w = (full_x1 - full_x0) / agg_n_x;
const cell_h = (full_y1 - full_y0) / agg_n_y;

// Cap at full data span: if agg has fewer cells than canvas pixels,
// 1:1 is already the full view.
const vp_w = Math.min(cell_w * canvas_w, full_x1 - full_x0);
const vp_h = Math.min(cell_h * canvas_h, full_y1 - full_y0);

const cx = (x_range.start + x_range.end) / 2;
const cy = (y_range.start + y_range.end) / 2;

const new_x0 = Math.max(full_x0, Math.min(cx - vp_w / 2, full_x1 - vp_w));
const new_x1 = new_x0 + vp_w;
const new_y0 = Math.max(full_y0, Math.min(cy - vp_h / 2, full_y1 - vp_h));
const new_y1 = new_y0 + vp_h;

x_range.start = new_x0;
x_range.end   = new_x1;
y_range.start = new_y0;
y_range.end   = new_y1;
""",
    ))

    # Place the buttons in a toolbar row above the raster layout
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
const is_dark  = fig.background_fill_color === 'black';
const label_c  = is_dark ? '#333333' : '#cdd6f4';
const grid_c   = is_dark ? '#cccccc' : '#45475a';
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

    app_context.ui = bk_column(
        bk_row(btn_1to1, btn_bg),
        vr.layout,
    )
    log.info("Saving HTML and opening browser tab …")
    app_context.show()

    # ------------------------------------------------------------------
    # 6. CommMgr server loop
    # ------------------------------------------------------------------
    log.info(
        "CommMgr on %s transport.  "
        "Pan/zoom the plot — re-renders fire automatically.  "
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
