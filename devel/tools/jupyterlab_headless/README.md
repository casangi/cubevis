# Headless JupyterLab harness

Drives a JupyterLab notebook running visplot in headless Chromium, to check
the Jupyter comm transport (`CommsTransport`) the way a user meets it: page
reloads, a second view of the output, the kernel connection dropping.  Written
for transport review JC-1 (issue #35, 2026-11-24).

## Setup

    pip install jupyterlab anywidget playwright
    MS=/path/to/some.ms python make_notebook.py /tmp/jl
    jupyter lab --no-browser --port 8899 --ServerApp.token='' --ServerApp.password='' \
        --ServerApp.disable_check_xsrf=True --LabApp.expose_app_in_browser=True \
        --ServerApp.root_dir=/tmp/jl &

`expose_app_in_browser` gives the page `window.jupyterapp`, through which
cells are run with JupyterLab's own commands.

## Running

    python drive.py examples/jc1_views_reload.py /tmp/out/jc1

Every run starts with a fresh kernel.  The steps file is exec'd with `pg`,
`ctx` (`ctx.set_offline(True)` drops the kernel connection), `run_cell(i)`
(returns the cell's output text), `add_cell(code)`, `cell_output(i)`,
`wait_idle()`, `reload()`, `console` and `out`.  The page's console goes to
`OUT_console.txt`, a screenshot to `OUT.png`.

Environment: `JL_URL` (default `http://127.0.0.1:8899/lab/tree/t.ipynb`),
`CHROME` (browser executable), `BUNDLE` (a cubevisjs.min.js to serve instead
of the one in `cubevis/__js__`, e.g. a fresh `bokeh build`).

## Seen with it (JupyterLab 4.6, 2026-11-24)

- A reload does not bring the visplot output back (`Bokeh is not defined`),
  but the bridge does, and opens a new comm.  A new Output View does the same.
- Losing the kernel connection for up to 60 s loses nothing: jupyter_server
  replays what was sent meanwhile.
- One output holds two Bokeh documents with the same CommMgr id on one kernel
  comm; Plot is sent by the one that does not receive, so it is never answered.
  Open, outside JC-1.
