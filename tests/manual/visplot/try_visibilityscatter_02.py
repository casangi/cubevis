"""
try_visibilityscatter_02.py
===========================
Extends try_visibilityscatter_01.py with:

* Pan/zoom re-composite (no backend re-query — cached DataFrames are
  re-run through ``Canvas.points()`` at the new viewport)
* "Fit to data" unzoom button — restores the full data extents from
  ``_state_source`` without a page reload
* Per-layer alpha sliders — change opacity of each polarization layer
  in real time via the Comm channel (re-composite only, no re-query)

Usage
-----
::

    MS=sis14_twhya_calibrated_flagged.ms python try_visibilityscatter_02.py

Notes on 1:1 zoom for scatter vs raster
----------------------------------------
For raster, "1:1" has a precise meaning: one canvas pixel maps to exactly
one agg cell (the fixed 2D grid returned by ``query_raster``).  Zooming
past this triggers nearest-neighbour rendering.

For scatter there is no fixed grid — Datashader bins samples into whatever
canvas resolution is requested.  The meaningful equivalent is:

* **Unzoom** (implemented here): restore the viewport to the full data
  extents so all samples are visible.
* **Fit to density** (future): zoom to the viewport where the canvas pixel
  size equals the median inter-sample spacing on the x-axis, i.e. where
  each pixel represents approximately one data point.  This would be the
  scatter analogue of the raster 1:1 button and is where flagging would
  be enabled.

The "Fit to data" button below implements unzoom.  The alpha sliders show
how ``set_alpha`` / ``_handle_set_alpha`` work over the Comm channel without
any page rebuild.
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

from bokeh.models import Button, CustomJS, Slider, Div
from bokeh.layouts import column as bk_column, row as bk_row

from cubevis.toolbox.visplot.data.msv2_backend import MSv2Backend
from cubevis.toolbox.visplot.axes import Axis
from cubevis.toolbox.visplot.selection import SelectionSpec
from cubevis.toolbox.visplot.visibility_scatter import VisibilityScatter, ScatterLayer


def _dark_button(label: str, width: int = 120) -> Button:
    return Button(
        label       = label,
        button_type = "default",
        width       = width,
        styles      = {
            "background": "#313244",
            "color":      "#cdd6f4",
            "border":     "1px solid #45475a",
            "font-size":  "12px",
        },
    )


def _alpha_slider(label: str, value: float = 1.0, width: int = 220) -> Slider:
    return Slider(
        title  = label,
        start  = 0.0,
        end    = 1.0,
        step   = 0.05,
        value  = value,
        width  = width,
        styles = {
            "color":       "#cdd6f4",
            "font-size":   "11px",
        },
    )


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
        title    = "VisibilityScatter pan/zoom/alpha demo — sis14",
    )
    comm_mgr = app_context.comm_mgr

    # ------------------------------------------------------------------
    # 3. Build VisibilityScatter
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
    # 5. "Fit to data" unzoom button
    #    Reads full_x0/x1/y0/y1 from _state_source at click time so
    #    it always reflects the current data extents even after update_axes.
    # ------------------------------------------------------------------
    # Shared references used by multiple buttons and sliders
    msg_set_alpha  = vs._msg_set_alpha
    msg_color_mode = vs._msg_color_mode
    comm           = vs._comm

    btn_fit = _dark_button("Fit to data", width=110)
    btn_fit.js_on_click(CustomJS(
        args={
            "x_range": fig.x_range,
            "y_range": fig.y_range,
            "state":   vs._state_source,
        },
        code="""
const d = state.data;
x_range.start = d['full_x0'][0];
x_range.end   = d['full_x1'][0];
y_range.start = d['full_y0'][0];
y_range.end   = d['full_y1'][0];
""",
    ))

    # ------------------------------------------------------------------
    # Colour mode toggle button
    #    "Global" — span anchored to full data y_range; stable on zoom.
    #    "Local"  — Datashader normalises to viewport; reveals detail.
    # Sends vs_set_color_mode j2p via the Comm channel; Python re-composites
    # without re-querying the backend.
    # ------------------------------------------------------------------
    btn_color = _dark_button("Global colour", width=130)
    btn_color.js_on_click(CustomJS(
        args={"comm": comm, "state": vs._state_source,
              "btn": btn_color, "image_source": vs._image_source},
        code=f"""
const current = state.data['color_mode'][0];
const next    = current === 'global' ? 'local' : 'global';
comm.send('{msg_color_mode}', {{mode: next}}, function(resp) {{
    if (resp && resp.status === 'ok') {{
        state.data = Object.assign({{}}, state.data,
                                   {{color_mode: [resp.color_mode]}});
        btn.label = resp.color_mode === 'global' ? 'Global colour' : 'Local colour';
        if (resp.image != null) {{
            image_source.data['image'] = [resp.image];
            image_source.data['x']     = [resp.x0];
            image_source.data['y']     = [resp.y0];
            image_source.data['dw']    = [resp.x1 - resp.x0];
            image_source.data['dh']    = [resp.y1 - resp.y0];
            image_source.change.emit();
        }}
    }}
}});
""",
    ))

    # ------------------------------------------------------------------
    # 6. Per-layer alpha sliders
    #    Each slider sends a vs_set_alpha j2p message via the Comm channel.
    #    Python calls set_alpha() → re-shades without re-querying the backend.
    #    The msg_set_alpha uuid is the per-instance message ID registered
    #    in VisibilityScatter._register_extra_comm_handlers().
    # ------------------------------------------------------------------
    alpha_controls = []

    for i, lyr in enumerate(vs._layers):
        slider = _alpha_slider(
            label = f"Alpha — {lyr.label}",
            value = lyr.alpha,
        )
        slider.js_on_change("value", CustomJS(
            args={"comm": comm, "layer_index": i,
                  "image_source": vs._image_source},
            code=f"""
comm.send('{msg_set_alpha}',
          {{layer_index: layer_index, alpha: cb_obj.value}},
          function(resp) {{
              if (resp && resp.status === 'ok' && resp.image != null) {{
                  image_source.data['image'] = [resp.image];
                  image_source.data['x']     = [resp.x0];
                  image_source.data['y']     = [resp.y0];
                  image_source.data['dw']    = [resp.x1 - resp.x0];
                  image_source.data['dh']    = [resp.y1 - resp.y0];
                  image_source.change.emit();
              }}
          }});
""",
        ))
        alpha_controls.append(slider)

    # ------------------------------------------------------------------
    # Dark / light background toggle — pure JS, no Comm round-trip.
    # Toggles background, border, axis labels, tick labels, and grid
    # lines so the plot is readable on both backgrounds.
    # ------------------------------------------------------------------
    btn_bg = _dark_button("Light BG", width=90)
    btn_bg.js_on_click(CustomJS(
        args={"fig": fig, "btn": btn_bg},
        code="""
const is_dark = fig.background_fill_color === 'black';
const label_color  = is_dark ? '#333333' : '#cdd6f4';
const grid_color   = is_dark ? '#cccccc' : '#45475a';
const bg_color     = is_dark ? 'white'   : 'black';
const border_color = is_dark ? '#f0f0f0' : '#1e1e2e';

fig.background_fill_color = bg_color;
fig.border_fill_color     = border_color;

// Axes: fig.below[0] = x-axis, fig.left[0] = y-axis
for (const axis of [fig.below[0], fig.left[0]]) {
    if (!axis) continue;
    axis.axis_label_text_color  = label_color;
    axis.major_label_text_color = label_color;
    axis.axis_line_color        = label_color;
    axis.major_tick_line_color  = label_color;
    axis.minor_tick_line_color  = label_color;
}

// Grids: fig.center contains grid renderers
for (const g of fig.center) {
    if (g.grid_line_color !== undefined) {
        g.grid_line_color = grid_color;
    }
}

btn.label = is_dark ? 'Dark BG' : 'Light BG';
""",
    ))

    # ------------------------------------------------------------------
    # 7. Assemble layout
    # ------------------------------------------------------------------
    toolbar_row = bk_row(
        btn_fit,
        btn_color,
        btn_bg,
        *alpha_controls,
        styles={"align-items": "center", "gap": "12px",
                "background": "#1e1e2e", "padding": "6px 8px"},
    )

    app_context.ui = bk_column(toolbar_row, vs.layout)
    log.info("Saving HTML and opening browser tab …")
    app_context.show()

    # ------------------------------------------------------------------
    # 8. CommMgr server loop
    # ------------------------------------------------------------------
    log.info(
        "CommMgr on %s transport.  "
        "Pan/zoom to recomposite.  "
        "Use sliders to adjust per-layer alpha.  "
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
