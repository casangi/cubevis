# Handoff: visplot `debug-0003` (closed 2026-11-20)

Successor to the `debug-0001` / `debug-0002` sessions (feedback issue #24,
help system in `debug-0002`).  The planning notes for the features are in
`devel/docs/visplot/visplot_feature_requests.md`; the transport findings in
`devel/notes/drs/review/transport_review_2026-11-16.md`.

## Where things stand

* **Branch `debug-0003`** on `casangi/cubevis`, from `main` `05769eef`
  (the `debug-0002` merge).  Pushed through `12f8ff97`, with all five JS
  bundles (Bokeh 3.6 .. 3.10) rebuilt from the TypeScript in that commit.
  A last zip adds this handoff, the issue texts' pointer in the feature
  requests, the updated transport review and the headless-harness
  additions (no code change).  Ready to merge.
* **Tests**: `devel/tools/visplot_headless/runtests.sh`, one file per
  process.  At the end, compared with the run before the transport work,
  everything matched except improvements: the new and extended files
  pass (`test_reconnection` 20, `test_time_gaps` 12, `test_closed_guard`
  4, `test_scan_iteration`, `test_custom_window`, `test_title_context`).
  **Failing before this branch and unchanged** (environment: no TW Hya
  MS / PS in the sandbox, no remote kernel): `test_flag_commit_real`,
  `test_info_block_integration`, `test_memory_guard`,
  `test_remote_flagging`, `test_remote_reduction_context`,
  `test_remote_selection`, `test_statistical_ui`,
  `test_threshold_scaling`, `test_vis_argument`; "no tests ran":
  `test_close_kinds`, `test_match_identity`, `test_probe_scatter_region`,
  `test_relay`, `test_scatter_helpers`.  Check these on a machine with
  the data before reading anything into them.

## What `debug-0003` contains

Features (from the #24 triage):

1. **Scan iteration**: Prev / ✱ / Next beside the Scan box step through the scans it selects (all when empty) for the field shown, with a "▶ Scan N · i of n" readout, and the grid's Play steps scans too.
2. **Plot titles say what is iterated**: a default title ends with the field, SPW, scan and antenna or baseline when the selection holds exactly one of several ("— 1642+689, SPW 1, scan 7, DA41&DV01").
3. **Typed windows**: the Time and Channel window dropdowns have *Custom…*, taking `45`, `45s`, `2min`, `1.5h`, or a channel count of at least 2, with a ⚠ for anything else.
4. **"Correlation shown" documented**: a raster may show a correlation product the Data ticks leave out, and the docs now say so (behaviour kept).
5. **Time gaps: Closed** (raster gear tab): the empty time between scans is removed from a raster's time axis, ticks and readout stay in real time, and flag boxes convert back to real time spans.
6. **Linked cursor with closed gaps**: the cursor shared between a raster with closed gaps and a scatter converts through real time, so tracking keeps working.

Fixes:

7. **The user-testing crash** ("MSv2Backend is not open" after `ConnectionClosedOK 1001`): a browser connection attempt that fails its handshake no longer shuts the session down (review WS-1).
8. **Handshake before takeover**: a new connection must complete its handshake, within 10 s, before it replaces the live one, so a stray or silent connection cannot knock out a working window (review WS-2).
9. **Slow close off the critical path**: the old socket is closed in the background, so a half-open peer cannot make the new connection miss the browser's handshake deadline (review WS-3).
10. **Newest tab takes over**: opening the session in a second tab (or reloading) moves it there, the first tab dims under "Open in another tab" and stops reconnecting, even on wake, and its *Use this tab* button takes the session back (review WS-2).
11. **Pages identify themselves**: each page now sends its own id in the handshake; until now every page sent `null`, so Python could not tell a second tab from a reconnect and two open tabs retired each other in a loop.
12. **One reconnect at a time**: the browser no longer starts a new reconnect attempt, and closes the one in progress, on every focus, wake or send (review JS-1).
13. **No double runs after a reconnect**: a request the browser sends again after a reconnect is answered from the original (dropped while it runs, its reply resent once it has finished) instead of running its handler twice, and replies Python resends keep their request id so the browser does the same (review RP-1).
14. **Closed session says so**: after `visplot` has ended or the plotter is closed, Plot, export, the summary, row updates and flag actions answer "This visplot session is closed…" instead of a backend traceback.

Docs and tools:

15. **End-user docs**: `visplot.rst` gains "The window in more than one tab"; `visplot_help.md` has entries for time gaps, Custom windows, scan stepping and the iteration titles.
16. **Transport review updated**: each fixed item carries a "Fixed in `debug-0003`" note and the summary table and suggested order show what is left.
17. **Headless harness**: `CHROME=` and `CVDEBUG=` for `drive.py` / `serve.py`, a two-tab example, and `make_scans_ms.py` for a small multi-scan MS.

## Follow-up issues (texts written, to be filed)

Issue bodies were written as markdown, one per issue (the session could
not create GitHub issues).  From the #24 triage, for discussion: 1 time
iteration (filed by Darrell), 2 scan intent (grounded in MSv4
`scan_intents`), 3 correlation iteration, 4 channel selection,
5 aggregation and windows for every quantity.  Written at the close:

6. **Raster cell widths** — a lone channel or integration is drawn as wide as the gap to its neighbour (`raster_grid.cell_edges` infers widths); carry the true widths. A drawing bug; needs a reader change.
7. **Scan stepping skips empty scans** — like antennas and baselines, using a `has_data` list from the reader. Needs a reader change.
8. **Rest of time-gap closing** — a `visplot()` argument, scatters with X = Time, frequency gaps between SPWs, re-query when zoomed.
9. **Transport review leftovers** — WS-4 and JS-2 (small), HB-1 and a per-request deadline, the `backend_id` mismatch, then JC-1 (Jupyter) and RK-1 (remote).

6 and 7 change the reader, which is in the remote-kernel fingerprint
closure: each one means a new fingerprint and a cubevis release on the
kernel side.  Suggested next: 6 (plots are wrong, not just inconvenient),
then 9's two small items.

## Working conventions (Darrell)

* Deliver complete changed files as a zip (paths from the repo root),
  built from `/home/claude/out`; list any file to delete.  Darrell tests,
  commits and pushes.  Never commit or push unless asked.
* Branch per debugging period (`debug-000N`), merged by PR.  Fetch and
  compare with `origin/<branch>` before each change; `git reset
  origin/<branch>` (mixed) then `git status` shows what really differs.
* **MSv4 decides features**: a feature only MSv2 can support is declined
  by default; arguing it up means getting it into MSv4 first.
* TypeScript changes need the bundles rebuilt for Bokeh 3.6 .. 3.10;
  Darrell does that.  The Python side must keep working with older
  bundles (the transport changes do: an old page sends `frontend_id:
  null` and is treated as before).
* Fingerprint closure: `flag_filters`, `selection_text` and the readers.
  Changing them means a new kernel-side fingerprint; say so up front.
  Help text and the visplot plotter files are outside it.

## Test kit (things that cost time to rediscover)

* **Build a bundle here** (to test TypeScript live): copy `cubevisjs/` to
  a directory *named* `cubevisjs` (module hashes come from the path), set
  `@bokeh/bokehjs` in its `package.json` to the installed Bokeh
  (`bokeh --version`, 3.10.2 here), run `bokeh build` in it with the venv
  active, copy `dist/cubevisjs.min.js` to `cubevis/__js__/bokeh-3.10/`.
  The output matched the shipped bundle apart from the module hashes.
  Reuse `node_modules` between builds.
* **Live GUI**: `devel/tools/visplot_headless/live.sh <steps.py> <out>
  ms=<ms>`.  In this sandbox playwright wants a newer headless shell than
  the one installed: `CHROME=/opt/pw-browsers/chromium_headless_shell-1243/chrome-headless-shell-linux64/chrome-headless-shell`.
  A second page: `b.new_page()` (not `pg.context.new_page()`).
  `CVDEBUG=cubevis.bokeh.transport._comm_mgr,cubevis.bokeh.transport._low_level_transport`
  shows each handshake (`initialization received: frontend=…`) in
  `serve.log`.
* **Transport without a browser**: `tests/manual/visplot/test_reconnection.py`
  has a fake frontend (`Frontend`, with `frontend_id=` and `claim=`) and a
  request-sending one (`_Caller`).  All messages go through
  `serialize`/`deserialize`, never raw JSON.
* **Data**: `make_scans_ms.py` (2 fields, 8 interleaved scans, no flags);
  the VLBA TL016B subset `tl016b_small.ms` from Darrell (22 scans of
  J1154+6022, a fringe finder with two scans 84 min apart) -- not in the
  repo; `runtests.sh` uses it through `VLBA=`.
* **Background tabs**: `requestAnimationFrame` pauses in a background
  page, so a check that waits on drawing needs `pg.bring_to_front()`.
* The full suite takes about 25 minutes; run it detached
  (`setsid nohup … & disown`) and poll `summary.txt` for `DONE`.

## Design notes worth keeping

* **Time gaps**: `raster_grid.TimeAxis` maps each time cell to a position
  with one blank position (GAP = 1) at each break found by `cell_edges`;
  the axis is positional, so ticks come through the categorical labels and
  detail re-query on zoom is off while closed.  The state source carries
  `x_tcells` / `y_tcells` (`[positions, lo, hi]`) for the cursor.
* **Scan stepping** lives in `scan_iteration.py` (`SCAN_ITER_JS`): the
  candidates are the box's scans filtered to the field shown, the current
  one in `window.__cvScanCur`; typing in the box, ✱ or Plot ends stepping.
* **Tab takeover**: Python `SUPERSEDED_CLOSE_CODE = 4001` and
  `__superseded__`; `CommMgr._superseded_pages` turns away a superseded
  page's own reconnects unless its handshake has `claim: true` (sent only
  by *Use this tab*).  Browser: `CommMgr.onSuperseded` / `reclaim` /
  `pageId` in `comm_mgr.ts`.
* **De-duplication**: one record per comm (`_last_request` in Python,
  `lastRequest` in `cm.ts`): each side has one request in flight per comm,
  so a replay is always the comm's last request.  The reply is recorded
  before it is sent, and even with no connection, because a lost send is
  exactly what a replay needs; a handler that ends without a reply leaves
  no record.
