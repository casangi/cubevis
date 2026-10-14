# HRS H8, part A: getting visplot ready for testing by astronomers

*2026-10-11, fourth session. Base `main` at `5c0d466`. Plan:
`hrs_visplot_plan.md` section 8; previous handoff:
`hrs_handoff_2026-10-11_h8.md`.*

The H8 handoff split the remaining work by one question: does it change
what a tester sees or uses? This session did the "before testing" part
(A0 to A5), the small hardcopy items (B1, B2), and the first look at VLBI
data, which added one more control (autocorrelations).

## Decisions taken with Darrell (2026-10-11)

| Question | Decision |
|---|---|
| UV range | Implement (row filter in both backends) |
| Per-panel arguments | Yes. Darrell floated `plot_a` ... `plot_d` for up to four panels; agreed form is one list, `panels=[{...}, {...}]`, which grows without new names. At most 2 for now, a third raises |
| Plot summary trigger | A Bokeh toolbar tool opening a separate page with the practical, useful details |
| Where the summary is built | Python, in the process holding the plotter (with a remote kernel that is still the local plotter, which reports the kernel); only the page crosses to the browser |
| Correlation as a raster axis | Left to Claude: removed (2 to 4 cells wide is not useful; AIPS picks the Stokes to show, and flags reach every Stokes already). A per-panel correlation selector if testers ask |
| Order | A items, then the refactor, then testing |
| Time format | UTC default, elapsed as an option |
| `visplot.notebook()` | Raise "not yet available" |
| VLBI data | Darrell supplied a subset of TL016B (below) |
| Autocorrelations | Proposed to Darrell after the VLBA look (section 7): exclude by default, switch to include or only |

## 1. A0: UTC time (new; found in the review)

Every time axis and the cursor readout showed elapsed time from the first
sample (`1m 46s`, no hours: a 24 h session would read `1439m 20s`), and
nothing showed UTC. Observing logs, `listobs` and the AIPS editors are in
UTC, and the Time range box takes UTC.

- `tick_format`: a UTC branch in `_JS_CORE` and `format_utc` in Python,
  integer arithmetic in units of the last decimal so the two agree
  exactly; decimals follow the tick spacing (`ticks[1] - ticks[0]` in the
  browser, the formatter's locations in matplotlib). Day prefix
  `d/hh:mm:ss` (AIPS style) only when the axis crosses a UTC midnight
  (`PanelSpec.time_day0`, state keys `x_day0` / `y_day0`).
- Ticks on whole seconds, minutes, hours: the time axis's
  `AdaptiveTicker` is switched to base 60, mantissas 1, 2, 5, 10, 15,
  20, 30 (`set_time_ticker`, `cvIntegerTicks`, the toggle JS); the PNG
  export uses `MultipleLocator(utc_tick_step(...))`, the same rule as
  BokehJS's `get_interval`.
- Axis label: `Time [UTC, 2012-11-19]`, or `Time [elapsed from
  2012-11-19 08:58:15 UTC]`. Readout: `2012-11-19 08:59:08.5 UTC`.
- `time_labels="utc" | "elapsed"`, and a "Time labels" switch in the
  Panels section. The switch is a label change only: Python returns each
  panel's state and labels, the browser installs them (no re-query).
- Fixed: the Time range help said the data were taken in 1901. The
  backends' times are UNIX seconds; `_mjd_to_iso` counted them from 1858.
  `SelectionSpec.time_range` was also documented as MJD seconds.
- `visplot.notebook()` raises `NotImplementedError` ("not yet
  available") instead of `ModuleNotFoundError` (template change in
  `scripts/sync_layers/templates`).

Tests: `test_time_labels.py` (Python and node against one table), and
`test_png_export.py` updated for the new default.

## 2. A1: Scan, Time range, UV range

`selection_text.py` parses CASA-style text (forms in its docstring);
bad text is refused with a message (an exception from the constructor,
the status line and no plot from the boxes). The boxes' text is sent
with every Plot; the status line lists what is in force.

- Scan: numbers, `a~b`, `>n`, `<n`, `!n`. Fixed on the way: the Scan
  help listed `ScanInfo.scan_id` (a position, 0, 1, 2 ...) instead of
  the scan numbers (`ScanInfo.name`).
- Time range: full dates, times of day (the day of the data, or the day
  inside the observation that contains them), `d/hh:mm:ss`, `>`, `<`,
  `start+length`, one time (+/- half an integration). One range.
- UV range: `[[lo, hi, unit], ...]` in `SelectionSpec.uv_range` (lists,
  `None` for an open end, so the value survives the remote wire). Rows
  outside the range are made to look like rows the data do not have --
  NaN visibilities, weights, UVW and effective integration time, all
  flags set -- in `XArrayReader._apply_uv_range`, called at the end of
  both backends' `_apply_selection`. Everything downstream already
  leaves absent rows out: plots, statistics, the flag evaluator
  (`valid_mask`) and baseline stepping (`baselines_with_rows`, finite
  UVW; its cache key now includes the UV range). Wavelengths at the
  window's centre frequency, one value per row, as CASA's row selection.
- Flagging under a UV range: a raster box is stored as samples, not a
  coordinate region (a region would also reach the rows the range
  leaves out); the flag message says so. Flag reaches that need a region
  are then refused with the existing message.
- `uvdist_range=` was inert too; it is now the numeric form of
  `uvrange` (shown in the box as text). `time_range=` likewise for
  `timerange`. Give one form or the other.

Two bugs found while testing:

- Plot did nothing when only Scan / Time / UV changed: both
  `axes_changed` blocks compared a hand-kept list of six selection
  fields. Replaced by `_rows_selection_changed`, which compares every
  field in `_ROW_FIELDS`. (`test_antenna_iteration` checks the field is
  in that list now.)
- The first `_apply_uv_range` used `xr.where`, which dropped the
  frequency coordinate's attrs, and with them the spectral window name
  `spw_key_of` matches on: pending flags were silently not applied.
  Now `DataArray.where`, attrs checked by a test.

Tests: `test_selection_text.py` (parsers, both backends, MSv2 = MSv4,
stepping, a raster flag box under a UV range).

## 3. A4: Correlation removed from the raster axis lists

From `_RASTER_AXIS_OPTIONS`, the axes help and the `raster_y` docstring.
The backends' refusal stays (a script could still ask). No other option
answering "not implemented" was found in the Selects or arguments.

## 4. A2: `panels=[...]`

`_resolve_slot_configs` produces one settings dict per slot: slot A from
the flat arguments and preset, slot B the complementary kind (or the pair
preset's Phase scatter), then each `panels[i]` applied and validated like
the flat arguments (unknown keys and a third panel raise). `_build_panels`
builds each slot from its dict, and the gear tabs read each slot's own
axes. Raster dicts also take `scaling`, `vmin`, `vmax`. The rule for the
slot kinds moved there; `test_layout_kind_shortcut` calls it directly.

Tests: `test_panels_argument.py`.

## 5. A3: Describe this plot

A toolbar action beside the gear on every figure opens a tab in the click
(pop-up blockers) and fills it when Python answers (`_SUMMARY_JS`,
`_handle_summary`). The page (`plot_summary.py`) is built from the
panel's render state and the selection its last Plot used, plus the
browser's current zoom: data set (path, format, column, local / remote
and kernel, observation span, contents), the selection in full, the plot
(axes, quantity, averaging, combine, order, slope, windows, colormap,
scaling, range, image cells and decimation, extents, zoom), pending flag
count, software (cubevis with its git commit when run from a checkout,
libraries, Python, platform), and the reproducing `visplot(...)` call
with a Copy button, plus what the call does not reproduce.
`VisibilityPlotter.describe(slot, viewport, html)` and
`reproducing_call()` give the same from Python.

Tests: `test_plot_summary.py` -- the printed call, executed, rebuilds a
plotter that prints the same call (three cases, including `panels`, UV
range and baselines).

Not done: B4 (an image of the panel in the page); the hotkey (C list).

## 6. B1, B2: hardcopy

- `plotfile=` (constructor and task): implies `headless=True`; the task
  template returns `_app()` (the PNG path) when it is set.
- PNG export: every categorical label up to 30 (antenna matrix, small
  arrays), upright when longer than 3 characters, as in the browser; the
  footer wraps between its fields (it was cut off in Over / Under);
  colour-bar labels get as many significant digits as needed to differ
  (`0.001` was printed twice); the scatter legend's peak density shows
  decimals below 10 (`<=0 pts/px` for averaged points).
- Not done from B2: the preset name in the footer (the summary page has
  everything; the footer is the status line). B3 (`hardcopy=` with
  iteration) not started.

## 7. VLBI: TL016B subset

`tl016b_sub.ms` (from Darrell; split from the CASA VLBA tutorial data):
10 VLBA stations, 4 SPWs x 128 channels at 5.0-5.26 GHz, RR/LL, 2 s
integrations, 44 scans over 1.5 h, fields 4C39.25 (fringe finder, first
and last scans), J1154+6022, J1203+6031, autocorrelations present, DATA
only (uncalibrated). All twelve presets export headless.

Findings:

1. **Autocorrelations flattened every all-baseline view.** Median
   amplitude: autocorrelations 1.05, cross-correlations 0.0016. vplot,
   radplot, Time series and Spectrum were scaled to the autocorrelations.
   Added `SelectionSpec.autocorr` (`XArrayReader._apply_autocorr`, both
   backends; `None` = include, the backends' default) and the plotter's
   `autocorr="exclude" | "include" | "only"`, default exclude, with an
   "Autocorrelations" switch in the Data section. An autocorrelation
   named as a baseline (`BR&BR`) is kept. The Ant x Ant diagonal is blank
   under Exclude (help text says so). `test_autocorr.py` (needs `VLBA`).
2. **Sparse fields leave most of the time axis blank** (the fringe
   finder: two scans 1.5 h apart). A consequence of real time with gaps
   (Darrell's decision, raster / scatter consistency); the Scan and Time
   range boxes are the workaround now. "Compress gaps" only if testers
   ask.
3. Spectrum takes 47 s headless on 2 scans of 4C39.25 (Time series 31 s;
   other presets about 11 s): section 7 of the plan.
4. The SPW table shows `none` as the window name (the VLBA MS has empty
   names). Cosmetic, not changed.
5. The phase-vs-frequency panel shows the sloped tracks of residual
   delays: visplot is showing what an uncalibrated VLBI data set looks
   like.

## 8. Also fixed

- Deadlock: a `VisibilityPlotter.__del__` running on a dask worker
  thread (garbage collection of an earlier plotter) blocked on the frame
  cache lock held by the thread waiting for that worker. Seen as a hang
  when two test files built several plotters in one process.
  `__del__` now closes inside `reader.finalizing()`, under which
  `_clear_frame_cache` uses the non-blocking `try_drop_token` (the same
  rule already applied to the backend's `weakref.finalize`).

## 9. Regression

`devel/tools/visplot_headless/runtests.sh`, one file per process, with
`MS`, `PS` (TW Hya MSv4 made with `xarray-ms` `to_zarr`) and `VLBA` set,
compared with the same run on a pristine export of `5c0d466` in the 8 GB
sandbox. Identical results apart from the new files (`test_time_labels`
29, `test_selection_text` 57, `test_panels_argument` 11,
`test_plot_summary` 4, `test_autocorr` 7, all passing) and two existing
tests updated (`test_antenna_iteration`: the change check is now one
helper; `test_raster_zoom`: pins elapsed labels to test the origin).
Unchanged in both: four files killed for memory
(`test_colorize_by_axis_part5d_legend_status`, `test_frame_cache`,
`test_info_block_integration`, `test_visibility_scatter`), 4 setup errors
in `test_zscore_colorization`, five files collecting nothing, and in this
sandbox `test_reconnection` (10 failed) and three `test_remote_*`
collection errors, which fail the same way on the pristine tree.

## 10. Follow-up after Darrell's first look (base `bae9852`)

- Summary page title: the tab reads `tl016b_sub.ms · panel A` (the tab
  shows only the start of a title, so the data set leads); the heading
  `visplot — tl016b_sub.ms, panel A (raster)`.
- **Blocked pop-ups** ("about:blank" twice, Darrell's Chrome): one click
  on Describe ran its callback twice. Every figure is laid out in both
  the Side by Side and Over / Under containers, so each toolbar tool has
  two views, and a `CustomAction` runs once per view; the first run opens
  the tab and spends the click, the second is blocked. The summary
  callback now ignores a repeat within 1 s. Found with
  `POPUPS=block` (new in `drive.py`: Chromium's pop-up blocker on, which
  Playwright otherwise disables). The gear also runs twice; harmless.
- **Spectrum on VLBA: 47 s -> about 18 s.** Grouping for scatter
  averaging turned the categorical name columns into a Python string per
  row (`astype(str)`, 11 s); it now uses the category codes
  (`_scatter_average._key`, test in `test_scatter_average.py`). Most of
  the rest is reading the raw frame twice (Amplitude and Phase panels,
  231 MiB frames against an 803 MiB cache); sharing it between the two
  panels is a C-list item.
- GUI layout: proposal written up, deferred until after testing
  (`visplot_layout_proposal.md`).

## 11. Stepping after a selection change without Plot (base `c89335a`)

The known issue from 2026-10-10 ("after changing the field without
plotting, the first Prev / Next uses the previous field's rows with
data"). The tables' `has_data` flags came only with a Plot reply. Now a
change of Field, the SPW table, Autocorrelations, or the Scan / Time /
UV boxes asks Python for the flags of the selection as it stands
(`_handle_rows`, read-only; text that does not parse is left to Plot to
report), and the tables and stepping use them at once. Checked live on
TW Hya: field 3c279 -> J0522-364 without Plot, 171 -> 210 baselines with
data; UV `<50m`, 14. Test in `test_selection_text.py`.

Still open from the known issues, judged minor for testing:
autocorrelations detected from the first and last 100k rows; the
Baseline table not narrowed to the ticked antennas (Baseline Prev / Next
already steps only through their baselines).

## 12. Remote execution verification (base `e066a79`, 2026-10-12)

Goal: every H8 feature works through `backend="remote"` exactly as it
does locally, with automated checks for regression runs and demo scripts
to learn from.

**Found and fixed**

- The reproducing call ("Describe this plot") left out `kernel_name`, so
  pasting it reopened the data locally. It now names the kernel (the
  remote context is found under `_context` or `_reader`).
- A single-time selection's half-integration fell back to ±0.5 s on a
  remote kernel (the per-row integration time was read locally). The
  observation metadata now carries `integration_time` (median of the
  time coordinate's `integration_time`, both backends), and
  `_integration_half` reads it first.
- `cubevis.__version__` does not exist, so flag exports, the MS HISTORY
  row written by Write to MS, and the worker's diagnostics recorded
  `None`/"unknown". One source now: `plot_summary.cubevis_version()`.
- No check that the kernel runs the same cubevis. The worker now answers
  `software_versions()`; on open the plotter compares (commit, dirty flag,
  version string) with `version_mismatch` and, if they differ, logs a
  warning, adds a yellow ⚠ line to the status line, and the summary page
  gets a "Software on the kernel" section. Identical strings (even dirty)
  are quiet. An older worker without the call reports "unknown (older
  than 2026-10-12)".
- Layer-2 script `remote_visplot_check.py`: its export step still compared
  the removed flagdata export and JSON fields that change every run; now
  JSONL only, ignoring `delta_id`, `created`, `exported`.
- The remote pytest files fail to *collect* without `pytest-asyncio`;
  noted in REMOTE_TESTING.md.

**Tests** (`tests/manual/visplot/`)

- `test_remote_selection.py` (new): local vs remote for Scan, Time range,
  UV range (m, kλ, two ranges), autocorrelations exclude / only; a UV
  range narrows the image; flag box under a UV range; `integration_time`
  in metadata; `software_versions`; `version_mismatch` cases; a remote
  plotter built with `uvrange`, `autocorr`, `time_labels`, `panels`
  (render parity, summary sections, reproducing call, rows refresh,
  integration half, no version warning). 19 pass on the simulated MS,
  31 with `MS=`/`PS=` (TW Hya MSv2 and MSv4), python3 kernel.
- `remote_visplot_check.py`: new steps version check, selection in the
  worker, rows refresh, summary, time labels. 22/22 pass, python3 kernel.
- REMOTE_TESTING.md: layer 1/2 lists, and layer-3 checklist items for the
  version warning, UV / autocorrelation / scan / time selection, time
  labels, Describe this plot, JSON save / load and Write to MS (the stale
  flagdata items replaced).

**Demos** (`demos/visplot-remote/`, not tests): `01_local_and_remote.py`,
`02_remote_flagging.py` (headless, explain each step, `--step` pauses),
`03_remote_gui.py` (a tour, then the window; falls back to running the
plotter as the task does when casatasks is missing). README explains the
two halves and what to look for.

Not checked here: a kernel on a second host (only the local `python3`
kernel is available in this sandbox). The layer-2 script and demo 1 with
`--kernel cvpost106_python312 --remote-ms ...` are the check to run.

## 13. Darrell's first remote run (base `e066a79`, 2026-10-12, second zip)

- **Demo 3 timed out on the first Plot** (cvpost140, TW Hya, all fields):
  a bare `TimeoutError` from the scatter's `query_columns`. Cause: the
  plotter never passed `call_timeout=`, so every remote call had 30 s;
  all the tests passed `call_timeout=300` and never saw it. Now
  `RemoteReductionContext` gives the data calls (`DATA_METHODS`: images,
  scatter, probes, flag evaluation, rows with data, pending-flag sync)
  600 s, `CUBEVIS_REMOTE_TIMEOUT` overrides; small calls keep 30 s; an
  explicit `call_timeout=` still sets one budget for all. A timeout now
  says which call, which kernel, how long, and what to do. Tests:
  `test_timeout_message_and_recovery` (the next call after a timeout is
  answered), `test_plotter_gets_the_data_timeout`.
  Not measured: how long the all-fields TW Hya scatter takes on cvpost
  (this sandbox runs out of memory on it).
- **"Reproduce this plot" drew Over / Under panels twice as tall.** Not
  the call: `layout="over"` at startup kept the side-by-side height
  (550); only the Over / Under button set 280. Startup now sizes them as
  the button does (unless `plot_height=` is given). Checked live.
- **Demos:** `--ms` is only required by demo 1 (02 and 03 need just the
  kernel's path); each step that stands for a user call shows it first
  (`# in your own script:` with `>>>`), and `--step` pauses after showing
  it; the repository root goes first on `sys.path` (Darrell's traceback
  showed a `cubevis` inside `demos/visplot-remote/`), and the first line
  printed is which cubevis is in use.

**Panel sizes (discussion, not built).** `plot_width=` / `plot_height=`
exist but only at construction, and the layout buttons reset to fixed
constants. Proposed: a per-panel Height in the gear tab, stored with the
panel and written into `panels=[dict(..., height=...)]` by the
reproducing call; width keeps stretching to the window. A drag handle
between panels would need a cubevisjs model; better with the layout
refactor after testing.

## 14. Darrell's full regression run (2026-10-12): 27 failures, all explained

- **22 in `test_scatter_average.py` / `test_scatter_presets.py`**
  (`'NoneType' object is not subscriptable`): test order, not code.
  `test_frame_columns.py`'s fixture set the *process-wide* frame cache to
  0 (with `MS=` set) and never restored it, so every later module ran
  without a frame cache and `_query_columns_cached_raw` returned None.
  Passes alone, fails after it; reproduced with the pair (14 failures),
  fixed by restoring the budget in the fixture. Likely present since the
  scatter averaging tests (H6, 10-10).
- **2 metadata contract** (`test_msv2/msv4_backend`): `integration_time`
  (section 12) was not in `reader.METADATA_KEYS`. Added.
- **3 in `test_info_block_integration.py`**: stale tests. The gear-tab pin
  (H7, 10-11) put the close button in a small row with the pin; the X
  pan/zoom link (10-09) added a second `change:end` callback. The tests
  now look into the nested row and pick the busy-cursor callback. This
  module's fixture plots all fields and runs out of memory in the
  sandbox (8 GB), so it was run against a one-field plotter here.

## 15. Panel sizes: `row_height` / `column_width` (base `8683650`, 2026-10-13)

Decided with Darrell: every layout is a grid (One 1 x 1, Side by Side
1 x 2, Over / Under 2 x 1, later 2 x 2), sized by rows and columns;
`plot_width` / `plot_height` removed, no aliases (they applied only at
startup and the first layout click reset them). A size is the plot's
figure (plot with axes, title, toolbar), not the readout and colour bars
under it. Fit is an option, not the Over / Under default: on a 1000 px
window the two panels' readouts and colour bars take ~360 px, so a fit
gives ~220 px rows; Fit has a 250 px floor and the plot area scrolls.

- `panel_sizes.py`: `parse_size` (None, `"fit"`, number, list of up to
  `MAX_ENTRIES`=2; strings like `"300px"`, `"[250,400]"` from the shell),
  `row_heights` / `column_widths` / `cell_sizes`, defaults
  (`ROW_HEIGHT_DEFAULT` 550/550/280, `FILE_WIDTH` 1020/500/1020).
- Plotter: arguments `row_height`, `column_width`; each slot drawn at its
  own cell; numeric `column_width` makes the figures fixed-width;
  startup sizes per position; `_size_source` (JSON spec) for the browser;
  `_adopt_sizes` takes the browser's spec with Plot, Describe this plot
  and Export (which also now send the layout as shown).
- Browser: `_SIZES_JS` init script defines `window.__cvApplySizes`
  (heights per screen position on both kinds, widths when numeric; fit
  measured per panel block, re-fitted on window resize and every second
  as colour bars change). Layout buttons, both preset kinds and Swap call
  it (the old fixed sizes stay as the fallback). *Height* menu
  (`_row_height_sel`, toolbar beside the layout buttons): layout default,
  fit window, 200-900 px, plus the constructor's value if not listed
  (`rows:250,400` for a list).
- Output: `plotfile=` cells are `column_width` x `row_height` (data
  area); `export_png` takes `row_heights` / `col_widths` for rows /
  columns of different sizes (the GUI Export sends each panel's size).
  Note the headless default canvas changed with this: One / Over / Under
  are 1020 wide (were 500), Over / Under rows 280 high (were 550).
- Reproducing call: `row_height` / `column_width` when not the default;
  summary page: Layout, Row height, Column width.
- Tests: `test_panel_sizes.py` (parsing, resolution, PNG rows of
  different heights, per-cell canvas, plotfile size, call/summary,
  adopting the browser's spec, GUI startup sizes and menu). Live check:
  menu 400 -> layout switches keep 400; presets keep 350; `[250, 400]`
  per row, Swap moves them with the positions; fit tracks the window
  (820 px area: 250 floor; 1120 px: 370).
