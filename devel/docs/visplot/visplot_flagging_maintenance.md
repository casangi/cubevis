# visplot flagging — design, implementation and maintenance

*FlagDB v2, September–October 2026.  Companion documents:
`visplot_flagging_follow_on.md` (what is left to do),
`visplot_optimization.md` (the remote-execution performance work),
`flagdb_v2_notes.md` (the dated development log with every measurement).*

---

## 1. What the user gets

* **Flag / Unflag boxes** on raster and scatter panels (FlagTool, red / white
  flag icons) and an **InfoTool box** that reports exactly what a Flag or
  Unflag box with the same corners would address.
* **Filters** (Flagging panel): *All selected (immediate)* (AIPS style),
  Amplitude range, Z-Score, Amplitude MAD, Phase deviation, plus user Python
  filters passed as `VisibilityPlotter(flag_filters={name: callable})`.
  Parameters get controls automatically.
* **Pending flags** live in a FlagDB with Undo / Redo / Clear; an optional
  **preview** (accept / reject) per proposal; *Extend to all correlations /
  channels*.
* **Display**: *Hide flagged* (draws what a fresh open of the committed data
  would draw) or *Show in colour* (on-disk flags hidden, pending ones painted
  in the pending colour); *Show flagged data (to unflag)* paints everything
  flagged — on disk or pending — in the flagged colour so an Unflag box can
  aim at it.
* **Export / commit** menu (MSv2 and MSv4 alike): *Save flags as JSON*,
  *Write flags to the MS / PS*, *Load flags from JSON (as pending)*, *Restore
  flags from a commit backup* (dropdown of backups found next to the data),
  and in remote sessions *Recover autosaved flags*.
* **Describe pending flags**: a standalone HTML report (summary, one section
  per operation with UTC and MS/PS time spans, SPWs with MS ids, channel
  ranges, per-correlation / antenna / baseline counts, integrations).

Constructor arguments: `flag_filters`, `flag_preview`, `flag_display`,
`flag_color`, `flag_show_flagged`, `flag_flagged_color`, `frame_cache_mb`
(all passed through the `visplot()` task; `sync_layers` regenerates it).
Python API: `plotter.flag_db`, `plotter.flags` (the `FlagController`),
`plotter.export_flags(path)` (JSON Lines), `plotter.remote_call_stats()`.

---

## 2. Architecture

```
browser (Bokeh + cubevisjs)                P_local (Python, the plotter)              data side (in-process, or the remote worker)
FlagTool / InfoTool box  ── comm ──▶  FlagController.handle_box  ── reader ──▶  flag_engine.evaluate_request / probe_region
Flagging controls        ── comm ──▶  FlagController.handle_action                flag_commit (write / verify / restore)
panel pan/zoom           ── comm ──▶  VisibilityPlot._handle_rerender_async ──▶  query_raster / query_columns (+ flag views)
                                       FlagDB (ordered, undoable)  ── set_pending_flags ──▶ backend pending state
```

Everything that touches visibilities runs **where the data are**: in-process
for local sessions, in the worker subprocess on the kernel host for remote
ones (`RemoteReductionContext` ↔ `VisplotRemoteBackend`).  Only small
results cross the wire (deltas, counts, images).

### 2.1 Modules

| module | role |
|---|---|
| `flag_model.py` | Data model, numpy only: `SpwKey`, `SpwChannels`, `SampleBlock`, `FlagDelta`, `BlockCoords`, `FlagCounts`, `fold_deltas`, `delta_mask`, `time_to_datetime` |
| `flag_filters.py` | `ParamSpec`, `FlagFilter`, built-in filters, `FilterRegistry`, `make_flag_filter` |
| `flag_db.py` | `FlagDB`: ordered, versioned, undo/redo (incl. Clear), listeners, JSON Lines (de)serialization |
| `flag_engine.py` | Data-side evaluation: box → `FlagDelta` + exact counts, pending application (`apply_pending`), per-row flag views of cached frames (`frame_keep_mask`), InfoTool probe, cached-frame fast paths |
| `flag_controls.py` | `FlagController`: GUI widgets and JS, actions, preview, display modes, overlays, export/commit/load/restore/recover menu, autosave, report page |
| `flag_commit.py` | Writing flags: arcae (MSv2) / zarr (MSv4), backups, verification, HISTORY, restore, cached-frame refresh, backup listing |
| `flag_export.py` | JSON Lines only (`to_jsonl`) — CASA flagdata generation was removed (§6.4) |
| `data/reader.py` | `XArrayReader`: flag views (`_flag_mask`, `_FLAG_VIEW` contextvar), raw frame cache (`_raw_frames`, `_query_columns_cached_raw`), commit / restore / backup API |
| `visibility_plot.py`, `visibility_raster.py`, `visibility_scatter.py` | Panels: stale handling after flag changes, overlays, async re-render, busy cursor JS |
| `remote_reduction_context.py`, `remote_registrations.py` | Remote API (P_local / worker) |
| `cubevis/remote/*` | Transport (relay pass-through, binary buffers, frame raw segments) |
| `cubevisjs/src/bokeh/tools/flag_tool.ts` | FlagTool: sends the box, calls `response_callback`, busy cursor |

### 2.2 Data model and conventions

* **`FlagDelta`** (frozen) is one operation, in one of two representations:
  * **region** — coordinates (time range, `SpwChannels`, baselines as
    antenna-name pairs, correlation, scans, fields, extend flags).  Produced
    by raster boxes with the identity filter, and verified against every
    partition (falls back to samples if not exactly reproducible).
  * **sample set** — `SampleBlock`s (per SPW: times, baselines, frequencies,
    channels, correlations, bit/index mask).  Produced by scatter boxes and by
    any value-based filter; materialized once and frozen.
* **Order matters**: `fold_deltas` applies operations in order; a later
  unflag overrides an earlier flag.  Padding slots (xarray-ms fills missing
  (time, baseline) rows; `EFFECTIVE_INTEGRATION_TIME` is NaN) are never
  changed, drawn or written.
* **Time** is UNIX seconds in both backends (xarray-ms and xradio present
  `time.attrs["format"] == "unix"`); each delta records `time_format`.  The
  MS `TIME` column is MJD seconds = UNIX + 40587·86400 (used by the arcae
  row lookup and the report's *MS time span*).
* **SPW identity** may be non-unique by name (simulated data: every window
  `<Unknown>`): `SpwKey` = identity + frequency span + channel count;
  `SpwKey.matches` tolerates float noise (keys rebuilt from JSON).
* **Channels** are indices into the full SPW window; frequency order equals
  MS channel order (checked on TW Hya).
* **Baselines** are antenna-name pairs, never integer ids.

### 2.3 From a box to pending flags

1. TS `FlagTool` sends `{x0,x1,y0,y1,flag}` on the panel's flag comm (busy on).
2. `FlagController.handle_box` (no re-entrant lock — the comm already runs it
   under the panel's render lock; taking it again deadlocked) builds the
   request (axes, layers, hidden categories, selection, filter) and runs
   `reader.evaluate_flag_request` in `asyncio.to_thread`.
3. `flag_engine.evaluate_request`:
   * **scatter box, identity filter, no Z-Score layer → cached-frame fast
     path** (`_scatter_box_from_frames`): rows of the panel's own raw frames
     (drawn x/y, identity, on-disk flag) inside the box, not hidden, and
     displayed (Flag) or flagged (Unflag) → `SampleBlock`s.  Same rules and
     counts as the MS path (equivalence tests); `force_ms=True` forces the MS
     path.
   * otherwise the **MS path**: per partition, box mask on the plotted
     quantities, eligibility, the filter (`prepare_fn` for reference
     populations such as Z-Score), then a region or sample-set delta.
4. Preview (if on) shows the proposal (orange overlay, `flag_view="proposal"`);
   Accept → `FlagDB.add`.
5. FlagDB listener → `push_state()` → `reader.set_pending_flags(deltas,
   version, proposal)`; panels get the new `pending_version` stamped on their
   selection and are marked **stale**; the response tells the browser which
   ranges to re-emit, which triggers the debounced re-render.

### 2.4 Flag views (`SelectionSpec.flag_view`, `_FLAG_VIEW` contextvar)

| view | drawn samples | used for |
|---|---|---|
| `effective` | not flagged on disk ⊕ pending | Hide flagged |
| `disk` | not flagged on disk | Show in colour (base image) |
| `pending` | samples whose state pending changes | pending overlay |
| `proposal` | samples the proposal under review changes | proposal overlay |
| `flagged` | flagged now (disk ⊕ pending), never padding | Show flagged data overlay |
| `none` | all valid samples | building raw frames |

Raster: `_flag_mask(ds)` computes the view lazily with dask
(`apply_pending`).  Scatter: raw frames + `frame_keep_mask` (§2.5).

### 2.5 The scatter raw frame cache (central to performance)

* `XArrayReader._raw_frames(xaxis, [(yaxis, pol)], selection)` returns, per
  layer, **every valid sample** with plotted `x`, `y`, identity (`time`,
  `baseline_*`, `frequency`, `__spw`, `__chan`), category columns and
  `__disk_flag`.  Keyed by (backend token, axes, pol, selection fingerprint
  **without** pending version and flag view) + `("raw", cache_generation)`.
* Every view of every pending state is a **row mask** over the same frame:
  `frame_keep_mask(backend, df, pol, view)`.  Row identity is decoded once per
  frame (`_Rows`, lazily, hash-based `pd.factorize`) and the effective state
  is cached per pending-delta list, reusing the longest cached prefix (a new
  flag applies one delta, an undo returns a cached state).  Per-frame
  scratch lives in `flag_engine._FRAME_SLOTS` keyed by `id(df)` with a
  weakref finalizer.
* Filtered frames per state are memoised (`_cv_view_memo`); Z-Score is
  finalized after the view, so its reference population is the drawn one.
* The process-wide `_FrameCache` (LRU, thread-safe, coalesces concurrent
  identical reads) has a budget: `frame_cache_mb` /
  `CUBEVIS_VISPLOT_FRAME_CACHE_MB`, else 1/10 of RAM clamped to
  [256 MiB, 4 GiB].  **If frames do not fit, every flag / redraw re-reads them**
  (logged, §5).
* Single-dish stores (no `baseline_id`) cannot carry identity: detected
  before reading (`_cv_raw_unsupported`) and served by the legacy per-state
  cache.

### 2.6 Redraws after a change

* Stale panels re-read on their next re-render (`_handle_rerender`).
  Zoomed in clearly beyond the reference resolution, the scatter does a single
  viewport (Level-2) query (`_prepare_stale_render` returns True); its result
  already carries the full-data extent, global scaling and colour-bar inputs.
* Overlay-only changes (Show flagged, colours, display switch with nothing
  pending) don't mark panels stale: `push_state(data_changed=False)` sets
  `_overlays_stale`; the raster recomputes only its overlay aggregates.
* Re-renders run in a worker thread (`_handle_rerender_async`) — on the event
  loop they starved the websocket heartbeat.
* Busy cursor: `window.__cvSetBusy` is reference-counted, defined by a page
  init script, released two animation frames after the image update, held
  across the 300 ms re-render debounce after flag / Plot replies, with a
  200 ms grace before going idle and a visible "Working…" chip (the OS only
  redraws the mouse cursor on movement).

### 2.7 Writing flags (`flag_commit.py`)

Common to both formats: confirmation dialog → `reader.commit_pending_flags`
(in the worker for remote sessions) →

1. **expected state**: only partitions an operation touches (coordinates
   only), and only their touched (time × baseline) rectangle, are read; the
   pending fold is the display's own (`apply_pending`).
2. **backup** of exactly the samples / rows that change (none when nothing
   changes — then nothing is written and the GUI says so).
3. **write**, close/reopen the backend.
4. **verify**: the on-disk flags of the touched rectangles must equal the
   expected state; mismatches are reported by kind (not flagged, not
   unflagged, collateral) with the backup name.
5. **refresh cached frames**: raw frames snapshot before closing are put
   back with `__disk_flag` updated by the verified fold (no re-read); the GUI
   then only re-filters.  If that is not possible the cache generation is
   bumped (full re-read).

* **MSv2 (arcae)**: row lookup `(DATA_DESC_ID, TIME µs, ANTENNA1, ANTENNA2)`
  (refuses missing or ambiguous rows); FLAG written per data description
  (variably shaped across DDIDs); `FLAG_ROW = all(FLAG)` for written rows;
  backup `<ms>.visplot_flag_backup_<ts>.npz` (rows, FLAG, FLAG_ROW per DDID);
  a CASA flag version via `flagmanager` when casatools works; one **HISTORY**
  row per commit and restore (MESSAGE, ORIGIN `cubevis.visplot`,
  APPLICATION `visplot`, CLI_COMMAND ≤ 50 operation descriptions,
  APP_PARAMS cubevis version / backup / counts).
* **MSv4 (zarr)**: coordinate (`vindex`) writes into the data group's flag
  variable; bit-field flags keep other bits; backup
  `<store>.visplot_flag_backup_<ts>.npz`.
* **Restore**: `restore_backup` dispatches on the backup manifest
  (`cubevis.visplot.flag_backup.msv2` / v4); `list_backups` finds them next
  to the data, newest first.

### 2.8 JSON Lines, autosave, report

* `to_jsonl(deltas, header)`: header (format/version, source, data format,
  SPW table, data column, cubevis version, time) + one line per operation.
  Loading refuses files whose SPWs do not exist in the open data and skips
  operations already pending (same `delta_id`).
* **Autosave** (remote sessions only): debounced background write to
  `~/.cache/cubevis/visplot/autosave/<name>-<sha1>.flags.jsonl` (atomic
  replace; removed when nothing is pending).
* Export file names are time-stamped; an explicit existing name is refused.
* Report: `FlagController.report_html()` (reads no visibilities).

### 2.9 Remote execution specifics

* `set_pending_flags` sends one JSON string; afterwards **incremental**
  `sync_pending_flags` (ordered ids + unseen deltas, full resend on mismatch).
* `axis_info` / `identity_tables` are memoised on P_local (coordinate-only;
  key excludes pending state and flag view, includes the data generation).
* Relay pass-through: workers pre-encode `call_method` results; raw frame
  segments worker→kernel; Jupyter binary buffers kernel→P_local.
* `RemoteReductionContext.eval_code / exec_code / reopen(path)` (tests,
  diagnostics), `call_stats()`, `runtime_info()`, `list_flag_backups()`,
  `set_frame_cache_limit_mb()`.

---

## 3. Invariants worth protecting

1. **The display, the InfoTool and the Flag box resolve samples with the same
   code** (cached-frame paths share rules; `force_ms` equivalence tests).
2. **A write is never trusted, it is verified** against the display's fold.
3. **Raw frames are not mutated** — except by `refresh_cached_frames` after a
   verified commit, which also clears that frame's effective-state cache.
   Rendering never writes into frames (the shared-binning memo relies on it).
4. **Padding is never data**: never drawn, flagged, counted or written.
5. **Order of operations is preserved** everywhere (FlagDB, fold, sync,
   JSON, commit).
6. **Nothing heavy on the event loop**: box evaluation, re-renders, commits,
   restores run in threads.
7. **No silent fallbacks that change results**: unusable fast paths fall back
   to the exact MS path; a failed write path reports, it does not guess.

---

## 4. Tests

All under `tests/manual/visplot/` (pytest; most need no data — they build
small simulated MSv2 / MSv4 data sets).

| file | covers |
|---|---|
| `test_flagdb_v2.py` | model, FlagDB, filters, engine, views, overlays, display toggles, report, cached-frame equivalence, single-query redraws |
| `test_flag_commit.py` | MSv2 arcae / MSv4 zarr commit, verify, restore, history, menu, JSON round trip, backup dropdown, autosave, post-commit frame refresh == fresh read |
| `test_flag_commit_real.py` | the same on real data (`MS`, `PS`; copied to tmp) and remotely (`CUBEVIS_TEST_KERNEL` + `CUBEVIS_TEST_KERNEL_MS/_PS`; copied on the kernel host by the worker) |
| `test_flagdb_remote.py`, `test_remote_flagging.py` | remote vs local equality (simulated data with a local kernel, or real data on both hosts via `MS`/`PS` + `CUBEVIS_TEST_KERNEL_MS/_PS`) |
| `test_frame_cache*.py` | cache budget, coalescing (legacy and raw paths) |
| `bench_remote_overhead.py` | remote/local timings incl. `--gui` and per-method breakdown |

Notes: `pytest-asyncio` is needed for `tests/manual/remote/`; some older
scatter/frame tests expect TW Hya and fail on the simulated MS identically
with and without these changes; the real-CASA commit test was removed with
the CASA write path.

---

## 5. Debugging and maintenance

### 5.1 Logs (INFO unless noted)

| line | meaning |
|---|---|
| `visplot timing: flag scatter box evaluated in N s` | whole box handling |
| `visplot timing: scatter box via cached frames resolved / NOT usable` | fast path taken or not |
| `visplot timing: scatter box detail: frames … (rows …), flag state …, box/rows …, sample blocks …` | where a box's time goes; *frames* > 0 means a cache miss |
| `visplot timing: Raster/Scatter redraw N s (after a flag change)` | per-panel re-render |
| `visplot commit (arcae): {expected, rows, write+reopen, verify, refresh_frames}` | commit steps |
| `visplot frame cache: evicted …` / WARNING `… exceeds the … budget` | cache too small → re-reads |

`CUBEVIS_DEBUG=1` adds remote call logging; `CUBEVIS_FRAME_DEBUG=1`
(+ `CUBEVIS_FRAME_DEBUG_PATH`) traces transport frames (slow; transport bugs
only).

### 5.2 Symptoms and first checks

| symptom | check |
|---|---|
| flag box / redraw slow on large data | `scatter box detail` *frames* time and eviction lines → raise `frame_cache_mb` (TW Hya all fields: ~1.5 GiB per frame, two layers ⇒ ≥ 3.5 GiB; on a 24 GB Mac the default 2.4 GiB evicted them: box 3.6 s → 0.5 s, redraw 10.2 s → 7.6 s with 12 GiB) |
| busy cursor gaps | `window.__cvBusyN` in the browser console; every request must balance true/false |
| "busy, then nothing happens" | a handler awaiting a lock its comm already holds (the original deadlock) |
| websocket reconnects | something heavy on the event loop; must be `asyncio.to_thread` |
| remote numbers unchanged after an update | `remote.runtime_info()` / benchmark header: the kernel environment imports its own installed cubevis |
| remote tests skip with `create_object` errors | path not visible on the kernel host (no shared filesystem); set `*_KERNEL_MS/_PS` |
| commit reports mismatches | do not retry blindly: restore the backup; compare `verify` kinds; reproduce with `test_flag_commit_real.py` |
| scatter box result differs from expectation | rerun with `force_ms=True`; the two paths must agree |

### 5.3 Making changes safely

* New quantity, axis or category column the flag paths must honour: extend
  the raw frame (identity columns are cheap) and add it to the equivalence
  tests (`*_matches_ms_path`, `*_from_frames_*`).
* New remote method: add it to `VisplotRemoteBackend` (timed automatically)
  and `RemoteReductionContext` (pre-encoded automatically); structured
  arguments as one JSON string; coordinate-only results through `_memo_call`;
  add a local-vs-remote test.
* New constructor parameter: document it in the `VisibilityPlotter`
  docstring and run `python3 scripts/sync_layers` (`--check` in CI).
* TypeScript changes (FlagTool): rebuild `cubevisjs` and update the bundled
  `cubevis/__js__/…/cubevisjs.min.js`.
* Anything that writes data: keep the five commit steps of §2.7 (expected,
  backup, write, verify, refresh) — verification is what found the CASA
  shortfall.

---

## 6. Decisions and their reasons

1. **Pending flags first, writing later**; preview optional and off by default.
2. **Scatter flags only what is displayed** (hidden categories excluded).
3. **The 1:1 zoom gate was removed**; flags are exact at any zoom.
4. **CASA `flagdata` writes and exported flagdata scripts were removed**
   (2026-10-01): written through CASA's selection language, a 52,624-sample
   operation on TW Hya came out 2,689 samples short (reproducible with the
   exported `sis14_twhya_calibrated_flagged.ms.flagdata.py`).  JSON is the
   exchange format; arcae / zarr writes are exact and verified.
5. **MSv4 backups are side files** (no flag versions in MSv4).
6. **"Show flagged data" stays fully in the flag colour**; **"Hide flagged"
   shows what a fresh open after the commit shows** (pinned by a test) — no
   partial-cell marker.
7. **Autosave only for remote sessions** (no overhead locally).
8. **HISTORY rows, not flag-command tables**, for the MS audit trail.
