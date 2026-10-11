# HRS visplot: handoff, 2026-10-09

For whoever (person or Claude session) picks up the HRS work on `visplot`
next. Replaces `hrs_handoff_2026-10-07.md`. Updated at the end of the
third session (same day): state, next steps and practical notes below. Everything needed is in this
directory and in `devel/tools/visplot_headless/`; this note says where,
what state things are in, and what was learned the hard way.

## Read first

| File | What it is |
|---|---|
| `hrs_requirements.md` | The requirement (CASR-385) |
| `hrs_visplot_plan.md` | The plan, H1 to H8. Each milestone has a dated **State** line; section 6 is the dated tracking table; section 7 is the running list of performance and compute pain points |
| `hrs_h6_scatter_average.md`, `hrs_h6_scatter_presets.md` | H6 slices 1 and 2 (the latter also covers the busy-indicator watchdog, autocorrelations, render size, sparse points) |
| `hrs_h7_antenna_matrix.md` | H7, the cross-partition raster combine, the two-line-title repaint fix |
| `hrs_h5_flag_reach.md`, `hrs_h5_filters.md` | Flag reaches and the curated filters; most of the flag-tool browser lessons are here |
| `devel/tools/visplot_headless/README.md` | How to run a live plotter in headless Chromium and drive it |

The per-milestone notes (`hrs_h1_...` to `hrs_h4_...`, `hrs_raster_zoom.md`)
are reference for those areas.

## State

`main` at `3d34624` (H6 slice 2 committed after Darrell's macOS test),
plus the 2026-10-10 follow-up delivery (`hrs_h6_scatter_presets.md`,
section 8): wheel zoom off by default, busy overlay and watchdog
hardened, Pin layout, stepping skips rows without data.

| Item | State |
|---|---|
| H1 raster averaging, H1b phase-safe resample | Done |
| H2 phase RMS, coherence, windows, DIFF quantities, presets | Done |
| H3 selection tables, baseline stepping and ordering | Done |
| H4 all-baseline waterfall, `baseline_combine`, decimated zoom | Done |
| H5 Flag reaches, Shift / Option boxes, curated filters | Done; confirmed by Darrell on macOS |
| H6 slice 1, scatter averaging | Done and confirmed |
| Busy indicator | Fixed (watchdog); follow-up for "lasts after the plot" delivered 2026-10-10, awaiting test |
| H6 slice 2, Spectrum / Time series, autocorrelations | Committed (`3d34624`) |
| Pin layout, stepping without empty baselines | Delivered 2026-10-10, awaiting test |
| Raster cells several partitions share (multi-SPW) | Combined by weight; delivered 2026-10-11 |
| H7 antenna x antenna matrix | Delivered 2026-10-11, awaiting test |
| H8 plot summary dialog, static output, user guide | Not started; dialog design agreed (plan, H8) |

## Next, in order

0. **Darrell's test of the H7 delivery** (`hrs_h7_antenna_matrix.md`):
   Ant×Ant; multi-SPW rasters now combine SPWs; the title fragment.
   Then H8 (plot summary dialog first).
0. **Darrell's test of the 2026-10-11 delivery** (busy after a long
   Plot; `hrs_h6_scatter_presets.md`, section 9). The 2026-10-10 items
   (stepping, Pin layout, wheel zoom) are confirmed.
1. **Darrell's test of the 2026-10-10 delivery**: a long Plot then a
   two-finger scroll (the page should scroll, nothing zoom); the busy
   cursor ending with the last redraw; Pin layout with each preset;
   Baseline / Antenna ◀ ▶ on TW Hya 3c279 (no empty plots; dimmed
   "(no data)" rows).
2. H7, then H8 (plot summary dialog first), then Correlation as a raster
   axis (offered in the axis lists; says "not implemented").

## Decided with Darrell (2026-10-10)

- Wheel zoom is off until its toolbar button is clicked.
- Stepping skips baselines / antennas with no rows; no "All" checkbox
  (a fully flagged baseline has rows and is still stepped through).
- Pin layout: a sidebar checkbox; the Layout buttons do not pin.

## Decided with Darrell (2026-10-09)

- **Add only what users ask for**, preferably more than once. The H5 / H6
  surface is large enough; new conveniences wait for user requests.
  Accordingly: scatter boxes do **not** honour Flag reaches (raster boxes
  do); not built unless asked.
- A stale error message reappearing after a success message clears: not
  reproduced, minor, left until it shows again.

## Known open issues

- The "rows with data" for stepping come with each Plot: after changing
  the field without plotting, the first ◀ ▶ uses the previous field's.
- Autocorrelations are detected from the first and last 100k rows only.
- The Baseline table does not narrow to the ticked antennas.
- Short noisy windows read somewhat low in Phase RMS; `PHASE_DIFF` above
  90 deg where a window has no coherent mean.
- No VLBI-style test dataset. TW Hya (ALMA, one SPW, so "all spectral
  windows" reach cannot be shown there) is the real-data check.

## Working conventions (Darrell's)

- Deliveries are zip files of complete files, named with part of the base
  commit hash, unzipped at the root of a cubevis checkout. He tests in a
  browser and runs the regression tests, then commits and pushes to
  `main`. A Claude session works in a read-only clone and **never
  pushes**; before each slice, `git fetch` and check `origin/main` equals
  the tree last delivered, then move onto it. Check what he committed:
  `13dc470` missed a new test file.
- MSv2 primary; MSv4 parity tests for new data paths.
- One kind of help only: status-area text while the pointer is over a
  control (`VisibilityPlotter._hover`, `EvHover`). No tooltips.
- Every new GUI control gets a constructor / task argument with a literal
  default; then `python -m scripts.sync_layers` and `--check`.
- Fix problems where found. Say what a view is *for*; he is not an
  astronomer.
- No Bokeh server; transport is `cubevis.bokeh.transport`.

## Practical notes

- **Tests:** `devel/tools/visplot_headless/runtests.sh <outdir>`, one file
  per process. Baseline at `ca927cc` in the 8 GB cloud sandbox: all pass
  except four files killed for memory
  (`test_colorize_by_axis_part5d_legend_status`, `test_frame_cache`,
  `test_info_block_integration`, `test_visibility_scatter`), 4 setup
  errors in `test_zscore_colorization`, and five files that collect no
  tests (rc=5). Compare the summary before and after a change.
- **Environment (cloud sandbox):** `pip install -e '.[visplot,notebook]'
  pytest pytest-timeout jupyter_client ipykernel`; without
  `jupyter_client` the remote test files fail at import (that was the
  third session's first baseline: ignore those rows or install it).
- **Slow replies:** `SLOW_PLOT=<s>` delays every Plot reply in the live
  harness; `VW` sets the browser width.
- **Live GUI checks:** `devel/tools/visplot_headless/live.sh`. Use
  `field=3c279` on TW Hya (all fields runs out of memory). Use real
  Playwright mouse / keyboard; synthetic DOM events miss Bokeh's gestures.
- cubevisjs: TypeScript in `cubevisjs/src/bokeh`; `npm ci` then `bokeh
  build` in `cubevisjs`; copy `dist/cubevisjs.min.js` to all five
  `cubevis/__js__/bokeh-3.{6..10}/`. The build reproduces the committed
  bundle byte for byte, so a diff there is only your change. **It does
  not fail on type errors**: a method added to the wrong class (two
  classes in `comm_mgr.ts` have `setSharedState`) compiled and shipped;
  check the result in the page (`Object.getOwnPropertyNames(...)`).
- Panels render at the size the browser sends (`set_pixel_size`), so
  headless checks of image shapes need a Plot or redraw message with
  `w` / `h`; constructed panels keep `plot_width` / `plot_height`.
- `_do_plot_js` and its JS string constants stay bare module-level names
  (`test_checkbox_guard.py` resolves them by AST); a test pins the
  substring `panel1_sd_sel, panel1_st_sel, panel1_sc_sel);`.
- A hidden gear tab's `Select` throws when set before the tab was opened;
  preset JS wraps such assignments in `try`.
- Bokeh puts `max-height: 100%` on column children; the sidebar sections
  override it (`max-height: none`), and the status area has a fixed
  height so hover help cannot shrink the sidebar.
- While any flag tool is active, `UIGestures.press_threshold` is raised
  so a drag that pauses before moving is not taken as a press.
