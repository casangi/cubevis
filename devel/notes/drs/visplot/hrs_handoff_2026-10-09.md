# HRS visplot: handoff, 2026-10-09

For whoever (person or Claude session) picks up the HRS work on `visplot`
next. Replaces `hrs_handoff_2026-10-07.md`. Everything needed is in this
directory and in `devel/tools/visplot_headless/`; this note says where,
what state things are in, and what was learned the hard way.

## Read first

| File | What it is |
|---|---|
| `hrs_requirements.md` | The requirement (CASR-385) |
| `hrs_visplot_plan.md` | The plan, H1 to H8. Each milestone has a dated **State** line; section 6 is the dated tracking table; section 7 is the running list of performance and compute pain points |
| `hrs_h6_scatter_average.md` | The milestone in progress |
| `hrs_h5_flag_reach.md`, `hrs_h5_filters.md` | Flag reaches and the curated filters; most of the flag-tool browser lessons are here |
| `devel/tools/visplot_headless/README.md` | How to run a live plotter in headless Chromium and drive it |

The per-milestone notes (`hrs_h1_...` to `hrs_h4_...`, `hrs_raster_zoom.md`)
are reference for those areas.

## State

`main` at `ca927cc`, plus the housekeeping delivery of 2026-10-09 (plan
state lines, section 7, `devel/tools/visplot_headless/`, this note).

| Item | State |
|---|---|
| H1 raster averaging, H1b phase-safe resample | Done |
| H2 phase RMS, coherence, windows, DIFF quantities, presets | Done |
| H3 selection tables, baseline stepping and ordering | Done |
| H4 all-baseline waterfall, `baseline_combine`, decimated zoom | Done |
| H5 Flag reaches, Shift / Option boxes, curated filters | Done; confirmed by Darrell on macOS |
| H6 slice 1, scatter averaging | Done and confirmed, **except the busy-cursor bug below** |
| H6 slice 2 | **Next** |
| H7 antenna x antenna matrix | Not started |
| H8 plot summary dialog, static output, user guide | Not started; dialog design agreed (plan, H8) |

## Next, in order

1. **Busy cursor ends before an averaged scatter finishes** (Darrell,
   macOS). `ca927cc` made the viewport-redraw debounce count as busy from
   the moment it is scheduled; the headless timeline
   (`examples/busy_timeline.py`) then shows busy held to the final image
   on TW Hya / 3c279, yet Darrell still sees it end early. Leading
   suspect: `GIVE_UP_MS = 30000` in `_CV_SET_BUSY_JS`
   (`visibility_plot.py`) forces idle after 30 s whatever is still in
   flight. Ask how long his Plot takes and on what data. If that is it,
   the give-up should apply to a request that has had no reply (per
   request, reset by progress), not to the whole busy period. Second
   suspect: the redraw after Plot recomputes the averaging (plan,
   section 7).
2. **H6 slice 2.** Spectrum preset (Amplitude above Phase vs Frequency,
   scan averaged) and Time series preset (Amplitude above Phase vs Time,
   all channels averaged): both panels scatter, Over / Under layout,
   averaging set by the preset. Confirm autocorrelations can be selected
   and plotted. Presets must work with baseline and antenna iteration
   (Prev / Next). Exit (plan): averaged values match numpy.
3. H7, then H8 (plot summary dialog first), then Correlation as a raster
   axis (offered in the axis lists; says "not implemented").

## Decided with Darrell (2026-10-09)

- **Add only what users ask for**, preferably more than once. The H5 / H6
  surface is large enough; new conveniences wait for user requests.
  Accordingly: scatter boxes do **not** honour Flag reaches (raster boxes
  do); not built unless asked.
- A stale error message reappearing after a success message clears: not
  reproduced, minor, left until it shows again.

## Known open issues

- Busy cursor (above).
- Multi-SPW Baseline x Time shows the first SPW only.
- The Baseline table does not narrow to the ticked antennas.
- Two-line title: a clipped fragment above it after a replot (seen once
  by Darrell; not reproduced headlessly).
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
- **Live GUI checks:** `devel/tools/visplot_headless/live.sh`. Use
  `field=3c279` on TW Hya (all fields runs out of memory). Use real
  Playwright mouse / keyboard; synthetic DOM events miss Bokeh's gestures.
- cubevisjs: TypeScript in `cubevisjs/src/bokeh`; `bokeh build` in
  `cubevisjs`; copy `dist/cubevisjs.min.js` to all five
  `cubevis/__js__/bokeh-3.{6..10}/`.
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
