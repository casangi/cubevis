"""grid_view.py -- many small plots of one kind, with one set of tools.

The grid view of ``devel/docs/gridplot/grid_and_calplot_plan.md`` (stage
G1b; sections 3, 9.1-9.3; prototype in ``devel/tools/gridplot_proto``):
a page of cells, each one value of an iteration axis -- a baseline, an
antenna, an SPW -- drawn by a ``GridSource`` as a ``RenderedPanel``.

Built once (cubevis has no Bokeh server: every model exists before the page
is shown), changed in the browser:

* **a pool of cells**, ``pool`` rows x cols (default 6 x 6), each a figure
  with one ``image_rgba`` glyph, sized by its *frame* so every cell has the
  same plot area whichever axes it shows.  Rows x cols change in the
  browser by giving the grid new children -- never by hiding cells, which
  costs a re-layout per cell (prototype: 8 s for 55 cells);
* **one toolbar** over every cell of the pool: the tools are identical in
  every cell (Bokeh merges them into one button each) and each callback
  finds its cell from the event's figure (``origin.tags[0]``, the slot);
* **axes**: at a common scale the X axis runs across the top of the top
  row and the Y axis down the left of the first column, each axis *name*
  once (plan 1.3, decided 2026-11-11); per cell, every cell has its own;
* **common or per-cell scale**: every cell has its own ranges; at a common
  scale a zoom in one is copied to all, and the whole page is drawn again
  at the new viewport in one request;
* **paging** (first / previous / next / last), **playback** (pages on a
  timer, prefetched: a 1 x 1 grid is the tapedeck), **probe** (the line
  under the grid says what the cursor is over), **crosshair** (one, in
  every cell), **tap** to drill down (``on_drill``), **Save PNG** of the
  page (``png_export``: the same picture as a file).

The Python side holds no browser state: every request says which page,
rows, cols, scale and viewport it is for.  The source may change what it
shows (an application's Plot button): ``reload`` in the browser
(``window.__cvGrid[view.key].reload()``) starts again from page 1 with the
source's labels as they are then.

Tick labels are the panels' own (``tick_format.TICK_FORMATTER_JS``): each
cell carries the axis state its ``PanelSpec`` gives (time axes in UTC,
SI-prefixed units, the names along a Baseline axis).
"""
from __future__ import annotations

import asyncio
import base64
import logging
import math
import os
import tempfile
from typing import Callable, Optional, Sequence, Tuple
from uuid import uuid4

import numpy as np

from .grid_source import GridSource

log = logging.getLogger(__name__)

#: the plan's interactive cap (9.3); the pool may not be larger
MAX_POOL = (8, 8)
#: smallest cell plot area, px (the plan's legibility floor, 9.3)
MIN_FRAME = (120, 90)

#: the PanelSpec.to_state_data keys the tick formatter reads
_AXIS_KEYS = ("x_is_time", "y_is_time", "x_t0", "y_t0", "x_scale", "y_scale", "x_cat",
              "y_cat", "time_labels", "x_day0", "y_day0")
_AXIS_DEFAULT = {"x_is_time": [0], "y_is_time": [0], "x_t0": [0.0], "y_t0": [0.0],
                 "x_scale": [1.0], "y_scale": [1.0], "x_cat": [None], "y_cat": [None],
                 "time_labels": ["utc"], "x_day0": [None], "y_day0": [None]}


def _axis_state(spec) -> Optional[dict]:
    """The tick formatter's keys from a cell's spec (``None`` if it has
    no usable spec)."""
    try:
        d = spec.to_state_data()
    except Exception:                                   # noqa: BLE001
        return None
    return {k: d[k] for k in _AXIS_KEYS if k in d}


class GridView:
    """A grid of cells from *source*, with its controls, as one layout.

    Parameters
    ----------
    source : GridSource
    comm_mgr : cubevis CommMgr of the application showing this view
    rows, cols : the first page's shape (at most *pool*)
    pool : the most rows and cols the page can have (default 6 x 6; at most
        8 x 8).  Every cell of the pool is built up front (about 55 ms a
        cell), so a larger pool costs start-up time
    cell : (width, height) of each cell's plot area, px -- when *area* is
        not given, every page shape uses it
    area : (width, height), px, the cells of a page share: cells are sized
        to fill it at every rows x cols (at least ``MIN_FRAME``, keeping
        *cell*'s aspect ratio), as plotms fills its window
    theme : "light" or "dark"
    on_drill : ``fn(index, label) -> str or dict or None``, called when a
        cell is tapped.  A string is shown in the status line; a dict is
        handed to *drill_js* (its ``"text"`` is shown).  An application
        opens the cell in a full panel here
    drill_js : a ``CustomJS`` run in the browser with the drill response as
        ``cb_obj`` (an application switches to its panel view there)
    autoload : draw page 1 when the page loads (else on the first
        ``reload``)
    extra_controls : widgets placed at the start of the control row (an
        application's "Iterate by")
    idle_text : the status line before the first page (no autoload)
    embedded : the layout is the grid alone; the application places the
        controls (``widgets``) where its own controls are -- visplot puts
        the shape in its sidebar, paging in its toolbar, the readout over
        its status bar
    app_context : the application's ``BokehAppContext``: the view's script
        is one of its init scripts (else it runs on ``curdoc()``'s
        DocumentReady, which a page saved from the app context may not
        have)
    """

    def __init__(self, source: GridSource, comm_mgr, *, rows: int = 3, cols: int = 3,
                 pool: Tuple[int, int] = (6, 6), cell: Tuple[int, int] = (200, 140),
                 area: Optional[Tuple[int, int]] = None,
                 theme: str = "light",
                 on_drill: Optional[Callable[[int, str], object]] = None,
                 drill_js=None, autoload: bool = True,
                 extra_controls: Sequence = (), app_context=None,
                 idle_text: str = "", embedded: bool = False,
                 colour: Optional[str] = None) -> None:
        pr, pc = int(pool[0]), int(pool[1])
        if not (1 <= pr <= MAX_POOL[0] and 1 <= pc <= MAX_POOL[1]):
            raise ValueError(f"pool must be within 1 x 1 .. {MAX_POOL[0]} x {MAX_POOL[1]}")
        if cell[0] < MIN_FRAME[0] or cell[1] < MIN_FRAME[1]:
            raise ValueError(f"cell must be at least {MIN_FRAME[0]} x {MIN_FRAME[1]} px")
        self._source = source
        self._pool = (pr, pc)
        self._rows, self._cols = min(max(1, int(rows)), pr), min(max(1, int(cols)), pc)
        self._cell = (int(cell[0]), int(cell[1]))
        self._area = (int(area[0]), int(area[1])) if area else None
        self._theme = theme
        self._on_drill = on_drill
        self._drill_js = drill_js
        self._autoload = bool(autoload)
        self._extra = list(extra_controls)
        self._app_context = app_context
        self._idle_text = idle_text
        self._embedded = bool(embedded)
        # the colour scopes the source offers (render(colour=...)): none, a
        # source whose cells each colour themselves
        self._colours = tuple(getattr(source, "COLOUR_SCOPES", ()))
        self._colour = colour if colour in self._colours else (
            "page" if "page" in self._colours else None)
        self._labels = list(source.labels())
        self._comm_mgr = comm_mgr
        self._pipe = comm_mgr.open(squash_queue=False, description="grid view")
        # The probe on its own, squashed: a moving cursor makes many, only
        # the last matters, and a page request must not wait behind them.
        self._probe_pipe = comm_mgr.open(squash_queue=True, description="grid view probe")
        self._ids = {k: str(uuid4()) for k in ("page", "probe", "drill", "save")}
        #: this view's namespace in the browser: ``window.__cvGrid[key]``
        self.key = self._ids["page"]
        self._pipe.register(self._ids["page"], self._handle_page)
        self._pipe.register(self._ids["drill"], self._handle_drill)
        self._pipe.register(self._ids["save"], self._handle_save)
        self._probe_pipe.register(self._ids["probe"], self._handle_probe)
        self.layout = self._build()

    # ------------------------------------------------------------------
    # paging arithmetic (shared by the handlers and tests)
    # ------------------------------------------------------------------
    @property
    def n_cells(self) -> int:
        return len(self._labels)

    def n_pages(self, rows: int, cols: int) -> int:
        return max(1, math.ceil(self.n_cells / max(1, rows * cols)))

    def page_indices(self, page: int, rows: int, cols: int) -> list:
        per = max(1, rows * cols)
        page = min(max(0, int(page)), self.n_pages(rows, cols) - 1)
        return list(range(page * per, min(self.n_cells, (page + 1) * per)))

    # ------------------------------------------------------------------
    # j2p handlers
    # ------------------------------------------------------------------
    async def _handle_page(self, msg, context=None):
        try:
            return await asyncio.to_thread(self.page, msg)
        except Exception as exc:                      # noqa: BLE001 -- shown, not raised
            log.warning("grid view: page failed: %s", exc, exc_info=True)
            return {"error": str(exc)}

    def page(self, msg: dict) -> dict:
        """The cells a request asks for, drawn.

        *msg*: ``page``, ``rows``, ``cols``; ``common`` (bool, default
        True); ``viewport`` ([x0, x1, y0, y1] or null: the full extent);
        ``w``, ``h`` (each cell's plot area, px); optionally ``indices``
        (cells to draw again, e.g. one zoomed at a per-cell scale) -- else
        the page's."""
        self._labels = list(self._source.labels())
        rows = min(max(1, int(msg.get("rows", self._rows))), self._pool[0])
        cols = min(max(1, int(msg.get("cols", self._cols))), self._pool[1])
        n_pages = self.n_pages(rows, cols)
        page = min(max(0, int(msg.get("page", 0))), n_pages - 1)
        per = rows * cols
        indices = msg.get("indices")
        if indices is None:
            indices = self.page_indices(page, rows, cols)
        else:
            indices = [int(i) for i in indices if 0 <= int(i) < self.n_cells]
        common = bool(msg.get("common", True))
        vp = msg.get("viewport")
        if vp is not None:
            vp = tuple(float(v) for v in vp)
        elif common:
            vp = tuple(float(v) for v in self._source.extent())
        w = max(MIN_FRAME[0] // 2, int(msg.get("w") or self._cell[0]))
        h = max(MIN_FRAME[1] // 2, int(msg.get("h") or self._cell[1]))
        colour, ckw = self._colour_kw(msg, page, rows, cols)
        rendered = self._source.render(indices, vp, (w, h), **ckw) if indices else []
        cells = []
        for i, rp in zip(indices, rendered):
            spec = rp.spec
            x0, x1, y0, y1 = rp.viewport or (spec.x_range + spec.y_range)
            cell = {"index": i, "slot": i - page * per, "title": spec.title or self._label(i),
                    "x0": float(x0), "x1": float(x1), "y0": float(y0), "y1": float(y1),
                    "note": spec.note or ""}
            if rp.image is not None:
                cell["image"] = np.ascontiguousarray(rp.image, dtype=np.uint32)
            if spec.status == "ok":
                axes = _axis_state(spec)
                if axes:
                    cell["axes"] = axes
            cells.append(cell)
        xn, yn = self._source.axis_names()
        ext = [float(v) for v in self._source.extent()]
        return {"page": page, "n_pages": n_pages, "n_cells": self.n_cells, "rows": rows,
                "cols": cols, "cells": cells, "x_name": xn, "y_name": yn, "extent": ext,
                "partial": msg.get("indices") is not None,
                "colour": self._colour_state(colour)}

    def _colour_kw(self, msg: dict, page: int, rows: int, cols: int):
        """The colour scope a request asks for, and ``render``'s keywords
        for it (none for a source without scopes)."""
        if not self._colours:
            return None, {}
        colour = msg.get("colour") or self._colour
        if colour not in self._colours:
            colour = self._colour
        return colour, {"colour": colour,
                        "page_indices": self.page_indices(page, rows, cols)}

    def _colour_state(self, colour) -> dict:
        fn = getattr(self._source, "colour_state", None)
        return fn(colour) if (fn and colour) else {"pending": False, "note": ""}

    async def _handle_probe(self, msg, context=None):
        try:
            return {"text": self._source.probe(int(msg["index"]), float(msg["x"]),
                                               float(msg["y"]))}
        except Exception as exc:                      # noqa: BLE001 -- shown, not raised
            return {"text": f"({exc})"}

    async def _handle_drill(self, msg, context=None):
        i = int(msg.get("index", -1))
        if not (0 <= i < self.n_cells):
            return {"text": ""}
        if self._on_drill is None:
            return {"text": f"{self._labels[i]} (cell {i + 1} of {self.n_cells})"}
        try:
            r = self._on_drill(i, self._labels[i])
            if isinstance(r, dict):
                return dict(r, text=r.get("text") or "")
            return {"text": r or ""}
        except Exception as exc:                      # noqa: BLE001
            log.warning("grid view: drill-down for %s failed: %s", self._labels[i], exc)
            return {"text": f"opening {self._labels[i]} failed: {exc}"}

    async def _handle_save(self, msg, context=None):
        try:
            return await asyncio.to_thread(self.save, msg)
        except Exception as exc:                      # noqa: BLE001
            log.warning("grid view: save failed: %s", exc, exc_info=True)
            return {"error": str(exc)}

    def save(self, msg: dict, path: Optional[str] = None) -> dict:
        """The page as a PNG (``png_export.export_png``): ``{name, png}``,
        *png* base64 for the browser to download; or written to *path*."""
        from .png_export import export_png
        self._labels = list(self._source.labels())
        rows = min(max(1, int(msg.get("rows", self._rows))), self._pool[0])
        cols = min(max(1, int(msg.get("cols", self._cols))), self._pool[1])
        page = min(max(0, int(msg.get("page", 0))), self.n_pages(rows, cols) - 1)
        indices = self.page_indices(page, rows, cols)
        common = bool(msg.get("common", True))
        vp = msg.get("viewport")
        vp = tuple(float(v) for v in vp) if vp is not None else (
            tuple(self._source.extent()) if common else None)
        w = int(msg.get("w") or self._cell[0])
        h = int(msg.get("h") or self._cell[1])
        colour, ckw = self._colour_kw(msg, page, rows, cols)
        if colour == "grid" and hasattr(self._source, "wait_for_grid_range"):
            self._source.wait_for_grid_range()      # the file is coloured as asked
        rendered = list(self._source.render(indices, vp, (w, h), **ckw))
        # used rows only: a partly filled last page is not padded with
        # empty rows; the empty cells of its last row stay framed
        used_rows = max(1, math.ceil(len(rendered) / cols))
        name = f"grid_page{page + 1:02d}_of{self.n_pages(rows, cols):02d}.png"
        out = path or os.path.join(tempfile.mkdtemp(prefix="cubevis-grid-"), name)
        export_png(rendered, out, nrows=used_rows, ncols=cols, theme=self._theme,
                   axes="outer" if common else "each")
        if path is not None:
            return {"name": name, "path": out}
        with open(out, "rb") as f:
            data = base64.b64encode(f.read()).decode()
        return {"name": name, "png": data}

    def frame_for(self, rows: int, cols: int, common: bool = True) -> Tuple[int, int]:
        """Each cell's plot area at *rows* x *cols*: *cell*, or the share of
        *area* (what the browser's ``frameFor`` computes too)."""
        if not self._area:
            return self._cell
        aw, ah = self._area
        w = (aw - 36 - (36 if common else cols * 46) - cols * 8) // max(1, cols)
        h = (ah - (24 if common else rows * 24) - rows * 26) // max(1, rows)
        return max(MIN_FRAME[0], int(w)), max(MIN_FRAME[1], int(h))

    def fit_to(self, model) -> None:
        """Size the cells to fill *model* (a layout of the application's
        page, e.g. the area the grid is shown in) as it is in the browser,
        at every rows x cols and window size; ``area`` is then only the
        size used before the browser has measured it.  Call before the page
        is shown."""
        self._init_cb.args = dict(self._init_cb.args, fit_to=model)

    def set_theme(self, theme: str) -> None:
        """The theme Save PNG draws its chrome in (the browser restyles the
        cells: ``restyle``)."""
        self._theme = "dark" if theme == "dark" else "light"

    def _label(self, i: int) -> str:
        return self._labels[i] if 0 <= i < self.n_cells else ""

    # ------------------------------------------------------------------
    # the layout
    # ------------------------------------------------------------------
    def _build(self):
        from bokeh.events import DocumentReady
        from bokeh.io import curdoc
        from bokeh.layouts import group_tools
        from bokeh.models import (BoxZoomTool, Button, CheckboxGroup, ColumnDataSource,
                                  CrosshairTool, CustomJS, CustomJSTickFormatter, Div,
                                  GridPlot, HoverTool, InlineStyleSheet, PanTool, Range1d,
                                  ResetTool, Select, Span, TapTool, Toolbar, WheelZoomTool)
        from .tick_format import TICK_FORMATTER_JS
        from bokeh.layouts import column, row
        from bokeh.plotting import figure

        dark = self._theme == "dark"
        fg = "#d8d8d8" if dark else "#333333"
        bg = "#1e1e1e" if dark else "#ffffff"
        pr, pc = self._pool
        fw, fh = self.frame_for(self._rows, self._cols, True)
        x0, x1, y0, y1 = (float(v) for v in self._source.extent())
        xn, yn = self._source.axis_names()

        crosshair = CrosshairTool(overlay=[
            Span(dimension="width", line_color="#ff7f0e", line_width=1, line_dash="dashed"),
            Span(dimension="height", line_color="#ff7f0e", line_width=1, line_dash="dashed")])
        figs, sources, states = [], [], []
        for slot in range(pr * pc):
            f = figure(frame_width=fw, frame_height=fh, x_axis_location="above",
                       x_range=Range1d(x0, x1), y_range=Range1d(y0, y1),
                       tools="", toolbar_location=None, tags=[slot],
                       min_border_left=4, min_border_right=4, min_border_top=2,
                       min_border_bottom=4, background_fill_color=bg, border_fill_color=bg,
                       outline_line_color="#888888")
            # a title from the start (a no-break space): a figure built with
            # none has no room for one when the page's labels arrive
            f.title.text = "\u00a0"
            f.title.align = "center"
            f.title.text_color = fg
            f.title.text_font_size = "11px"
            for ax in (f.xaxis, f.yaxis):
                ax.major_label_text_color = fg
                ax.axis_line_color = fg
                ax.major_tick_line_color = fg
                ax.minor_tick_line_color = fg
                ax.major_label_text_font_size = "9px"
            f.grid.visible = False
            # the panels' own tick labels, from this cell's axis state
            st = ColumnDataSource(dict(_AXIS_DEFAULT))
            for axes, k in ((f.xaxis, "x"), (f.yaxis, "y")):
                axes.formatter = CustomJSTickFormatter(
                    args={"state": st, "axis_key": f"{k}_is_time", "t0_key": f"{k}_t0",
                          "scale_key": f"{k}_scale", "cat_key": f"{k}_cat",
                          "day_key": f"{k}_day0"},
                    code=TICK_FORMATTER_JS)
            states.append(st)
            src = ColumnDataSource({"image": [np.zeros((1, 1), np.uint32)], "x": [x0], "y": [y0],
                                    "dw": [x1 - x0], "dh": [y1 - y0]})
            f.image_rgba(image="image", x="x", y="y", dw="dw", dh="dh", source=src)
            f.add_tools(PanTool(), WheelZoomTool(), BoxZoomTool(), ResetTool(), TapTool(),
                        HoverTool(tooltips=None), crosshair)
            figs.append(f)
            sources.append(src)

        # One toolbar over the whole pool: Bokeh merges equal tools into one
        # button (a ToolProxy) -- built here over every cell, because the
        # grid itself only ever holds the page's rows x cols.
        toolbar = Toolbar(tools=group_tools([t for f in figs for t in f.tools]),
                          autohide=False)
        kids = [(figs[r * pc + c], r, c) for r in range(self._rows) for c in range(self._cols)]
        # toolbar at the right: the X axis name sits right over the top axis
        grid = GridPlot(children=kids, toolbar=toolbar, toolbar_location="right")

        b_first = Button(label="⏮", width=40)
        b_prev = Button(label="◀", width=40)
        page_div = Div(text="", width=150, styles={"color": fg, "text-align": "center",
                                                    "padding-top": "6px"})
        b_next = Button(label="▶", width=40)
        b_last = Button(label="⏭", width=40)
        sel_rows = Select(title="Rows", value=str(self._rows),
                          options=[str(i) for i in range(1, pr + 1)], width=60)
        sel_cols = Select(title="Cols", value=str(self._cols),
                          options=[str(i) for i in range(1, pc + 1)], width=60)
        sel_scale = Select(title="Scale", value="common", options=["common", "per cell"],
                           width=100)
        _names = {"cell": "per cell", "page": "this page", "grid": "whole grid"}
        sel_colour = Select(title="Colour", value=self._colour or "cell",
                            options=[(c, _names.get(c, c)) for c in self._colours] or
                            [("cell", "per cell")], width=100,
                            visible=bool(self._colours),
                            description="One colour range for each cell, for the cells "
                                        "on this page, or for every cell in the grid "
                                        "(found in the background)")
        b_play = Button(label="Play", width=70, button_type="success")
        sel_speed = Select(title="Pages/s", value="1", options=["0.5", "1", "2", "4", "8"],
                           width=70)
        cb_loop = CheckboxGroup(labels=["Loop"], active=[0])
        b_save = Button(label="Save PNG", width=90)
        # the controls' titles in the theme's text colour (restyle changes it)
        label_css = InlineStyleSheet(css=_LABEL_CSS % fg)
        for w in (sel_rows, sel_cols, sel_scale, sel_colour, sel_speed, cb_loop, *self._extra):
            w.stylesheets = list(w.stylesheets) + [label_css]
        status = Div(text="", width=170, styles={"color": fg})
        # The cursor readout above the grid, two lines high whatever it says:
        # below it, it moved with the grid's height and (when Bokeh had not
        # caught up with a reshape) was drawn over the cells (2026-11-11).
        probe = Div(text="\u00a0", width=1000, height=34, styles={
            "color": fg, "font-family": "monospace", "font-size": "11px",
            "overflow": "hidden", "line-height": "16px"})
        x_name = Div(text=xn, width=self._cols * fw, styles={"color": fg, "text-align": "center",
                                                      "font-weight": "bold"})
        y_name = Div(text=yn, width=22, height=self._rows * fh, styles={
            "color": fg, "writing-mode": "vertical-rl", "transform": "rotate(180deg)",
            "text-align": "center", "font-weight": "bold"})

        args = dict(pipe=self._pipe, probe_pipe=self._probe_pipe, ids=self._ids, figs=figs,
                    sources=sources, states=states, grid=grid, page_div=page_div,
                    status=status, probe=probe, sel_rows=sel_rows, sel_cols=sel_cols,
                    sel_scale=sel_scale, sel_colour=sel_colour, b_play=b_play, sel_speed=sel_speed, cb_loop=cb_loop,
                    x_name=x_name, y_name=y_name, pool=[pr, pc], frame=[fw, fh],
                    cell=list(self._cell), area=list(self._area) if self._area else None,
                    min_frame=list(MIN_FRAME), autoload=self._autoload, label_css=label_css,
                    label_tpl=_LABEL_CSS, idle_text=self._idle_text,
                    drill_js=self._drill_js, key=self._ids["page"])
        args["fit_to"] = None
        if self._app_context is not None:
            self._app_context.add_init_script(code=_GRID_JS, args=args, description="grid view")
            self._init_cb = self._app_context.init_scripts[-1][0]
        else:
            self._init_cb = CustomJS(args=args, code=_GRID_JS)
            curdoc().js_on_event(DocumentReady, self._init_cb)

        def call(fn, extra=""):
            return CustomJS(args={"key": self._ids["page"]},
                            code=f"const g = window.__cvGrid && window.__cvGrid[key]; "
                                 f"if (g) g.{fn}({extra});")
        b_first.js_on_click(call("go", "0"))
        b_prev.js_on_click(call("step", "-1"))
        b_next.js_on_click(call("step", "1"))
        b_last.js_on_click(call("go", "Infinity"))
        sel_rows.js_on_change("value", call("reshape"))
        sel_cols.js_on_change("value", call("reshape"))
        sel_scale.js_on_change("value", call("rescale"))
        sel_colour.js_on_change("value", call("recolour"))
        sel_speed.js_on_change("value", call("respeed"))
        b_play.js_on_click(call("togglePlay"))
        b_save.js_on_click(call("save"))
        on_move = call("onMove", "cb_obj")
        on_tap = call("onTap", "cb_obj")
        on_ranges = call("onRanges", "cb_obj")
        on_reset = call("onReset", "cb_obj")
        for f in figs:
            f.js_on_event("mousemove", on_move)
            f.js_on_event("tap", on_tap)
            f.js_on_event("rangesupdate", on_ranges)
            f.js_on_event("reset", on_reset)

        #: the controls, for an application that places them itself
        #: (``embedded``): ``nav`` (paging), ``play`` (Play, Pages/s, Loop),
        #: ``shape`` (Rows, Cols, Scale), ``colour``, ``status``, ``probe``, ``save``
        self.widgets = dict(nav=[b_first, b_prev, page_div, b_next, b_last],
                            play=[b_play, sel_speed, cb_loop],
                            shape=[sel_rows, sel_cols, sel_scale], colour=sel_colour,
                            status=status, probe=probe, save=b_save)
        plot = row(y_name, column(x_name, grid))
        if self._embedded:
            self._models = dict(args)
            return column(plot, styles={"background-color": bg} if dark else {})
        controls = row(*self._extra, b_first, b_prev, page_div, b_next, b_last, sel_rows,
                       sel_cols, sel_scale, sel_colour, b_play, sel_speed, cb_loop, b_save)
        self._models = dict(args, controls=controls)
        return column(controls, row(status, probe), plot,
                      styles={"background-color": bg} if dark else {})


#: the grid's control titles ("Rows", "Cols", ...): their colour, per theme
_LABEL_CSS = ".bk-input-group label, label, .bk-clearfix { color: %s !important; }"

# One namespace per view (window.__cvGrid[key]); everything the buttons and
# the cells' events call.  Args: see GridView._build.
_GRID_JS = r"""
window.__cvGrid = window.__cvGrid || {};
if (window.__cvGrid[key]) return;
const S = {page: 0, nPages: 1, nCells: 0, rows: +sel_rows.value, cols: +sel_cols.value,
           common: sel_scale.value === 'common', colour: sel_colour.value,
           viewport: null, playing: null, colourTimer: null,
           cache: new Map(), cacheKey: '', loading: false, syncing: false, zoomTimer: null,
           extent: null, gen: 0, frame: [frame[0], frame[1]], xEach: false, yEach: false};
const PR = pool[0], PC = pool[1];
const AXIS0 = Object.assign({}, states[0].data);
// a 1 x 1 image as Python sent it (a 2-D array, which image_rgba needs):
// what a blank cell shows
const BLANK = sources[0].data.image[0];

function send(p, id, msg) {
    return new Promise((res) => p.send(id, msg, (r) => res(r)));
}
function shape() { return {rows: S.rows, cols: S.cols, common: S.common, colour: S.colour,
                           viewport: S.viewport, w: S.frame[0], h: S.frame[1]}; }
function keyOf() { return JSON.stringify([S.rows, S.cols, S.common, S.colour, S.viewport, S.frame]); }
// each cell's plot area: *cell*, or the page's share of *area* (as
// GridView.frame_for computes it)
// The area the cells share: *fit_to*'s room below and right of the grid's
// top-left corner as the browser lays it out, else *area*.
function areaNow() {
    if (!fit_to || !window.Bokeh) return area;
    try {
        const box = Bokeh.index.find_one_by_id(fit_to.id);
        const gv = Bokeh.index.find_one_by_id(grid.id);
        if (!box || !gv) return area;
        const b = box.el.getBoundingClientRect(), g = gv.el.getBoundingClientRect();
        const w = Math.floor(b.right - g.left - 12), h = Math.floor(b.bottom - g.top - 12);
        if (w < 200 || h < 150) return area;          // hidden, or not laid out yet
        return [w, h];
    } catch (e) { return area; }
}
function frameFor(rows, cols, common) {
    const area = areaNow();
    if (!area) return [cell[0], cell[1]];
    const yAll = !common || S.yEach, xAll = !common || S.xEach;
    const w = Math.floor((area[0] - 36 - (yAll ? cols * 46 : 36) - cols * 8) / Math.max(1, cols));
    const h = Math.floor((area[1] - (xAll ? rows * 24 : 24) - rows * 26) / Math.max(1, rows));
    return [Math.max(min_frame[0], w), Math.max(min_frame[1], h)];
}
// the panels' tick rules (VisibilityPlotter's cvIntegerTicks): ticks at
// whole positions on an axis of names, base 60 on a UTC time axis, every
// name when there are few, long names along x written upright
function setTicks(f, d) {
    const utcMode = (d['time_labels'] || ['elapsed'])[0] === 'utc';
    const fix = (axes, cat, isTime) => {
        for (const a of (axes || [])) {
            const t = a && a.ticker;
            if (!t || t.min_interval === undefined) continue;
            const names = (cat && cat[0]) ? cat[0] : null;
            const want = names ? 1 : 0;
            if (t.min_interval !== want) t.min_interval = want;
            if (t.base !== undefined) {
                const utc = utcMode && isTime && isTime[0];
                const b = utc ? 60 : 10;
                if (t.base !== b) { t.base = b; t.mantissas = utc ? [1, 2, 5, 10, 15, 20, 30] : [1, 2, 5]; }
            }
            if (t.desired_num_ticks !== undefined) {
                const n = names ? names.length : 0;
                const dn = (n > 0 && n <= 12) ? n : 5;
                if (t.desired_num_ticks !== dn) t.desired_num_ticks = dn;
            }
        }
    };
    const xs = [...(f.below || []), ...(f.above || [])].filter(a => a.type && a.type.includes('Axis'));
    const ys = [...(f.left || []), ...(f.right || [])].filter(a => a.type && a.type.includes('Axis'));
    fix(xs, d['x_cat'], d['x_is_time']);
    fix(ys, d['y_cat'], d['y_is_time']);
    const xc = (d['x_cat'] && d['x_cat'][0]) ? d['x_cat'][0] : null;
    const up = (xc && xc.length > 0 && xc.length <= 12 && Math.max(...xc.map(s => String(s).length)) > 3);
    for (const a of xs) {
        const o = up ? 'vertical' : 'horizontal';
        if (a.major_label_orientation !== o) a.major_label_orientation = o;
    }
}
function fetchPage(page) {
    const k = keyOf();
    if (S.cacheKey !== k) { S.cache.clear(); S.cacheKey = k; }
    if (S.cache.has(page)) return S.cache.get(page);
    const pr = send(pipe, ids.page, Object.assign({page: page}, shape()));
    S.cache.set(page, pr);
    return pr;
}
function setAxes() {
    // common scale: X across the top of the top row, Y down the left of
    // the first column; per cell: every cell its own
    for (let r = 0; r < S.rows; r++)
        for (let c = 0; c < S.cols; c++) {
            const f = figs[r * PC + c];
            const xa = f.above.find(a => a.type && a.type.includes('Axis'));
            const ya = f.left.find(a => a.type && a.type.includes('Axis'));
            // an axis whose cells name their positions differently (a
            // Baseline axis in a grid by antenna) has its labels on every cell
            if (xa) xa.visible = !S.common || r === 0 || S.xEach;
            if (ya) ya.visible = !S.common || c === 0 || S.yEach;
        }
    x_name.width = S.cols * S.frame[0];
    y_name.height = S.rows * S.frame[1];
}
function regrid() {
    S.frame = frameFor(S.rows, S.cols, S.common);
    const kids = [];
    for (let r = 0; r < S.rows; r++)
        for (let c = 0; c < S.cols; c++) {
            const f = figs[r * PC + c];
            if (f.frame_width !== S.frame[0]) f.frame_width = S.frame[0];
            if (f.frame_height !== S.frame[1]) f.frame_height = S.frame[1];
            kids.push([f, r, c]);
        }
    grid.children = kids;
    setAxes();
}
function blank(f, src) {
    src.data = {image: [BLANK], x: [0], y: [0], dw: [1], dh: [1]};
    f.title.text = '\u00a0';
}
function apply(resp, page) {
    if (!resp || resp.cells == null) return;
    S.nPages = resp.n_pages; S.nCells = resp.n_cells; S.extent = resp.extent;
    if (!resp.partial) S.page = resp.page;
    x_name.text = resp.x_name; y_name.text = resp.y_name;
    const per = S.rows * S.cols, filled = new Set();
    S.syncing = true;
    try {
        for (const c of resp.cells) {
            const slot = c.index - S.page * per;
            if (slot < 0 || slot >= per) continue;
            const r = Math.floor(slot / S.cols), col = slot % S.cols;
            const f = figs[r * PC + col], src = sources[r * PC + col];
            filled.add(r * PC + col);
            f.title.text = c.title;
            if (c.axes) { states[r * PC + col].data = c.axes; setTicks(f, c.axes); }
            if (c.image != null)
                src.data = {image: [c.image], x: [c.x0], y: [c.y0], dw: [c.x1 - c.x0], dh: [c.y1 - c.y0]};
            else { src.data = {image: [BLANK], x: [c.x0], y: [c.y0], dw: [1], dh: [1]};
                   f.title.text = c.title + (c.note ? ' (' + c.note + ')' : ''); }
            if (!resp.partial || !S.common) {
                f.x_range.setv({start: c.x0, end: c.x1});
                f.y_range.setv({start: c.y0, end: c.y1});
            }
        }
        if (!resp.partial)
            for (let r = 0; r < S.rows; r++)
                for (let c = 0; c < S.cols; c++) {
                    const k = r * PC + c;
                    if (!filled.has(k)) blank(figs[k], sources[k]);
                }
        // do the page's cells name their positions alike (png_export's
        // _same_ticks)?  If not, that axis is labelled on every cell
        if (!resp.partial) {
            const cats = (ax) => new Set(resp.cells.filter(c => c.axes)
                .map(c => JSON.stringify(c.axes[ax + '_cat'] ? c.axes[ax + '_cat'][0] : null)));
            const xEach = cats('x').size > 1, yEach = cats('y').size > 1;
            if (xEach !== S.xEach || yEach !== S.yEach) {
                S.xEach = xEach; S.yEach = yEach;
                regrid();
            }
        }
    } finally { S.syncing = false; }
    page_div.text = `page ${S.page + 1} of ${S.nPages}<br><small>${S.nCells} cells</small>`;
}
async function show(page, why) {
    page = Math.max(0, Math.min(page, S.nPages - 1));
    const gen = ++S.gen;
    status.text = (why || 'drawing') + '…';
    const t0 = performance.now();
    S.loading = true;
    let resp;
    try { resp = await fetchPage(page); } finally { if (gen === S.gen) S.loading = false; }
    if (gen !== S.gen) return;          // superseded
    if (resp && resp.error) {
        S.cache.clear();
        status.text = '<span style="color:#e06c75">' + resp.error + '</span>';
        return;
    }
    apply(resp, page);
    status.text = `drawn in ${(performance.now() - t0).toFixed(0)} ms`;
    const col = resp.colour || {};
    if (col.pending) {
        // the whole grid's range is still being found: this page was
        // coloured by its own range; ask again until it is known
        S.cache.clear();
        status.text += '<br><span style="opacity:0.7">' + col.note + '</span>';
        clearTimeout(S.colourTimer);
        S.colourTimer = setTimeout(() => {
            if (gen === S.gen && S.colour === 'grid' && !S.loading) show(S.page, 'colouring');
        }, 1500);
    }
    if (S.playing) prefetch();
}
function prefetch() {
    // playback: the next two pages on their way while this one shows
    for (let k = 1; k <= 2; k++) {
        let p = S.page + k;
        if (p >= S.nPages) { if (cb_loop.active.includes(0)) p -= S.nPages; else break; }
        fetchPage(p);
    }
}
const G = {
    go(page) { if (page === Infinity) page = S.nPages - 1; show(page); },
    step(d) {
        // playback does not overtake itself: a tick while a page is still
        // on its way is skipped (the next pages are prefetched meanwhile)
        if (S.playing && S.loading) return;
        let p = S.page + d;
        if (p < 0 || p >= S.nPages) {
            if (S.playing && cb_loop.active.includes(0)) p = (p + S.nPages) % S.nPages;
            else { if (S.playing) G.togglePlay(); return; }
        }
        show(p);
    },
    reshape() {
        const first = S.page * S.rows * S.cols;          // keep the first cell in view
        S.rows = +sel_rows.value; S.cols = +sel_cols.value;
        regrid();
        S.nPages = Math.max(1, Math.ceil(S.nCells / (S.rows * S.cols)));
        show(Math.floor(first / (S.rows * S.cols)), 'reshaping');
    },
    recolour() { S.colour = sel_colour.value; clearTimeout(S.colourTimer); show(S.page, 'colouring'); },
    rescale() { S.common = sel_scale.value === 'common'; S.viewport = null; regrid(); show(S.page, 'rescaling'); },
    restyle(dark) {
        // the application's Light / Dark toggle: the chrome now (the cells'
        // pixels follow with redraw, once Python has the new palettes)
        const fg = dark ? '#d8d8d8' : '#333333', bg = dark ? '#1e1e1e' : '#ffffff';
        for (const f of figs) {
            f.background_fill_color = bg; f.border_fill_color = bg;
            f.title.text_color = fg;
            for (const a of [...f.above, ...f.left])
                if (a.type && a.type.includes('Axis'))
                    a.setv({major_label_text_color: fg, axis_line_color: fg,
                            major_tick_line_color: fg, minor_tick_line_color: fg});
        }
        for (const d of [page_div, status, probe, x_name, y_name])
            d.styles = Object.assign({}, d.styles, {color: fg});
        label_css.css = label_tpl.replace('%s', fg);
    },
    redraw() {
        // the same page again, drawn anew (new palettes)
        S.cache.clear(); S.cacheKey = '';
        if (S.nCells > 0) show(S.page, 'redrawing');
    },
    refit() {
        // the window or the grid's area changed size: cells to fit, the
        // page drawn again at that size
        const before = JSON.stringify(S.frame);
        regrid();
        if (JSON.stringify(S.frame) !== before && S.nCells > 0) show(S.page, 'resizing');
    },
    reload(why) {
        // the source changed (the application's Plot): page 1, full extent
        clearTimeout(S.zoomTimer);
        if (S.playing) G.togglePlay();
        S.cache.clear(); S.cacheKey = ''; S.viewport = null; S.page = 0; S.nPages = 1;
        S.gen++;
        regrid();
        show(0, why || 'drawing');
    },
    togglePlay() {
        if (S.playing) { clearInterval(S.playing); S.playing = null; b_play.label = 'Play'; return; }
        b_play.label = 'Pause';
        S.playing = setInterval(() => G.step(1), 1000 / (+sel_speed.value));
        prefetch();
    },
    respeed() { if (S.playing) { G.togglePlay(); G.togglePlay(); } },
    async save() {
        status.text = 'writing PNG…';
        const r = await send(pipe, ids.save, Object.assign({page: S.page}, shape()));
        if (!r || !r.png) { status.text = 'PNG failed' + (r && r.error ? ': ' + r.error : ''); return; }
        const a = document.createElement('a');
        a.href = 'data:image/png;base64,' + r.png; a.download = r.name;
        document.body.appendChild(a); a.click(); a.remove();
        status.text = 'saved ' + r.name;
        window.__cvGridSaved = r.name;
    },
    onMove(ev) {
        const slot = ev.origin.tags[0], r = Math.floor(slot / PC), c = slot % PC;
        const index = S.page * S.rows * S.cols + r * S.cols + c;
        if (index >= S.nCells) { probe.text = ' '; return; }
        window.__cvGridProbe = {index: index, x: ev.x, y: ev.y};
        probe_pipe.send(ids.probe, {index: index, x: ev.x, y: ev.y},
                        (resp) => { probe.text = (resp && resp.text) || ' '; });
    },
    onTap(ev) {
        const slot = ev.origin.tags[0], r = Math.floor(slot / PC), c = slot % PC;
        const index = S.page * S.rows * S.cols + r * S.cols + c;
        if (index >= S.nCells) return;
        window.__cvGridTapped = (window.__cvGridTapped || []).concat([index]);
        pipe.send(ids.drill, {index: index}, (resp) => {
            if (resp && resp.text) status.text = resp.text;
            if (resp && drill_js) {
                try { drill_js.execute(resp, {index: index}); }
                catch (e) { console.warn('grid drill-down failed:', e); }
            }
        });
    },
    onRanges(ev) {
        if (S.syncing) return;
        const f = ev.origin, slot = f.tags[0];
        const vp = [f.x_range.start, f.x_range.end, f.y_range.start, f.y_range.end];
        if (S.common) {
            S.syncing = true;
            try {
                for (let r = 0; r < S.rows; r++)
                    for (let c = 0; c < S.cols; c++) {
                        const g = figs[r * PC + c];
                        if (g === f) continue;
                        g.x_range.setv({start: vp[0], end: vp[1]});
                        g.y_range.setv({start: vp[2], end: vp[3]});
                    }
            } finally { S.syncing = false; }
        }
        clearTimeout(S.zoomTimer);
        S.zoomTimer = setTimeout(async () => {
            if (S.common) { S.viewport = vp; show(S.page, 'zooming'); return; }
            const r = Math.floor(slot / PC), c = slot % PC;
            const index = S.page * S.rows * S.cols + r * S.cols + c;
            const resp = await send(pipe, ids.page, Object.assign(
                {page: S.page, indices: [index]}, shape(), {viewport: vp}));
            apply(resp, S.page);
        }, 300);
    },
    onReset(ev) {
        clearTimeout(S.zoomTimer);
        S.viewport = null;
        show(S.page, 'resetting');
    },
    state() { return Object.assign({}, S, {cache: S.cache.size}); },
};
window.__cvGrid[key] = G;
regrid();
if (fit_to) window.addEventListener('resize', () => {
    clearTimeout(S.fitTimer);
    S.fitTimer = setTimeout(() => G.refit(), 250);
});
// The first page waits for the connection to Python: a message sent while
// the websocket is still opening makes cubevis reconnect, which drops the
// opening connection (and the session with it; seen 2026-11-11 when visplot
// starts in Grid).
function connected() {
    try { const m = pipe._mgr; return !m || (m.transport ? m.transport.isConnected() : false); }
    catch (e) { return true; }
}
if (autoload) {
    status.text = 'connecting…';
    (function first(n) {
        if (connected() || n >= 600) show(0, 'drawing');
        else setTimeout(() => first(n + 1), 100);
    })(0);
} else status.text = idle_text || '';
"""
