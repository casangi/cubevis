# visplot flagging — follow-on work

*Remaining improvements after the FlagDB v2 session (September–October 2026),
roughly in priority order, with suggested approaches.  Design and current
behaviour: `visplot_flagging_maintenance.md`.*

---

## 1. Large data sets: frame cache memory

**Status.** TW Hya with all fields has two 31 M-sample scatter frames of
~1.5 GiB each.  On a 24 GB Mac the default budget (1/10 of RAM, 2.4 GiB)
evicted them, so every flag box and redraw re-read the MS:

| | default budget | `CUBEVIS_VISPLOT_FRAME_CACHE_MB=12000` |
|---|---:|---:|
| scatter box evaluation | 3.63 s (3.42 s re-reading frames) | 0.47 s |
| raster redraw after flag | 0.66 s | 0.87 s |
| scatter redraw after flag | 10.16 s | 7.63 s |

`frame_cache_mb=` (constructor, applied in the worker for remote sessions)
and the environment variable are available; evictions and oversize frames
are logged.

**Approach.**
1. **Shrink the frames** (largest win, no behaviour change):
   * `time` float64 → int32 index into a per-frame unique-times array (the
     `_Rows` decoding already builds it);
   * `frequency` float64 → derived from `__spw` + `__chan` (drop the column);
   * keep `x`/`y` float64 unless a fidelity review accepts float32 for
     display (it would not change flagging, which uses identity, but would
     change the drawn positions at the 1e-7 relative level — needs an explicit
     decision);
   * audit columns that are carried but not used by display, flagging,
     probing or colourising.
   Target: ≤ 0.6 GiB per TW Hya all-field frame, so the default budget holds
   both layers.  Test: frame equality of every derived quantity and the
   existing equivalence tests.
2. **Budget policy**: scale the default with the data actually opened, or
   warn once with a recommended `frame_cache_mb` computed from the frame
   sizes seen.
3. **Out-of-core fallback**: when frames cannot fit, keep the x/y columns
   (needed for drawing and boxes) and decode identity on demand per box.

## 2. Large data sets: incremental redraw after a flag

**Status.** With frames cached, the scatter redraw after a flag still takes
~7.6 s on TW Hya all fields: every change re-filters 2 × 31 M rows,
re-aggregates, recomputes the colour scaling and the reference image.

**Approach.** A flag operation changes few samples, so update the drawing by
**subtracting / adding exactly those samples**:
* keep per-layer `sum` and `count` aggregates (display canvas and reference)
  instead of only means; the samples a delta changes are known (row mask from
  `row_delta_mask`); subtract their (x, y) contributions → new mean = sum/count;
* keep the colour-scaling histogram as counts and update it the same way
  (eq-hist curves are recomputed from the histogram, cheap);
* the hover id-grid and Z-Score reference need a rule: recompute when the
  changed samples touch them, else keep;
* undo = add back; clear/reload = full render.
**Fidelity gate:** pixel-identical images, histograms and colour bars
against a full re-render for flag, unflag, undo, hidden categories, both
display modes and both formats (same style as
`test_cached_frames_after_commit_match_a_fresh_read`).

## 3. Large data sets: zoom / pan

**Status.** A plain zoomed scatter redraw (no flag change) took 5.4–5.7 s on
TW Hya all fields with the default budget (partly the eviction above).

**Approach.** Measure again with frames cached; then
* pre-filter rows to the viewport before Datashader (cheap boolean mask,
  helps deep zooms);
* consider a spatial index (sorted by x, or a coarse grid of row ranges) so a
  viewport touches only its rows;
* parallel aggregation (Datashader with dask / numba parallel) for full
  extents;
* reconsider the reference resolution per session from measured timings.

## 4. Remaining flag paths on the MS

Z-Score layers and non-identity filters (amplitude range, MAD, phase
deviation, user filters) still evaluate on the MS path (0.3–0.6 s on the
remote hosts, more on large data).
* Z-Score layers: cache the reference statistics per cell with the frames;
  then the plotted z of each row is computable from the frame.
* Value filters: carry the complex visibility (or amplitude/phase) needed by
  the filter in the frame, opt-in per filter (`ParamSpec`-like declaration of
  required columns).
* Keep `force_ms=True` and add equivalence tests for each.

## 5. Raster cache

The raster re-reads its aggregate after every flag (0.7–0.9 s on TW Hya).
A per-cell `sum`/`count` cache with the incremental update of §2 would make
raster flags O(changed cells).  Region deltas map directly to cells.

## 6. casacore table locking during commits

Not tested: another process (e.g. a CASA session) holding the MS open while
visplot commits.  Approach: explicit `lockoptions` on the arcae writable
table (e.g. `user` locking with a timeout), a clear error when the lock
cannot be acquired, and a test with a second process holding a write lock.
Document the expected behaviour for CASA users.

## 7. CASA `flagdata` shortfall (report upstream)

`sis14_twhya_calibrated_flagged.ms.flagdata.py` (exported before the
removal) reproduces CASA applying exact list-mode selections incompletely
(2,689 of 52,624 samples missed).  Package it with a small MS subset and the
expected sample list for the CASA team.  If CASA fixes it, a flagdata
*script* export could return as an optional, clearly-labelled convenience
(never as the write path).

## 8. Remote sessions

* **Start-up**: zuul06 spends ~3 × 51 s per connection (SSH stall, awaiting
  the sshpyk PR / SSH settings); kernel reuse across sessions would remove
  most of it.
* **Version handshake**: compare client and worker cubevis versions at
  connect (the benchmark already warns via `runtime_info()`).
* **Binary arrays end to end**: arrays still travel base64-in-JSON inside the
  pre-encoded payload (~0.1 s per MB remaining relay cost).
* **Restore / backups remotely**: the dropdown lists kernel-host backups; a
  "download backup" action could help users who want a local copy.

## 9. Usability

* Reload with pending flags: confirm before discarding them (local sessions
  have no autosave by design).
* Operation titles in the report could use the raster's displayed units
  (e.g. channel numbers vs frequencies) consistently.
* Very large sample-set operations: the report builds dense grids per block
  (`SampleBlock.dense()`); for millions of samples switch the tables to
  sparse counting.

## 10. Test infrastructure

* Put `tests/manual/visplot` flag tests into CI with the simulated data sets
  (they need no external data); keep real-data and remote tests opt-in via
  the documented environment variables.
* Fix or mark the older scatter/frame tests that assume TW Hya when run on
  the simulated MS.
* Add a scale test (synthetic ~10 M-sample frame) that asserts the frame
  cache is hit and records timings, to catch regressions like the eviction
  case early.
