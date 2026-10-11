# HRS visplot: handoff for H8 (before consolidation testing)

*2026-10-11, end of the third session. `main` at `f931a8c`. Supersedes
`hrs_handoff_2026-10-09.md` (kept for its history; its conventions and
practical notes are repeated in sections 7 and 8 here).*

## 1. Where things stand

The sprint to get every HRS requirement into visplot is over. H1 to H7
are done and committed (plan, `hrs_visplot_plan.md`, section 3). What
comes next is a long **consolidation period**: astronomers helping with
visplot test it, and their feedback is fixed. Darrell expects to
refactor the GUI during that period (the configuration panel has grown
by accretion and would be better organised in tabs).

So H8 is no longer "static output and hardening, ongoing". It is split
by one question: **does it change what a tester sees or uses?** Anything
that adds a control, a button, or changes what an argument means goes
in *before* the refactor and testing, so it is refactored and tested
with everything else. Hardcopy output is in between. Performance work
runs alongside the testers' feedback.

| Requirement (CASR-385) | Delivered |
|---|---|
| Faster, more reliable than plotms | Two-level rendering, frame cache, remote path, busy watchdog. No benchmark (deferred by decision) |
| Waterfall | Per baseline, all baselines, phase waterfall; stepping by baseline and antenna |
| Phase rms vs time and frequency | Phase RMS / Coherence rasters and scatters, windows, slope removal, presets |
| Flagging like TVFLG, SPFLG, FTFLG | The three views, flag reaches (baseline / antenna / all, channels, correlations, SPWs, scan, field), filters, preview, undo, report, commit with backup |
| Beyond the requirement | Z-Score survey, Spectrum and Time series pairs (POSSM / VPLOT), antenna × antenna matrix (LISTR MATX), multi-SPW rasters combined |

## 2. Read first

1. This document.
2. `hrs_visplot_plan.md`: section 3 (state of each milestone), section 7
   (performance list), section 8 (the H8 partition, the short form of
   section 5 here).
3. For the plot summary: `visplot_export_handoff.md` section 0 and 9.2
   (render state vs pending widget values; the five rules), and
   `panel_spec.py`.
4. For hardcopy: `task-hardcopy-output.md` (the `hardcopy=` design) and
   `visplot_export_handoff.md` 8.19 (`__call__` as the export verb).
5. The milestone notes (`hrs_h1_...` to `hrs_h7_...`) only for the area
   being changed.

## 3. Findings of the H8 review

Each was checked against the code at `f931a8c` or by running it.

**F1. Scan, Time range and UV range do nothing.** The three boxes in the
sidebar's Data section (`_stub_input`, `visibility_plotter.py`) and the
`scan` / `timerange` / `uvrange` constructor and task arguments are
stored and shown, and nothing reads them (`_build_selection`); noted as
known in `visplot_export_handoff.md` 8.4c in August. Their status-area
help describes the syntax and gives examples (`time_hint`,
`uvrange_hint`), so a tester will type a time range, press Plot and
believe the plot is filtered. The backends already filter on
`SelectionSpec.scan` and `SelectionSpec.time_range`; only the text is not
parsed into them. There is no UV-distance row filter in the backends
(`uvdist_range` is a display range).

**F2. Arguments describe one panel; the GUI has two.** The constructor
and `visplot()` take one `raster_y` / `raster_x` / `raster_qty`, one
`scatter_x` / `scatter_y`, one `averaging`, `detrend`, windows,
`scatter_avg_*`, `kind`. Each panel's gear tab holds its own copy of all
of these. Two-panel states (Spectrum: Amplitude over Phase; any panel B
different from the defaults) can only be reached by a `preset`, not
written as arguments. Consequences: the plot summary cannot print "the
`visplot(...)` call that reproduces this", and hardcopy cannot describe
what to draw. Colour scaling limits, colormaps per layer, colouring
mode and panel order are not arguments either.

**F3. Headless export works for every preset.** On TW Hya 3c279,
`VisibilityPlotter(..., preset=p, headless=True)(plotfile=..., theme=
"light")` wrote a correct-looking PNG for waterfall, zscore,
phaserms-time, phase-waterfall, waterfall-all, antmatrix, spectrum and
timeseries in 5 to 8 s each (one process each). Defects seen:
- antenna × antenna in the PNG labels every fourth antenna, horizontally
  (the PNG compositor has its own ticks; the GUI now shows every name,
  upright);
- the provenance footer is cut off at the right in a narrow Over / Under
  export (`... SPW 1/1: ALMA_RB_07#B`);
- the footer does not name the preset, averaging or the stepping
  baseline, which the plot summary would.

The `visplot` task has no `plotfile` / `hardcopy`: the export is
reachable from Python and the GUI's Export PNG button only.

**F4. The user guide is out of date.**
`docs/source/applications/visplot.rst`, "Status of this document" and
"Preview scope", say flagging, averaging, export, iteration by scan and
colorize-by-axis are not available. All of them are now (except
scan / time / UV selection, F1). There is no commissioning workflow
section yet (an H8 item).

**F5. Offered but not implemented.** Correlation is in the raster axis
lists and answers "not implemented". A flag box on the antenna ×
antenna matrix is refused with a reason (that one is a deliberate,
explained refusal and can stay).

**F6. What the plot summary can be built from.**
- `VisibilityPlot._panel_spec()` (`panel_spec.py`): what a panel *drew*
  (axes, quantity, labels, ranges, theme), already the source for export.
  The summary must report render state, not pending gear-tab values
  (export handoff 2.6).
- The pending-flags report (`flag_controls._REPORT_JS`): opens a tab in
  the click (pop-up blockers), Python builds the HTML, the tab is filled
  on reply. The same pattern fits the summary.
- The per-panel toolbar already has the gear `CustomAction`; a second
  action beside it is how a per-panel summary would be reached.
- `casalib.hotkeys` (hotkeys-js, scoped per hovered plot) is how iclean
  binds keys (`toolbox/_cube.py`). Never used from visplot. Key delivery
  in notebooks goes through the `Showable` wrapper.

**F7. Size of the GUI** (input to the refactor; appendix A lists it).
51 constructor arguments. Sidebar: Data (column, field, SPW table,
correlation, scan, antenna table, baseline table, time range, UV range),
Flagging (filter, preview, reaches, reason, display, undo / redo /
clear, report, export / commit, backups), Panels (Pin layout, the gear
tabs). Toolbar: sidebar toggle, Plot, Reload, three layouts, twelve
presets, Export PNG, theme. Each gear tab: about a dozen controls for a
raster, eighteen or so for a scatter.

## 4. Decisions to take with Darrell first

Proposed answers in italics.

1. **F1, UV range.** Implement a UV-distance row filter in both backends
   now, or hide the box and argument until it is? *Implement: it is a
   standard plotms selection and testers will expect it; the per-row UVW
   is already read for `baselines_with_rows`.*
2. **F2, per-panel arguments.** Form? *`panel_a=dict(...)`,
   `panel_b=dict(...)`, keys named as today's flat arguments plus `kind`;
   the flat arguments stay and mean panel A (and both panels where a
   preset does not say otherwise); unknown keys raise. Literal defaults
   for `sync_layers` (`None`).*
3. **Plot summary trigger.** *A per-panel toolbar action next to the gear
   ("Describe this plot"), opening a tab like the flag report. A hotkey
   later, with the consolidation work, once `casalib.hotkeys` has been
   tried in visplot.*
4. **Summary content assembled where?** *In Python, on request (as the
   flag report): it already holds selection, render state and versions,
   and the reproducing call is Python text.*
5. **F5, Correlation as a raster axis.** *Remove it from the lists until
   someone asks for it (the "add only what users ask for" rule).*
6. **Order relative to the refactor.** *A1 to A5 (section 5), then the
   refactor, then testing starts. B1 can ride with A if it is small.*

## 5. The partition

### A. Before consolidation testing (they add or change controls)

Ordered by dependency. Each with both backends where it touches data,
hover help, constructor / task arguments, tests, notes.

| # | Item | Size | Why before testing |
|---|---|---|---|
| A1 | **Scan / Time range / UV range selection made real** (F1): parse the CASA-style text (scan list and ranges, `t0~t1` in the format the help shows, `0~50klambda`, `100~300m`, `>200m`) into `SelectionSpec`; UV filter in both backends; applied by Plot like the other Data controls; bad text reported in the status area, not ignored | M | Testers will use them on the first day; today they silently do nothing |
| A2 | **Per-panel arguments** (F2; decision 2): `panel_a` / `panel_b` in the constructor and task, through `sync_layers`; presets expressed through the same path; colour scaling limits and colormap per panel included | M | Prerequisite for A3's reproducing call and for B3; changes the task signature testers learn |
| A3 | **Plot summary** (plan H8; decisions 3 and 4): per-panel toolbar action; a tab listing dataset (path, column, backend, local / remote), selection in full, axes and quantity, averaging and the H2 window settings, baselines combined, colormap and scaling limits, decimation, displayed range, rows / baselines drawn, pending flags count, cubevis version and git commit, and the `visplot(...)` call (from A2) that reproduces it; a Copy button for the call; settings with no argument listed as such | M | It is what testers attach to a report; it needs a toolbar slot before the refactor |
| A4 | **Remove what does not work** (F5): Correlation from the raster axis lists; check every Select / argument for other "not implemented" answers | S | A tester should not find dead options |
| A5 | **Tester essentials**: correct the guide's status and scope sections (F4); a "Reporting a problem" section (attach the summary); a commissioning workflow section (survey with Z-Score / Phase RMS / Ant×Ant, confirm on the waterfall, flag with reach, commit) written around what each view is for, so it survives the refactor; the control-by-control tour waits for the refactor | S-M | Testers need to know what to try and how to report |

### B. Hardcopy (between the two)

| # | Item | Size | Notes |
|---|---|---|---|
| B1 | `plotfile=` in the `visplot` task (export handoff 8.19, "Remaining"): construct headless, call once, return the path | S | Can ride with A |
| B2 | Export fidelity for the HRS views (F3): matrix labels (every name, upright, as the GUI), footer wrapping in narrow layouts, preset / averaging / baseline in the footer | S | |
| B3 | `hardcopy=` dict (`task-hardcopy-output.md`) with iteration, including **baseline** iteration (one page per baseline is the natural record of a Spectrum / Time series pass), `{iter}` file names, sanitised | M | Needs A2 for "what to draw" |
| B4 | Optional image in the plot summary (the export of that panel) | S | After A3 |

### C. Alongside consolidation feedback

Performance and depth; nothing a tester has to learn.

- Performance (plan section 7): unaveraged scatters re-read large
  selections on every zoomed redraw; first-use numba compile (~2 to 3 s,
  could be warmed at start-up); TW Hya all fields does not fit in 8 GB;
  frame cache budget; `visplot_flagging_follow_on.md` sections 1 to 3
  and 5; `query_columns` cache (export handoff 9, item 2c).
- Known issues: stepping uses the previous field's "rows with data" for
  one step after the field changes without a Plot; autocorrelations
  detected from the first and last 100k rows; the Baseline table does
  not narrow to the ticked antennas; short noisy windows read somewhat
  low in Phase RMS; `PHASE_DIFF` above 90 deg where a window has no
  coherent mean.
- Depth, only if testers ask: coherent combination across SPWs (needs
  complex sums from the partitions); flagging from the antenna matrix;
  Antenna × Time.
- Plot summary hotkey through `casalib.hotkeys`.
- Test infrastructure: the four test files that run out of memory on
  their own; CI with the simulated data sets
  (`visplot_flagging_follow_on.md` section 10).
- Export handoff leftovers: SPW name to id through SPECTRAL_WINDOW (6b),
  MSv4 `field_ids` (6e), a multi-partition single-SPW backend test (6c).
- **A VLBI-style test MS** (plan section 4): an action for Darrell or the
  astronomers; the earlier it arrives, the more of the consolidation it
  can inform. TW Hya has one SPW and 43 antennas.

## 6. Starting A: where things are

- **A1.** Inputs: `_stub_input` and the three `_hint_*` texts in
  `VisibilityPlotter._build_sidebar` (search `scan_inp`); stored strings
  `self._scan_str`, `self._timerange_str`, `self._uvrange_str`. Payload:
  `_SELECTION_PAYLOAD_JS` (what Plot sends), `_handle_plot`,
  `_build_selection`. Data: `SelectionSpec.scan`, `.time_range`
  (`selection.py`); backend filtering in `msv2_backend.py` (search
  `sel.scan`) and `msv4_backend.py`; the shared parts belong in
  `data/reader.py` (export handoff rule 4). The SPW / Antenna / Baseline
  "tick from text" parsers (`antenna_baseline_select.py`) show the
  existing style for CASA-like text. Remote: new selection fields must
  survive `remote_reduction_context` (wire types in `_wire_types.py`).
  `baselines_with_rows` is keyed on field / scan / SPW / time; add UV.
- **A2.** Constructor: `VisibilityPlotter.__init__` and its docstring;
  per-slot state lives on `self._slots[i]` (`.raster`, `.scatter`,
  `.kind`); presets in `_PRESETS`, `_PAIR_PRESETS`, `_pair_preset_js`;
  task layer regenerated by `python -m scripts.sync_layers` (then
  `--check`). Tests that pin argument lists: `test_layout_kind_shortcut`,
  the preset tests.
- **A3.** Toolbar action: where the gear `CustomAction` is added
  (`_GEAR_ICON_DATA_URI`, end of the gear-tab loop); page pattern
  `flag_controls._REPORT_JS` and its Python handler (`action:
  'report'`); render state from `_panel_spec()`; the comm channel and
  `_handle_*` dispatch as for Plot. Version: there is no
  `cubevis.__version__`; `importlib.metadata.version("cubevis")` gives
  the setuptools-scm form (`1.0.92.dev30+g7c026d8`, commit included,
  but only as of the last install of an editable checkout: read `git
  rev-parse` when the package is a checkout).
- **A4.** `_RASTER_AXIS_OPTIONS` in `visibility_plotter.py` (Correlation);
  the refusal itself is in each backend's `_raster_2d` (`if
  Axis.CORRELATION in (y_dim, x_dim)`); `raster_y` / `raster_x`
  docstrings list it too.
- **A5.** `docs/source/applications/visplot.rst`; the workflow content
  is in the milestone notes' "what it is for" paragraphs and the survey
  (`hrs_commissioning_workflow_survey.md`).

## 7. Working conventions (Darrell's)

- Deliveries are zip files of complete files, named with part of the base
  commit hash, unzipped at the root of a cubevis checkout. He tests in a
  browser (macOS, Chrome) and runs the regression tests, then commits and
  pushes to `main`. A Claude session works in a read-only clone and
  **never commits or pushes** (a stop hook asks; decline). Before each
  slice, `git fetch` and check `origin/main` equals the tree last
  delivered, then move onto it. Check what he committed.
- MSv2 primary; MSv4 parity tests for new data paths.
- One kind of help only: status-area text while the pointer is over a
  control (`VisibilityPlotter._hover`, `EvHover`, `_STATIC_HINTS`). No
  tooltips.
- Every new GUI control gets a constructor / task argument with a literal
  default; then `python -m scripts.sync_layers` and `--check`.
- Add only what users ask for, preferably more than once.
- Fix problems where found. Say what a view is *for*; Darrell is not an
  astronomer.
- No Bokeh server; transport is `cubevis.bokeh.transport`; remote
  execution `cubevis.remote`.

## 8. Practical notes

- **Tests:** `devel/tools/visplot_headless/runtests.sh <outdir>`, one file
  per process (env `MS`, `PS`, `CUBEVIS_SRC`). Baseline in the 8 GB cloud
  sandbox at `f931a8c`: all pass except four files killed for memory
  (`test_colorize_by_axis_part5d_legend_status`, `test_frame_cache`,
  `test_info_block_integration`, `test_visibility_scatter`), 4 setup
  errors in `test_zscore_colorization`, five files that collect no tests
  (rc=5). Compare the summary before and after a change.
- **Environment:** `pip install -e '.[visplot,notebook]' pytest
  pytest-timeout jupyter_client ipykernel`. TW Hya MS arrives split:
  `cat sis14_twhya_calibrated_flagged-ms.tar.gz.part* | tar -xzf -`.
- **Live GUI checks:** `devel/tools/visplot_headless/live.sh <steps.py>
  <out> key=value ...` (`SLOW_PLOT`, `VW`, `VH`, `DRIVE_TIMEOUT`). Use
  `field=3c279` (all fields runs out of memory). Use real Playwright
  mouse / keyboard; synthetic DOM events miss Bokeh's gestures.
- **Headless export check:** `VisibilityPlotter(ms=..., field="3c279",
  preset=p, headless=True)(plotfile=..., theme="light")`; one process per
  preset keeps memory down.
- **cubevisjs:** TypeScript in `cubevisjs/src/bokeh`; `npm ci` then
  `bokeh build`; copy `dist/cubevisjs.min.js` to all five
  `cubevis/__js__/bokeh-3.{6..10}/`. The build reproduces the committed
  bundle byte for byte. It does **not** fail on type errors; check the
  result in the page.
- Panels render at the size the browser sends (`set_pixel_size`).
- `_do_plot_js` and its JS string constants stay bare module-level names
  (`test_checkbox_guard.py` resolves them by AST).
- A hidden gear tab's `Select` throws when set before the tab was opened;
  preset JS wraps such assignments in `try`.
- Bokeh puts `max-height: 100%` on column children; sidebar sections
  override it; the status area has a fixed height.
- While a flag tool is active, `UIGestures.press_threshold` is raised.
- A title that changes line count needs the `min_border_top` nudge
  (`hrs_h7_antenna_matrix.md` section 3); anything that changes a figure's
  chrome after the first paint may need the same.

## Appendix A. Controls today (for the refactor)

**Toolbar (top).** ⟨ / ⟩ sidebar; Plot ▶; Reload; One / Side by Side /
Over / Under; presets vplot, radplot, Waterfall, Z-Score, φrms·t,
φrms·ν, φrms·uv, φ Wfall, All-BL, Ant×Ant, Spectrum, Time series; Export
PNG; Dark / Light.

**Sidebar, Data.** Data column; Field; SPW table (+ tick-from-text);
Correlation checkboxes; Scan (inert, F1); Antenna table with Either /
Both ends, ◀ ▶ (+ text); Baseline table, ◀ ▶ (+ text); Time range
(inert); UV range (inert).

**Sidebar, Flagging.** Filter (curated, with its settings); Preview each
proposal; Flag reaches (baselines, channels, correlations, SPWs, scan,
field); Reason; Pending flags display (hide / colour, colour); Show
flagged data (colour); Undo / Redo / Clear; Describe pending flags;
Export / commit (JSON, write to MS, backups list).

**Sidebar, Panels.** Pin layout; the gear tabs.

**Gear tab, each panel.** Pin, ✕; Raster | Scatter; Swap. Raster: Y,
X, quantity, Averaging, Baselines combined, Baseline order, Phase slope,
Time window, Channel window, Colour scaling (histogram with draggable
limits, min / max, reset), Info display (All, cursor tracking, colour
bar). Scatter: X, Y, Average over time, over channels, Averaging, Phase
slope, Time window, Channel window, Layer, Colour scaling per layer,
Colourize (Continuous / Categorical / Statistical and their settings),
Info display.

**Each figure's toolbar.** Pan, box zoom, wheel zoom (off by default),
reset, the flag tools (box, Shift / Option variants), gear.

**Status area (bottom).** Status line, or the hover help of the control
under the pointer.
