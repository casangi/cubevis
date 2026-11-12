"""visplot's Grid layout (gridplot G2) live in a browser: a ``VisibilityPlotter``
served as the ``visplot`` task serves it, driven by Playwright.

    python tests/manual/gridplot/visplot_grid_check.py --ms DATA.ms [--chrome PATH]
        [--out DIR] [--preset waterfall] [--start over|grid] [--kernel NAME]

Checks, one line each (PASS/FAIL), and screenshots in DIR:

    load            the page loads (time to the first panel drawn); Layout
                    has four buttons; the grid is hidden -- or, with
                    ``--start grid``, shown and drawn
    grid shown      Layout Grid: the grid instead of the panels, page 1 drawn
                    (no Plot needed)
    plot in grid    Plot: page 1 of the baselines, every cell drawn
    axis names      each once, the panels' own (UTC date in the time axis)
    tick labels     the cells' tick formatter has the panel's axis state
    iterate by      Iterate by Antenna: one cell per antenna, drawn again
    probe           the cursor over a cell: that cell's value
    zoom common     a box zoom in one cell: every cell at that viewport
    drill-down      a tap: the cell in the full panel (One), the sidebar
                    set to its selection
    back to grid    Layout Grid again: the grid, the sidebar as it was
    theme           Light / Dark: the grid's chrome follows, the page is
                    drawn again in the new palette
    preset          a preset pressed in Grid: Grid stays, the grid repeats
                    the preset's panel
    export          Export PNG in Grid writes the page
    console         no errors in the browser console

``--kernel NAME`` reads the data through a remote kernel (``backend="remote"``;
``python3`` is one on this host).

Needs playwright and Chrome/Chromium (``--chrome``: an executable or a macOS
``.app``; without it, the installed Google Chrome).  Any MS works; the
quick tests' synthetic one: ``python tests/ci/make_tiny_ms.py /tmp/ci.ms``.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
sys.path.insert(0, HERE)
from grid_browser_check import chrome_exe          # noqa: E402

SERVE = r"""
import os, sys, warnings, logging
warnings.filterwarnings("ignore")
logging.basicConfig(level=logging.WARNING)
sys.path.insert(0, {root!r})
from uuid import uuid4
from cubevis import exe
from cubevis.toolbox.visplot.visibility_plotter import VisibilityPlotter
p = VisibilityPlotter(ms={ms!r}, preset={preset!r}, layout={start!r}, gridrows=3, gridcols=3,
                      theme="light", **{remote!r})
ctx = exe.Context(exe.Mode.SYNC)
ui, task = p(ctx, uuid4())
ui.show()
print("SERVING", flush=True)
ctx.execute(task, uuid4())
"""

MODELS = "[...Bokeh.documents[0]._all_models.values()]"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--ms", required=True)
    ap.add_argument("--chrome", default=os.environ.get("CHROME"))
    ap.add_argument("--out", default=os.path.join(tempfile.gettempdir(), "visplot_grid_check"))
    ap.add_argument("--preset", default="waterfall")
    ap.add_argument("--start", default="over", choices=["over", "grid"],
                    help="the layout visplot starts in")
    ap.add_argument("--kernel", default=None, help="read through this remote kernel")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    url_file = os.path.join(a.out, "url.txt")
    if os.path.exists(url_file):
        os.unlink(url_file)
    rec = os.path.join(a.out, "record_url.sh")
    with open(rec, "w") as f:
        f.write(f'#!/bin/sh\necho "$1" > {url_file}\n')
    os.chmod(rec, 0o755)
    log = open(os.path.join(a.out, "serve.log"), "w")
    srv = subprocess.Popen(
        [sys.executable, "-c", SERVE.format(root=ROOT, ms=os.path.abspath(a.ms), preset=a.preset,
                                         start=a.start,
                                         remote=(dict(backend="remote", kernel_name=a.kernel)
                                                 if a.kernel else {}))],
        env={**os.environ, "BROWSER": rec}, stdout=log, stderr=subprocess.STDOUT)
    results = []

    def check(name, ok, detail=""):
        results.append(ok)
        print(f"{'PASS' if ok else 'FAIL'}  {name:14s} {detail}", flush=True)

    try:
        t0 = time.time()
        while not os.path.exists(url_file):
            if srv.poll() is not None or time.time() - t0 > 300:
                print(open(os.path.join(a.out, "serve.log")).read()[-3000:])
                raise SystemExit("server did not start")
            time.sleep(0.2)
        url = open(url_file).read().strip()
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            kw = {"executable_path": chrome_exe(a.chrome)} if a.chrome else {"channel": "chrome"}
            b = p.chromium.launch(args=["--no-sandbox", "--allow-file-access-from-files"], **kw)
            ctx = b.new_context(viewport={"width": 1600, "height": 1200}, accept_downloads=True)
            pg = ctx.new_page()
            errs = []
            pg.on("console", lambda m: errs.append(m.text[:300]) if m.type == "error" else None)
            pg.on("pageerror", lambda e: errs.append(str(e)[:300]))
            t_load = t_start = time.time()
            pg.goto(url)
            try:
                pg.wait_for_function("() => !!(window.__cvGrid && Object.keys(window.__cvGrid)"
                                     ".length > 0 && window.__cvGridLayout)", timeout=120000)
            except Exception:
                print("page did not start:", errs[:5], pg.evaluate(
                    "[!!window.__cvGrid, !!window.__cvGridLayout, !!window.Bokeh]"))
                raise
            pg.wait_for_function(f"""() => {MODELS}.filter(m => m.type === 'Div')
                .some(d => d.text.includes('Layout:'))""", timeout=180000)
            t_load = time.time() - t_load
            G = "Object.values(window.__cvGrid)[0]"

            def model(pred):
                return f"{MODELS}.find(m => {pred})"

            def grid_shown():
                return pg.evaluate(f"""() => {{ const g = {model("m.type === 'GridPlot'")};
                    const v = Bokeh.index.find_one_by_id(g.id);
                    return !!v && v.el.getBoundingClientRect().width > 0; }}""")

            RADIO = "m.type === 'RadioButtonGroup' && m.labels.includes('Grid')"
            GRIDPLOT = "m.type === 'GridPlot'"

            def radio():
                return pg.evaluate(f"() => {model(RADIO)}.active")

            def set_radio(i):
                pg.evaluate(f"(i) => {{ {model(RADIO)}.active = i; }}", i)

            def grid_status():
                return pg.evaluate(f"() => {G}.state()")

            def status_div_text(pred):
                return pg.evaluate(f"""() => {MODELS}.filter(m => m.type === 'Div')
                    .map(d => d.text).find(t => {pred})""")

            def set_marker():
                pg.evaluate(f"""() => {{ for (const d of {MODELS})
                    if (d.type === 'Div' && d.text.startsWith('drawn in')) d.text = ''; }}""")

            def wait_drawn(timeout=180000):
                pg.wait_for_function(f"""() => {MODELS}.filter(m => m.type === 'Div')
                    .some(d => d.text.startsWith('drawn in'))""", timeout=timeout)
                pg.wait_for_timeout(300)

            def titles():
                return pg.evaluate(f"""() => {model("m.type === 'GridPlot'")}
                    .children.map(([f]) => f.title.text)""")

            def drawn_cells():
                return pg.evaluate(f"""() => {model("m.type === 'GridPlot'")}.children.map(([f]) => {{
                    const r = f.renderers.find(x => x.data_source);
                    const im = r.data_source.data.image[0]; return im ? (im.length || 0) : 0; }})""")

            def click_button(label):
                pg.evaluate(f"""(l) => {{ const b = {MODELS}.find(m => m.type === 'Button'
                    && m.label === l); for (const cb of b.js_event_callbacks['button_click'])
                    cb.execute(b); }}""", label)

            def select(title, value):
                pg.evaluate(f"""([t, v]) => {{ {MODELS}.find(m => m.type === 'Select'
                    && m.title === t).value = v; }}""", [title, value])

            def cell_xy(r, c, fx=0.5, fy=0.5):
                return pg.evaluate(f"""([r, c, fx, fy]) => {{
                    const g = {model("m.type === 'GridPlot'")};
                    const f = g.children.find(([f, rr, cc]) => rr === r && cc === c)[0];
                    const v = Bokeh.index.find_one_by_id(f.id);
                    const rc = v.el.getBoundingClientRect(), fb = v.frame.bbox;
                    return [rc.left + fb.left + fb.width * fx, rc.top + fb.top + fb.height * fy];
                    }}""", [r, c, fx, fy])

            def tool(name):
                pg.evaluate(f"""(n) => {{ const tb = {MODELS}.filter(m => m.type === 'Toolbar')
                    .find(t => t.tools.some(x => x.type === 'ToolProxy'));
                    tb.tools.find(x => (x.tools ? x.tools[0].type : x.type) === n).active = true; }}""",
                            name)

            labels = pg.evaluate(f"() => {model(RADIO)}.labels")
            if a.start == "grid":
                wait_drawn()
                t_load = time.time() - t_start
                check("load", labels[-1] == "Grid" and grid_shown() and radio() == 3
                      and all(x > 1 for x in drawn_cells()),
                      f"{t_load:.1f} s to page 1 of the grid; Layout {labels}")
            else:
                check("load", labels[-1] == "Grid" and not grid_shown(),
                      f"{t_load:.1f} s to the first panel; Layout {labels}")
            pg.screenshot(path=os.path.join(a.out, "load.png"))

            if a.start != "grid":
                set_marker()
                set_radio(3)
                wait_drawn()
            check("grid shown", grid_shown() and radio() == 3 and all(x > 1 for x in drawn_cells()),
                  "Grid: the grid, no panels, page 1 drawn without pressing Plot")

            set_marker()
            t = time.time()
            click_button("Plot ▶")
            wait_drawn()
            st, tt, d = grid_status(), titles(), drawn_cells()
            ok = (st["nCells"] > 9 and len(tt) == 9 and all("-" in x for x in tt)
                  and all(x > 1 for x in d))
            check("plot in grid", ok, f"{st['nCells']} baselines, page 1 of {st['nPages']} "
                                      f"in {time.time() - t:.1f} s; first {tt[:2]}")
            pg.screenshot(path=os.path.join(a.out, "grid_baselines.png"))

            names = pg.evaluate(f"""() => {MODELS}.filter(m => m.type === 'Div'
                && (m.styles || {{}})['font-weight'] === 'bold').map(d => d.text)""")
            ok = any("UTC" in n for n in names) and len([n for n in names if n]) >= 2
            check("axis names", ok, f"{names}")

            ax = pg.evaluate(f"""() => {{ const f = {model("m.type === 'GridPlot'")}.children[0][0];
                const ya = f.left.find(a => a.formatter), xa = f.above.find(a => a.formatter);
                const s = ya.formatter.args.state.data, x = xa.formatter.args.state.data;
                return [s.y_is_time[0], s.time_labels[0], x.x_is_time[0]]; }}""")
            check("tick labels", ax[0] == 1 and ax[1] == "utc",
                  f"y_is_time {ax[0]}, labels {ax[1]}, x_is_time {ax[2]}")

            set_marker()
            select("Iterate by", "antenna")
            wait_drawn()
            st, tt = grid_status(), titles()
            ok = st["nCells"] >= 3 and "-" not in tt[0] and tt[0] != ""
            check("iterate by", ok, f"{st['nCells']} antennas; first {tt[:3]}")
            pg.screenshot(path=os.path.join(a.out, "grid_antennas.png"))

            x, y = cell_xy(0, 1)
            pg.mouse.move(x - 5, y)
            pg.mouse.move(x, y, steps=3)
            pg.wait_for_timeout(1000)
            text = status_div_text("t.startsWith('<b>' + " + repr(tt[1]) + ")")
            check("probe", bool(text) and ":" in text, f"{(text or '')[:110]!r}")

            set_marker()
            tool("BoxZoomTool")
            x, y = cell_xy(1, 1, 0.2, 0.2)
            x2, y2 = cell_xy(1, 1, 0.6, 0.7)
            pg.mouse.move(x, y); pg.mouse.down(); pg.mouse.move(x2, y2, steps=6); pg.mouse.up()
            wait_drawn()
            rngs = pg.evaluate(f"""() => [...new Set({model("m.type === 'GridPlot'")}.children
                .map(([f]) => [f.x_range.start, f.x_range.end, f.y_range.start, f.y_range.end]
                .map(v => v.toPrecision(6)).join(',')))]""")
            check("zoom common", len(rngs) == 1 and grid_status()["viewport"] is not None,
                  f"one viewport: {rngs[0]}")
            pg.screenshot(path=os.path.join(a.out, "grid_zoom.png"))
            click_button("⏮")

            tool("TapTool")
            pg.mouse.click(*cell_xy(0, 1))
            pg.wait_for_function(f"""() => {MODELS}.filter(m => m.type === 'Div')
                .some(d => d.text.includes('Layout: One panel'))""", timeout=180000)
            pg.wait_for_timeout(800)
            sel = pg.evaluate(f"""() => {MODELS}.filter(m => m.type === 'ColumnDataSource'
                && m.data.name && m.data.has_data && !m.data.ant1)
                .map(s => [s.data.name[s.selected.indices[0]], s.selected.indices.length])""")
            ok = radio() == 0 and not grid_shown() and any(s[0] == tt[1] and s[1] == 1 for s in sel)
            check("drill-down", ok, f"Layout {radio()}, antenna ticked {sel}")
            pg.screenshot(path=os.path.join(a.out, "drill_down.png"))

            set_radio(3)
            pg.wait_for_timeout(600)
            after = pg.evaluate(f"""() => {MODELS}.filter(m => m.type === 'ColumnDataSource'
                && m.data.name && m.data.has_data && !m.data.ant1)
                .map(s => Array.from(s.selected.indices))""")
            ok = grid_shown() and all(len(x) == 0 for x in after) and titles()[0] == tt[0]
            check("back to grid", ok, f"selection restored {after}; page kept: {titles()[:2]}")

            set_marker()
            before = pg.evaluate(f"() => {model(GRIDPLOT)}.children[0][0].background_fill_color")
            pg.evaluate(f"""() => {{ const t = {MODELS}.find(m => m.type === 'Toggle'
                && /Dark|Light/.test(m.label)); t.active = !t.active; }}""")
            wait_drawn()
            after = pg.evaluate(f"() => {model(GRIDPLOT)}.children[0][0].background_fill_color")
            check("theme", before != after and after in ("#1e1e1e", "#ffffff"),
                  f"cell background {before} -> {after}, page drawn again")
            pg.screenshot(path=os.path.join(a.out, "theme.png"))

            set_marker()
            click_button("vplot")
            wait_drawn()
            names = pg.evaluate(f"""() => {MODELS}.filter(m => m.type === 'Div'
                && (m.styles || {{}})['font-weight'] === 'bold').map(d => d.text)""")
            yall = pg.evaluate(f"""() => {model(GRIDPLOT)}.children.every(([f]) =>
                f.left.some(a => a.type && a.type.includes('Axis') && a.visible))""")
            check("preset", radio() == 3 and grid_shown() and any("Baseline" in n for n in names)
                  and yall, f"vplot in Grid: axis names {names}; each antenna's baselines "
                            f"labelled on every cell: {yall}")
            pg.screenshot(path=os.path.join(a.out, "preset_vplot.png"))

            with pg.expect_download(timeout=120000) as dl:
                click_button("Export PNG")
            path = os.path.join(a.out, dl.value.suggested_filename)
            dl.value.save_as(path)
            ok = os.path.getsize(path) > 10000 and open(path, "rb").read(8) == b"\x89PNG\r\n\x1a\n"
            check("export", ok, f"{dl.value.suggested_filename}, {os.path.getsize(path) // 1024} KB")
            check("console", not errs, "; ".join(errs[:3]))
            b.close()
    finally:
        srv.terminate()
        try:
            srv.wait(timeout=10)
        except Exception:                                    # noqa: BLE001
            srv.kill()
        log.close()
    n_fail = results.count(False)
    print(f"{len(results) - n_fail} of {len(results)} passed; screenshots in {a.out}")
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())
