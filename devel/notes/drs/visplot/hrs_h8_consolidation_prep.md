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
