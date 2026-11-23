# Handoff: visplot `debug-0004` (closed 2026-11-22)

Successor to `debug-0003` (`visplot_handoff_2026-11-20_debug-0003.md`).
Issues worked: #38 (raster cell widths), #37 (scan stepping skips scans
with no data), #36 (the rest of gap closing).  #35 (transport review
leftovers) is left for its own branch; see the end of this note.  The
planning notes for the features are in
`devel/docs/visplot/visplot_feature_requests.md`; the transport findings
in `devel/notes/drs/review/transport_review_2026-11-16.md`.

## Where things stand

* **Branch `debug-0004`** on `casangi/cubevis`, from `main` `30560f08`
  (the `debug-0003` merge), pushed through `224eb0c1`.  `main` has one
  later commit (`65c1c949`, a note under `devel/notes/lmc/`) that touches
  nothing here.  No TypeScript changed, so no bundles need rebuilding.
  The last zip adds this handoff and the feature-requests update (no
  code change).  Ready to merge.
* **Kernel-side fingerprint**: `k2-5289ebf2d42ab89ca80e` (the readers,
  `raster_grid`, `_scatter_render` changed).  A remote kernel needs a
  cubevis release with this branch.  Check with
  `python -m cubevis._fingerprint --changed origin/main`.
* **Tests** (run one file per process; see the test kit): the new and
  extended files pass: `test_cell_widths` 55, `test_scans_with_rows` 28,
  `test_axis_runs` 24, `test_time_gaps` 17, `test_scatter_time_gaps` 32,
  `test_freq_gaps` 50, `test_gap_arguments` 14, `test_tick_format` 22,
  `test_scan_iteration` 15, `tests/manual/setup/test_fingerprint.py` 12.
  **Failing before this branch and unchanged**: `test_statistical_ui`
  (a stale code string), `test_threshold_scaling`.  **Environment only
  in this sandbox** (no `jupyter_client` / `pytest_asyncio`, no TW Hya
  data): `test_casa_optional`, `test_remote_*`, `test_reconnection`,
  and the files that skip without `MS=`.  Check these on a machine with
  the data before reading anything into them.

## What `debug-0004` contains

Fixes:

1. **Lone channel / integration widths (#38)**: the aggregate carries
   `time_cell_width` / `frequency_cell_width` (times the stride after
   decimation; merged across partitions), and `raster_grid.cell_edges`
   uses a cell's own width wherever it is known, in the gap test too.  A
   one-channel SPW or a lone integration is no longer drawn as wide as the
   distance to its neighbour.
2. **Scan stepping skips scans with no data (#37)**: `reader.scans_with_rows`
   (remote too) for the field, SPWs, time and UV range; Prev / Next and
   "i of n" count only those.  The Antenna / Baseline ticks are ignored
   (documented).
3. **Light mode**: the band under the status boxes no longer stays dark.

Gap closing (#36), "remove every gap" on both panel kinds (Darrell's call
after trying blanks and separator lines: users ask for the gaps when they
want them):

4. **Runs-based closed axis**: `raster_grid.GapAxis` (alias `TimeAxis`)
   lays the runs of data (`reader.axis_runs(selection, dim)`, cell bounds
   joined where they meet) side by side, to scale, with every gap removed.
   Replaces `debug-0003`'s positional axis with a blank per break, so the
   raster now re-queries finer cells when zoomed with the gaps closed.
   Panel specs carry `x_runs` / `y_runs` (`[starts, lo, hi]`); the tick
   formatter (`GAP_AXIS_JS`, `cvGapReal` / `cvGapPos`), PNG export, the
   linked cursor and the axis-unit prefix read real values through them.
5. **Scatter, X = Time**: the closed axis is applied to the points
   before datashader (`_scatter_render.close_x_gaps`), so the image itself
   is gap-free and lines up with a closed raster.  Probe, flag boxes and
   "outside the data" messages convert to real time.
6. **Frequency gaps between SPWs**: the same for a raster's Channel axis
   when the selected windows differ (drawn in Hz) and a scatter's
   X = Frequency.  A channel-index axis has no gaps and is left alone.
7. **Scatter column cap**: a scatter image has at most one column per
   sample along X = Time (integration), Frequency (channel width) and
   Channel (one index), times the averaging.  Without it, samples a bit
   more than a pixel apart left empty columns, drawn as stripes (seen
   with 6 s integrations, and 384 channels across a 900-pixel panel).
   The Level-1 reference is capped the same way (`reference_size`) and
   records the spacing (`ScatterLayerReference.ref_x_step`), so a zoom in
   X on such an axis is still redrawn locally (`needs_level2_requery`).
   Without the reference cap, the redraw after a step showed a seam: half
   the plot denser than the other.
8. **Controls and arguments**: "Time gaps" and "Frequency gaps" in each
   panel's gear tab (scatter: shown for X = Time / Frequency; raster
   frequency: shown when an axis is Channel); `time_gaps=` /
   `freq_gaps=` on `VisibilityPlotter`, in `panels=[...]`, in the
   reproducing call and summary page, and in the generated casatasks /
   casashell `visplot` (`sync_layers`, short `inp()` text in
   `project_layers.yaml`).

Tools and cleanup:

9. **`python -m cubevis._fingerprint`** (`--changed <ref>`): the
   kernel-side fingerprint of the working tree and whether it differs
   from a git ref.
10. Removed the `/tmp/cubevis_dataframe_debug.log` logging in
    `_wire_types` and the unused `flag_engine._raster_axis_mask`.
11. **Docs**: `visplot_help.md`: scan stepping (empty scans skipped),
    "Time gaps" rewritten for removal, new "Frequency gaps", arguments.

## Left open, found during this branch

* **Scatter draws twice per step**: after a Plot or a baseline step the
  range settles and a Level-1 redraw follows the Level-2 image.  Cheap
  (no read) and, with the reference cap, it now looks the same along X;
  it can still differ slightly in Y (the reference is 2x in Y).  Worth
  finding what moves the range, if it is visible.
* **Frequency gaps on real data**: tested on a simulated MS with two
  windows only.  Darrell has no multi-window data at hand yet.
* **Scatter X = Channel across windows**: a scatter's Channel stays a
  channel index, so it gets no frequency-gap control.  The raster's
  Channel falls back to Hz when the windows differ; the scatter does not.
* **Grid layout**: grid panels ignore both gap settings.
* **Simulated-MS oddities, not followed up**: with the simulator's
  windows, `SelectionSpec(freq_range=...)` selecting one window still
  left a raster's Channel axis in Hz (the other partition seems to count
  as empty-but-present in `channel_axis_is_unambiguous`), and
  `SelectionSpec(spw=[0])` matched no partition on the MSv4 copy.  Check
  on real data before treating either as a bug.

## #35: transport review leftovers (next branch)

Issue #35 lists what `debug-0003` left of the 2026-11-16 review (it fixed
WS-1, WS-2, WS-3, RP-1, JS-1).  Details and line references are in
`devel/notes/drs/review/transport_review_2026-11-16.md` (§1 table, §2-§6;
each fixed section says so; open checks in §8).

The issue's suggested order, with what each step touches:

1. **WS-4 + JS-2**, one small branch with a bundle rebuild (Bokeh
   3.6 .. 3.10).
   * WS-4 (S2): `__goodbye__` latches (`llt.py` `_goodbye_received`,
     `llt.ts` `goodbyeSent`, sent on `pagehide` *and* `beforeunload`).
     After a cancelled "Leave site?" prompt, a later abrupt drop is taken
     as a clean close and the session ends after the 3 s grace
     (`cm.py`).  Send goodbye on `pagehide` only, and/or clear the flag on
     the next inbound frame; reset `goodbyeSent` likewise.
   * JS-2 (S2): `sendImmediate` in `cm.ts` marks the comm pending, then
     finds the transport not open and returns; `WebSocketTransport.send`
     (`llt.ts`) drops with a warning.  Mark pending only after the write,
     or queue as `cm.py` does.
   * Tests: `test_reconnection.py`'s fake frontend (`Frontend`, `_Caller`)
     for the Python side; the headless harness (`live.sh`, `CVDEBUG=`) for
     the browser side.
2. **HB-1 with the per-request deadline.**
   * HB-1 (S1/S2): the browser pings every 15 s with a 10 s pong deadline
     (3 s after focus / wake); Python answers in the socket read loop, so
     a handler holding the event loop longer gets the socket declared
     dead.  Since RP-1 the handler no longer runs twice, but slow Plots
     still cause a reconnect.  First audit which handlers block the loop
     and for how long (visplot uses `asyncio.to_thread` in its
     scatter / raster handlers; `iclean/_ui.py` and `_cube.py` appear
     not to), then move long ones off the loop; a tolerant deadline in
     `llt.ts` (no "dead" on one missed pong while a request is in flight)
     is the browser-side half.
   * No per-request deadline in either `CommMgr` (`cm.py`, `cm.ts`): a
     lost message is a permanent wait.  A default deadline that clears
     the pending state and calls the callback with an error turns hangs
     into visible failures (review §1, "Deadlock / wedge conditions").
     Must leave room for legitimately long requests (a big Plot, a flag
     commit), so per-request overrides or a generous default.
3. **JC-1, then RK-1**, each needing a runtime check before and after.
   * JC-1 (S1): `CommsTransport._on_comm_open` (`llt.py`) never registers
     `on_close`, and nothing resets or replays when the notebook replaces
     the frontend comm; a comm with a request in flight waits forever.
     Hook reopen to `abandon_inflight` / reset + flush.  Confirm how a
     JupyterLab 4 reload behaves first.  JC-2 (send failures swallowed),
     JC-3, JC-4 nearby.
   * RK-1 (S1): a timed-out request leaves the link's single `"worker"`
     comm pending (`_bridge.py` `request()`, `link.py`, `ro.py` abandons
     only on heartbeat gaps); a reply lost on iopub wedges every later
     call.  Add `forget(request_id)` on timeout / cancel, or abandon on
     every `TimeoutError`.  Remote modules may be in the fingerprint
     closure: check with `_fingerprint --changed`.

Also open, not in the order above:

* WS-2 remainder: a handshake for a different `backend_id` only warns
  (`llt.py`); refuse it.  Small, could ride with step 1.
* S3 cleanups: WS-5, WS-6 (`ws://` hard-coded; matters only for a page
  served over https), JS-3, PY-1, PY-2.
* Jupyter: JC-2 (S2), JC-3, JC-4.
* Colab: CO-1 .. CO-8 (S2/S3): inflight accounting, chunk reassembly
  order, the dead per-message `js_code` build (CO-3, a cheap win),
  kernel `_parents`, messages before the bridge listens.
* Remote: RK-2 .. RK-9; OPT-1 .. OPT-5 (OPT-1, arrays as raw segments
  instead of base64-in-JSON, is the one to measure with
  `tests/manual/visplot/bench_remote_overhead.py`).

## Working conventions (Darrell)

* Investigate and confirm the design before changing code when anything
  is a choice; deliver complete changed files as a zip (paths from the
  repo root); list any file to delete.  Darrell tests, commits and
  pushes.  Never commit or push unless asked.
* Branch per debugging period (`debug-000N`), merged by PR.  Fetch and
  compare with `origin/<branch>` before each change; `git reset
  origin/<branch>` (mixed) then `git status` shows what really differs
  (a hard reset is blocked in the sandbox).
* **MSv4 decides features**: a feature only MSv2 can support is declined
  by default.
* Fingerprint closure: `flag_filters`, `selection_text`, the readers,
  `raster_grid`, `_scatter_render`.  Say up front when a change moves
  it.  The plotter, panels, help text and task layers are outside it.
* Generated task layers: never edit `cubevis/private/casatasks/visplot.py`
  or `casashell/visplot.py`; change the plotter's signature or docstring
  (or `scripts/sync_layers/project_layers.yaml`) and run
  `python -m scripts.sync_layers` (`--check` to verify).

## Test kit (things that cost time to rediscover)

* **Python**: `/home/claude/venv/bin/python`, with
  `CUBEVIS_SRC=$PWD/cubevis` (and `PYTHONPATH=$PWD` for scratch scripts).
* **Run files one at a time**: the whole `tests/manual/visplot` in one
  pytest process was killed for memory (exit 137).  A loop over the files
  with `timeout 900` each, results appended to a log, takes about 25
  minutes; run it with `nohup` and poll.
* **Multi-window data**: `xarray_ms.testing.simulator.MSStructureSimulator`
  takes `data_description=[(chan_freq_array, ["XX", "YY"]), ...]`, so
  windows at chosen frequencies need no real data (`test_freq_gaps.py`).
  It writes `CHAN_WIDTH` as `(f[-1] - f[0]) / nchan`, a little less than
  the spacing; `runs_of` joins the cells anyway (`GAP_FACTOR`).  The MSv4
  twin: `xr.open_datatree(ms, engine="xarray-ms:msv2").to_zarr(...)`.
* **Scatter stripes / seams without a browser**: build the frame,
  `compute_canvas_size(..., x_step=)`, `build_layer_reference` and
  `resample_layer_reference` directly and compare column occupancy
  between the two halves (`TestReferenceStep` in `test_freq_gaps.py`).
* Screen recordings were the only way to catch the second (Level-1)
  draw; it arrives under a second after the first.

## Design notes worth keeping

* **Runs**: `reader.axis_runs(selection, dim)` returns `[(lo, hi), ...]`
  in real units from the cell bounds of every selected partition (memo
  ignores antenna / baseline choices).  `GapAxis.from_runs` merges
  overlaps; `positions_of` maps real to closed (NaN outside a run),
  `real_of` back, `real_span(lo, hi)` gives the real span a closed box
  covers (strict overlap, so a box touching a run boundary does not grab
  the neighbour).  A raster falls back to `GapAxis.build` from its own
  cells when the reader cannot say.
* **Scatter closing** happens in the kernel (`query_columns(x_runs=)`),
  after averaging and before binning; the identity columns (`time`,
  `frequency`) stay real so the hover probe reports real values.  The
  memoized frame's `attrs["extent"]` is cleared on the closed copy.
* **Column cap**: `reader.scatter_x_step(selection, xaxis)` dispatches to
  `scatter_time_step` / `scatter_freq_step` / `scatter_chan_step`; the
  backend passes it to `compute_canvas_size`, `reference_size` and
  `build_layer_reference`.  It applies with the gaps shown too.
* **Scan-has-data** is reader-side (`scans_with_rows`), memoized per
  selection without antennas, sent to the browser as
  `cvScanHasNames`; `cvScanCandidates` narrows to it.
