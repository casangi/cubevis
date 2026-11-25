"""Drive a JupyterLab notebook in headless Chromium (see README.md).

    python drive.py STEPS.py OUT_PREFIX

STEPS.py is exec'd with: pg (the page), ctx, run_cell(i, timeout) -> output
text of cell i, add_cell(code) -> index, cell_output(i), wait_idle(), reload(),
console (list of console lines), out.
"""
import os, sys, time, json
from playwright.sync_api import sync_playwright

URL = os.environ.get("JL_URL", "http://127.0.0.1:8899/lab/tree/t.ipynb")
steps, out = sys.argv[1], sys.argv[2]
console = []

# A fresh kernel for every run: shut down any the server has.
import urllib.request
_base = URL.split("/lab")[0]
for _k in json.load(urllib.request.urlopen(_base + "/api/kernels")):
    urllib.request.urlopen(urllib.request.Request(_base + f"/api/kernels/{_k['id']}", method="DELETE"))
for _s in json.load(urllib.request.urlopen(_base + "/api/sessions")):
    urllib.request.urlopen(urllib.request.Request(_base + f"/api/sessions/{_s['id']}", method="DELETE"))

with sync_playwright() as p:
    b = p.chromium.launch(executable_path=os.environ.get("CHROME") or None,
                          args=["--no-sandbox"])
    ctx = b.new_context(viewport={"width": 1600, "height": 1100})
    # cubevis loads its scripts from CDNs in a notebook (set_js_loading
    # "network"); serve those from this machine.  BUNDLE= picks cubevisjs.
    import re, glob
    import bokeh
    STATIC = os.path.join(os.path.dirname(bokeh.__file__), "server", "static", "js")
    JS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "cubevis", "__js__")
    def _serve(route):
        url = route.request.url
        m = re.search(r"/(bokeh(?:-[a-z0-9]+)?)-\d+\.\d+\.\d+(?:\.[a-z0-9]+)?(\.min)?\.js$", url)
        if "cdn.bokeh.org" in url and m:
            path = f"{STATIC}/{m.group(1)}.min.js"
        elif "cubevis" in url and url.endswith("cubevisjs.min.js"):
            path = os.environ.get("BUNDLE") or f"{JS}/" + url.split("/__js__/")[1]
        elif "cubevis" in url and "/__js__/" in url:
            path = f"{JS}/" + url.split("/__js__/")[1]
        else:
            console.append(f"ROUTE-MISS: {url}")
            return route.abort()
        route.fulfill(path=path, content_type="application/javascript")
    ctx.route(re.compile(r"https://(cdn\.bokeh\.org|cdn\.jsdelivr\.net)/.*"), _serve)
    pg = ctx.new_page()
    pg.on("console", lambda m: console.append(f"{m.type}: {m.text}"))
    pg.on("pageerror", lambda e: console.append(f"PAGEERROR: {e}"))

    def open_nb():
        pg.goto(URL)
        pg.wait_for_function("() => window.jupyterapp && window.jupyterapp.shell.currentWidget"
                             " && window.jupyterapp.shell.currentWidget.sessionContext"
                             " && window.jupyterapp.shell.currentWidget.sessionContext.session"
                             " && window.jupyterapp.shell.currentWidget.sessionContext.session.kernel",
                             timeout=60000)
        wait_idle()

    def wait_idle(timeout=60000):
        pg.wait_for_function("() => { const w = window.jupyterapp.shell.currentWidget;"
                             " return w.sessionContext.session.kernel.status === 'idle' }",
                             timeout=timeout)

    def cell_output(i):
        return pg.evaluate(f"""() => {{ const nb = window.jupyterapp.shell.currentWidget.content;
            const c = nb.widgets[{i}]; if (!c.outputArea) return '';
            return c.outputArea.node.innerText }}""")

    def run_cell(i, timeout=60000, wait=True):
        pg.evaluate(f"""() => {{ const nb = window.jupyterapp.shell.currentWidget.content;
            nb.activeCellIndex = {i}; nb.deselectAll();
            return window.jupyterapp.commands.execute('notebook:run-cell') }}""")
        if not wait:
            return None
        time.sleep(0.5)
        wait_idle(timeout)
        return cell_output(i)

    def add_cell(code):
        return pg.evaluate("""(code) => { const panel = window.jupyterapp.shell.currentWidget;
            const nb = panel.content; const m = panel.context.model;
            nb.activeCellIndex = nb.widgets.length - 1;
            return window.jupyterapp.commands.execute('notebook:insert-cell-below').then(() => {
              nb.activeCell.model.sharedModel.setSource(code); return nb.activeCellIndex }) }""", code)

    def reload():
        pg.reload()
        pg.wait_for_function("() => window.jupyterapp && window.jupyterapp.shell.currentWidget"
                             " && window.jupyterapp.shell.currentWidget.sessionContext"
                             " && window.jupyterapp.shell.currentWidget.sessionContext.session"
                             " && window.jupyterapp.shell.currentWidget.sessionContext.session.kernel",
                             timeout=60000)
        wait_idle()

    open_nb()
    try:
        exec(open(steps).read())
    finally:
        with open(out + "_console.txt", "w") as f:
            f.write("\n".join(console))
        try:
            pg.screenshot(path=out + ".png", full_page=False)
        except Exception:
            pass
    b.close()
