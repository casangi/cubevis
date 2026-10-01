# FlagDB v2 — implementation notes

Status (part 2, 2026-09-28): implemented end to end and tested
(`tests/manual/visplot/test_flagdb_v2.py`, 35 tests on a simulated MSv2, incl.
the real box handler); smoke-tested on sis14_twhya (Ceres) in the sandbox.
Browser-side behaviour (FlagTool response callback, Flagging sidebar section,
review dialog, panel refresh) still needs a live GUI check.

## Part 2 (GUI)
- `flag_controls.FlagController` owns the FlagDB, the filter registry,
  preview/display/colour/extend settings, proposals, export and the widgets.
- `VisibilityPlotter(flag_filters=, flag_preview=False, flag_display="hide",
  flag_color="#ff00ff")`; `plotter.flag_db`, `plotter.flags`,
  `plotter.export_flags(path, fmt="flagdata"|"jsonl")`.
- Box handler: per-panel-object callbacks; box resolved on the displayed
  (union) grid of the plotted axis; a Channel axis that the backend did not
  relabel (windows differ) is treated as Frequency (the aggregate is the truth).
- Flag views (`SelectionSpec.flag_view`): effective | disk | pending | proposal,
  applied by `LocalVisibilityReader` via a contextvar read in `_flag_mask`.
  "hide" = effective; "color" = disk + pending overlay; a proposal under
  review is painted orange in both modes.
- Panels: `_flag_stale` -> next pan/zoom re-render re-queries at full extent
  first; overlays composited in raster `_render`/`_shade_viewport` and scatter
  `_push_image`. Per-figure rerender debounce timers (shared timer bug fixed).
- FlagTool: new `response_callback` property (Python + TS); tool activation no
  longer zooms to 1:1 (gate removed; a plain click still zooms).
- Pre-existing bug fixed: Flag-fraction raster masked padding after reducing,
  which re-broadcast baseline (3-D result for Time x Frequency) and let padded
  slots inflate the fraction.

## Decisions (agreed 2026-09-28)
- Flag, Unflag, Undo, Redo, Clear all operate on the DB (ordered fold).
- Display is user selectable: default "hide" (pending-flagged points vanish,
  AIPS-like); alternative shows pending points in a user-selected colour.
  Backend support: `set_pending_flags(..., apply=False)` + `pending_flag_mask()`.
- The 1:1 zoom gate is removed (part 2); filters replace it.
- Constructor parameter: `flag_filters=`.
- Z-Score filter reference matches the colouring of the panel being flagged:
  scatter = whole selection per baseline+correlation (`reference="selection"`),
  raster = per spectral window (`reference="spw"`); `granularity="cell"`
  reproduces the raster's per-cell max with the Sidak-corrected cutoff.
- Scatter boxes flag what is displayed (visible layers, hidden categories excluded).

## Findings that shaped the design
- `time` is UNIX seconds in both xarray-ms (MSv2) and xradio (MSv4)
  (`time.attrs["format"] == "unix"`), not MJD seconds as older docstrings say.
  Deltas carry `time_format`; export converts.
- SPW identity may be a non-unique name (simulated data: every window
  `<Unknown>`). `SpwKey` = identity + frequency span + channel count.
  MSv2 export maps windows to SPECTRAL_WINDOW rows via arcae (`spw_casa_ids`);
  otherwise `name:f0~f1Hz`, else `*:f0~f1Hz` with a WARNING comment when ambiguous.
- xarray-ms pads missing (time, baseline) slots as flagged with NaN
  EFFECTIVE_INTEGRATION_TIME. Such samples are "invalid" and never changed
  (an unflag box cannot expose padding).
- Every render path in both backends gets flags from `_flag_mask(ds)`; the
  backends now implement `_disk_flag_mask` and `XArrayReader._flag_mask`
  folds pending deltas lazily (`dask.map_blocks`).
- Scatter frame cache generation is now `(cache_generation, pending_version)`.

## Representation
- Raster box + identity filter -> region delta, verified against every
  partition's coordinates; falls back to explicit samples if not exact.
- Scatter box or any non-identity filter -> sample-set delta materialized at
  proposal time (frozen selection), compact encoding (indices or bits).
- Export never uses `clip` for value conditions: materialized `manual` lines.

## Open / later
- Remote: built-in filters run in the worker; user filters are local-only
  (clear error remotely).
- Scatter flag evaluation re-reads the selection (exact but not cached);
  optimise later if slow on large data.
- Frame cache re-reads on every pending change; later: apply pending mask to
  cached frames instead.

## Remote (2026-09-28)
- Verified end to end with a real worker (local `python3` kernel, simulated MS
  and sis14_twhya): box resolution, filters, pending-flag application, flag
  views, overlays and the InfoTool box probe run in the worker; results match
  local exactly (`tests/manual/visplot/test_remote_flagging.py`).
- User filters: refused remotely with `UserFilterNotRemoteError` (clean status
  message, no traceback); labelled "(user, local data only)" in the Filter menu.
- Debug timing logs for remote flag calls (`CUBEVIS_DEBUG=1`).
- Generic remote testing: `tests/manual/visplot/remote_visplot_check.py`
  (local-vs-remote parity + timings) and `REMOTE_TESTING.md` (plan + manual
  GUI checklist).
- Light mode: legend and colour-bar Divs now recoloured with the info Divs.

## Remote execution (2026-09-28)
- `tests/manual/visplot/test_flagdb_remote.py`: 9 tests through a real Jupyter
  kernel worker (default kernel `python3`, `CUBEVIS_TEST_KERNEL` to override):
  box evaluation (raster, scatter, Z-Score) identical to local, pending flags
  and flag views applied in the worker, InfoTool probe, SPW ids, user filters
  refused, and the plotter end to end with `kernel_name=`.
- Timing: `RemoteReductionContext.call_stats()` returns client round trip,
  worker compute time (measured inside `VisplotRemoteBackend`) and their
  difference (overhead) per method. Debug logs (`CUBEVIS_DEBUG=1`) on both sides.
- Wire: pending-flag state and evaluation results now cross as one JSON string;
  nested lists through the Bokeh serializer cost ~0.7 s for 100 region deltas
  of 325 baselines, JSON ~50 ms.
- `bench_remote_overhead.py --ms PATH [--field F] [--kernel K]`: local vs remote
  vs worker vs overhead vs payload per GUI operation. Local kernel, TW Hya
  (Ceres): ~3.5 ms per call floor; +~10 ms for a 37 kB raster, +~20 ms for a
  2 MB scatter render; flag evaluation overhead ~5 ms on 0.2-0.6 s of compute.
  A real remote kernel adds network latency/bandwidth on top.

## Light/dark info strips
- Info/legend/colorbar Divs use `var(--cv-info-bg)` / `var(--cv-info-fg)`;
  the theme toggle sets the variables on the page root (intermittently the
  strips stayed dark in light mode before).

## Layout / overflow (2026-09-28, round 7)
- Several colour bars in one panel use a compact one-row-per-layer form
  (label | bar + ticks, ~22 px) so the fixed 100 px colourbar box shows them all.
- Plot area gets class `cv-plot-area`; a page script shows a small
  "▾ more below — colour bars / info" chip at its bottom edge only while
  content is hidden below (click scrolls down). Verified headless at 640 px
  (chip shown) and 820/1300 px (hidden).

## Remote round 2 (2026-09-28)
- Pending-flag sync to the worker is incremental (`sync_pending_flags`: ordered
  ids + only unseen deltas; full resend on any mismatch). Tested.
- `VisibilityPlotter.remote_call_stats()`.
- `bench_remote_overhead.py --gui`: user-visible latency through the plotter,
  local vs kernel (after a warm-up run). TW Hya/Ceres, local kernel: flag box
  +3 ms, flag box + both redraws ~1.3 s either way (redraw dominated: both
  panels re-query at full extent after a pending change -- the next
  optimisation target, independent of remote execution).

## 2026-09-29: kernel_name= without backend="remote" ran locally
- `VisibilityPlotter(kernel_name=K)` with the default backend="auto" silently
  opened the MS locally (zuul06 bench failed with a local FileNotFoundError for
  the kernel-host path; the earlier "GUI kernel" numbers and the end-to-end
  remote test were in fact local). Now kernel_name + auto => remote (logged),
  kernel_name with another explicit backend => warning. Bench and test assert
  a RemoteReductionContext.
- True remote GUI timing (local kernel, TW Hya Ceres): scatter full re-render
  pays ~0.5-1.9 s of wire overhead, driven by `ref_scale=2` reference
  aggregates (2 layers x (1100x1000 float64 + uint32)). Bokeh's remote
  encoding is already compact (8.8 MB array -> 1.3 MB), zlib gained nothing
  (tried, reverted); the cost is in the transport hops. Next: profile the
  worker->supervisor->kernel->client path, or keep references worker-side.

## 2026-09-29: zuul06 / cvpost140 results and remote ref_scale
- zuul06: kernel start 51 s; cvpost140: 2.3 s. Per-call floor ~22 ms on both.
  Flag evaluation overhead ~25 ms. GUI redraws after a flag +2.8-6.4 s.
- Cause: remote scatter default ref_scale=4.0 (reference grids 16x the canvas)
  shipped on every full render. Local-kernel sweep, TW Hya Ceres, 2 layers:
  ref 0: 126 kB / 34 ms overhead; 1: 846 kB / 139 ms; 2: 1.8 MB / 295 ms;
  4: 3.9 MB / 834 ms (+~100 ms worker compute). Remote default now 1.0.
- The relay costs ~200 ms per MB even over loopback (worker -> supervisor ->
  kernel -> client, JSON/base64 each hop): next target.
- bench: --ref-scales sweep.

## 2026-09-29: step 1 -- remote breakdown, and a leftover diagnostic
- `cubevis/remote/_worker_transport.py`: the 2026-09-06 frame diagnostics ran
  unconditionally -- open/append/close of /tmp/cubevis_frame_debug2.log for
  every frame and every 4 kB chunk, plus two MD5s per payload. Now only with
  CUBEVIS_FRAME_DEBUG=1 (path CUBEVIS_FRAME_DEBUG_PATH); payload read with one
  readexactly(). Sandbox (fast /tmp): overhead per MB ~200 -> ~150 ms; the
  effect on zuul06 depends on its /tmp.
- Relay stats: worker FRAME_STATS (encode/decode, bytes), kernel KERNEL_STATS
  (re-encode), P_local CLIENT_STATS (decode); exposed via call_stats()["relay"].
- bench --gui prints a per-operation breakdown: calls, bytes, worker compute,
  worker encode, client decode, relay/net remainder.
- Sandbox (local kernel, TW Hya Ceres): redraws after a flag are now worker
  compute dominated (1.0-1.8 s of 1.4-2.4 s); relay/net 0.26-0.39 s.
  A scatter flag triggers two query_columns (look at in step 3).

## 2026-09-30: remote results with part 11 confirmed on both hosts
- w-encode now non-zero; per-MB overhead unchanged (~250 ms/MB zuul06,
  ~230 cvpost140) -> the debug writes were not the remote cost there (the old
  logs were ~20 MB). Remaining per MB: worker encode ~55 ms, client decode
  ~20 ms, kernel decode/re-encode + Jupyter + network ~170 ms.
- Step 3 started: scatter Level-1 may serve viewports beyond a FULL-extent
  reference (nothing outside it); the redraw after a scatter flag that
  shrinks the extent now issues 1 query_columns instead of 2 (images 99.9%
  pixel-identical to a forced Level-2, the usual Level-1 resampling edge).

## 2026-09-30: step 3b -- cached RAW scatter frames
- The scatter frame cache now holds raw frames (every valid sample, built
  under the new flag view "none") with `__disk_flag`, `__spw` (code into
  backend._cv_spw_codes) and `__chan`, keyed without the pending version.
  `flag_engine.frame_keep_mask` applies the current view row by row (region
  and sample-set deltas, extend options, proposal); Z-Score frames are
  finalized after the view. One filtered result per layer is memoised.
- Tests: raw-path frames == fresh reads for AMPLITUDE/PHASE/Z_SCORE x 4 views x
  4 pending states (flag, sample set, unflag of committed flags, extend,
  proposal); no MS re-read on a flag change. Real-MS frame/probe/zscore tests pass.
- TW Hya Ceres, local: scatter query after a flag change 0.51 s (0.14 s view
  filter + 0.37 s binning/reference) instead of a re-read (cold 3.4 s).
- Single-dish stores (antenna_name dim) fall back to the per-state cache.
- Relay binary pass-through (step 2) NOT done yet.

## 2026-09-30: bench 006 (part 13 on both hosts)
- Local (Mac) redraws improved ~20-25%; remote raster/undo redraws improved
  ~0.1-0.2 s, but the scatter-flag redraw got slower (zuul06 2.22 -> 2.45 s):
  the row filter matched every raw row against each sample-set delta with a
  binary search (~0.33 s on TW Hya locally, 2-3x on the hosts).
- Fix (part 14): per raw frame, row identity is decoded once (unique times /
  frequencies + inverse index, cached baseline pair lookups) and the effective
  flag state is cached per pending-delta list, reusing the longest cached
  prefix (a flag applies one delta; an undo returns a cached state).
  Row filter for the TW Hya scatter frame (1.46M rows, one sample-set delta):
  from scratch 166 ms, cached 0 ms, one new delta 66 ms (was ~330 ms every
  redraw). Test: incremental results == from-scratch for add/undo/redo/unflag.

## 2026-09-30: step 2 -- relay pass-through (cubevis/remote)
- Remote call_method requests carry `pre_encoded: True`; the worker encodes
  the result once (`{"__cv_pre_encoded__": str}`, worker_main).
- Worker -> kernel: frames with a pre-encoded result use raw segments after
  the JSON (length word high bit = flag), so the big string is never
  JSON-escaped/parsed (_worker_transport._write_frame/_read_frame).
- Kernel -> P_local: KernelCommTransport moves it into a Jupyter comm binary
  buffer; KernelClientTransport restores it; RemoteReductionContext._acall
  decodes once. The kernel no longer rebuilds arrays or re-serializes.
- Opt-in per request: an older client never sees buffers/segments. Kernel env
  and P_local must both have this version for the gain (the worker and
  kernel share the kernel env's install).
- Local-kernel sweep (TW Hya Ceres, 2 layers, overhead): ref 1: 120 -> 64 ms;
  2: 256 -> 133 ms; 4: 742 -> 412 ms (~45% less). Remaining is mostly worker
  Bokeh encoding (base64 arrays) + P_local decode.
- Tests: large results identical to local through the relay; frame raw
  segment round trip; all tests/manual/remote (except host-specific
  test_query_raster*) and remote flag tests pass.

## 2026-09-30: bench 008 + round trips
- Part 15 on both hosts: relay overhead halved (ref 1: 247->126 ms zuul06,
  227->120 cvpost140); scatter flag+redraws zuul06 2.09->1.94 s.
- RemoteReductionContext memoises axis_info / identity_tables (coordinate
  only) keyed by selection fingerprint minus flag view, + cache_generation.
  A redraw after a flag now makes 4 remote calls instead of 9.
- bench --gui: per-method worker table + zoomed case. Sandbox findings:
  query_columns 535-610 ms per redraw (drawing + reference); a SCATTER flag
  box's evaluate_flag_request 561 ms (it re-reads the selection from the MS,
  not the cached raw frames); zoomed redraw makes 2 query_columns.

## 2026-09-30: bench 009 + shared binning (part 17)
- zuul06 part 16: raster flag+redraws 1.51 s, scatter 1.73 s, undo 1.39 s,
  zoomed 2.02 s. Worker per method: query_columns 0.61-0.72 s (1.0 s x2
  zoomed), scatter-box evaluate 0.415 s vs raster 0.22 s, query_raster 0.1 s.
- _scatter_render: render_layer() and build_layer_reference() now share the
  hover-probe id grid (always identical) and, at equal resolution (remote
  ref_scale=1), the (x, y) mean/count aggregation, via a small identity-keyed,
  weakref-checked, locked memo (_bin_memo, 8 entries). Bit-identical output
  (test). TW Hya Ceres, 2 layers: ref 1: 352 -> 265 ms; ref 2: 388 -> 328 ms.
- Next: scatter-box evaluation from cached raw frames; zoomed double query.

## 2026-09-30: bench 010 + scatter box from cached frames (part 18)
- Part 17 on hosts: zuul06 raster flag+redraws 1.51->1.33 s, scatter
  1.73->1.65, undo 1.39->1.29, zoomed 2.02->1.86; query_columns -120 ms.
- XArrayReader._raw_frames(): raw-frame retrieval factored out of
  _query_columns_cached_raw; raw keys no longer include the flag view.
- flag_engine._scatter_box_from_frames: identity-filter scatter boxes on
  non-Z-Score layers are resolved over the panel's cached raw frames (drawn
  x/y, identity, disk flag + pending view); `force_ms=True` forces the old
  path. Tests: identical counts and flag effect vs the MS path for two-layer
  amplitude, phase, hidden categories, unflag over pending flags; fast path
  reads no visibilities. TW Hya Ceres two-layer box: 607 ms -> 38 ms.
- Still on the MS path: Z-Score layers, non-identity filters, InfoTool probe.

## 2026-09-30: part 19 -- zoomed redraw, InfoTool from frames; success criteria
- bench 011 (part 18): zuul06 scatter flag+redraws 1.65 -> 1.19 s (scatter
  box evaluate 414 -> 32 ms).
- Zoomed redraw after a flag: VisibilityPlot._prepare_stale_render hook;
  the scatter skips the full-extent REFERENCE on the stale full re-read when
  the viewport is clearly finer than that reference can serve (10% margin);
  the Level-2 query that follows supplies the zoomed reference. Test: first
  query without reference, final image identical to the old path.
- InfoTool box probe (probe_region) resolved over the cached raw frames with
  the same rules as the Flag box; sample identity includes __spw (windows may
  share frequencies -- caught by the equivalence test). Tests vs force_ms for
  multi-layer, hidden categories, pending flags. TW Hya: 360 -> 51 ms.
- Agreed success criteria: no duplicated access/compute per flag operation;
  remote adds <= ~0.3 s beyond worker compute; flag/unflag/undo + both
  redraws <= 1.5 s on zuul06 zoomed or not; exact, tested sample selection.

## 2026-09-30: bench 012 (part 19) and part 20
- zuul06: raster 1.31 s, scatter 1.23 s, undo 1.29 s, zoomed 1.63 s (target
  1.5); cvpost140 1.07 / 1.00 / 1.07 / 1.31. Skipping the full-extent
  reference cut transfer, not worker time (binning is shared since part 17).
- Part 20: zoomed stale redraw is ONE Level-2 query -- the viewport result
  already carries the full-data extent, global scaling and colour-bar inputs.
  _prepare_stale_render now returns True to skip the full re-read (refreshes
  axis info, drops stale references). Test: one query; image, extent,
  colour bar and histograms identical to the old full-then-zoomed path.

## 2026-09-30: part 21 -- show flagged data (unflag on-disk flags)
- New flag view "flagged": only effectively flagged (on-disk and/or pending)
  VALID samples are drawn (padding never). Dask path: ~(eff & valid);
  raw frames: eff (frames hold valid samples only).
- FlagController: show_flagged / flagged_color (GUI checkbox + colour picker;
  VisibilityPlotter(flag_show_flagged=False, flag_flagged_color="#7f849c");
  sync_layers regenerated). Overlay drawn first so pending/proposal colours
  stay on top; init_panels() applies configured overlays and redraws panels
  already rendered in their constructors so the first page carries it.
- Unflag boxes already addressed flagged samples; with the overlay they can
  now be aimed. Report page lists the setting.
- Tests: flagged view == eff & valid; raw-frame "flagged" view == fresh read;
  plotter: initial overlay, scatter Unflag box restores the committed
  spectrum, toggle off clears overlays. Headless Chrome: checkbox toggles,
  raster cells and scatter points drawn grey.

## 2026-09-30: part 25 -- export / commit (flag_commit.py)
- Decisions (Darrell): MSv2 written ONLY via CASA (flagdata/flagmanager);
  MSv4 backup as a side file; write option disabled with a reason when the
  tools are missing. MSv4 gets no script option (JSON + "Describe pending
  flags" cover it).
- Menu "Export / commit" (context-sensitive): JSON (both), flagdata script
  (MSv2), write to data (MSv2 casatasks / MSv4 zarr), load JSON as pending,
  restore commit backup (MSv4). Writes need a confirmation (preview dialog).
- MSv2 commit: flagmanager save -> one flagdata(mode='list') per delta, in
  order -> reopen -> VERIFY against the display's fold (counts of
  not-flagged / not-unflagged / collateral), report with flag version name.
- MSv4 commit: exactly the changed samples written with zarr vindex into the
  data group's flag variable; previous raw values saved first to
  <store>.visplot_flag_backup_<ts>.npz; restore_msv4_backup; verify.
- Runs where the data are (worker for remote); P_local clears the DB, bumps
  cache generation, refreshes.
- Tests: tests/manual/visplot/test_flag_commit.py (MSv4 commit/verify/
  restore, via menu, remote via local kernel; MSv2 call order with a
  recording casatasks stand-in + verification catches unwritten changes;
  real-CASA test skipped unless casatools imports; script compiles; JSON
  round trip and SPW check).
- Sandbox note: the PyPI casatools wheel (RHEL build) cannot load on Ubuntu
  24 (bundled libssl/libldap need RHEL OpenSSL symbols), so the real-CASA
  test must run on NRAO hosts.

## 2026-09-30: part 26 -- responsiveness of display toggles
- Report: toggling Hide/Show in colour or "Show flagged data" started long
  operations with no busy cursor, and stalled the websocket heartbeat
  (browser declared the socket dead after 10 s and reconnected).
- Causes: (1) panel re-render comm handler ran synchronously on the asyncio
  event loop; (2) busy cursor was a boolean, cleared by the first reply
  while the other panel was still rendering, and the flag-control scripts
  never set it; (3) every toggle marked panels stale, re-reading the drawn
  data even when only overlays changed.
- Fixes: _handle_rerender_async runs the re-render in a worker thread (comm
  lock still serializes per panel); __cvSetBusy is reference counted, every
  flag-control request balances its own true/false and defines the helper
  itself; push_state(data_changed=False) for overlay-only changes (Show
  flagged, colours, display switch with nothing pending) -> panels refresh
  only overlays (_overlays_stale / _refresh_flag_overlays).
- Tests: toggles make no main-data queries; rerender runs off the main
  thread; headless: busy counter returns to 0.
- Part 27: busy cursor stays on until the new image is painted (rerender
  reply clears it two animation frames after the image update), and a flag
  response that refreshes panels holds busy across the 300 ms re-render
  debounce. Headless trace: busy continuous from click to paint, no gaps.
- Part 28: (a) __cvSetBusy defined by a page init script (the FlagTool's
  first box had no busy state before any CustomJS had defined it); (b) going
  idle is deferred 200 ms so hand-offs (FlagTool clears busy before its
  asynchronously compiled response callback starts the refresh) don't
  flicker; (c) a visible "Working..." chip under the toolbar while busy --
  the OS mouse cursor only updates on mouse movement; (d) exports get
  time-stamped default names <data>.flags.<YYYYmmdd-HHMMSS>.jsonl /
  .flagdata.<ts>.py, and an explicit existing file name is refused.
- Part 29: Plot/presets (doPlot) release busy only after their own updates are painted and hold it across the ~300 ms re-render debounce the new ranges trigger; headless trace after Z-Score: one continuous busy span from click to the redrawn plot.

## 2026-09-30: part 30 -- MSv2 commit left points unflagged (investigation)
- Export audit with an MSSelection-semantics emulator (cross product of
  timerange list x baselines x spw:chan range x correlations) found a real
  bug: sample-set channel runs were taken over the TRIMMED grid columns, so
  '1~3' could be written for samples on channels 1 and 3 (collateral flags
  on 2). Fixed: runs over actual channel numbers. Commands are now grouped
  exactly (per baseline/correlations/channel run -> time LIST; identical
  groups share an antenna list): TW Hya Ceres dense box 19,935 samples ->
  11,861 commands, emulated selection == sample set exactly.
- Before: one command per (time, pols, run) -> ~one per sample; a 40,899
  sample operation became one flagdata call with tens of thousands of
  commands. Calls are now chunked (FLAGDATA_CHUNK=500); the result message
  reports commands/calls. The missing flags are most likely CASA-side with
  that single huge list -- needs the commit message / CASA log to confirm.
- Dialog readability: page colour variables, full-strength text, labels by
  opacity, solid accent border, sans-serif.

## 2026-09-30: part 31 -- exact MSv2 write with arcae (default)
- Darrell's run with part 30: CASA flagdata write verified with 2,689 of
  52,624 samples NOT flagged (0 collateral) -- although the exported
  selections are exact under MSSelection semantics (emulator test).
- New default MSv2 write (flag_commit.commit_msv2_arcae): compute the final
  flag of every changed sample with the display's engine, map each to its MS
  row via (DATA_DESC_ID, TIME to the microsecond, ANTENNA1, ANTENNA2) --
  refusing on missing or ambiguous rows -- and write FLAG per data
  description (FLAG is variably shaped across DDIDs) with arcae putcol;
  FLAG_ROW = all(FLAG) for written rows. Previous FLAG/FLAG_ROW of the rows
  saved first to <ms>.visplot_flag_backup_<ts>.npz (restore_backup), plus a
  CASA flag version when casatools works. Then verify.
- CASA flagdata write kept as a menu option ("CASA flagdata"); arcae needs
  no CASA, so writing is available wherever visplot reads MSv2.
- TW Hya copy, Ceres box of 146,966 samples: 2,449 rows written in 1.8 s,
  verified 0 mismatches. Tests: sim commit/verify/restore, FLAG_ROW
  consistency, menu flow, remote (local kernel) for MSv2 and MSv4.
