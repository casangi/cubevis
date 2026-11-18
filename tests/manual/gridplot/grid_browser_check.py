"""The grid view (G1b) live in a browser: a ``GridApp`` of a synthetic
source served as the applications are (``cubevis.exe``, the page from a
file, a websocket to Python), driven by Playwright with a real mouse.

    python tests/manual/gridplot/grid_browser_check.py [--chrome PATH] [--out DIR]
        [--kind raster|scatter] [--n 45] [--cost 0]

Checks, one line each (PASS/FAIL), and screenshots in DIR:

    first page      4 x 4, the first 16 labels, every cell drawn
    paging          next / last: the right labels; the last page partly
                    filled, its empty cells blank
    axes            common scale: X axis on the top row only, Y axis on the
                    first column only; per cell: every cell's
    reshape         3 x 3: the first cell kept in view, page count follows
    probe           the cursor over a cell names that cell
    tap             a tap reaches Python's drill-down with the cell tapped
    zoom common     a box zoom in one cell: every cell at the same viewport,
                    redrawn
    zoom per cell   per cell: a zoom in one cell leaves the others
    playback        pages advance on the timer, looping
    save            Save PNG downloads the page
    console         no errors in the browser console

Needs playwright and Chrome/Chromium (``--chrome``: an executable or a
macOS ``.app``; without it, the installed Google Chrome).
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

SERVE = r"""
import os, sys, warnings
warnings.filterwarnings("ignore")
sys.path.insert(0, {root!r})
from uuid import uuid4
from cubevis import exe
from cubevis.toolbox.gridplot.grid_app import GridApp
from cubevis.toolbox.gridplot.grid_source import SyntheticGridSource
src = SyntheticGridSource({n}, kind={kind!r}, cost={cost})
drilled = []
def on_drill(i, label):
    drilled.append(i)
    print("DRILL", i, label, flush=True)
    return "would open " + label
app = GridApp(src, rows=4, cols=4, pool=(6, 6), on_drill=on_drill, title="grid check")
ctx = exe.Context(exe.Mode.SYNC)
ui, task = app(ctx, uuid4())
ui.show()
print("SERVING", flush=True)
ctx.execute(task, uuid4())
"""


def chrome_exe(chrome):
    if not chrome:
        return None
    chrome = chrome.rstrip("/")
    if chrome.endswith(".app"):
        name = os.path.basename(chrome)[:-4]
        return os.path.join(chrome, "Contents", "MacOS", name)
    return chrome


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--chrome", default=os.environ.get("CHROME"))
    ap.add_argument("--out", default=os.path.join(tempfile.gettempdir(), "grid_check"))
    ap.add_argument("--kind", default="raster", choices=["raster", "scatter"])
    ap.add_argument("--n", type=int, default=45)
    ap.add_argument("--cost", type=float, default=0.0)
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
    srv = subprocess.Popen([sys.executable, "-c", SERVE.format(root=ROOT, n=a.n, kind=a.kind,
                                                              cost=a.cost)],
                           env={**os.environ, "BROWSER": rec}, stdout=log, stderr=subprocess.STDOUT)
    results = []

    def check(name, ok, detail=""):
        results.append(ok)
        print(f"{'PASS' if ok else 'FAIL'}  {name:14s} {detail}", flush=True)

    try:
        t0 = time.time()
        while not os.path.exists(url_file):
            if srv.poll() is not None or time.time() - t0 > 120:
                print(open(os.path.join(a.out, "serve.log")).read()[-3000:])
                raise SystemExit("server did not start")
            time.sleep(0.2)
        url = open(url_file).read().strip()
        from cubevis.toolbox.gridplot.grid_source import SyntheticGridSource
        labels = SyntheticGridSource(a.n, kind=a.kind).labels()
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            kw = {"executable_path": chrome_exe(a.chrome)} if a.chrome else {"channel": "chrome"}
            b = p.chromium.launch(args=["--no-sandbox", "--allow-file-access-from-files"], **kw)
            ctx = b.new_context(viewport={"width": 1500, "height": 1150}, accept_downloads=True)
            pg = ctx.new_page()
            errs = []
            pg.on("console", lambda m: errs.append(m.text[:200]) if m.type == "error" else None)
            pg.on("pageerror", lambda e: errs.append(str(e)[:200]))
            pg.goto(url)
            pg.wait_for_function("window.__cvGrid && Object.keys(window.__cvGrid).length > 0",
                                 timeout=60000)
            G = "Object.values(window.__cvGrid)[0]"

            def wait_drawn():
                pg.wait_for_function(
                    "(() => { const t = [...Bokeh.documents[0]._all_models.values()]"
                    ".filter(m => m.type === 'Div').map(d => d.text);"
                    " return t.some(x => x.startsWith('drawn in')); })()", timeout=60000)
                pg.wait_for_timeout(200)

            def titles():
                return pg.evaluate("""() => {
                    const g = [...Bokeh.documents[0]._all_models.values()].find(m => m.type === 'GridPlot');
                    return g.children.map(([f, r, c]) => f.title.text); }""")

            def drawn_cells():
                return pg.evaluate("""() => {
                    const g = [...Bokeh.documents[0]._all_models.values()].find(m => m.type === 'GridPlot');
                    return g.children.map(([f]) => {
                        const r = f.renderers.find(x => x.data_source);
                        const im = r.data_source.data.image[0];
                        return im ? (im.length || 0) : 0; }); }""")

            def set_status_marker():
                pg.evaluate("""() => { for (const d of Bokeh.documents[0]._all_models.values())
                    if (d.type === 'Div' && d.text.startsWith('drawn in')) d.text = ''; }""")

            def cell_xy(slot_r, slot_c, fx=0.5, fy=0.5):
                return pg.evaluate("""([r, c, fx, fy]) => {
                    const g = [...Bokeh.documents[0]._all_models.values()].find(m => m.type === 'GridPlot');
                    const f = g.children.find(([f, rr, cc]) => rr === r && cc === c)[0];
                    const v = Bokeh.index.find_one_by_id(f.id);
                    const rc = v.el.getBoundingClientRect(), fb = v.frame.bbox;
                    return [rc.left + fb.left + fb.width * fx, rc.top + fb.top + fb.height * fy]; }""",
                                   [slot_r, slot_c, fx, fy])

            def tool(name):
                pg.evaluate("""(n) => {
                    const tb = [...Bokeh.documents[0]._all_models.values()].filter(m => m.type === 'Toolbar')
                        .find(t => t.tools.some(x => x.type === 'ToolProxy'));
                    const t = tb.tools.find(x => (x.tools ? x.tools[0].type : x.type) === n);
                    t.active = true; }""", name)

            def axes():
                return pg.evaluate("""() => {
                    const g = [...Bokeh.documents[0]._all_models.values()].find(m => m.type === 'GridPlot');
                    return g.children.map(([f, r, c]) => [r, c,
                        f.above.some(a => a.visible), f.left.some(a => a.visible)]); }""")

            def click_button(label):
                pg.evaluate("""(l) => { const b = [...Bokeh.documents[0]._all_models.values()]
                    .find(m => m.type === 'Button' && m.label === l);
                    for (const cb of b.js_event_callbacks['button_click']) cb.execute(b); }""", label)

            def select(title, value):
                pg.evaluate("""([t, v]) => { const s = [...Bokeh.documents[0]._all_models.values()]
                    .find(m => m.type === 'Select' && m.title === t); s.value = v; }""", [title, value])

            wait_drawn()
            pg.screenshot(path=os.path.join(a.out, "page1.png"))
            t, d = titles(), drawn_cells()
            check("first page", t == labels[:16] and all(x > 1 for x in d),
                  f"{len(t)} cells, first {t[:2]}, all drawn: {all(x > 1 for x in d)}")
            ax = axes()
            ok = all(x == (r == 0) and y == (c == 0) for r, c, x, y in ax)
            check("axes", ok, "X on the top row, Y on the first column")

            set_status_marker(); click_button("▶"); wait_drawn()
            check("paging", titles() == labels[16:32], f"page 2 starts {titles()[0]}")
            set_status_marker(); click_button("⏭"); wait_drawn()
            t, d = titles(), drawn_cells()
            n_last = len(labels) - 32
            ok = t[:n_last] == labels[32:] and all(x.strip() == "" for x in t[n_last:]) and \
                all(x <= 1 for x in d[n_last:])
            pg.screenshot(path=os.path.join(a.out, "last_page.png"))
            check("last page", ok, f"{n_last} of 16 cells, the rest blank")

            set_status_marker(); click_button("⏮"); wait_drawn()
            set_status_marker(); click_button("▶"); wait_drawn()      # page 2: cells 16..31
            set_status_marker(); select("Rows", "3"); wait_drawn()
            set_status_marker(); select("Cols", "3"); wait_drawn()
            st = pg.evaluate(f"{G}.state()")
            t = titles()
            ok = len(t) == 9 and labels[16] in t and st["nPages"] == -(-len(labels) // 9)
            check("reshape", ok, f"3 x 3, page {st['page'] + 1} of {st['nPages']}, "
                                 f"cell {labels[16]} at {t.index(labels[16]) if labels[16] in t else '-'}")
            pg.screenshot(path=os.path.join(a.out, "reshape_3x3.png"))

            x, y = cell_xy(1, 2)
            pg.mouse.move(x - 5, y)
            pg.mouse.move(x, y, steps=3)
            pg.wait_for_timeout(400)
            pr = pg.evaluate("window.__cvGridProbe")
            want = labels[st["page"] * 9 + 5]
            text = pg.evaluate("""() => [...Bokeh.documents[0]._all_models.values()]
                .filter(m => m.type === 'Div').map(d => d.text).find(t => t.includes(' Time'))""")
            check("probe", pr is not None and text is not None and text.startswith(want),
                  f"{text!r}")

            tool("TapTool")
            pg.mouse.click(*cell_xy(0, 1))
            pg.wait_for_timeout(500)
            tapped = pg.evaluate("window.__cvGridTapped || []")
            log.flush()
            served = open(os.path.join(a.out, "serve.log")).read()
            want_i = st["page"] * 9 + 1
            check("tap", tapped == [want_i] and f"DRILL {want_i} {labels[want_i]}" in served,
                  f"tapped {tapped}, Python drilled {labels[want_i] if f'DRILL {want_i}' in served else '-'}")

            set_status_marker()
            tool("BoxZoomTool")
            x, y = cell_xy(1, 1, 0.2, 0.2)
            x2, y2 = cell_xy(1, 1, 0.6, 0.7)
            pg.mouse.move(x, y); pg.mouse.down(); pg.mouse.move(x2, y2, steps=6); pg.mouse.up()
            wait_drawn()
            rngs = pg.evaluate("""() => {
                const g = [...Bokeh.documents[0]._all_models.values()].find(m => m.type === 'GridPlot');
                return [...new Set(g.children.map(([f]) => [f.x_range.start, f.x_range.end,
                    f.y_range.start, f.y_range.end].map(v => v.toFixed(2)).join(',')))]; }""")
            st = pg.evaluate(f"{G}.state()")
            check("zoom common", len(rngs) == 1 and st["viewport"] is not None,
                  f"one viewport for all 9 cells: {rngs[0]}")
            pg.screenshot(path=os.path.join(a.out, "zoom_common.png"))

            set_status_marker(); select("Scale", "per cell"); wait_drawn()
            ax = axes()
            check("axes per cell", all(x and y for r, c, x, y in ax), "every cell has both")
            before = pg.evaluate("""() => {
                const g = [...Bokeh.documents[0]._all_models.values()].find(m => m.type === 'GridPlot');
                return g.children.map(([f]) => [f.x_range.start, f.x_range.end]); }""")
            x, y = cell_xy(0, 0, 0.3, 0.3)
            x2, y2 = cell_xy(0, 0, 0.5, 0.6)
            pg.mouse.move(x, y); pg.mouse.down(); pg.mouse.move(x2, y2, steps=6); pg.mouse.up()
            pg.wait_for_timeout(1200)
            after = pg.evaluate("""() => {
                const g = [...Bokeh.documents[0]._all_models.values()].find(m => m.type === 'GridPlot');
                return g.children.map(([f]) => [f.x_range.start, f.x_range.end]); }""")
            changed = [i for i, (u, v) in enumerate(zip(before, after)) if u != v]
            check("zoom per cell", changed == [0], f"cells whose range changed: {changed}")
            pg.screenshot(path=os.path.join(a.out, "zoom_per_cell.png"))

            select("Scale", "common"); wait_drawn()
            select("Pages/s", "4")
            seen = []
            click_button("Play")
            for _ in range(26):                       # 2.6 s at 4 pages/s, looping
                pg.wait_for_timeout(100)
                pnow = pg.evaluate(f"{G}.state().page")
                if not seen or seen[-1] != pnow:
                    seen.append(pnow)
            st = pg.evaluate(f"{G}.state()")
            click_button("Pause")
            looped = any(b < a for a, b in zip(seen, seen[1:]))
            check("playback", len(seen) >= 6 and looped and st["cache"] >= 2,
                  f"pages shown in 2.6 s at 4/s: {seen}; {st['cache']} cached")

            with pg.expect_download(timeout=60000) as dl:
                click_button("Save PNG")
            path = os.path.join(a.out, dl.value.suggested_filename)
            dl.value.save_as(path)
            ok = os.path.getsize(path) > 10000 and open(path, "rb").read(8) == b"\x89PNG\r\n\x1a\n"
            check("save", ok, f"{dl.value.suggested_filename}, {os.path.getsize(path) // 1024} KB")
            check("console", not errs, "; ".join(errs[:3]))
            b.close()
    finally:
        srv.terminate()
        try:
            srv.wait(timeout=10)
        except Exception:
            srv.kill()
        log.close()
    n_fail = results.count(False)
    print(f"{len(results) - n_fail} of {len(results)} passed; screenshots in {a.out}")
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())
