# Z-Score: benchmarking, optimization and remaining features (handoff)

Purpose: everything needed to pick up Z-Score performance work (and the
Z-Score feature backlog) in a fresh chat. Supersedes `PART6_HANDOFF.md` for
Z-Score topics. Status at time of writing: Part 6 is functionally complete and
confirmed in the GUI on `sis14_twhya_calibrated_flagged.ms`; the Z-Score raster
is noticeably slow but usable.

Suggested first message for the new chat:
> Continuing visplot Z-Score work. Read ZSCORE_OPTIMIZATION_HANDOFF.md.
> Start with section 3 (benchmark on the real MS), then propose which of
> section 4's options to build. Latest files are in the zips listed in section 8.

---

## 1. What exists today (map of the implementation)

**Statistic.** Per sample: `Z = r / scale * sqrt(2 ln 2)` where `r` = radial
distance of the complex visibility from the baseline's median (median of real
and imaginary parts separately), `scale` = median of `r` over the baseline.
Under noise Z is Rayleigh with unit scale: `P(Z>t) = exp(-t^2/2)`; the
per-sample cutoff 3.5 (Iglewicz & Hoaglin) is a 0.22 % false-alarm rate.
Flagged samples are excluded. Reference population = *all* samples of a
baseline in the current selection (all times, channels).

| Piece | Where | Notes |
|---|---|---|
| Scatter Z-Score (y axis, and "statistical" color source) | `data/_scatter_render.py::compute_baseline_zscore` (pandas `groupby().transform("median")`), staged in `msv2_backend.py` / `msv4_backend.py` `_query_partition_scatter`, finalized by `reader.py::_finalize_zscore_frame` | MSv4 OPT-B fuses partitions so the reference spans all of them |
| Raster Z-Score quantity | `_raster_2d` Z_SCORE branch in both backends (xarray/dask: 3 medians, then `max` over the reduced dim) | Max, not mean, on purpose (mean dilutes outliers) |
| n-aware raster cutoff | `_scatter_render.zscore_cell_cutoff(n)` (Sidak: holds the *per-cell* false-alarm rate at the per-sample one; n=1 -> 3.5, n=384 -> ~4.9) | Backends attach `attrs["zscore_n_reduced"]`; `VisibilityRaster._apply_zscore_cell_cutoff` applies it after each render while the cutoff is automatic |
| Auto threshold scaling | `VisibilityRaster.update_axes` -> `_switch_scaling_owner`: scaling settings are remembered **per quantity** (`scaling_memory.py`); Z-Score's first-visit default is threshold at the n-aware cutoff | Keyed on `_scaling_owner`, *not* on the previous quantity, because `_handle_plot` sets `panel._quantity = None` before `update_axes`. General save/restore framework: see `VIEW_STATE_DESIGN.md` |
| "statistical" scatter coloring | `ScatterLayerSpec.coloring="statistical"`; GUI: third mode in `colorize_controls()`; payload in `buildColorizeArray()`; `_make_scatter_layers` (threshold, vmin 3.5) | Uses the **raster ramp** (`set_statistical_cmap`); compositor treats it like categorical (`_collapse_and_composite`) |
| Per-antenna readout | `compute_antenna_zscore_summary`, `info_panel._antenna_summary_html` | Shown when exactly one antenna is selected (antenna iteration, Prev/Next) |
| Preset | `_PRESETS["zscore"]`, toolbar "Z-Score" button; JS also sets scatter layers to Statistical | Other presets revert a lingering Statistical to Continuous |
| Frame cache | `reader.py::_FrameCache`, `_query_columns_cached` | Lock is held across build **on purpose** (coalesces concurrent requests); finalizer uses non-blocking `try_drop_token` |

## 2. Cost model (why Z-Score is slower)

All other raster quantities are single-pass streaming reductions (mean over
channels). Z-Score needs per-baseline **medians**, which are not streaming: dask
rechunks each baseline's whole time x channel block into memory, three times
(real, imag, radial residual). Cost is therefore both time (~5x Amplitude in the
synthetic benchmark) and **memory** (peak RSS ~4x the data size in the benchmark;
this is the scaling risk on big datasets). Correctness of the exact path was
verified: xarray/dask median over chunked reduced dims equals numpy exactly for
several chunkings.

The scatter path uses pandas groupby medians on flat frames; its cost profile has
**not** been measured.

## 3. Benchmark work still to do (do this first)

Synthetic result (400 time x 200 baselines x 384 chan, 1 core, in-memory data,
identical data for every variant; script: `bench_zscore_synthetic.py`):

| Variant | Time | vs exact |
|---|---|---|
| Amplitude raster (mean) | 0.38 s | - |
| Z-Score exact (today) | 2.10 s | - |
| Reference from 1/8 of samples | 0.73 s | mean rel. diff 0.45 %, max 2.1 % |
| Reference from 1/32 | 0.58 s | mean 0.95 %, max 4.1 % |
| Reference from 1/128 | 0.50 s | mean 1.94 %, max 8.8 % |

Planted outliers (a bad time window on one baseline; one bad sample) were still
found by every variant. **Caveat:** synthetic, in memory, one core: real cost also
includes reading VISIBILITY from disk, which is identical for Amplitude and
Z-Score, so the *relative* benefit on a real MS will be smaller.

To do on the real data (`sis14_twhya_calibrated_flagged.ms`, and the `.ps.zarr`
for MSv4; add an HSA-sized dataset when available):
1. Wall time and peak RSS for the same selection: Amplitude raster, Z-Score raster
   (Time x Baseline), Z-Score raster (Frequency x Baseline), Z-Score scatter (y
   axis), scatter with `coloring="statistical"` vs continuous. Repeat cold and
   warm (frame cache) and with 1 vs N dask threads.
2. Split each into: read, median/reference, elementwise, reduction, shade, wire
   (remote kernel). A quick way: time Amplitude and Z-Score rasters on identical
   selections; the difference is the Z-Score overhead that optimization can
   recover.
3. Record `plotms` timings for equivalent plots (needed for the HRS "faster than
   plotms" requirement; see `HRS_VISPLOT_PLAN.md`).
4. Profile with `py-spy` (already used successfully this session) and dask's
   diagnostics for rechunk/spill.

## 4. Optimization options (ranked)

1. **Subsampled reference.** Estimate each baseline's median/scale from a strided
   subsample (e.g. every 8th time x 4th channel); still test *every* sample
   against it. ~3.6x faster at 1/32 with ~1 % change in values. Cuts the memory
   problem too (only the subsample is rechunked). Decision needed: it makes the
   raster (approximate) differ slightly from the scatter (exact) unless both use
   it; parity tests (MSv2 == MSv4) compare exact values and would need tolerances.
2. **Shared per-baseline reference cache** (this is the deferred "raster/scatter
   shared-reference cache"). Cache `(median_re, median_im, scale)` per baseline
   keyed like the frame cache: `(backend token, selection fingerprint,
   polarization, cache_generation)`. Reused by: re-plot with same selection,
   polarization change, axis swaps, raster + scatter in one view, statistical
   coloring. Independent of (1) and composes with it. Must invalidate on flagging
   (`cache_generation`), since flags change the reference population.
3. **Streaming median.** Two-pass histogram median (bin counts per baseline in a
   streaming pass, then locate the CDF crossing): exact to bin resolution, no
   rechunk, constant memory. More code than (1); worth it if (1)'s approximation
   is unacceptable.
4. **Fuse reads.** When the raster and scatter (or Z-Score and Amplitude) are
   requested together, compute from one VISIBILITY read (OPT-B already does this
   for scatter layers).
5. **Micro-optimizations:** float32 throughout; avoid the separate real/imag
   copies; compute `r` once; thread-count defaults.
6. **UX mitigations independent of speed:** busy indicator exists (`cvSetBusy`);
   consider progressive rendering (coarse first) and a "computing reference..."
   status line.

## 5. Constraints any optimization must preserve

- MSv2 and MSv4 must agree (existing exact-equality tests; relax to tolerance
  only deliberately and document why).
- Pure-noise behavior: with the n-aware cutoff, <2 % of noise cells flagged (test:
  `test_zscore_cell_cutoff.py::TestRealPipelineOnPureNoise`); planted outliers
  must still stand out.
- Flagged samples excluded from the reference.
- Warnings: numpy "All-NaN slice" is suppressed **at the `.compute()` call site**
  (dask is lazy; suppressing inside `_raster_2d` does nothing). Keep it there.
- Frame-cache lock semantics (see table above).
- Suggested acceptance criteria: Z-Score raster <= 2x the Amplitude raster on the
  same selection; peak memory <= ~2x data size; the set of cells above the cutoff
  from the fast path vs the exact path agrees (Jaccard >= 0.98) on the real MS;
  100 % recall on planted outliers.

## 6. Z-Score feature backlog (beyond performance)

Highest value first.
1. **Reference grouping by baseline + scan (or field).** Today the reference is one
   median per baseline over the whole selection. If the selection spans scans or
   fields with genuinely different amplitudes (the sis14 raster shows a bright
   time window across *all* baselines; the scatter shows amplitudes to ~120 vs a
   typical <60), real structure reads as anomalous. Grouping the reference by
   (baseline, scan) would fix this, but changes the statistic and the cache key.
   Not yet built; unclear how much of the busy look is this (a single-Field
   selection is the quick test).
2. **Bandpass/spectral structure.** A per-(baseline, channel) reference (or robust
   low-order fit across frequency) so smooth spectral shape is not flagged.
3. **Cutoff transparency and control.** Show the effective cutoff and its
   false-alarm rate (n-aware value) in the colorbar/status; expose a
   false-alarm-rate control instead of a raw number.
4. **Scatter: count-aware max per pixel.** The statistical scatter shows the *mean*
   Z per pixel, which dilutes sparse outliers; the raster uses max. A max with a
   per-pixel n-aware cutoff (`zscore_cell_cutoff` vectorized over a count
   aggregation) is the principled analogue. Bigger change (mapping, colorbar,
   histogram).
5. **Scatter y-axis choice for the preset:** Amplitude-vs-Time colored by Z is
   partly redundant (Z is largely a function of y when baseline medians are
   similar). Consider Z-Score on y, or coloring by amplitude Z but plotting phase.
6. **Separate amplitude-Z and phase-Z** (radial residual mixes both); useful for
   distinguishing gain problems from phase problems. Ties into phase RMS
   (see the HRS plan).
7. **Antenna ranking table:** per-antenna fraction of samples above cutoff for all
   antennas at once (today's readout is one antenna at a time via iteration).
8. **Time-varying reference** (rolling median) for slowly varying gains.
9. **Candidate flag generator:** "cells above cutoff" as a selectable region that
   feeds the flagging tool (bridge to the HRS flagging requirement).
10. **Palette control in the GUI** (today only constructor `raster_cmap=` /
    `scatter_cmap=`; statistical layers follow `raster_cmap`).
11. Verify the gear-tab "Color scaling" dropdown reflects server-side scaling
    changes (auto threshold on entering Z-Score, restore on leaving); not checked.

## 7. Hazards and lessons learned (read before touching this code)

- `VisibilityPlotter._handle_plot` sets `panel._y_dim = _x_dim = _quantity = None`
  before `update_axes(...)`. Any logic that depends on the *previous* quantity
  silently never fires. Tests must drive `update_axes` the same way
  (`test_zscore_threshold_scaling.py::_plotter_style_update`).
- Unit tests with numpy-backed xarray are eager; dask-backed data is lazy. Tests for
  laziness-sensitive behavior (warnings, compute placement) need dask arrays.
- `xr.where(cond, a, b)` orders dims by `cond` first; transpose explicitly.
- A weakref finalizer must never block; the cache lock is held across builds on
  purpose. An "obvious" fix that split the critical section broke request
  coalescing (`test_frame_cache.py::test_concurrent_requests_for_one_key_read_once`).
- Scaling settings are per-quantity and tracked by `_scaling_owner`; do not reintroduce
  logic that reads the previous `self._quantity` inside `update_axes`.
- Scaling is a separate mechanism from `doPlot()`: set it server-side inside
  `update_axes`; do not send a separate comm message (it raced and crashed).
- `VisibilityRaster._comm` is a different channel object from the plotter's `ctrl`.
- `test_visibility_raster.py` and other real-MS tests cannot run in the sandbox;
  ask the user to run the full suite after each change.
- Local testing of `visibility_plotter.py` / `visibility_raster.py` needs bokeh stubs
  for `cubevis.bokeh.*`; presets/JS are tested by AST-lifting and running the
  shipped JS under node.
- Mutation-check new tests (disable the feature, confirm failures); it caught
  several tests that passed for the wrong reason.

## 8. File and test inventory

Latest version of each changed file (use these, not older zips):
- `visibility_plotter.py`, `visibility_raster.py`, `view_state.py`, `scaling_memory.py`,
  `test_zscore_threshold_scaling.py`, `test_view_state.py`, `test_scaling_memory.py`:
  `view_state_step1.zip` (supersedes `stat_palette.zip` / `leave_zscore2.zip` for these files)
- `visibility_scatter.py`, `test_statistical_palette.py`: `leave_zscore.zip`
- `msv2_backend.py`, `msv4_backend.py`, `_scatter_render.py`,
  `test_zscore_cell_cutoff.py`: `cell_cutoff.zip`
- `reader.py`, `test_frame_cache_deadlock_fix.py`: `frame_cache_fix2.zip`
- `test_raster_zscore.py`, `test_raster_zscore_msv4.py`: `zscore_nan_warning_fix.zip`
- `test_statistical_ui.py`: `statistical_ui.zip`; `test_zscore_preset.py`:
  `zscore_threshold_scaling_fix.zip`
- Earlier Part 6 pieces (threshold scaling, statistical backend, antenna
  iteration, Slice 2 readout, MSv4 mirror): their own zips; unchanged since.
- Benchmark: `bench_zscore_synthetic.py`

Tests (all synthetic unless noted): `test_zscore_colorization.py`,
`test_threshold_scaling.py`, `test_statistical_coloring*.py`,
`test_antenna_iteration.py`, `test_antenna_zscore_summary.py`,
`test_msv4_statistical_and_antenna_summary.py`, `test_raster_zscore*.py`,
`test_zscore_cell_cutoff.py`, `test_zscore_threshold_scaling.py`,
`test_zscore_preset.py`, `test_statistical_ui.py`, `test_statistical_palette.py`,
`test_frame_cache_deadlock_fix.py`. Last local regression: 349 passed, 20 skipped
(real-MS-only); user's real-MS runs passed at each step.

## 9. Open decisions for the user

- Accept ~1 % approximate values for a large speedup (option 4.1), or require
  exact (then 4.3)?
- Should raster and scatter share one reference (exactly consistent) or may they
  differ slightly?
- Priority of backlog item 6.1 (per-scan reference): it changes the statistic.
