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
    python drive.py examples/visplot_flow.py /tmp/out/flow

Every run starts with a fresh kernel and a reset workspace.  The steps file is exec'd with `pg`,
`ctx` (`ctx.set_offline(True)` drops the kernel connection), `run_cell(i)`
(returns the cell's output text), `add_cell(code)`, `cell_output(i)`,
`wait_idle()`, `reload()`, `console` and `out`.  The page's console goes to
`OUT_console.txt`, a screenshot to `OUT.png`.

Environment: `JL_URL` (default `http://127.0.0.1:8899/lab/tree/t.ipynb`),
`CHROME` (browser executable), `BUNDLE` (a cubevisjs.min.js to serve instead
of the one in `cubevis/__js__`, e.g. a fresh `bokeh build`), `INIT_JS` (a
script run in the page before anything else, for tracing).

## Seen with it (JupyterLab 4.6, 2026-11-24)

- A reload of a notebook **not saved** since the GUI was shown leaves no GUI
  (JupyterLab rebuilds the page from the file) while the session runs on in
  the kernel, which cannot tell.  `vp.show(redisplay=True)` /
  `ic.show(redisplay=True)` shows it again and it carries on; a plain second
  `show()` says so (`examples/reload_unsaved.py`; iclean checked the same way,
  with CASA 6.7.6 and a simulated VLA MS).
- After a reload, saved or not, the iclean GUI starts from its state when
  `ic.show()` ran, while the kernel carries on from where it was
  (`examples/reload_state.py`, 2026-11-25).  After one cycle with niter=40
  (5 left) and an unsent cycleniter edit, both paths came back with
  niter 50, cycleniter 10, status "initial residual image" and a one-point
  convergence plot.  A saved notebook keeps the output as it was first
  rendered; `redisplay=True` renders the kernel-side Bokeh models, which the
  browser's changes never reach.  The image and the mask come from the cubes,
  so they are current.  Continue after such a reload sends niter=50, not 5.
  **Fixed on `44-visplot-in-notebooks` (2026-11-25, spin-off issue 1):** the
  page asks the kernel for the session's state when it loads
  (`InteractiveCleanUI.session_state`).  With niter=400 and cycleniter=15
  sent, then cycleniter=7 typed: both paths come back with niter 345,
  cycleniter 15 (7 was never sent), the cycle's status and a two-point plot,
  and Continue carries on from 345.  `examples/reload_running.py` reloads
  while a cycle runs (a notebook that slows `arun_cycle`): the page shows the
  cycle's settings and progress with the buttons disabled, and its result
  when it ends.
- A reloaded notebook page that sent before its comm was found (the bridge
  can take a while, JC-3) took that for a lost connection: the CommMgr closed
  the comm and retried a **WebSocket**, which a notebook page has no address
  for (`object null is not iterable`, for ever).  The page then never heard
  from the kernel again.  Seen first through the iclean state request above,
  which is sent from an init script; any early send could do it.  Fixed in
  `comm_mgr.ts` (2026-11-25): the comms setup counts as the attempt under way
  (early sends wait for it) and a reconnect makes a transport of the kind
  chosen at load.  Needs rebuilt bundles.
- `visplot(...)` / `iclean(...)` run in a cell open a browser tab, which
  could not reach the kernel: its CommMgr had chosen Jupyter comms, so the
  first request (stepping to the first Antenna) never returned (2026-11-25,
  review WS-7).  The tasks now ask for a websocket
  (`examples/visplot_task_tab.py`: cell 0 records `webbrowser.open`'s URL,
  which the steps open in a second page).
- A reload did not bring the visplot output back (`Bokeh is not defined`: the
  restored embed script ran before BokehJS loaded); the bridge did, opening a
  new comm.  Fixed the same day (`_showable._wait_for_bokehjs`): the output
  comes back and carries on the session.
- Losing the kernel connection for up to 60 s loses nothing: jupyter_server
  replays what was sent meanwhile.
- When the cell output and an Output View of it render at the same moment
  (JupyterLab restoring a workspace with the view open), both copies share one
  kernel comm; the copy that does not hold `comm.onMsg` gets no replies.  Open.
- The basic flow (`examples/visplot_flow.py`: Plot, the flag tool, layouts,
  iteration) behaves as in a browser tab.  The top toolbar row is wider than a
  default notebook output and is cut off on the right.
- JupyterLab restores the panels of the previous run (an Output View left open
  renders the output a second time); `drive.py` opens with `?reset` to avoid
  it.
