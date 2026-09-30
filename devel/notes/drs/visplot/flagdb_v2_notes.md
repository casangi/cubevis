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
