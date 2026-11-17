"""G1b prototype: one toolbar for a grid of lightweight image cells.

Answers the questions the grid view is built on (plan section 9.2 and 9.3,
``devel/docs/gridplot/grid_and_calplot_plan.md``) before it is built:

1. does ``gridplot(merge_tools=True)`` give *one* toolbar -- one button per
   tool -- for N cells, cubevis's own tools (``InfoTool``, ``FlagTool``)
   included, and what must be true of the tools for that;
2. does every tool's event know which cell it happened in (tap, mouse
   move, box zoom) when the tools are identical across cells;
3. does one crosshair tool, shared by every cell, draw in all of them;
4. what a page switch costs in the browser -- new RGBA images written into
   every cell's source -- for 9, 16, 36 and 64 cells; and what building
   and loading a 64-cell pool costs;
5. can rows x cols change in the browser, without a server, from a pool
   built once: by hiding cells, or by giving the grid new children.

A static page (no kernel): what is in question is the browser's side.  The
images are made in the browser (as if they had arrived over the comm), so
the measurement is Bokeh's, not the wire's.

    python devel/tools/gridplot_proto/proto_g1b.py --open        # look and try, by hand
    python devel/tools/gridplot_proto/proto_g1b.py [--out DIR] [--chrome PATH]   # automated

``--open`` writes the page and opens it in the default browser: no
playwright.  Hover and tap in cells (the line above the grid says which
cell), use the one toolbar, and press **Measure** for the page-switch and
rows x cols timings, which the page shows itself.  Without ``--open`` the
same checks run unattended (playwright drives a real mouse) and the
results are printed.

Writes ``DIR/proto_g1b.html``, screenshots, and ``DIR/results.json``; prints
a summary.  Needs ``pip install playwright`` and a Chrome or Chromium:
``--chrome`` names one (an executable, or a macOS ``.app`` bundle such as
``"/Applications/Google Chrome.app"``); without it, the installed Google
Chrome is used (playwright's ``channel="chrome"``), and failing that
playwright's own (``playwright install chromium``).  The page alone needs
neither: open ``proto_g1b.html`` in a browser.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import warnings

warnings.filterwarnings("ignore")

POOL_ROWS = POOL_COLS = 8          # the pool: the plan's interactive cap (9.3)
CELL_W, CELL_H = 180, 140
IMG_W, IMG_H = 120, 90             # image pixels per cell


def build(path: str) -> dict:
    import numpy as np
    import cubevis                                         # noqa: F401 -- its JS bundle
    from bokeh.embed import file_html
    from bokeh.layouts import column, gridplot, row
    from bokeh.models import (BoxZoomTool, Button, ColumnDataSource, CrosshairTool, CustomJS,
                              Div, HoverTool, PanTool, ResetTool, Span, TapTool, WheelZoomTool)
    from bokeh.plotting import figure
    from bokeh.resources import Resources
    from cubevis.bokeh.tools._flag_tool import FlagTool
    from cubevis.bokeh.tools._info_tool import InfoTool

    t0 = time.perf_counter()
    n = POOL_ROWS * POOL_COLS
    probe = Div(text="probe: -", width=600)
    tapped = Div(text="tap: -", width=600)
    zoomed = Div(text="range: -", width=600)

    # Identical tools on every cell: only then does merge_tools make one
    # button of them.  The cell is found from the event's origin (the
    # figure), never from the tool -- a per-cell argument would make the
    # tools differ and give one button per cell.
    on_move = CustomJS(args={"probe": probe}, code="""
        const f = cb_obj.origin;
        probe.text = `probe: cell ${f.tags[0]} x=${cb_obj.x.toFixed(2)} y=${cb_obj.y.toFixed(2)}`;
        window.__probe = {cell: f.tags[0], x: cb_obj.x, y: cb_obj.y};
    """)
    on_tap = CustomJS(args={"tapped": tapped}, code="""
        const f = cb_obj.origin;
        tapped.text = `tap: cell ${f.tags[0]}`;
        window.__tapped = (window.__tapped || []).concat([f.tags[0]]);
    """)
    on_range = CustomJS(code="""
        const f = cb_obj.origin;
        window.__ranges = (window.__ranges || []).concat([{cell: f.tags[0],
            x0: cb_obj.x0, x1: cb_obj.x1}]);
    """)
    width_span = Span(dimension="width", line_dash="dotted", line_color="orange")
    height_span = Span(dimension="height", line_dash="dotted", line_color="orange")
    crosshair = CrosshairTool(overlay=[width_span, height_span])   # ONE instance, every cell

    common_x = None
    common_y = None
    figs, sources = [], []
    rng = np.random.default_rng(1)
    for i in range(n):
        kw = {}
        if common_x is not None:
            kw = dict(x_range=common_x, y_range=common_y)   # common scale: shared ranges
        f = figure(width=CELL_W, height=CELL_H, title=f"cell {i}", tools="", tags=[i],
                   toolbar_location=None, min_border=2, **kw)
        if common_x is None:
            common_x, common_y = f.x_range, f.y_range
        img = (rng.random((IMG_H, IMG_W)) * 0xFFFFFF).astype(np.uint32) | np.uint32(0xFF000000)
        src = ColumnDataSource({"image": [img]})
        f.image_rgba(image="image", x=0, y=0, dw=10, dh=10, source=src)
        f.add_tools(PanTool(), WheelZoomTool(), BoxZoomTool(), ResetTool(), TapTool(),
                    HoverTool(tooltips=None), crosshair,
                    InfoTool(), FlagTool(flag=True), FlagTool(flag=False))
        f.js_on_event("mousemove", on_move)
        f.js_on_event("tap", on_tap)
        f.js_on_event("rangesupdate", on_range)
        f.xaxis.visible = f.yaxis.visible = (i % POOL_COLS == 0)   # outer axes only (sketch)
        figs.append(f)
        sources.append(src)
    t_figs = time.perf_counter() - t0

    grid = gridplot(figs, ncols=POOL_COLS, merge_tools=True, toolbar_location="above")
    grid.toolbar.autohide = False

    # Page switch: new RGBA images into every visible cell's source, timed
    # to the second animation frame after (the paint).
    page_js = CustomJS(args={"sources": sources, "figs": figs}, code="""
        window.__page = function(ncells, seed) {
            const W = %d, H = %d;
            const t0 = performance.now();
            for (let i = 0; i < ncells; i++) {
                const a = new Uint32Array(W * H);
                let s = (seed * 7919 + i * 104729) >>> 0;
                for (let k = 0; k < a.length; k++) {
                    s = (s * 1664525 + 1013904223) >>> 0;
                    a[k] = 0xFF000000 | (s >>> 8);
                }
                const img = new Uint32Array(a.buffer);
                sources[i].data = {image: [ndarray(img, H, W)]};
                figs[i].title.text = `page ${seed} cell ${i}`;
            }
            const t1 = performance.now();
            return new Promise(res => requestAnimationFrame(() => requestAnimationFrame(() =>
                res({write_ms: t1 - t0, paint_ms: performance.now() - t0}))));
        };
        function ndarray(buf, h, w) {
            // a Bokeh ndarray: a typed array with dtype and shape
            buf.dtype = "uint32"; buf.shape = [h, w]; return buf;
        }
        // rows x cols in the browser: (a) hide cells; (b) new grid children
        window.__showByVisible = function(rows, cols) {
            const t0 = performance.now();
            for (let i = 0; i < figs.length; i++) {
                const r = Math.floor(i / %d), c = i %% %d;
                figs[i].visible = r < rows && c < cols;
            }
            return performance.now() - t0;
        };
    """ % (IMG_W, IMG_H, POOL_COLS, POOL_COLS))
    gp = grid
    regrid_js = CustomJS(args={"grid": gp, "figs": figs}, code="""
        window.__showByChildren = function(rows, cols) {
            // GridPlot children: [plot, row, col] triples
            const t0 = performance.now();
            const kids = [];
            for (let r = 0; r < rows; r++)
                for (let c = 0; c < cols; c++) {
                    const f = figs[r * %d + c];
                    f.visible = true;
                    kids.push([f, r, c]);
                }
            grid.children = kids;
            return performance.now() - t0;
        };
    """ % POOL_COLS)
    report = Div(text="", width=900)
    measure = Button(label="Measure", button_type="primary", width=120)
    measure.js_on_click(CustomJS(args={"report": report}, code="""
        (async () => {
            report.text = 'measuring...';
            const rows = [];
            for (const n of [9, 16, 36, 64]) {
                const t = [];
                for (let s = 1; s <= 5; s++) t.push(await window.__page(n, s + n));
                t.sort((a, b) => a.paint_ms - b.paint_ms);
                rows.push(`<tr><td>page switch, ${n} cells</td><td>${t[2].paint_ms.toFixed(0)} ms</td></tr>`);
            }
            for (const [r, c] of [[3, 3], [4, 4], [6, 6], [8, 8]]) {
                const ms = await new Promise(res => { const t0 = performance.now();
                    window.__showByChildren(r, c);
                    requestAnimationFrame(() => requestAnimationFrame(() => res(performance.now() - t0))); });
                rows.push(`<tr><td>rows x cols to ${r}x${c}</td><td>${ms.toFixed(0)} ms</td></tr>`);
                await new Promise(res => setTimeout(res, 300));
            }
            report.text = '<table>' + rows.join('') + '</table>';
        })();
    """))
    init = Button(label="(init)", visible=False)
    from bokeh.events import DocumentReady
    from bokeh.io import curdoc
    layout = column(row(probe, tapped), row(measure, report), grid, init)
    doc = curdoc()
    doc.clear()
    doc.add_root(layout)
    doc.js_on_event(DocumentReady, page_js)
    doc.js_on_event(DocumentReady, regrid_js)
    html = file_html(doc, Resources(mode="absolute"), "G1b prototype")
    with open(path, "w") as f:
        f.write(html)
    from bokeh.layouts import group_tools  # noqa: F401 -- for the record below
    tool_buttons = [type(t).__name__ + ("x%d" % len(t.tools) if hasattr(t, "tools") else "")
                    for t in grid.toolbar.tools]
    return {"cells": n, "python_build_s": t_figs, "python_total_s": time.perf_counter() - t0,
            "html_bytes": len(html), "toolbar_python": tool_buttons}


def chrome_executable(chrome: str | None) -> str | None:
    """*chrome* as an executable: a macOS ``.app`` bundle becomes the program
    inside it (``X.app/Contents/MacOS/X``)."""
    if not chrome:
        return None
    chrome = os.path.expanduser(chrome.rstrip("/"))
    if chrome.endswith(".app") and os.path.isdir(chrome):
        name = os.path.basename(chrome)[:-4]
        inner = os.path.join(chrome, "Contents", "MacOS", name)
        if not os.path.exists(inner):
            macos = os.path.join(chrome, "Contents", "MacOS")
            found = sorted(os.listdir(macos)) if os.path.isdir(macos) else []
            if not found:
                raise SystemExit(f"--chrome {chrome!r}: no program in {macos}")
            inner = os.path.join(macos, found[0])
        return inner
    if not os.path.exists(chrome):
        raise SystemExit(f"--chrome {chrome!r} does not exist")
    return chrome


def launch(p, chrome: str | None):
    args = ["--no-sandbox", "--allow-file-access-from-files"]
    exe = chrome_executable(chrome)
    if exe:
        return p.chromium.launch(executable_path=exe, args=args)
    try:
        return p.chromium.launch(channel="chrome", args=args)     # the installed Google Chrome
    except Exception as exc:                                       # noqa: BLE001 -- try the next
        first = str(exc).splitlines()[0]
    try:
        return p.chromium.launch(args=args)                        # playwright's own
    except Exception as exc:                                       # noqa: BLE001 -- said
        raise SystemExit(
            "no browser to drive: installed Google Chrome not found "
            f"({first}), and playwright's own Chromium not installed "
            f"({str(exc).splitlines()[0]}).  Pass --chrome PATH, or run "
            "'playwright install chromium'.")


def drive(path: str, outdir: str, chrome: str | None) -> dict:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise SystemExit(
            f"playwright is not installed: 'pip install playwright' (no browser download "
            f"needed with --chrome or an installed Google Chrome).  The page is written: "
            f"open {path} in a browser to try it by hand.")
    res = {}
    with sync_playwright() as p:
        b = launch(p, chrome)
        pg = b.new_page(viewport={"width": 1700, "height": 1300})
        errs = []
        pg.on("console", lambda m: errs.append(f"{m.type}: {m.text[:200]}")
              if m.type in ("error", "warning") else None)
        t0 = time.perf_counter()
        pg.goto("file://" + os.path.abspath(path))
        pg.wait_for_function("window.__page !== undefined && Bokeh.documents.length > 0",
                             timeout=120000)
        pg.wait_for_function("document.querySelectorAll('.bk-Figure, .bk-Plot').length > 0 || true")
        pg.wait_for_timeout(500)
        res["load_s"] = time.perf_counter() - t0

        # 1. the toolbar as the browser has it
        res["toolbar_browser"] = pg.evaluate("""() => {
            const tb = [...Bokeh.documents[0]._all_models.values()]
                .filter(m => m.type === 'Toolbar');
            const g = tb.find(t => t.tools.some(x => x.type === 'ToolProxy')) || tb[0];
            return g.tools.map(t => t.type === 'ToolProxy'
                ? 'ToolProxy(' + t.tools[0].type.split('.').pop() + ' x' + t.tools.length + ')'
                : t.type.split('.').pop());
        }""")
        res["toolbar_buttons_dom"] = pg.evaluate(
            "document.querySelectorAll('.bk-ToolbarButton, .bk-tool-icon').length")
        pg.screenshot(path=os.path.join(outdir, "pool_8x8.png"))

        def cell_center(i):
            return pg.evaluate("""(i) => {
                const f = [...Bokeh.documents[0]._all_models.values()]
                    .find(m => m.type === 'Figure' && m.tags && m.tags[0] === i);
                const v = Bokeh.index.find_one_by_id(f.id);
                const r = v.el.getBoundingClientRect(), fb = v.frame.bbox;
                return [r.left + fb.left + fb.width / 2, r.top + fb.top + fb.height / 2];
            }""", i)

        def click_tool(name_part):
            # the merged toolbar's button for a tool type
            ok = pg.evaluate("""(part) => {
                const tb = [...Bokeh.documents[0]._all_models.values()]
                    .filter(m => m.type === 'Toolbar')
                    .find(t => t.tools.some(x => x.type === 'ToolProxy'));
                const t = tb.tools.find(x => (x.type === 'ToolProxy' ? x.tools[0].type : x.type)
                                             .includes(part));
                if (!t) return false;
                t.active = true;
                return true;
            }""", name_part)
            return ok

        # 2. events know their cell: real mouse
        hits = {}
        for i in (0, 9, 27, 63):
            x, y = cell_center(i)
            pg.mouse.move(x, y)
            pg.wait_for_timeout(100)
            hits[i] = pg.evaluate("window.__probe && window.__probe.cell")
        res["mousemove_cell"] = hits
        res["tap_tool_activated"] = click_tool("TapTool")
        for i in (5, 42):
            x, y = cell_center(i)
            pg.mouse.click(x, y)
            pg.wait_for_timeout(150)
        res["tap_cells"] = pg.evaluate("window.__tapped || []")
        # box zoom in cell 12: does the zoom reach other cells (shared ranges)?
        res["boxzoom_activated"] = click_tool("BoxZoomTool")
        x, y = cell_center(12)
        pg.mouse.move(x - 30, y - 20)
        pg.mouse.down()
        pg.mouse.move(x, y, steps=5)
        pg.mouse.move(x + 30, y + 20, steps=5)
        pg.mouse.up()
        pg.wait_for_timeout(300)
        ranges = pg.evaluate("window.__ranges || []")
        res["boxzoom_range_events_cells"] = sorted({r["cell"] for r in ranges})[:5] + (
            ["..."] if len({r["cell"] for r in ranges}) > 5 else [])
        res["boxzoom_range_events_count"] = len({r["cell"] for r in ranges})
        res["shared_range_after_zoom"] = pg.evaluate("""() => {
            const fs = [...Bokeh.documents[0]._all_models.values()].filter(m => m.type === 'Figure');
            return [...new Set(fs.map(f => f.x_range.start.toFixed(3) + '..' + f.x_range.end.toFixed(3)))];
        }""")
        # 3. the one crosshair: drawn in how many cells?
        click_tool("CrosshairTool")
        x, y = cell_center(20)
        pg.mouse.move(x, y)
        pg.wait_for_timeout(150)
        res["crosshair_spans"] = pg.evaluate("""() => {
            const ss = [...Bokeh.documents[0]._all_models.values()].filter(m => m.type === 'Span');
            return ss.map(s => ({dim: s.dimension, loc: s.location}));
        }""")
        pg.screenshot(path=os.path.join(outdir, "crosshair.png"))

        # 4. page switch cost
        pg.evaluate("Bokeh.documents[0]._all_models.values().next()")
        sw = {}
        for ncells in (9, 16, 36, 64):
            t = []
            for seed in range(1, 6):
                t.append(pg.evaluate(f"window.__page({ncells}, {seed + ncells})"))
            sw[ncells] = {"write_ms_median": sorted(x["write_ms"] for x in t)[2],
                          "paint_ms_median": sorted(x["paint_ms"] for x in t)[2]}
        res["page_switch"] = sw
        pg.screenshot(path=os.path.join(outdir, "after_page.png"))

        # 5. rows x cols in the browser
        res["show_3x3_by_visible_ms"] = pg.evaluate("window.__showByVisible(3, 3)")
        pg.wait_for_timeout(500)
        res["grid_px_after_visible"] = pg.evaluate("""() => {
            const g = [...Bokeh.documents[0]._all_models.values()].find(m => m.type === 'GridPlot');
            const r = Bokeh.index.find_one_by_id(g.id).el.getBoundingClientRect();
            return [Math.round(r.width), Math.round(r.height)];
        }""")
        pg.screenshot(path=os.path.join(outdir, "show_3x3_visible.png"))
        res["show_4x4_by_children_ms"] = pg.evaluate("window.__showByChildren(4, 4)")
        pg.wait_for_timeout(800)
        res["grid_px_after_children"] = pg.evaluate("""() => {
            const g = [...Bokeh.documents[0]._all_models.values()].find(m => m.type === 'GridPlot');
            const r = Bokeh.index.find_one_by_id(g.id).el.getBoundingClientRect();
            return [Math.round(r.width), Math.round(r.height)];
        }""")
        res["probe_after_regrid"] = None
        x, y = cell_center(9)            # row 1, col 1 of the 4x4 (pool index 9)
        pg.mouse.move(x, y)
        pg.wait_for_timeout(100)
        res["probe_after_regrid"] = pg.evaluate("window.__probe && window.__probe.cell")
        pg.screenshot(path=os.path.join(outdir, "show_4x4_children.png"))
        res["console"] = errs[:20]
        b.close()
    return res


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", default="/tmp/proto_g1b")
    ap.add_argument("--chrome", default=os.environ.get("CHROME"))
    ap.add_argument("--open", action="store_true",
                    help="open the page in the default browser to try by hand (no playwright)")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    path = os.path.join(a.out, "proto_g1b.html")
    built = build(path)
    print("built:", json.dumps(built))
    if a.open:
        import webbrowser
        print(f"opening {path}: hover and tap in cells, use the toolbar, press Measure")
        webbrowser.open("file://" + os.path.abspath(path))
        return 0
    res = drive(path, a.out, a.chrome)
    res["built"] = built
    with open(os.path.join(a.out, "results.json"), "w") as f:
        json.dump(res, f, indent=1, default=str)
    print(json.dumps(res, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
