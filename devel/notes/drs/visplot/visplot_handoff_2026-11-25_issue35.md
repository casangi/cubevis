# Handoff: issue #35, transport review leftovers (closed 2026-11-25)

Successor to `debug-0004` (`visplot_handoff_2026-11-22_debug-0004.md`).
Branch `35-transport-bugs` worked issue #35, the leftovers of the 2026-11-16
transport review (`devel/notes/drs/review/transport_review_2026-11-16.md`,
"the review"; its top block lists every item's status). What is left
is drafted as new issues in `devel/notes/drs/review/issue35_spinoffs.md`.

## Where things stand

* **Branch `35-transport-bugs`** on `casangi/cubevis`, from `main` at the
  `debug-0004` merge, with `main` merged in once (`7dd5055a`, the ssh
  wrapper, #41). Darrell's commits run through `9498838c`; **zip 9**
  (below) is the last one, on top of `9498838c`. After it the branch is
  ready to merge.
* **TypeScript changed** in this branch (`comm_mgr.ts`,
  `low_level_transport.ts`; zip 9 adds WS-6): rebuild the bundles for
  Bokeh 3.6 .. 3.10. Notebooks load cubevisjs from jsdelivr at the release
  tag unless `CUBEVIS_JS_TAG=<full commit SHA>` is set; testing notebooks
  against a branch needs the SHA of a commit with rebuilt bundles.
* **Kernel-side fingerprint** changed (zip 9 moves it again through
  `_bokeh_app_context`): a cubevis release is needed with this branch.
  `python -m cubevis._fingerprint --changed origin/main`.
* **Tests**: the full `tests/manual/visplot` run (one file per process,
  `devel/tools/visplot_headless/runtests.sh`) gives the same results
  before and after zip 9; the remaining failures need the TW Hya MS
  (absent in the sandbox). New this branch: `test_reask.py` (12),
  `test_comms_transport.py` (7), `test_showable_notebook_script.py` (6,
  node), `test_tab_transport.py` (6), additions to `test_reconnection.py`.

## What the branch contains

Transport (details and checks in the review, item by item):

1. **WS-4** goodbye only on `pagehide`, cleared by any later frame.
   **WS-2 rest**: a page for another session (`comm_mgr_id` or
   `backend_id`) gets `__refused__` and close code 4002; the page shows
   "Session ended". **WS-5** logs ids, not messages. **WS-6** `wss://`
   from an https page.
2. **JS-2** `sendImmediate` requeues when disconnected; **JS-3** async JS
   handlers; **PY-1**, **PY-2**, **CO-3** cleanups.
3. **No wall-clock deadlines (decided with Darrell, 2026-11-23)**: a user
   may start iclean on site, unplug, go home and resume. Instead a
   request unanswered for `REASK_AFTER` (30 s) is sent again with the same
   `request_id` and `reask: true`; the peer answers with the stored reply,
   a `__running__` notice, or runs it. It is forgotten only if the re-ask
   goes unanswered while the peer shows liveness
   (`REASK_LIVENESS_TICKS` = 2 inbound frames), and never while
   disconnected. `CommMgr.forget(request_id | callback, comm_id, error)`
   removes a request in flight or queued and moves the queue on (order is
   kept by the receiver's per-comm lock). WebSocket only (`liveness`).
4. **HB-1 (1) and (2)**: the page tolerates `MISSED_PONGS_WHILE_BUSY` = 3
   missed pongs while a request awaits its reply; a loop-lag monitor in
   `CommMgr` logs `event loop blocked for N s (running: <handler> ...)`
   (DEBUG from 1 s, INFO from 8 s). The rest of HB-1 is spin-off issue 3.
5. **RK-1**: `_bridge.request()` forgets its request when it gives up.
6. **JC-1 .. JC-4, JupyterLab comms**: the active comm is the one whose
   page sent `comm_opened`; a new comm opening makes it unconfirmed
   (in-flight requests requeued), a handshake makes it ready (flush);
   replies go to the comm a request came on; send with no active comm
   raises `TransportNotConnectedError`; the page polls 5 s for the window
   bridge. Found on the way: `CommMgr.send` now queues behind held
   messages (a replay could be overtaken).
7. **Notebook reload**: `_showable._wait_for_bokehjs` (a restored output's
   script waited for nothing); iclean's `pagehide` stop only for a tab
   (`stop_on_unload`); `ic.show(redisplay=True)` / `vp.show(redisplay=True)`
   for a reload of an unsaved notebook (the output is gone, the session
   runs on); `VisibilityPlotter.show()` keeps its `Showable`.
8. **WS-7 (zip 9)**: `visplot(...)` / `iclean(...)` run in a notebook cell
   open a browser tab, but their CommMgr chose Jupyter comms; the tab's
   first request (stepping to the first Antenna) never returned. Where the
   GUI is shown now decides: the visplot task layers pass
   `transport='websocket'` (a layer argument in `project_layers.yaml`),
   `InteractiveClean._TRANSPORT = 'websocket'`,
   `InteractiveCleanNotebook._TRANSPORT = 'auto'`, and
   `BokehAppContext.show` refuses a comms CommMgr with a message.

Product fixes found while fixing tests (zip 2): scatter `x_step` kept on
resize; Custom window column sizing.

## Found, left for the spin-off issues

* **GUI state after a reload** (issue 1): saved or not, the GUI starts
  from its state at `show()`; Continue after a reload sends the original
  niter. `examples/reload_state.py` reproduces it.
* **visplot in notebooks** (issue 2): no `visplot.notebook()`, toolbar cut
  off, Output View sharing the comm, stray "Bokeh is not defined".
* createmask / createregion: their `beforeunload` sends the result; they
  have no CommMgr and no resume, so nothing to change.

## Tools

* `devel/tools/jupyterlab_headless/`: JupyterLab in headless Chromium
  (Playwright). `drive.py STEPS.py OUT` with a fresh kernel and `?reset`
  workspace each run; CDN scripts served locally (`BUNDLE=` picks a
  cubevisjs build, `INIT_JS=` for tracing). Examples: `jc1_views_reload`,
  `visplot_flow`, `reload_unsaved`, `reload_state`, `visplot_task_tab`.
  The README lists what was seen with it.
* `devel/tools/visplot_headless/`: a browser-tab visplot; `serve.py`
  `CVDEBUG=`, `BLOCK_PLOT=<s>` (hold the loop), `drive.py CHROME=`,
  `runtests.sh <outdir>` (one file per process; compare `summary.txt`).

## Sandbox kit (costly to rediscover)

* venv `/home/claude/venv`: jupyterlab, anywidget, jupyter_bokeh,
  playwright, pytest_asyncio, casatools/casatasks 6.7.6, casadata.
  `~/.casa/config.py`: auto-updates off, `measurespath` = casadata's
  `__data__`. Chrome: `/opt/pw-browsers/chromium_headless_shell-1243/
  chrome-headless-shell-linux64/chrome-headless-shell`.
* Data: simulated VLA MS `/home/claude/ictest/sim.ms` (iclean; give a
  `mask=` so a cycle cleans, else "Zero mask"); xarray-ms simulated MS
  `/home/claude/data/live.ms` (visplot). No TW Hya.
* Bundle build: copy `cubevisjs/src` and `index.ts` to
  `/home/claude/build/cubevisjs`, `bokeh build` there (Bokeh 3.10 only).
* JupyterLab: `jupyter lab --allow-root --no-browser --port 8899
  --ServerApp.token='' --ServerApp.password=''
  --ServerApp.disable_check_xsrf=True --LabApp.expose_app_in_browser=True
  --ServerApp.root_dir=/home/claude/jl`. It dies between turns; restart.
* iclean `_app.py` is generated: edit `_app.mustache`, run
  `python scripts/expand_templates` (needs java and casatasks).
  visplot task layers: `python -m scripts.sync_layers` (`--check`).
* Clicking a Bokeh button from a test: `Bokeh.index.find_one(model).click()`;
  `CustomJS.execute(model)` lacks the event (`cb_obj.origin`) and fails
  for iclean.

## Working conventions (Darrell)

Unchanged from `debug-0004`: investigate before changing code; complete
files in a zip with repo-root paths; Darrell tests, commits, pushes and
rebuilds bundles. The review doc is the status record for transport
items; the Known Issues table in `visibility_plotter_implementation_plan.md`
for visplot.
