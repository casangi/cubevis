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
  (`_row_height_sel`, toolbar beside the layout buttons): "Height:
  default", "Height: fit", 200-900 px, plus the constructor's value if not listed
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

## 16. Wide plots: no stray scrolling, "more to the right" (base `d81e821`, 2026-10-14)

Checked first: with plots wider than the window (`column_width=900`, Side
by Side, 1400 px window) the page does not scroll sideways; the plot
area does, inside itself, and the status bar keeps the window's width at
the bottom with the configuration panel pinned left (the stretch_both
layout of 2026-09). Two fixes:

- **Stray overflow with default sizes.** The page was 2 px too wide (the
  toolbar's last button, its border past the window edge) and the plot
  area 5 px (each readout / colour-bar strip had `width: 100%` plus
  Bokeh's default 5 px margin). Strips are now `calc(100% - 10px)`
  (keeping the 2026-09 guard against a colour bar narrower than its
  plot); the toolbar row has 6 px right padding. Page and plot area are
  exactly the window's width at 900, 1100, 1280 and 1400 px, in all
  three layouts.
- **"more to the right ▸" chip** in `_MORE_BELOW_JS`, beside the "more
  below" one: shown while the plot area has more than 8 px hidden to the
  right, at the area's right edge; a click scrolls to the end. Needed
  because on a Mac the scrollbar is hidden until used.

Tests: `test_panel_sizes.py` (the cue, strip widths, toolbar padding);
`test_info_block_integration.py` updated for the strip width.

## 17. ssh wrapper for password-only login hosts (base `e323e5f`, 2026-10-14)

`demos/visplot-remote/bin/ssh` (install as `~/bin/ssh`, `ln -s ssh
~/bin/scp`): a generic rewrite of Darrell's perl wrapper, for sites whose
login host (and sometimes the hosts behind it) asks for a password even
with a key installed, so sshpyk (no terminal) cannot log in. Nothing
site-specific in the script: the hosts come from
`~/.config/ssh-wrapper/rules` (`<glob>[,<glob>] service=... [account=...]
[backend=security|secret-tool|pass|command] [command=...]`), matched
against each hop's effective HostName; no rules file means a plain
pass-through.

Why the git clone failed with the old wrapper (Darrell, 2026-10-14):
- the rule matched the whole domain, so the key-only git server (port
  7999) was treated as a password host;
- `-p 7999` was not passed to the pre-connect or `-O check`, which went to
  port 22 / the wrong ControlPath;
- git's `ssh -G <host>` probe was treated as a connection.

Found while testing the rewrite, against a local rig (password-only
bastion :2201, password-only compute host behind it via ProxyJump :2202,
key-only git-only server :2203):
- under sshpass, the second hop's shared connection never survived: the
  ProxyJump helper (`ssh -W` mux client) stays on sshpass's pty and is
  killed by the hangup when sshpass exits. Now the login runs in its own
  session (setsid), no terminal, with the wrapper itself as SSH_ASKPASS
  (`SSH_ASKPASS_REQUIRE=force`, OpenSSH 8.4+), so sshpass is no longer
  needed and the password store is read only when ssh asks;
- one password attempt (`NumberOfPasswordPrompts=1`); after a failed login
  with no terminal the wrapper stops (exit 255) instead of running the
  command, whose ProxyJump helper otherwise made two failed attempts of its
  own (lockout risk). With a terminal it hands over so ssh can ask.
- exit status 255 = login failed; any other status = logged in but the
  host refused `true` (git-only servers), treated as success.
- askpass answers only prompts containing "password" (not passphrases,
  host-key questions, or one-time codes).

Rig results: ssh through the bastion with no terminal (both hops shared,
reused, re-established after `-O exit`); scp; git clone from the key-only
server with and without an over-broad rule; `ssh -G`/`-V` pass through;
`-P`/`-o ProxyJump` parsing for scp; sshpyk-style `-O check`, BatchMode,
`-f -N -L`; wrong stored password = exactly one failed attempt on the
server. Not tested here: macOS (`security` backend, Apple's OpenSSH).

## 18. Remote setup guide and helper (base `2ba4a08`, 2026-10-14)

`demos/visplot-remote/00_setup.md` (the one supported path) and
`00_remote_setup.py` (asks for the names, prints the steps filled in, writes
`remote-setup/{ssh_config.txt,rules.txt,steps.md,answers.json}`; `--check`
tests every step read-only, `--check --kernel` also starts the kernel and
draws a PNG). Generic throughout: only the test-data URL names the site.

Path: Miniforge in `$HOME` on both sides (no root, conda-forge default);
`mamba create -n visplot python=3.12 pip ipykernel jupyter_client scipy
regions 'zarr>=3' datashader certifi` + `pip install
"cubevis[visplot]==1.0.95"` on both (same release: the version check);
locally also `pip install sshpyk` and an HTTPS clone of cubevis for the
demos and `bin/ssh`; wrapper in `~/bin` (PATH line after conda's);
password in the keychain (macOS `security`, Linux `secret-tool`, or a
gpg-encrypted file via `command=` — conda-forge has no `pass`); rules file;
`~/.ssh/config` aliases `vp-login` / `vp-remote` (ProxyJump) with
ControlMaster/ControlPath/ControlPersist in a section for just those two;
`sshpyk add ... --remote-kernel-name python3`; `sshpyk list --remote` (what
to look for); test MS `sis14_twhya_calibrated_flagged.ms.tar.gz`; headless
PNG, then `03_remote_gui.py`.

Wrapper change found while writing it: sshpyk recommends `BatchMode yes`,
which disables askpass; the wrapper's login now passes `-o BatchMode=no`.

Checked in the rig (password bastion + password compute host): wrapper with
`BatchMode yes`; `sshpyk add` / `sshpyk list --remote` from a cold start
(all (v)); a kernel started through it (ran on the compute host);
`VisibilityPlotter(..., backend="remote", kernel_name=...)` through the
sshpyk kernel: no version warning, PNG written; the helper script's
`--check --kernel`: all checks pass (X11 warning shown for a `Host *`
`ForwardX11 yes`). Not checked here: macOS, Miniforge installs, the pip
install of 1.0.95 from PyPI.

The visplot-remote demos' examples no longer use site host / kernel names
(`vp_remote`, `/data/on/remote/...`).

## 19. Password stores: pass, and release 1.0.96 (base `1ebb71b`, 2026-10-14)

- `00_remote_setup.py` asks where the password is kept: `keychain`
  (macOS), `secret-tool`, `pass`, or `gpg-file`; the default follows the
  platform and what is installed (macOS keychain; Linux secret-tool, then
  pass, then a gpg file). It prints only that store's commands and writes
  the matching rules line (`backend=security|secret-tool|pass`, or
  `command=gpg -q -d ~/.ssh/<entry>.gpg`); `--check` reports the store.
- `00_setup.md` step 4: a table of the four stores and their rules keys;
  `pass` installation by package manager or, without root, from source
  (`make install PREFIX=$HOME/.local`; not tested here: the sandbox cannot
  reach the source host); gpg-agent caching (`default-cache-ttl` /
  `max-cache-ttl`).
- Wrapper: a failed login with a gpg-backed store (pass, or a gpg command)
  adds a hint that gpg-agent needs the key's passphrase.
- Release pinned to 1.0.96 (`pip install "cubevis[visplot]==1.0.96"`), and
  the demos cloned at the same tag (`git clone --depth 1 --branch v1.0.96`)
  so the wrapper and demos match the installed release.
- Rig: pass (system package, gpg key without passphrase) and gpg-file each
  pass `--check --kernel` from a cold start; a locked key gives the hint.

## 20. Setup guide after Darrell's fresh-account run (base `737fd0e`, 2026-10-15)

- **Two home directories.** The guide read as if the remote host shared
  your home directory (an example path that looked like the local one,
  "same commands on both"). Now: a note that the remote home is normally a
  different place and a path printed there is a path there; and step 1
  handles a *shared* home (install once when both systems match; otherwise
  a separate prefix such as `~/miniforge3-$(uname -s)-$(uname -m)` on the
  remote host) -- installing "on both" into one shared `~/miniforge3` would
  have collided.
- **conda throughout.** `mamba create` + `conda activate` mixed two tools;
  conda (libmamba solver since 23.10) is as fast, and Miniforge sets up
  `conda init`, while `mamba activate` needs `mamba shell init`. mamba
  mentioned as equivalent.
- **Existing conda.** Step 1 is skipped if conda is already installed
  (Miniforge, Miniconda, Anaconda, mamba); step 2's `conda create` now says
  `-c conda-forge`, so a `defaults`-channel installation still uses
  conda-forge.
- `00_remote_setup.py` mirrors these; `--check` now finds the remote
  environment under miniforge3, mambaforge, miniconda3, anaconda3,
  micromamba, conda, or the per-system Miniforge prefix (rig: found under
  `~/miniconda3`).

## 21. Setup guide: no Python on a new account (base `40fbc61`, 2026-10-15)

A new macOS account has no `python` (Darrell's fresh-account run), so the
helper cannot run before the environment exists. Kept in Python (its
`--check` needs jupyter_client and, with `--kernel`, cubevis; a bash
version could only ask and print) and made the order explicit: steps 1-2
from the page, then `conda activate visplot` and the helper from step 3 on
(it still prints 1-2 with your names for the remote host). `git` added to
the environment (a Mac without the command-line tools would offer to
install them, needing an administrator; command-line tools are per
machine, so a new account on a set-up Mac has git). Step 2 allows a
checkout made earlier to read the guide (fetch and check out the release
tag). Problems table: `python: command not found`.

## 22. cubevis-setup: an interactive setup script (base `e6954b4`, 2026-10-16)

Darrell's second fresh-account run did not succeed: a guide plus a helper
that prints commands leaves the user to copy files, edit rules and PATH,
and run tests in the right order, and nothing said when a step silently
did not take. (The failure itself: the account's rules file was in the old
wrapper's `user,host` form; the new wrapper read `ssh` and `dschieb` as
whole-host patterns, matched nothing, and quietly ran `/usr/bin/ssh`.)
His notes also asked: no `(base)` added to the shell start-up; no
assumption that the checkout is in `$HOME`; the environment named
`cubevis` (iclean demos will use it too); a less invasive option than the
wrapper first on PATH; a clear pass / secret-tool choice on Linux.

Replaced by `setup/cubevis-setup`, a Configure-style bash script (bash 3.2,
for macOS): each step checks, explains, lists its changes, asks, makes
them, and tests them; failures offer retry / skip / quit.

- **State in `~/.config/cubevis`** (XDG): `setup.conf`, one
  `hosts/<alias>.conf` per ssh host (login hosts too), generated
  `ssh/config` and `ssh/rules`, the wrapper in `bin/`, `activate`,
  `changes.log` (every change, with backups' names), `setup.log` (every
  command and its output), `backups/`. Plain `key=value` files, never
  sourced. Meant to hold user defaults later.
- **Re-runnable, several hosts.** Steps that pass are not repeated; a host
  not finished is picked up where it stopped (the menu defaults to
  continuing); `--add-host`, `--host ALIAS`, `--remove-host ALIAS`,
  `--check [ALIAS]` (read-only), `--redo`, `--status`. A login host is set
  up once and offered to later hosts; hosts with the same password share
  one stored entry.
- **`~/.ssh/config` is not needed.** The kernels get
  `sshpyk add --ssh-config ~/.config/cubevis/ssh/config --ssh
  ~/.config/cubevis/bin/ssh`; OpenSSH passes `-F` on to the ProxyJump hop
  (checked). So the wrapper is not on PATH and the user's own ssh config
  is untouched. Optional: two marked lines at the top of `~/.ssh/config`
  (`Include ...`), written in place after a backup (a dotfiles symlink
  stays a link); a Host block in an included file does not swallow the
  lines after the Include (checked). Aliases are checked to be unused
  (`ssh -G`) and different from the real name (sshpyk requires it).
- **Remote host from what is there.** A read-only probe script (run with
  `bash -s` whatever the login shell is) reports system, home, conda
  installations that *run* there, an existing environment, downloads
  (github, conda-forge, PyPI), and a shared home (a random id in
  `~/.config/cubevis/.home-id` seen from both sides). Setup then writes one
  script for that host, `hosts/<alias>/remote-setup.sh` (variables, then
  the common `lib/remote-setup.sh`), and runs it over ssh or copies it for
  the user to run. A shared home on the same kind of system reuses the
  local environment; a shared home otherwise gets
  `~/miniforge3-<os>-<arch>`.
- **Password stores**: keychain, secret-tool, pass, gpg-file, command,
  chosen when a host first needs one; on Linux the default is what is in
  use (pass with a store, else secret-tool with a running keyring). For
  gpg-based stores setup has gpg ask for the passphrase in the terminal
  before each login test (the wrapper has none); `GPG_TTY` set.
- **Wrapper changes** (`setup/bin/ssh`, moved from
  `demos/visplot-remote/bin/ssh`): reads every rules file that exists (your
  own `~/.config/ssh-wrapper/rules`, setup's, and `../ssh/rules` beside
  itself); its own `-o BatchMode=no -o NumberOfPasswordPrompts=1` now come
  before the caller's options (ssh keeps the first value, so a caller's
  `BatchMode=yes` turned askpass off); `--explain HOST`; and it now says
  so when a rule looks meant for a host but does not match it (a short
  name, or the old `user,host` form) instead of doing nothing silently.
- **Library fix** (`remote_reduction_context.py`): create Jupyter's runtime
  directory before starting the kernel; on a new account the sshpyk
  provisioner's connection file failed with FileNotFoundError (found by
  the fresh-account test; setup also creates it, for 1.0.96).
- **Tests**: `tests/manual/setup/test_setup_scripts.py` (syntax under the
  chosen bash, no bash-4 forms, state helpers, quoting, the wrapper's
  `--explain` and near-miss notes; `CUBEVIS_TEST_BASH` selects the bash).
  End to end in the sandbox, as fresh local accounts with Apple's bash
  3.2.57 built from source, against the two-hop password rig: pass,
  gpg-file (with and without a passphrase), a symlinked `~/.ssh/config`, a
  second host through the same login host with another user and password,
  `--check`, `--redo`, `--remove-host`, the copy-and-run-yourself path, and
  Miniforge download + install on both sides. conda-forge is blocked in
  the sandbox, so environments were made by a stand-in `conda` (venv +
  pip); not tested: macOS itself (keychain), a real `conda create`.
- Removed: `demos/visplot-remote/00_remote_setup.py`;
  `demos/visplot-remote/00_setup.md` now points at the script.

## 23. Design: remote sessions and `python -m cubevis.setup` (base `ee125b6`, 2026-10-17)

After the bash setup (section 22), Darrell proposed moving setup into the
package (no checkout needed to add kernels) and decoupling kernels from
hosts (cluster reservations change nodes, not environments). Agreed
design in `remote_session_design.md`: a JSON state file as the source of
truth with ssh files generated from it; networks behind one login host;
nodes chosen per session through a `cubevis.remote.Session` object
(health checks, diagnostics, later detach/reattach); a generic per-network
node query; version checks that print upgrade commands; Configure-style
`python -m cubevis.setup`; a bash bootstrap for the first install.
Verified on the rig: an sshpyk kernel starts from a kernel spec held only
in memory (`AsyncKernelManager(kernel_spec_manager=...)`). Work split into
five stages; handoff and test-kit rebuild notes in
`remote_session_handoff.md`.

## 24. Before astronomer testing: fixes and small additions (base `45ee0aa`, 2026-10-18)

Fifth session.  From the readiness review (project doc
`hrs_consolidation_readiness_2026-10-17.md`): a regression run, a live
tour of every preset, and memory measurements on TW Hya.  Agreed with
Darrell: fix A1-A4, add B1-B3, then remote sessions.

**A1. Scatter memory guard.**  A scatter holds every selected sample as a
DataFrame row while it is built; peak use measured at ~65 bytes per
sample on 10-20 M samples and ~110 on 34 M (8 GB Linux).  One TW Hya
field (67 M samples, two correlations) was killed by the kernel at
7.5 GB; a plain `visplot(vis=...)` on all fields (102 M) at start-up.
Now both backends' `_query_columns_raw` estimate the samples from
coordinates first (`_estimate_samples`, summed over the selected
partitions; MSv2 gained the pre-pass MSv4 already had) and
`reader.check_scatter_memory` refuses with `SelectionTooLargeError`
(a `ValueError`) when `110 B x samples` exceeds 70 % of available memory
(`/proc/meminfo` MemAvailable, psutil, else half of physical).
`CUBEVIS_VISPLOT_MEMORY_GUARD=<MB>|off`.  Where it shows: Plot (the
existing "⚠ Panel B scatter error: ..." path), a headless export / task
`plotfile=` (raised), and the GUI's start-up render (panel left empty,
reason in the status line; `VisibilityScatter._render` under `_build`).
Runs where the data are, so with a remote kernel it is the kernel host's
memory.  Averaging does not lower the estimate (applied after reading);
the real fix -- averaging inside the reduction, smaller frames -- is
still the C-list item.
Also found: a headless One-panel export rendered slot B as well (all
the memory of a raster PNG of one TW Hya field went to an invisible
scatter); slot B is no longer rendered then.  Raster PNG of J1037-295:
peak 1.6 -> 0.6 GB.

**A1, corrected the same day (Darrell's test).**  Refusing by default was
wrong: on his Mac (and on Linux with swap) TW Hya with all fields loads
-- the session above was started with `CUBEVIS_VISPLOT_FRAME_CACHE_MB=8192`
and was still refused.  The 8 GB sandbox has no swap, so "killed at
7.5 GB" was a fact about the sandbox, not about visplot on a workstation;
`MemAvailable` / psutil "available" says little about what will fit with
swap and compressed memory.  Now: no limit by default (a selection
estimated above 70 % of available memory is logged as a warning and
loaded); `CUBEVIS_VISPLOT_MEMORY_GUARD=<MB>` is an opt-in hard limit for
hosts where swapping is not wanted.  The frame cache budget is a separate
thing (what is kept between redraws) and was never consulted by the
guard.  Open question raised by Darrell: whether the ~65-110 bytes per
sample peak while a scatter frame is built (about 12x the visibilities)
is inherent, inefficient, or a leak -- to be measured, not assumed.

**A2. Field selection.**  `field="3c279,J0522-364"` was one literal
name: nothing matched, "no unflagged data", no error.
`selection_text.parse_field_text`: names, FIELD_IDs, `a~b`, `>n`/`<n`,
globs, `!`; a name is tried before an id; text matching nothing is
refused at construction.  `_parse_field_names` feeds `SelectionSpec.field_names`
(the backends already took lists); `_parse_field_string` stays for the
one-field uses (Field ◀ ▶ position, menu value) and never raises.  Several
fields from `field=` get one extra Field menu entry (value comma-joined);
Field ◀ ▶ skips it.

**A3. Guide** (`docs/source/applications/visplot.rst`): the launch example
passed `mode="both"` (TypeError: the first thing a tester ran failed);
Installing (Python 3.12/3.13; `arcae` wheels for Linux x86_64 / aarch64,
macOS 14+ arm64 and macOS 15+ x86_64, none for Windows -- corrected after
Darrell pointed to the macOS 15 Intel wheel); `vis`, `kernel_name`, `backend`, `field`, `raster_corr`
rows; layout tour without `mode`; Plot's orange state; Large selections;
RR / LL advice in the workflow; scope.

**A4. Tests.**  The three files killed for memory plotted all fields
(fixtures and Plot messages `field=""`); now 3c279, and they pass
(part5d 44, frame_cache 64, info_block_integration 28).
`test_visibility_scatter` passes per backend but grows past 8 GB over the
MSv2 + MSv4 passes in one process (a leak across its tests, not the frame
cache: clearing that between passes did not help); `runtests.sh` now runs
files with `_backend_matrix` once per backend (`name[msv2]`,
`name[msv4]` in `summary.txt`).  `test_zscore_colorization`: the 4 setup
errors were a `@classmethod` fixture pytest 8+ no longer finds; fixed,
which exposed one test faking 10^9 samples (guard switched off there).

**B1. Correlation shown, per raster.**  A raster showed the first ticked
correlation only, so on RR / LL data an LL-only problem did not show
unless RR was unticked.  `raster_corr="auto" | "parallel" | <product>`
(argument, `panels` key, gear-tab "Correlation shown").  `parallel` is
(XX+YY)/2 or (RR+LL)/2, valid where both hands are unflagged
(`reader.select_polarization`); a product or `parallel` not covered by
the ticks is read anyway (`VisibilityRaster._query_correlation`); flag
boxes on a parallel raster flag both hands (engine `expand_polarization`;
`flag_controls` gives the request the raster's correlations).  Default
unchanged (`auto`).  Title `pol=parallel hands (mean)`; summary and
reproducing call carry it.  Not done: parallel hands for scatter layers
(they already overplot each correlation).

**B2. `vis=`** (any of vis / ms / ps, exactly one): `data_format.py`
detects MSv2 (casacore `table.dat` + `table.info` "Measurement Set", or
ANTENNA/SPECTRAL_WINDOW sub-tables) or MSv4 (`zarr.json` / `.zgroup` /
`.zmetadata`); refuses archives, files, other casacore tables (named by
type) and plain directories with a message.  With a kernel and a path
not on this machine the worker decides (`VisplotRemoteBackend(backend_kind="auto")`,
`RemoteReductionContext.backend_kind`).  The reproducing call prints
`ms=` / `ps=` as resolved.

**B3. Plot ▶ pending.**  Orange (`button_type="warning"`) when a control
doPlot() reads has changed since the last successful Plot, green when it
answers (`_attach_plot_pending`: every Select / TextInput / group / table
selection among doPlot's arguments, except the layout buttons and the
tabs' Pin).  Checked live: change "Correlation shown" -> orange; Plot ->
green.

Through a kernel (checked on the local python3 kernel):
- the worker's refusal came back as `RemoteBackendError` with the remote
  traceback in the text, so the GUI start-up raised instead of leaving the
  panel empty; `RemoteReductionContext._acall` now raises it as the local
  `SelectionTooLargeError`;
- **a constructor that failed after the kernel was started left it open**,
  and `__del__` at interpreter exit then blocked closing the link (seen
  after a failed remote call during the panel build; 10 min, killed).
  `_closing_on_error` around `_resolve_config` and `_build_panels` closes
  the data / kernel at once and re-raises.  Not specific to the guard:
  any failure in those phases with a kernel.

Tests: `test_vis_argument` 14, `test_field_selection` 26,
`test_memory_guard` 9 (one needs `PS`), `test_raster_correlation` 9,
`test_plot_pending` 3.  Updated: the three formerly OOM files (field),
`test_zscore_colorization`, `test_visibility_plotter_iteration` (lifts
`_parse_field_names` too), `test_baseline_combine` and
`test_raster_phase_stats` (payload signature has `rk_sel`).

Regression (`runtests.sh`, MS + PS, 8 GB sandbox) against the same run on
`45ee0aa`: no file killed for memory (was four); no errors (was four);
every file passes except the five that collect nothing, as before.  (The
full run found the four test updates listed above; those files and the
remote ones were re-run after the fixes, individually.)
`test_visibility_raster` / `_scatter` now appear as `[msv2]` / `[msv4]`.

## 25. Raster axis rule in the browser (base `50ad3ed`, 2026-10-18)

Darrell: choosing Antenna 1 (or 2) with a non-antenna axis in a gear tab
showed the right message, but only after the request had reached the
backend (`_antenna_matrix.check_axes`, a remote kernel when one is used)
and left a `NotImplementedError` traceback in the terminal.

One rule, `raster_axes_problem(y, x)` (Y == X; an antenna axis only with
the other), in Python and word for word in JavaScript
(`_RASTER_AXES_RULE_JS`):
- the gear tab's live message (was Y == X only) shows either problem as
  soon as the choice is made;
- doPlot's `rasterConflict` refuses to send and opens that tab (was
  Y == X only);
- `_handle_plot` checks it before anything is read (backstop for presets
  and programmatic callers);
- the constructor refuses it for `raster_y` / `raster_x` / `panels`.
The backend's `check_axes` stays as the last line.

Tests: `test_raster_axes_rule.py` (Python and node give the same answer
for every pair; Plot refuses without calling `query_raster`; constructor);
`test_checkbox_guard` resolves the new name when it rebuilds doPlot's JS.
Checked live: tab message at once, Plot sends nothing, nothing in the
server log.

## 26. Remote-plumbing tests pinned to a local kernel (base `ceec3b1`, 2026-10-18)

Darrell's full run (3224 passed) had one failure:
`test_memory_guard::test_remote_refusal_is_the_same_error` with
`CUBEVIS_TEST_KERNEL=cvpost140_python312`.  The test passed this machine's
(relative) MS path to that kernel, which does not have it; and it could not
have worked there anyway -- it needs `CUBEVIS_VISPLOT_MEMORY_GUARD` to reach
the worker and this checkout's code running in it, while a kernel on
another host runs its own installed cubevis with its own environment.  The
test is about plumbing, so it (and `test_vis_argument`'s `backend_kind="auto"`
test, which had the same flaw but swallowed it as a skip) now always uses a
python3 kernel on this host (`_local_kernel`, skipping without one), with
absolute paths (the kernel's working directory need not be the test's).
Passes with and without `CUBEVIS_TEST_KERNEL` set, and with a relative
`MS=`.

## 27. Remote sessions, stage 1: state and generated ssh files (base `2761bdc`, 2026-10-18)

Design `remote_session_design.md` section 13, stage 1; handoff
`remote_session_handoff.md`.

**`cubevis/setup/`** (new package, standard library only):
- `__init__.py`: `config_dir()` (`$XDG_CONFIG_HOME/cubevis`, else
  `~/.config/cubevis`), `data_file(name)`.
- `state.py`: `load()` (missing file -> empty schema 1; unreadable JSON, no
  schema or a newer schema refused, never overwritten), `save()` (temporary
  file in the same directory, fsync, `os.replace`; file 600, directory 700),
  `State` with plain-dict sections and accessors: `add_network` (login host
  `host` / `user` / `port` / `alias` / `password` / `service`; network
  `user`, `password`, `service`, `node_pattern` -- default `*.<domain>` --,
  `domain`, `shared_fs`, `default_node`, `query`), `add_node` (short names
  completed with the domain; alias `<network>-<node>`; aliases unique and
  different from the host name, as sshpyk needs), `add_environment`,
  `add_kernel`, removal guarded by what uses a record, `flag` / `mark_ok` /
  `flagged` (nodes and environments; flagged, never deleted), `MIGRATIONS`
  hook.
- `sshgen.py`: `config_text` (one `Host` per login host -- alias
  `<network>-login` -- and per node with `ProxyJump` to it; ControlMaster
  auto, ControlPath `<config>/cm/%C`, ControlPersist 8h, ServerAlive 30 x 6),
  `rules_text` (login hosts first, then each network's `node_pattern`, or its
  nodes' names when it has none; keys per store ported from the bash
  `store_rule_keys`), `write()` (rewrites only what changed, says which, warns
  when the ControlPath directory is too long for a socket), `install_wrapper()`
  (copies the packaged wrapper to `bin/ssh`, `bin/scp -> ssh`).
- `data/`: `ssh` (from `setup/bin/ssh`), `probe.sh` (from
  `setup/lib/remote-probe.sh`), `remote-setup.sh` (from
  `setup/lib/remote-setup.sh`) -- moved, not copied; the bash setup reads
  them there (`CV_DATA`) until stage 4 removes it.  pdm ships them: a built
  wheel has all three, the wrapper mode 755.

**Wrapper fix** (design stage 1): when its own login failed it printed the
gpg-agent hint whatever the cause.  The throwaway login's stderr now goes to
a scratch file (removed before the final exec, which skips END blocks) and
its last line is shown; the askpass call reports `found` / `notfound` /
`error` to a second file.  Messages: never asked -> "ssh failed before
asking for a password" (name, network, host key); `notfound` -> "no stored
password was found", with the gpg hint only for a gpg-backed store;
`error` -> the lookup's error; `found` -> "the stored password was
refused".  Checked on the rig for all five cases (also: the sandbox's sshd
had stopped, and the message said "Connection refused" instead of blaming
gpg).

**Tests** (`tests/manual/setup/`, 43 with the rig): `test_state.py` (paths,
data files, empty / bad / newer files, atomic save and a failed save leaving
the old file, migration hook, defaults, name and alias checks, domains,
removal rules, flag / mark_ok); `test_sshgen.py` (keys per store, rule order,
generated config read back with `ssh -F ... -G`, rewrite only on change,
long-path warning, wrapper install, the installed wrapper's `--explain`
finding the generated rules by itself, and with `CUBEVIS_SSH_RIG=1` a
two-hop password login through the generated files with both connections
shared); `test_wrapper_login_messages.py`; `test_setup_scripts.py` now
points at the moved wrapper.  Found by the tests: pytest's temporary paths
are too long for a ControlPath socket -- the tests use short `/tmp`
directories, and `write()` warns about exactly that.

**For Darrell:**
- `import cubevis` takes ~5.6 s (the package `__init__` imports visplot and
  bokeh), so `python -m cubevis.setup` will pay that at every start unless
  `cubevis/__init__.py` imports lazily (PEP 562 `__getattr__`).  Worth doing
  with stage 2; it changes nothing for users of `cubevis.visplot`.
- Alias names chosen: `<network>-login`, `<network>-<node>` (the design's
  example had `cv-node140`).

## 28. Remote sessions, stage 2a: `python -m cubevis.setup` (base `56cc39d`, 2026-10-19)

Stage 2 in two slices; this is 2a: this computer, networks, nodes, logging
in, `status`, `check`.  2b: environments, kernels, versions, `upgrade`,
`prune`, the node query.

**Decided with Darrell (2026-10-19):**
- `cubevis/__init__.py` imports `iclean` / `visplot` lazily (PEP 562
  `__getattr__`): `import cubevis.setup` 5.6 s -> 0.06 s; `from cubevis
  import visplot` unchanged.
- A network's login host is optional: without one there is no
  `<network>-login` and no ProxyJump (nodes reached directly).
- **Inside a network, no bastion.**  Decided at each connection, so a laptop
  that moves does the right thing: for a network with a login host and
  `inside` ranges, the node `Host` blocks carry no ProxyJump; a
  `Match originalhost <aliases> !exec "<python> -m cubevis.setup.inside NET"`
  block adds it when the test says outside.  The test (`inside.py`) is the
  *route*, not "any local address in range" -- Darrell's sample of NRAO
  addresses showed why: libvirt / Docker bridges (192.168.122.1,
  172.17.0.1) on many machines, a public and an inside address on the
  bastion, and home routers' 10.0.0.x.  It resolves up to three nodes
  (default first; 1 s limit) and asks which local address the system
  would send from (UDP `connect`, nothing sent): inside if that is in the
  ranges.  Anything odd answers "outside" (the login host works from
  anywhere).  `python -m cubevis.setup.inside -v NET` says why.  Ranges
  are asked for once (`inside: null` = not asked, `[]` = none), suggested as
  the /16s of the two addresses a node reports for the connection
  (`$SSH_CONNECTION`: the address it was reached from -- the bastion's
  inside one or this computer's -- and its own); the user adds others
  (e.g. 10.2.0.0/16 for desktops like ullr).

**`cubevis/setup/`** additions:
- `ui.py`: say / ask / ask_yn / ask_choice / secret, changes list, `run`
  (logged to `setup.log`), `record_change` (`changes.log`), backups,
  `run_step` (check; do only when the check fails; retry / skip / quit --
  skip when unattended, `--yes`, so it never loops).
- `stores.py`: the password stores, ported from `lib/store.sh` (choose,
  put, has_entry, where, unlock; gpg key creation, pass init).
- `__main__.py`: `python -m cubevis.setup [status | check [NET|NODE] |
  network [NAME] | node NET [NODE...]]`, `--yes`, `--redo`,
  `--answer KEY=VALUE`.  Steps: tools (OpenSSH 8.4+, perl), wrapper install,
  network questions, nodes, one login step per hop (the login host, then
  each node) -- key first, else a stored password (sharing an entry with
  another hop of the same user), with the bash setup's diagnoses (name,
  network, host key) -- inside ranges.  A node that fails is flagged (kept);
  behind a login host the bare "Connection closed by UNKNOWN" (the real
  reason goes to the shared connection) is followed up by asking the login
  host whether it resolves the node.  The old bash setup's `ssh/config` is
  copied to `backups/` before it is replaced.
- `state.py`: user, host and domain names validated (a user name with a
  space made an `ssh/config` that ssh refused, found on the first run).

**Tests** (`tests/manual/setup/`): 66 with `CUBEVIS_SSH_RIG=1` (56 + 10
skipped without): `test_inside.py` (the route rule against Darrell's
addresses, timeouts, state), `test_sshgen.py` additions (no login host;
Match block; on the rig, one hop inside and two outside or without ranges,
both logging in), `test_cli.py` (help, status, refusals, an unattended run
that fails once and skips, the bash config kept; on the rig the whole
interactive run through a real terminal with pexpect -- password login
host, node with the same password, inside ranges -- then `check` and
`status`, and a direct network).  Visplot tests that import through
`cubevis` unchanged with the lazy import.

**Found in Darrell's first run (macOS, 2026-10-19):**
`test_unresolvable_name_says_so_without_gpg_hint` failed. The messages
were the bash setup's older wrapper's: a copy of it was on his PATH, and
the packaged wrapper took it for the real ssh (it skipped only its own
directory), so one wrapper ran inside the other and logged in twice.
Kernels would have hit the same thing.  The wrapper now skips any copy of
itself on PATH (a `#!` script that mentions `SSH_WRAPPER_RULES` near its
top); `test_another_wrapper_copy_on_path_is_not_the_real_ssh`.
- The tests no longer touch the account: `CUBEVIS_SETUP_KNOWN_HOSTS` (tests
  only) adds a final `Host *` block to the generated config with that
  `UserKnownHostsFile` and `StrictHostKeyChecking accept-new`; the wrapper
  test's own config has its own known_hosts; `test_cli.py` clears
  `SSH_WRAPPER_DEBUG`.  The rig tests had been passing only because the
  rig's keys were already in the container's `~/.ssh/known_hosts`.
- 67 passed with `CUBEVIS_SSH_RIG=1` (57 + 10 skipped without), with no
  `~/.ssh/known_hosts` or wrapper log written.

## 29. OpenSSH minimum lowered from 8.4 to 8.0 (base `2ec5a25`, 2026-10-19)

Darrell's first run on RHEL 8 stopped at the tools step: RHEL 8 ships
OpenSSH 8.0p1 (`openssh-clients-8.0p1-25.el8_10`), and the step required
8.4.

* **Why 8.4 was there**: only for `SSH_ASKPASS_REQUIRE=force` (8.4+) in the
  wrapper's throwaway login (section 17).  Before 8.4, ssh uses
  `SSH_ASKPASS` when it has no terminal *and* `DISPLAY` is set.  The login
  already runs under `setsid` with stdin from `/dev/null` (no terminal) and
  sets `DISPLAY=:0` if unset, so older clients take the askpass path too.
  Nothing is drawn: the askpass is the wrapper itself.  8.4+ ignores
  `DISPLAY` under `force`.
* **What else sets a floor**: the generated config uses `ProxyJump` (7.3),
  `ControlPath %C` (6.7), `Match originalhost` / `exec` (6.5), and the
  tests' `StrictHostKeyChecking accept-new` (7.6); `ssh -G` is 6.8.  So 7.6
  by reading.  The check says 8.0 because that is the oldest client tested
  (7.6p1 does not build against OpenSSL 3 here).
* **Tested**: OpenSSH 8.0p1 client built from `openssh-portable` tag
  `V_8_0_P1` (`--without-openssl`), first on PATH, against the rig's
  9.6 sshd's: all 67 tests in `tests/manual/setup` pass with
  `CUBEVIS_SSH_RIG=1`, as they do with 9.6.  The wrapper log shows it ran
  `/opt/openssh80/bin/ssh`.  With the `DISPLAY` line removed, 8.0's login
  fails ("Permission denied (password)", askpass never called), so that
  line is load-bearing for pre-8.4 clients; its comment now says so.
* **Changed**: `cubevis/setup/__main__.py` (check `(8, 0)`), the wrapper's
  comments in `cubevis/setup/data/ssh` and `setup/bin/ssh`, and the bash
  setup's check and text (`setup/lib/local.sh`, `setup/README.md`).
* **Rig**: `devel/tools/ssh_rig.sh` (handoff "Test kit") had never been
  committed; it is in this zip now: `/etc/hosts` entries, user `alice`, password-only
  sshd's on 127.0.0.1:2201/2202 with `UsePAM yes`, from the distribution's
  `openssh-server`.

## 30. Remote sessions, stage 2b part i: a shared configuration directory (base `24f5af8`, 2026-10-21)

Design section 7b (agreed 2026-10-20/21): `~/.config/cubevis` may be a home
shared by a cluster's nodes and a GUI host among them, so nothing setup
keeps may be overwritten or corrupted by another computer, or by a rerun.

* **Per computer: `hosts/<id>/`** (`cubevis/setup/__init__.py`: `host_id`,
  `host_dir`).  `local.json` (the old `local` section and the sessions),
  `ssh/config` and `ssh/rules` (they name this computer's password store,
  and the inside test runs this computer's Python), the wrapper in `bin/`
  (computers may run different cubevis versions), `setup.log`,
  `changes.log`, `backups/`.  `<id>`: the short host name; macOS
  `scutil --get LocalHostName`; `$CUBEVIS_HOST_ID` overrides.
* **Shared: `state.json`**, schema 2 (networks, nodes, environments,
  kernels), and `secrets/` (gpg-file).
* **Saving** (`state.save`): per file, a lock (`FileLock`: `<file>.lock`
  by `O_EXCL`, holding host, pid, time; stale if this computer's process
  has ended, or after `LOCK_STALE` = 120 s; a stale lock is renamed away
  before removal so two runs cannot both break it; waits up to 30 s, then
  names the holder), re-read, three-way `merge` against what this run
  loaded, atomic write (skipped when unchanged).  Merge: records untouched
  here take the file's; untouched there take ours; both changed: field by
  field, ours wins a field both changed; removal vs. change: the removal
  wins over health-only changes (`HEALTH_FIELDS`), otherwise the change
  is kept.  The merged result is applied *in place* (`_apply_in_place`):
  `__main__` holds a network's dict across several saves.
* **Migration** schema 1 -> 2: `local` and `sessions` move to this
  computer's file at the first save; a second computer starts empty.
  `load` writes nothing; `load(host=False)` for the inside test (every
  ssh connection: no host file, no lock).
* **Sockets**: `/tmp/cubevis-<uid>/cm` (`runtime_dir`: mode 700, owner
  checked, a file in the way refused) or `$CUBEVIS_RUNTIME_DIR`.  The
  wrapper makes the directory again before a password login (reboots,
  tmp cleaners).  Not `$XDG_RUNTIME_DIR`: removed at the last logout.
  The socket-path length warning now concerns this directory only.
* **The wrapper's rules**: `../ssh/rules` beside the copy and the user's
  own `~/.config/ssh-wrapper/rules`; no longer `~/.config/cubevis/ssh/rules`
  (the earlier layout's, possibly another computer's).  Only the
  packaged wrapper; the bash setup's `setup/bin/ssh` is unchanged.
* **Earlier layout** (`ssh/`, `bin/ssh`, `cm/` in the directory itself,
  2a's and the bash setup's): left alone, named by `network` / the walk
  and `status`.  The old "replace the bash setup's ssh/config, with a
  backup" step is gone: nothing is written there any more.
* **A second computer** finding networks it did not set up: no password
  store yet, so `rules_text` leaves those hosts out with a comment and a
  warning; the login step then asks for the store and keeps the entry's
  name (`service`) instead of asking for a new one.  Warnings print once
  per run.
* **Tests** (`tests/manual/setup`, 83): `test_state_shared.py` (new:
  host id, split files, migration, merges incl. removal rules, 8 processes
  x 5 saves losing nothing, held and stale locks, socket directory
  checks); `test_cli.py`: the earlier layout left alone, two computers
  (`gui1`, `gui2`, each with its own socket directory) on one directory
  over the rig, the socket directory removed between runs.  All pass on
  the rig with OpenSSH 9.6 and 8.0p1.
* **Not done here** (parts ii-iv): environments and the node-side
  command, the GUI side reading `remote/`, `--inside-here`,
  `CUBEVIS_ROUTE_<net>`.

## 31. Remote sessions, stage 2b part ii: the node side (base `bbf9d27`, 2026-10-21)

`python -m cubevis.setup node-here [NETWORK]`, run logged in on the node
(design 7b).  The walk-through (no command) now asks first: "[p]lots-here
or [n]ode?", suggesting `n` when `$SSH_CONNECTION` is set.

* **New modules.**  `envs.py` (find, classify, check environments),
  `hostinfo.py` (full name, interface addresses with prefixes, the home's
  filesystem type), `nodefile.py` (`remote/<network>/<id>.json`),
  `node_here.py` (the command), `data/envcheck.py` (run with each
  candidate interpreter).
* **The network question.**  Over ssh the default is the domain's first
  label, else the host id without its number (`zuul06` -> `zuul`); not
  over ssh, `local` (this computer by itself: kernels for plots shown
  here; no domain, sharing or query asked).  A computer already in a
  network is offered that one.
* **What the node records** (`node` in its file): network, host id, full
  name, user, home and its filesystem type, addresses and their subnets
  (candidate inside ranges for the GUI side), domain, shared home (asked;
  default from the filesystem: nfs, lustre, gpfs, ... -> yes), the query
  command with the names it printed when run here (a failing command is
  kept, with its error), the cubevis that wrote it.
* **Environments.**  Listed: the ones enabled before, then the ones found
  -- the Python running setup, `$VIRTUAL_ENV`, `$CONDA_PREFIX`, and every
  conda / mamba / micromamba tool's `env list --json` (tools on PATH, in
  `$CONDA_EXE` / `$MAMBA_EXE`, and in `~/miniforge3`, `~/mambaforge`,
  `~/miniconda3`, `~/anaconda3`, `~/micromamba`, `/opt/conda`).  An
  environment several tools list goes with conda when conda lists it.
  Each shows whether cubevis is installed (its `dist-info`, nothing run).
  The user gives numbers and/or paths (an environment's directory or an
  interpreter); the default is the ones enabled before, else the ones
  with cubevis.  For each new one: its kind (asked; guessed from
  `conda-meta` / `pyvenv.cfg`) and a name (default from its directory).
  Not chosen: no longer used (removed from the file, logged).
* **The check** (`envs.check`): `python -c <envcheck.py>` in an empty
  temporary directory (no planted modules), `PYTHONHOME` unset, 300 s.
  It reports Python's version, prefix, the versions of cubevis,
  ipykernel, jupyter_client, bokeh, xarray-ms, arcae, casatasks; *ok* when
  `cubevis.remote.worker_main` (the kernel side) and `ipykernel` import;
  `visplot` when `xarray_ms` imports too (shown, not required).  Failures
  are recorded `flagged` with one line: not installed / does not import
  (the import error) / ipykernel missing / not Python 3 / does not exist;
  "cubevis needs 3.12" added for an older Python.
* **Identity**: the interpreter's path, absolute, symbolic links kept (a
  venv's `bin/python` links to the base interpreter).
* **The managing tool** of a conda/mamba environment: the tool that
  listed it if it is of the chosen kind, else the first of that kind
  found; None when there is none (upgrade scripts, part iv, will say so).
* **The node file** is written only by its computer, under the same lock
  and merge as the state (`state.merge` now takes field dicts other than
  `local`; `node` first in the file).  `state.json` is not touched by
  `node-here`.  `status` lists this computer's node files and their
  environments.
* **state.json environments** (for part iii): `add_environment(name,
  network, python, kind, prefix, tool, hosts)`; `hosts` is `"all"` or a
  list of the network's nodes (another network's node refused);
  `find_environment(network, python)` (identity); `environment_on(name,
  node)`.  `HEALTH_FIELDS` gained `versions`, `python_version`, `visplot`.
* **Tested**: `tests/manual/setup/test_node_here.py` (13): parsing of
  `ip -o addr` and macOS `ifconfig`, kinds and names, candidates from fake
  conda and micromamba tools, checks that fail for each reason, a
  passing check (`CUBEVIS_TEST_KERNEL_PYTHON`: a Python with cubevis,
  ipykernel, bokeh; test environments see its packages through a `.pth`),
  the node file under two concurrent runs, the command with `--yes`
  (rerun dropping an environment; `local`; a failing query; bad paths),
  the role question.  By hand on the rig: logged in as alice over ssh,
  `python -m cubevis.setup` answered at the terminal (role, network,
  shared home, query, a venv and a conda environment taken as mamba).
  Whole suite: 97 pass, OpenSSH 9.6 and 8.0p1, Python 3.12 and 3.13.
* **For part iii**: the GUI host's own environments (network `local`)
  belong in its `hosts/<id>/local.json`, not in the shared `state.json`
  -- with a shared home, gui1's local environments are not gui2's.

### 31a. Revision (2026-10-21, after Darrell's run on zuul06)

* **Choosing environments by name**: `Use [...]` takes numbers, names
  (the name an environment is enabled under, or its directory's name,
  e.g. `sshpyk-python312`) and paths.  A name two candidates share is
  refused with their numbers; nothing changes.
* **Three kinds**: `conda`, `venv`, `python`.  `mamba` dropped: the same
  environments; what differs is the program, now asked for a conda
  environment as "which command do you use to install and upgrade
  packages in it?" (conda, mamba, micromamba, a name on PATH, or a path;
  default: the tool that listed it, else conda) and recorded as `tool`.
  Old `mamba` records read as `conda`, tool kept (node file and
  `state.add_environment`).  Darrell: if cubevis ever upgrades or
  downgrades environments, that is the program it must name; the prompt
  says so.  Design section 8: upgrades stay the user's step; the
  in-use warning and the (not planned) environment lock are noted there.
* **Agreed for part iii** (not built yet): a working directory per
  environment (default `$HOME`; `Session(cwd=)` overrides), environment
  variables per environment set on the node (`node-here`), both applied
  by a small launcher script `node-here` writes on the node (`cd`,
  `export`, `exec <python> "$@"`) and given to sshpyk as `remote_python`
  -- sshpyk runs `$FP_PY -c ...` after checking only that it is an
  executable, and passes neither cwd nor environment.  Nothing is carried
  from the local computer by default; developers get an explicit
  `Session(env={...})`, never stored.
* **Node files moved** (2026-10-22, Darrell): from `remote/<network>/<id>.json`
  to `hosts/<id>/node/<network>.json`, so everything a computer writes is in
  its own directory (one writer per directory; one directory to look at or
  remove).  The GUI side finds a network's nodes as
  `hosts/*/node/<network>.json`.  No migration: while iterating, Darrell
  clears old files himself.  `node-here`'s opening text now names that
  directory (it said "nothing but its file in ~/.config/cubevis/remote",
  but the logs go to `hosts/<id>/` too).

### 31b. How cubevis was installed: detected, not asked (2026-10-22)

Darrell's point: in a conda environment cubevis is often installed with
pip (PyPI only, or from a checkout), so "which command do you install and
upgrade packages with" named the wrong program for the one upgrade setup
cares about.  Now:

* `data/envcheck.py` reports `install`: `INSTALLER` (pip, uv, conda) and
  `direct_url.json` (PEP 610: `url`, `dir_info.editable`, `vcs_info.vcs`)
  from cubevis's dist-info, and whether `conda-meta/cubevis-*.json` exists.
* `envs.install_method` -> `{"method": conda | checkout | vcs | pip | uv |
  unknown, "source"}` (conda-meta first, then VCS, then a `file://` URL
  -- checkout, editable or not -- then the installer).
* `node-here` records it as `cubevis_via` on each environment and shows it
  ("installed from the checkout /home/claude/cubevis", "installed by pip";
  also on a failed check, where it can explain the failure).  `tool` is
  set only for method conda: the tool that listed the environment
  (`listed_by`), else the first conda/mamba/micromamba found, preferring
  conda; the question is asked only when none is found.  The kind question
  stays (conda, venv, python); no tool question otherwise.
* Design section 8: the upgrade commands follow `cubevis_via`.
* Tests: `install_method` for each record shape; envcheck on an
  environment holding only a cubevis dist-info (editable record);
  `node-here` with pip-into-conda and a conda package (nothing asked;
  tool = the lister) and a conda package with no tool here (asked).

### 31c. The kind is reported, not asked (2026-10-22)

Darrell, at the kind question on zuul06: what does it feed into, and a user
may well pick the wrong one.  Now `envs.kind_from_report` takes it from the
check (the interpreter's own `sys.prefix`, `sys.base_prefix`, and whether
`conda-meta` is in `sys.prefix`): conda, then venv (prefix differs from
base prefix), else python.  Not `"conda" in sys.version` (Darrell's
sketch): a venv made from a conda-forge Python reports conda-forge there,
but its packages are the venv's.  The guess from the files (`kind_of`)
stands only until the check runs, or when it cannot run.  What the kind
feeds: display, and in part iii whether the launcher runs conda's
activation hooks; upgrades follow `cubevis_via` (31b).  The one question
left per new environment is its name, now with what it is for ("you choose
it by this name here, and the computer you plot from lists it under this
name").  Test: reports for each kind (the conda-forge-based venv included)
and `node-here` with a conda env, a venv and /usr/bin/python3, nothing
asked.

## 32. Remote sessions, stage 2b part iii: launchers, the GUI side reading nodes, routes (base `0a5b519`, 2026-10-22)

* **Launcher** (`cubevis/setup/launcher.py`).  `node-here` asks, for each
  new environment (again with `--redo`), its kernels' working directory
  (default `~`; must exist; `~` resets) and environment variables
  (`NAME=value ...`, shell quoting; `none` clears; `PATH`, `CONDA_PREFIX`,
  `VIRTUAL_ENV`, `PWD`, `HOME` refused: the launcher sets them), then
  writes `hosts/<id>/node/<network>/<env>.sh` (bash, mode 700, rewritten
  only when its text changes; removed with the environment).  It `cd`s
  (a missing directory stops it with a message naming `--redo`),
  activates (venv: `VIRTUAL_ENV`, `PATH`; conda: `CONDA_PREFIX`,
  `CONDA_DEFAULT_ENV`, `PATH`, and sources `etc/conda/activate.d/*.sh`
  with their output on stderr -- sshpyk reads stdout), exports the
  variables (after activation: they win), and `exec`s the python with its
  arguments.  sshpyk 1.23 runs `$FP_PY -c '...'` after checking only that
  `remote_python` is an executable file, so the launcher is given as
  `remote_python`.  **Verified**: a real kernel started through sshpyk
  with the launcher on the rig ran as alice in `~/visplot-data` with the
  node's variable and the venv active (`test_rig_a_kernel_runs_through_the_launcher`).
  Found on the way: the local Jupyter runtime directory must exist before
  sshpyk writes its persistence file (stage 3's Session makes it).
* **The GUI side reads node files** (`cubevis/setup/netsync.py`,
  `environments_step` in `network`).  Local first: `hosts/*/node/<net>.json`
  in this configuration directory (a home shared with the nodes); else, over
  ssh from each node logged in to, one `sh -c` printing `$(hostname)` and
  every such file in the node's configuration directory, with markers
  (stops after a node whose home is shared).  `sync` records:
  - nodes: the node ssh'd to is matched by its host name; others by full
    name or host id; unknown ones are added (host id, full name) and then
    logged in to by the same `network` run;
  - environments by identity (network, python): `hosts` "all" for a
    prefix in a shared home, else the list of nodes naming it; fields
    copied (kind, prefix, tool, cubevis_via, versions, python_version,
    visplot, workdir, env); `on`: {node: launcher, status, error, time,
    name there}; status ok if ok on any node;
  - a kernel per new environment (same name; `-kernel` if taken);
  - an environment a node's file no longer names: flagged on that node
    (`gone`), the environment flagged when no node has it ok -- not
    removed;
  - names: the node's; `<name>-<node>` when taken by another environment;
  - the nodes' `ranges`, offered first as inside ranges (private IPv4).
  `status` lists each network's environments with their nodes, versions,
  state and kernels.
* **Routes.**  `inside.override`: `CUBEVIS_ROUTE_<NET>=direct|bastion`
  wins over the ranges (and over a missing state); `status` says when it
  is set.  `sshgen`: every network with a login host gets the `Match`
  block, ranges or not, so the override always applies (without ranges:
  outside, as before).  `network NET --inside-here`: resolves a node here,
  tries it directly (`-o ProxyJump=none` -- the command line beats the
  config's Match; checked on OpenSSH 9.6 and 8.0), and only then offers
  the range of the interface it is reached from (else the address /32)
  and records it; already inside: says so; no direct connection or no
  name resolution: says so and records nothing.  At setup time only.
* **Times** (`state.now`) now carry their UTC offset
  (`2026-10-22T19:50-04:00`): the files are written by several computers.
* **Not done** (part iv and stage 3): version comparison and upgrade
  scripts from `cubevis_via`, the node query on the GUI side, `prune`,
  `check` for environments, the final kernel + plot test; `Session(cwd=,
  env=)`; the GUI host's own environments (`hosts/<id>/node/local.json`)
  are read directly by the Session, not copied into `state.json`.
* **Tests**: `test_launcher.py` (4), `test_netsync.py` (8), in
  `test_node_here.py` the settings and launcher, in `test_inside.py` the
  override, in `test_cli.py` three rig tests (alice's node file read over
  ssh; a real kernel through the launcher, with
  `CUBEVIS_TEST_KERNEL_PYTHON` having sshpyk; `--inside-here` recording
  only after a direct connection, nothing for a node that does not answer).
  Whole suite: 119 pass on the rig with OpenSSH 9.6 and 8.0p1.

### 32a. Revision (2026-10-23)

* A cubevis installed from a local wheel or sdist (`pip install
  /tmp/cubevis-...whl`, Darrell on zuul06) was shown as "installed from the
  directory /tmp/...whl": PEP 610 records such an install with
  `archive_info`, a directory with `dir_info`.  `envcheck` reports
  `archive`; `install_method` has method `file` ("installed from the file
  ..."); an older report is told by the file name.  Checked with a real
  wheel built from the checkout and pip-installed into a fresh environment.
* Design 7c: what must match between here and a kernel's environment is a
  computed fingerprint of the kernel-side code (a declared module list,
  hashed by syntax tree on each side), exactly -- not the cubevis version
  (environments would accumulate for GUI-only releases) nor a
  hand-maintained protocol number (it must cover the visplot backends; a
  forgotten bump is a silent mismatch).  A test traces the worker's
  imports so the list cannot drift.  Setup's environments are made for a
  cubevis version, never modified, reused while the fingerprint holds;
  creating a new one offers to remove superseded ones.  The kernel side
  reports the fingerprint in the `configure` round trip (stage 3).

## 33. Stage 2c (1): the kernel-side fingerprint (base `454e600`, 2026-10-23)

Design 7c: what must be identical between the plotting computer and a
kernel's environment is the code that runs on the kernel side -- computed,
not kept by hand.

* **`cubevis/_fingerprint.py`** (stdlib only; importing it imports nothing
  else of cubevis).  `ROOTS` declares the entry points: `cubevis.remote.
  worker_main` and `cubevis.toolbox.visplot.remote_registrations` (the
  `register_function` a worker loads by name).  `closure` follows `import`
  statements in the source from them, without importing anything:
  module- and function-level imports, relative ones resolved, `from X
  import Y` counting `X.Y` when it is a module, every parent package (its
  `__init__` runs).  `normalize` keeps a module's tokens without comments,
  blank lines, continuations, indentation width and string-only statements
  (docstrings) -- the tokenizer is the same from Python 3.12 on, `ast.dump`
  is not.  `fingerprint()` = `k1-` + 20 hex digits of a sha256 over the
  modules in name order.  `compatible(local, remote)` and the override
  `CUBEVIS_ALLOW_MISMATCH=1` (or `allow=`), which says so in its message.
* **`envcheck.py`** reports `fingerprint`, computed by the environment's
  own cubevis from its own files (null for a cubevis without the module).
* **Measured** on this checkout: 127 modules fingerprinted (of 164 in the
  package); importing the two roots really imports 116.  The kernel side
  takes in much of the visplot front end (`visibility_plotter`,
  `flag_controls`, `info_panel`, ...) and the clean tools (`toolbox._cube`,
  `_interactive_clean_ui`), because `cubevis/toolbox/__init__.py` and
  `cubevis/toolbox/visplot/__init__.py` import their public classes
  eagerly.  So today nearly every visplot change changes the fingerprint.
  With those two `__init__`s lazy (PEP 562 `__getattr__`; `from
  cubevis.toolbox.visplot import VisibilityPlotter` keeps working) the set
  would be 82, the visplot part being the backends, readers, flag engine,
  selection and formats.  Offered to Darrell, not done: it changes how the
  packages import.  `cubevis.bokeh`'s `__init__` brings in models and tools
  the same way.
* **Cost**: about 1.2 s warm (closure 0.7 s, normalizing 0.5 s), mostly
  the few very large modules (`visibility_plotter.py` is 563 kB); once per
  process.  The lazy `__init__`s would shrink it too.
* **Tested** (`tests/manual/setup/test_fingerprint.py`, 7): normalization;
  the closure on a made-up package (relative, function-level, parents; an
  unreached module and a comment do not change it, code does); the real
  roots; the same value under Python 3.12 and 3.13; `compatible` and the
  override; the import trace (every cubevis module the roots import is
  fingerprinted); and `envcheck` in the kernel test environment (editable
  on this checkout) reporting the same fingerprint.  Whole suite: 126 pass
  on the rig with OpenSSH 9.6 and 8.0p1.
* The kernel test environment (`/home/claude/venv-kernel`) now has the
  visplot extras (`pip install -e ".[visplot]"`), which the import trace
  needs; the suite is slower for it (the checks import xarray-ms).

## 34. Lazy package `__init__`s: `cubevis.toolbox`, `cubevis.toolbox.visplot` (base `454e600`, 2026-10-23)

Agreed with Darrell after section 33's measurement: the two package
`__init__`s imported their public classes eagerly, so a remote kernel --
which imports `cubevis.toolbox.visplot` only because
`remote_registrations` lives in it -- loaded the plotting front end and the
clean applications on the node, and the kernel-side fingerprint changed
with every front-end edit.

* **The change.**  Both `__init__`s keep their public names in a `_LAZY`
  table (name -> submodule) and import on first use through a
  module-level `__getattr__` (PEP 562; the value is then stored, an
  ordinary attribute from there on), with `__dir__`, `__all__`
  (`cubevis.toolbox` gains one: `CubeMask`, `RegionList`,
  `InteractiveCleanUI`), and the old imports under `if TYPE_CHECKING:` for
  editors and type checkers.  A comment in each says why, and to add new
  public names to `_LAZY`.  `from cubevis.toolbox.visplot import
  VisibilityPlotter` and `from cubevis.toolbox import CubeMask` work as
  before.  No attribute-style access to a submodule through the package
  (`cubevis.toolbox._cube.X` without importing it) exists in `cubevis`,
  `tests` or `demos`.  `cubevis.bokeh` was not eager (its `models` and
  `tools` enter only through an import inside `get_app_context`, which
  the fingerprint counts on purpose); `cubevis.utils` was left (750 lines
  of functions used everywhere, a guarded casatools import; about ten
  small modules to gain).
* **`_fingerprint.closure`** skips the bodies of `if TYPE_CHECKING:`
  (their `else` branches run and are kept).
* **Effect.**  Fingerprinted: 127 -> 82 modules; importing the two roots
  in a kernel environment: 116 -> 42 cubevis modules (1.9 s), without
  `visibility_plotter`, the panels, the flag controls, `toolbox._cube` or
  the clean UI.  The visplot modules left are the backends, readers, flag
  engine, selection and formats -- shared by both sides, rightly counted.
* **Tested.**  `tests/manual/setup/test_lazy_packages.py` (5): no eager
  relative import outside `TYPE_CHECKING`; every name in `_LAZY` imports
  as the same object as from its module; `dir`, `__all__` and `import *`
  agree; an unknown name raises `AttributeError`; importing the package
  alone imports none of its submodules; the kernel side does not load the
  front end.  `test_fingerprint.py`: `TYPE_CHECKING` imports do not count;
  the real set leaves out the front end.
* **No change in behaviour.**  The visplot suite (`tests/manual/visplot`,
  `CUBEVIS_SRC` set, the kernel test environment with the visplot extras,
  `pytest -n 8`) on the unchanged tree: 2279 passed, 694 skipped, 13
  failed; on the changed tree: 2281 passed, 694 skipped, 11 failed -- the
  failures differ between the runs and coincide with crashed xdist workers
  (eight workers on a 2-core, 8 GB sandbox).  All 22 tests that failed in
  either run pass when run one at a time on the changed tree.
  `tests/manual/remote` (real local kernels; without the two scripts that
  name Darrell's `zuul06_python312`): 39 passed.  A real `query_raster` on
  the TW Hya MS through a local kernel and worker: the same 10x10 raster on
  both trees, 8.5 s each.  Setup suite: 133 passed (OpenSSH 9.6 and 8.0p1).
* For the visplot suite here: `CUBEVIS_SRC=<checkout>/cubevis`; `-n 2` or
  no xdist, not `-n 8`.

## 35. Stage 2c (2): `setup HOST`, clusters, kernels, the discovery script (base `e6f4fe9`, 2026-10-23)

Design 7c's single path: the user names the host the data is on; networks,
routes and launchers are setup's own bookkeeping.

```
python -m cubevis.setup HOST [--kernel NAME]    make kernels work on HOST; again: check them
python -m cubevis.setup                         the same, asking for the host
python -m cubevis.setup status | check [NAME] | network NAME [--inside-here]
```

* **The flow** (`cubevis/setup/hostsetup.py`).  (1) Where HOST is: a node
  setup knows (by name, alias, host name or full name); a node of a known
  cluster (its name matches the pattern: added, nothing asked); a host in
  the domain of a known network (joins it, same route, said so); else a
  new network -- the route is asked as before (login host or direct; step
  4 takes it from `ssh -G` instead) and the network is *named, not asked*,
  from the domain (`cvpost140.cv.nrao.edu` -> `cv`).  (2) Logins, each hop
  (`steps.hop_steps`, unchanged).  (3) Discovery over ssh (below); the
  node's full name, home and subnets go into its record, its domain into
  the network's when it had none.  (4) "Is HOST one of a cluster's
  nodes?", once per node: default yes when the name ends in digits; name
  and pattern suggested (`cvpost140` -> `cvpost`, `cvpost*`); nodes known
  before that match join it.  (5) Inside ranges (the node's own private
  subnets suggested first).  (6) The environment.  (7) The kernel.
* **The environment** is chosen from discovery's list: each environment
  with cubevis is shown with whether it can be used and why -- "same
  kernel-side code as here", "its kernel-side code differs (there k1-...,
  here k1-...)", "cubevis is not installed in it", the import error.
  Usable first, setup's own (marker) first among them.  A number, a path
  on the node (checked there, one more round trip), or `none`.  A mismatch
  is usable only with `CUBEVIS_ALLOW_MISMATCH=1` (said each time).  None
  usable: setup says why and what to do -- a development install here
  (editable, a local directory, a version that is not a release): install
  the same checkout there, or the override; a release: the two commands
  for an environment of setup's own name (`<conda> create -y -n
  cubevis-<v> python=<here> pip`, `<conda> run -n ... pip install
  "cubevis[visplot]==<v>"`); no conda: Miniforge on a fast shared
  filesystem.  Creating it is step 3.  The record: name `<dir>-<cluster or
  node>`, `cluster` or `hosts: [node]`, versions, `cubevis_via`, `tool`
  (conda packages only), `fingerprint`, `marker`.
* **The kernel**: a name (default: the cluster's or the node's), the
  working directory on the node (`~`, `~/x`, absolute, or relative to the
  home; None = home) and variables.  Setup writes its launcher on the node
  and runs `launcher.TEST_CODE` through it -- the `cd`, the activation,
  `import ipykernel, cubevis.remote.worker_main`, the fingerprint -- and
  records the kernel only as ok when that works and the fingerprints are
  compatible; otherwise flagged with the reason (the launcher's own "cannot
  change to ..." for a missing directory) and "Change its settings and try
  again?".  `--kernel NAME` adds or changes one kernel on a host already
  set up, without the environment question (when the host has one
  environment).  A rerun without it checks the host's kernels (rewrites
  launchers whose text changed, runs each once); `--redo` asks their
  settings again.
* **Launchers** (`launcher.py`): per kernel, on the node at
  `~/.config/cubevis/launch/<gui-id>/<kernel>.sh` (the node's
  `$XDG_CONFIG_HOME`; `<gui-id>` so that two plotting computers that do not
  share a state never overwrite each other's).  Written by a small program
  for the node's python3 on stdin (atomic, mode 700, `same` when unchanged)
  -- no remote-shell quoting involved.  The header names the command that
  changes it (`python -m cubevis.setup HOST --kernel NAME`).
* **Discovery** (`cubevis/setup/discover.py`): `hostinfo.py` and the
  finding half of `envs.py` moved into it; standard library only and
  **Python 3.6** (RHEL 8's python3: no `from __future__ import
  annotations`, no `capture_output=`/`text=`).  Setup sends `payload()` --
  the file with `envcheck.py` embedded -- to the node's `python3 -` (the
  first of `python3`, `/usr/libexec/platform-python`, `python`, chosen by
  one `/bin/sh -c '...'` line that csh passes unchanged).  It reports the
  host (name, full name, domain, user, home and its filesystem, config
  directory, addresses and subnets, `$SSH_CONNECTION`), the conda tools and
  their `envs_dirs` (for step 3), and the environments (conda's lists, the
  active ones, the Python running it), each with its kind, cubevis from its
  `dist-info`, setup's marker (`cubevis-setup.json`), and `envcheck`'s
  report for those with cubevis, a marker, or given on the command line
  (`--only PATHS` for just those).  By hand on a node: `python3 -m
  cubevis.setup.discover` (readable text), or the bundle (`--bundle`),
  which setup also leaves in `hosts/<id>/discover.py` after a failed look
  and names.  `envs.py` keeps the interpretation (`interpret`,
  `kind_from_report` -- now `normpath`, not `realpath`: the paths are the
  node's -- `install_method`, `summary`).
* **State schema 3** (`state.py`; older cubevis refuses it rather than
  drop what it does not know).  New section `clusters` {network,
  pattern}; nodes gain `cluster`, `standalone`, `fqdn`, `ranges`, `home`;
  environments `cluster`, `fingerprint`, `marker` (an environment of a
  cluster is on all its nodes); kernels carry `workdir`, `env`,
  `launcher`, `fingerprint`, `checked_on` and the health fields, and can be
  flagged.  `find_node`, `add_cluster`, `cluster_for`, `node_for` (adds a
  node matching a cluster: what stage 3's `Session(kernel, node=)` will
  use), `kernels_on`.  Migration 2 -> 3: kernels take their environment's
  `workdir`/`env`, have no launcher (flagged "run setup HOST again"); the
  node-file sync's fields leave the environments.
* **ssh rules**: a network's nodes are matched by its `*.<domain>` pattern
  and its clusters' patterns, and *by name for every node none covers*.
  Before, a node typed by its short name (`zuul06`) in a network whose
  domain was learned later would have lost its password login.
* **Removed**: `node-here`, `node_here.py`, `nodefile.py`, `netsync.py`,
  `hostinfo.py` (into `discover`), the node files, the `node` command and
  the walk-through's role question; `network NAME` now only goes through
  an existing network (logins, inside ranges, `--inside-here`).  The bash
  setup's `probe.sh` and `remote-setup.sh` stay in `data/` (it reads them)
  until stage 4.  `__main__.py` split: `steps.py` (Ctx, tools, wrapper,
  logins, inside ranges), `remote.py` (discovery, launcher install, test
  run), `hostsetup.py`, `__main__.py` (commands, argv: anything that is
  not a command is a host; `--kernel` before or after it).
* **`check`** runs every kernel's launcher once on a node it is on (the
  last one checked first) and compares fingerprints; a target may be a
  kernel, node, cluster or network.  **`status`** lists clusters (pattern,
  nodes set up), nodes (cluster, port), environments and kernels (directory,
  variables, state).
* **Tested.**  `test_discover.py` (14; the node-side parts of the old
  `test_node_here.py` moved here): host facts, parsing, candidates with
  fake conda/micromamba, `envs_dirs`, markers, checks failing for each
  reason and passing, install methods; the payload on stdin (`--only`,
  missing paths), by hand (module, bundle, bare file without envcheck);
  **under Python 3.6.15** (built here; `CUBEVIS_TEST_PY36`), envcheck
  included; the command lines through **tcsh**.  `test_launcher.py` (6): per
  kernel, the install program (written / same / error), the test run's
  fingerprint and directory.  `test_state.py`: clusters, `node_for`,
  kernels, the 2 -> 3 migration.  `test_sshgen.py`: cluster and unmatched
  node rules.  `test_hostsetup.py` (6, no ssh): placement (new network
  named from the domain, same domain joins, any name finds a node, ports,
  addresses), the cluster question and a pattern that misses, the bundle
  left after a failure, the walk.  `test_cli.py` (14, 9 of them on the rig): `setup HOST`
  through a terminal (pexpect) via the password login host, then `check`
  and `status`; direct, `--kernel` adding a second kernel, a rerun that
  changes nothing; a path without cubevis refused, a mismatched
  environment (alice's copy of cubevis with one line added to
  `worker_main`) listed with both fingerprints, refused, then used with
  `CUBEVIS_ALLOW_MISMATCH=1` and flagged by `check` without it; a cluster
  (`comp`, `compute*`) and `compute2` (a second name for the rig's node,
  added to `ssh_rig.sh`) joining it with no route question and finding the
  kernel; two computers on one directory; alice's login shell tcsh; a real
  sshpyk kernel through the launcher, in `~/visplot-data` with its
  variable, opening the TW Hya MS by a relative path (xarray-ms); and
  `--inside-here`.  Whole suite: 133 pass with OpenSSH 9.6 and 8.0p1.
* **Not yet**: setup's own environments (creation, location, detached
  resumable, reuse, `--cleanup HOST`: step 3); the route from `ssh -G`,
  `diagnose HOST`, `--2fa` (step 4); `--sync-checkout`, `prune`, `--local`
  (2d).  The open point from 2026-10-19 on first contact with a login host
  straight to a node stands (setup still visits the login host first).

## 36. After Darrell's first `setup cvpost106` (base `9f21e53`, 2026-10-26)

Darrell's run from a fresh local account (macOS, OpenSSH 10.3, Keychain)
through `ssh.cv.nrao.edu` to cvpost106 (RHEL 8, python3 3.6.8, 31
environments): every step worked as designed -- discovery under 3.6, the
cluster suggested and taken (`cvpost`, `cvpost*`), his development
environment found with the same fingerprint, the 1.0.96 one refused for
having none, the kernel's launcher written and run.

* **Inside ranges: found ones are the answer, not a hint** (Darrell: users
  will not know their site's numbers, and should be assumed to answer
  inaccurately as often as accurately).  `steps.inside_step` now lists what
  it found, each with where it comes from ("cvpost106's own network", "a
  wider guess around it" -- the /16 of the connection's private addresses),
  says that keeping them is the usual answer, how `--inside-here` adds the
  exact one on site and that `CUBEVIS_ROUTE_<NET>=bastion` overrides, and
  asks `[u]se these, [c]hange them, or [n]one` (default use).  Nothing
  found: nothing asked, always the login host, `--inside-here` named.
  `--answer inside=...` still sets them directly (scripts, tests).
* **`python -m cubevis.setup jupyter KERNEL [NODE]`**: a bridge until stage
  3's Session -- today's plotters take `kernel_name=`, and setup kernels
  have no Jupyter definition.  It writes `cubevis-<kernel>` with sshpyk's
  `add` (the wrapper, the generated config, the node's alias -- default
  where the kernel was last checked; NODE may be any node of its cluster,
  added on the way -- and the kernel's launcher as `remote_python`),
  verifies the file was written (`python -m sshpyk` is a no-op that exits
  0: the CLI entry point is called instead), makes Jupyter's runtime
  directory, and prints the call:
  `visplot(ms="sis14_twhya_calibrated_flagged.ms", backend="remote",
  kernel_name="cubevis-<kernel>")`.  Run again with another node to move
  it.  `setup HOST`'s closing message names it.
* **Verified on the rig**: the printed call, headless, through the
  password login host: the kernel started in `~/visplot-data`, the MS
  opened by its relative name, first render 16 s (`field="Ceres"`).
  The relative path is passed to the kernel untouched
  (`VisibilityPlotter` only checks existence locally for `vis=`).
* **Found on the way, not fixed** (outside setup): (1) without a
  selection, the first scatter of the whole TW Hya MS takes the worker
  past 5.5 GB and the sandbox's memory limit killed it, local or remote
  (the scatter memory point of 2026-10-19); (2) when the worker dies,
  the plotter waits for its reply forever (`_bridge.run` ->
  `future.result()` without a timeout or a liveness check) instead of
  failing with the reason.  (3) `spw="0:0~15"` and `spw="<its name>:0~15"`
  were both ignored ("matched no spectral window; ignoring it"); not looked
  into -- which channel syntax `spw=` takes is to be checked.
* **Next** as planned: step 3, setup's own environments (Darrell's run
  offered none to create; that is step 3).
* Tests: `test_cli.py` -- the terminal run answers `[c]hange` then the
  range; found ranges taken by default with nothing typed; `jupyter`
  refusals (unknown kernel, no launcher, unknown node); on the rig the
  definition's fields and the printed call run headless against the MS.

## 37. A fresh account's first `visplot`: CASA, the login-host and password wording (base `9f21e53`, 2026-10-26)

Darrell's second clean-account run (a Miniforge `visplot` environment with
the `cubevis-1.0.97.dev35+gd1b8f20` wheel; casatasks installed, its data
not set up).

* **CASA is never imported to find out whether to use it**
  (`cubevis/utils/_casa.py`).  `from cubevis import visplot` failed in
  `casaconfig` (`AutoUpdatesNotAllowed: data_update: path must exist ...
  ~/.casa/data` -- an `Exception`, which the `except ImportError` around
  `from casatools import regionmanager` in `cubevis/utils/__init__.py` and
  `from casatasks import casalog` in `utils/_logging.py` did not catch), and
  `visplot(...)` required `casatasks.private.task_logging`.  Now:
  - casalog, and the task's start/end/except log lines, only when
    `casatasks` is already in `sys.modules` (inside CASA, or imported by the
    user first): `_casa.loaded()`, `_casa.task_logging()` (quiet stand-ins
    with casatasks' signatures otherwise);
  - `cubevis.utils`: `_have_casatools()` when a CASA region is made, not at
    import; `cubevis.bokeh.sources._image_pipe` and `cubevis.data.casaimage`
    (which visplot loads) get `imagetool` / `regionmanager` /
    `casatools_available` from `_casa`, importing casatools when an image is
    first opened -- in CASA's absence nothing changes for iclean, which
    needs it anyway;
  - the `auto` backend probe no longer imports casatasks (its context is
    not implemented: the import only cost seconds and could raise);
    `backend="casa6"` and committing flags with `flagdata` use
    `_casa.usable()`, which reports any failure as a reason.
  Tested (`tests/manual/visplot/test_casa_optional.py`, fresh interpreters
  with a fake CASA): a CASA that raises at import is not touched by `from
  cubevis import visplot` or a `visplot(...)` call, only by the flag-commit
  check (which says no); with `casatasks` imported first, casalog and the
  task log lines are used; installed but not imported, left alone.
* **`cubevis.private.apps`**: the `ImportProtectedModule` class swap
  replaced by a PEP 562 `__getattr__` over a `_LAZY` table (as
  `cubevis.toolbox` since section 34); `cubevis/utils/_import_protected_module.py`
  removed.
* **An unknown `kernel_name=`** fails before anything starts
  (`visibility_plotter._check_kernel_name`), naming what to run --
  `python -m cubevis.setup jupyter NAME` for `cubevis-NAME` -- the kernels
  known here and the directories searched, instead of jupyter_client's
  `NoSuchKernel` from deep in the start.  (Darrell's `NoSuchKernel:
  cubevis-cvpost` in the test account: not reproduced; `setup jupyter` now
  also prints where it wrote the definition.)
* **Wording** (setup): "Do you log in to an intermediate host before
  logging in to HOST?" (an intermediate host, "often called a login host or
  bastion"), "The intermediate host's name"; before storing a password:
  kernels cannot ask, so it is kept in an encrypted store that only the
  wrapper reads, never in setup's files or logs -- "Store your password for
  USER@HOST that way?"; each store's line says how it protects it
  (Keychain "encrypted, unlocked by your login", pass/gpg-file
  "gpg-encrypted"); when typing: "(twice); nothing is shown as you type",
  where it goes, "encrypted, without passing through setup".
* **Plain-text password commands refused**: a `command` store beginning
  with `echo`, `printf`, `cat`, ... would hold the password in setup's own
  `local.json` (the command is recorded there) or in a plain file;
  refused with the reason.  The tests' `echo` of the rig's password sets
  `CUBEVIS_SETUP_ALLOW_PLAIN_COMMAND=1`.
* **For step 3** (Darrell: "option to create an environment just for
  cubevis-1.0.97.dev35"): a cubevis installed here *from a wheel file*
  (`install_method` "file") is reproducible on a node -- copy that wheel
  there and `pip install` it into setup's new environment -- unlike a
  checkout (`--sync-checkout`, 2d).  Setup's environments must cover three
  sources: a release (PyPI), a local wheel (copied), a checkout (synced).
* **Note**: `cubevis/utils` is kernel-side code, so this changes the
  kernel-side fingerprint: the environment on the cluster needs the same
  code again (Darrell's development install there).
* **One Jupyter kernel per node** (Darrell: how is `cubevis-cvpost` used on
  cvpost106 *and* cvpost140?).  `setup jupyter KERNEL` makes
  `cubevis-KERNEL` on the node it was last checked on; `setup jupyter
  KERNEL NODE` makes `cubevis-KERNEL-NODE` (any node of the kernel's
  cluster, added on the way), so several nodes can be used at once.
  Stage 3's `Session(kernel, node=)` makes the node an argument and these
  files unnecessary.  A node added through a cluster's pattern now takes
  the ssh port the cluster's nodes share (when they share one other than
  22: the rig's compute2).
* Tested: the rig's jupyter test makes `cubevis-tw-hya` and
  `cubevis-tw-hya-compute2` side by side; `test_state.py` the port.
  Setup suite 138 pass (OpenSSH 9.6 and 8.0p1).
* **Checked against the rest** (these touch `cubevis.utils`, its logging and
  the image sources, which most of cubevis imports): the visplot suite
  (`-n 2`, `CUBEVIS_SRC` set) 2270 passed, 694 skipped; its 13 failures and
  3 errors all pass when run again by file -- 10 + 3 were `pytest-asyncio`
  missing from the test environment (now installed: add it to the kernel
  test environment's list), the other 3 coincided with an xdist worker
  killed for memory, as in section 34.  `tests/manual/remote` without the
  two scripts that name Darrell's `zuul06_python312`: 39 passed.

## 38. `setup HOST` ends with a kernel visplot can start (base `6294aad`, 2026-10-27)

Darrell's third clean-account run: setup worked, but nothing said what to
pass to `visplot` -- `kernel_name="cvpost"` and `"cubevis-cvpost"` both
failed (the Jupyter kernel was a separate `setup jupyter` step, named only
in a closing sentence), sshpyk was not in his environment (cubevis does not
depend on it), and `status` showed no kernel names.

* **`setup HOST` makes the Jupyter kernel itself** (`cubevis/setup/
  jupyterspec.py`, `hostsetup.use_kernel`): once a kernel works (new, or
  checked on a rerun), `cubevis-KERNEL` is written on that node -- rewritten
  only when its ssh files, alias or launcher changed -- and setup ends with
  "visplot can use kernel cvpost (on cvpost106):" and the call,
  `visplot(ms="sis14_twhya_calibrated_flagged.ms", backend="remote",
  kernel_name="cvpost")`, how to add another node of the cluster, another
  kernel, and that `status` lists them.
* **`kernel_name=` takes the setup kernel's name** (`visibility_plotter.
  _resolve_kernel_name`): a name Jupyter knows is used as is; else, if
  `cubevis-NAME` exists, that (logged at INFO); a setup kernel without one
  says `python -m cubevis.setup jupyter NAME`; anything else lists the
  Jupyter kernels and the setup kernels here.
* **sshpyk**: a `remote` extra (`sshpyk>=1.23`, `ipykernel`,
  `jupyter_client`), part of `visplot` (`pip install "cubevis[visplot]"`).
  When it is missing, setup says what it is for and offers `pip install
  'sshpyk>=1.23'` into the Python running it (not in unattended runs).
* **`status`** lists under each kernel its Jupyter kernels:
  `visplot(..., backend="remote", kernel_name="cvpost")   runs it on
  cvpost106` (the default one by the setup name), `... kernel_name=
  "cubevis-cvpost-cvpost140")   runs it on cvpost140`; or "no Jupyter
  kernel for it here: python -m cubevis.setup jupyter cvpost".
* `setup jupyter` is now the same code (`jupyterspec.make`), for another
  node or by hand.
* Tests: the rig's jupyter test -- `setup HOST` writes `cubevis-tw-hya` and
  prints `kernel_name="tw-hya"`; `setup jupyter tw-hya` again writes
  nothing; `compute2` alongside; `status` lists both; the headless plotter
  with `kernel_name="tw-hya"` against the MS.  `test_hostsetup.py`: no
  sshpyk (unattended: not installed, said so), `_resolve_kernel_name`
  (setup name -> `cubevis-NAME`; a setup kernel without one; unknown).
  The rig tests' Jupyter kernels go to the test's own `JUPYTER_DATA_DIR`.
* `setup jupyter` says what cannot work (an unknown node, no launcher)
  before the wrapper step or the sshpyk offer.  Setup suite 140 pass with
  OpenSSH 9.6 and 8.0p1; `test_casa_optional.py` 3.

## 39. Safari: a blank window (base `b3db19c`, 2026-10-27)

Darrell, a fresh account with the setup working end to end: `visplot(...,
kernel_name="cvpost")` connected, but Safari showed a white window with no
error; "Open in Chrome" from Safari worked.  Safari's Sources pane listed
all five scripts (casalib, bokeh, bokeh-widgets, bokeh-tables, cubevisjs)
in red, "An error occurred trying to load the resource".

* **Why.**  `BokehAppContext.show()` saves the page to a temporary
  directory and opens it as `file:///private/var/folders/.../T/visibility.../
  visibilityplotter-....html`; `resolve_js_library_paths` points its
  scripts at `file://` URLs into the installed packages (site-packages).
  Safari lets a local page read only its own directory and below, so none
  of them loaded; Chrome and Firefox allow it.  Every cubevis application
  is affected (visplot, iclean, createmask, createregion all show through
  `BokehAppContext`).
* **The fix** (`cubevis/bokeh/state/_local_scripts.py`, called by
  `BokehAppContext.show` after `save`): every `file://` script the page
  names is copied into `js/` beside the page and the page points at it
  relatively; CDN scripts are left alone; a script that cannot be found or
  copied is left as it was and logged; two files of one name get distinct
  copies.  About 3 MB per page, removed with the page's temporary
  directory.
* **Checked** here (no Safari here): the rewritten page renders in
  headless Chromium with every script from `js/`, no failed request, no
  page error -- a test page (`tests/manual/visplot/test_local_scripts.py`,
  which also checks that no `file://` script is left) and a real visplot
  page on the TW Hya MS (field Ceres), screenshot with both panels drawn.
  Darrell to confirm in Safari.

## 40. A test account without a keychain; defaults from the user's ssh configuration (base `f7e1ed6`, 2026-10-28)

* **The Keychain is checked before it is offered** (`stores.keychain_problem`):
  `security default-keychain -d user` must name a keychain file that exists
  and this account can write.  Darrell's new test account (its creation
  half-failed: no first-login "Welcome", the previous account's removal
  failed) had none: `security add-generic-password` failed ("The
  authorization was canceled by the user" / "Unable to obtain authorization"),
  macOS showed "Keychain Not Found" (whose Reset to Defaults the account
  was not allowed to do), and setup reported the *login* failure that
  followed and offered the same store again on retry.  Now an unusable
  Keychain is listed as not usable, with why and how one is made (Keychain
  Access, or logging in to the account at the Mac once); a store that fails
  says "the password was not stored in ..." (with the keychain's problem when
  that is it), and `put_or_switch` offers the other stores (`choose(exclude=)`)
  instead of the same one again.  No store usable: said so.
* **Defaults from the user's ssh configuration** (Darrell: his
  `~/.ssh/config` has `Host ssh / User dschieb / Hostname ssh.cv.nrao.edu`
  and the same for the nodes, and setup asked for his user name with the
  local one as the default).  `steps.user_ssh(HOST)` -- `ssh -G HOST`,
  nothing connected -- gives the real host name, user, port and ProxyJump;
  setup says what the configuration has for HOST, takes the ProxyJump's
  first hop as the default intermediate host, resolves the intermediate host
  (`ssh` -> `ssh.cv.nrao.edu`, its port) and the node (`cvpost106` ->
  `cvpost106.cv.nrao.edu`) to their real names, defaults the user names to
  the configured ones, and names the network from the real domain -- so a
  later `setup cvpost107` (short name) joins it.  Still asked, only with
  better defaults: the rest of step 4 (a generated configuration carrying the
  user's login options, the login test, `diagnose`) is to come.
  ``CUBEVIS_SETUP_USER_SSH_CONFIG`` points it at another file (ssh reads the
  account's home, not `$HOME`); the rig tests use `/dev/null`.
* Tests (`test_hostsetup.py`): Darrell's configuration shape (real names,
  user, ProxyJump, the network from the domain; a second short name joins);
  no configuration invents nothing; a failing store offers another; the
  Keychain check.
* Setup suite 144 pass with OpenSSH 9.6 and 8.0p1.

## 41. Kernels named for their purpose; cluster defaults (base `af0cae4`, 2026-10-28)

Darrell's first fully successful run (remote kernel, plot in Safari) raised
the default kernel name: `cvpost` named the cluster, the kernel and (as
`cubevis-cvpost`) the Jupyter kernel at once, and said nothing about what
the kernel was for.  Agreed with Darrell:

* **A kernel's name says what it is for, never which node** (reservations
  move; a node-named kernel would outlive its node).  `setup HOST` now asks
  the working directory and variables first, then the name, defaulting to
  the directory's name (`.../casa/visplot` -> `visplot`); in the home
  directory, the cluster's name, or a lone host's; a taken name gets `-2`
  (`hostsetup.default_kernel_name`).  A name that exists is refused there
  (`--kernel NAME` changes it).
* **Cluster defaults**: a cluster's first kernel's working directory and
  variables become the cluster's (`clusters.NAME.workdir`, `.env`; changes
  log "cluster defaults"); later kernels on the cluster start from them
  ("Cluster cvpost's defaults are offered below"); `status` shows "new
  kernels start from: directory ..., sets ...".
* **The plan agreed for what remains** (design 7c, stage 3):
  - the node is chosen when a session starts, not stored with the kernel:
    `Session("visplot")` -> the node given, else the cluster's reservation
    query (the per-network node query of design section 7, e.g. Slurm's
    `squeue -h --me -o %N | xargs -r scontrol show hostnames`), else the
    last node that answered; a node from an expired reservation fails at
    once naming the reserved ones.  The reservation query moves into stage
    3 (NRAO's clusters need it), and the Jupyter-kernel bridge, which pins
    a node, goes with the Session;
  - setup's own environments (step 3) are identified by network and path and
    reused by every node or host that sees the same conda installation (the
    marker records the fingerprint), so a new node of a cluster costs
    nothing.
* Tests: `test_hostsetup.py` (the name: directory, cluster, lone host,
  taken, odd characters); `test_cli.py` -- the terminal run answers the
  directory first and is offered `visplot-data`; the cluster test records
  the cluster's defaults and a second kernel (`--kernel imaging`, nothing
  answered) takes them.
* Setup suite 145 pass with OpenSSH 9.6 and 8.0p1.

## 42. The wrapper: stale shared connections (base `e66f0ba`, 2026-10-28)

* **Why**: Darrell's `~/bin/ssh` had been changed to cope with a shared
  connection whose socket still answers `ssh -O check` after its remote
  session has died (a Mac asleep, a network drop, Fast User Switching):
  every use then hung or failed until the socket was removed by hand.  The
  check only asks the local master process, which has not noticed yet.
* **Ported into `cubevis/setup/data/ssh`** (the packaged wrapper keeps its
  own extras: several rules files, `--explain`, the failed-login messages,
  socket-directory recreation, skipping other wrapper copies on PATH):
  - a connection confirmed less than `SSH_WRAPPER_STALE_AFTER` seconds ago
    (default 300; 0 probes every time) is trusted as before;
  - an older one gets a probe: `true` run over the existing master, as any
    multiplexed client (`BatchMode=yes`, `ConnectTimeout=5`; never
    `ControlMaster=no`, which would open a separate connection needing its
    own password and always fail);
  - new here: the probe is killed after `SSH_WRAPPER_PROBE_TIMEOUT` seconds
    (default 10).  On a link that is dead rather than closed the request
    otherwise waits until the master's keep-alives give up -- minutes;
  - a good probe refreshes the confirmation; a failed one says "the shared
    connection to 'X' is stale ... logging in again", runs `-O exit` and
    goes on to the ordinary login;
  - "confirmed" is the ControlPath socket's mtime (set after each login and
    good probe): no state file;
  - `--explain` shows "up (confirmed Ns ago; probed after 300s)".
* Tests (`test_wrapper_login_messages.py`, rig): a fresh master is used
  without a probe; with `SSH_WRAPPER_STALE_AFTER=0` it is probed and kept
  (same remote session); with the remote `sshd: alice` session processes
  stopped (SIGSTOP: the socket answers, nothing else does) the probe times
  out, the wrapper says "stale", logs in again and runs the command.
* Setup suite 148 pass with OpenSSH 9.6 and 8.0p1.

## 43. Setup's own environments (stage 2c step 3; base `858e4d5`, 2026-10-28)

`setup HOST` no longer ends at "no environment has the same kernel-side
code": it makes one (design 7c, "Setup's environments are minimal and never
modified").

* **What is made**: `<tool> create -y -p PREFIX python=X.Y pip` (X.Y: this
  computer's Python), then `PREFIX/bin/python -m pip install
  "cubevis[visplot]==VERSION"` (`PYTHONNOUSERSITE=1`, no pip prompts), named
  `cubevis-VERSION`; a name taken by anything (a user's environment, an
  earlier one) is never reused: `-2` ... `-9`.  The marker
  `PREFIX/cubevis-setup.json` (schema 1, name, version, spec, Python, the
  creating tool, created, `setup: {cubevis, host}` -- which computer's
  setup made it) is written **last**.  Setup never changes it afterwards
  and never installs into the user's own environments.
* **On the node** (`cubevis/setup/envmake.py`): one standard-library
  program (Python 3.6+, like discovery), sent as `python3 -` with its
  request embedded (`REQUEST = json.loads(...)`; no quoting for csh), whose
  one-line answer is `CUBEVIS-ENVMAKE {json}`.  Actions:
  - `plan` -- per candidate directory: writable?, free bytes, which names
    are taken; and the jobs earlier runs left (status, log tail);
  - `start` -- writes `<config>/make/NAME.job.json`, `.json` (status), `.log`
    and spawns a **detached worker** (`python3 -c WORKER job.json`, its own
    session, stdin /dev/null, output to the log), so the ssh call returns at
    once (tested: under 1.5 s with a 2 s install) and nothing holds the
    connection's pipes.  Refuses while one is running, and refuses an
    existing prefix unless an earlier job of its own left it (`failed` /
    `died`, same prefix);
  - `status` -- the status file, `died` when the recorded process is gone
    (node restarted, killed) or never started (> 60 s), with the log's tail;
  - `remove` -- only a prefix with a valid marker whose name matches, by the
    tool's `env remove -y -p`; **never by deleting files itself**; clears the
    job files; says so when the directory is still there.
  The worker records each finished step (`done: [create, install]`); started
  again for the same environment it skips them and first removes an
  unfinished prefix (`env remove`) when the create step never finished --
  so a failed solve or a dropped connection costs nothing already done.
* **On the computer the plots are on** (`hostsetup.py`):
  - **When offered**: no usable environment (the default answer is yes), or
    the user answers **`new`** at the environment question (new: makes setup's
    own even when others are usable).  A development install here (editable,
    a directory, a non-release version) and a node without conda keep their
    advice (Miniforge, "then run this again, and setup will make the
    environment"); when the user declines, or it fails, the same two
    commands are printed ("To make it yourself").
  - **The question** (once): why, what (Python X.Y, `cubevis[visplot]==V`
    from PyPI, about 1.5 GB -- the pip part measured 965 MB), that it is
    made in the background, where conda keeps environments (`envs_dirs[0]`
    from discovery, free space there), and a directory prompt (`env_dir`) for
    another place -- at NRAO a project or Lustre area; an unwritable
    directory, or one with every name taken, is refused; under 3 GB free
    warns.
  - **Waiting** (`wait_for_job`): polls every 5 s over the shared
    connection, says each step once ("creating the environment (conda)",
    "installing cubevis (pip)"), again every minute; Ctrl-C stops the
    waiting, not the work ("goes on being made on NODE (its log: ...); run
    this again"); six failed polls in a row do the same.
  - **Running setup again** finds the job: still running -> "An environment
    is being made on NODE", waits for that one; stopped (`failed`/`died`) ->
    its error and the log's tail, "Try again? It goes on from the step it
    stopped at"; finished while away -> it is simply in discovery's list
    (made by setup, same kernel-side code, sorted first) and is used.
  - **After it is made**: checked through discovery (`--only PREFIX`:
    fingerprint, ipykernel), recorded like any environment -- with its
    `marker`, so `status` says "made by setup" -- and the kernel goes on.
* **Older ones** (`own_environments`, `offer_cleanup`, `cmd_cleanup`):
  after a kernel is set up, setup's own environments **made from this
  computer** (`marker.setup.host == host_id()`), used by no kernel here and
  not of this computer's kernel-side code, are offered for removal -- default
  **no**, with "`--cleanup HOST` removes them whenever you like".
  `python -m cubevis.setup --cleanup HOST` (also `HOST --cleanup`) lists every
  environment with setup's marker on HOST, with what uses it ("used by
  kernel X" / "not used ..., same code" / "... its kernel-side code
  differs" / "made from gui2"); the default selection is the ones made from
  this computer, unused and superseded; one made from another computer
  needs a separate yes ("whose kernels may use it"); one a kernel here uses
  is never removed.  The user's own environments are never listed.  The
  state's record goes with it (`State.remove_environment`, refused while a
  kernel uses it).  The prompt takes numbers or `none`; Enter takes the
  default (as everywhere in setup).
* **Decisions beyond the design text -- for Darrell to confirm**:
  1. `create -p` with an explicit prefix, not `-n NAME`: the place is then
     what the user was shown (conda's `-n` takes the first *writable*
     `envs_dirs` entry) and a chosen directory needs no special case.
  2. Removal is offered only for environments made from **this** computer
     (marker host): with a configuration directory per laptop, another
     laptop's kernels may be using an environment this one's state knows
     nothing about.  (`--cleanup` can still remove it, after a yes.)
  3. The user's conda configuration decides the channels (not forced to
     conda-forge): only Python and pip come from conda, cubevis and its
     dependencies from PyPI.  At a site whose conda is Anaconda's with the
     default channels, recent conda versions ask for the terms of service
     to be accepted first; the failure shows in the log tail.
  4. Dependencies are whatever PyPI resolves on the day (design 7c: "not
     covered", pinning the `visplot` extra stays open).
* **Test hooks** (the run's environment; never set in normal use):
  `CUBEVIS_SETUP_TEST_INSTALL` -- a shell command that replaces the pip
  install (`$PREFIX` is the new environment) and lifts the development-install
  refusal; `CUBEVIS_SETUP_POLL_SECONDS` -- the polling interval.
* **Tests**: `test_envmake.py` (no ssh; the node program under `python3 -`
  with a fake conda: plan, detached start returns at once, marker last,
  the default pip command and its failure, resume after a failed create
  (partial prefix removed) and after a failed install (create not repeated),
  a second start while running, nothing that is not setup's is touched or
  removed, bad requests, a dead worker reported `died`, `remove` through the
  tool only; every one of these also runs the program under Python 3.6.15
  when `CUBEVIS_TEST_PY36` or `/opt/py36/bin/python3.6` exists, worker
  included -- all pass, no 3.6 changes were needed); `test_hostsetup.py` (the node mocked:
  development install, no conda, the question's content and declining, an
  unwritable or full directory, taken names -> `-2`, the request sent,
  waiting and its end states, an unreachable node, Ctrl-C, which
  environments are setup's and which may go, the offer's default no,
  `State.remove_environment`, `--cleanup` on an unknown host); `test_cli.py`
  (the rig, fake conda extended with `create -y -p` / `env remove -y -p` and
  a `~/fail-conda-create` switch): made on request (`new`) and used,
  `--cleanup` refusing the one in use; none usable -> made without asking,
  an older one offered and removed, the user's own untouched; `--cleanup`
  with a mine / another computer's pair; Ctrl-C while it is made and the
  rerun waiting for the same job (made once); the same finished while away
  (found, nothing made again); a failed create, its message and log tail,
  and the next run going on; the argument checks.
* **Found on the way**: the fake install first pointed the environment at
  cubevis and slept afterwards, so a half-made environment already passed
  discovery's check and was picked up as a user's usable one.  A real pip
  install is not like that (cubevis does not import until it is done), so
  the hook now sleeps *before*.  A window remains in principle -- between
  pip finishing and the marker, a rerun could list the environment as an
  ordinary usable one -- a few milliseconds; harmless (it is the same code).
* **Fixed after Darrell's first real run** (cvpost106, development install):
  answering `new` while an environment was already usable printed "No
  environment on NODE has the same kernel-side code" (false: #1 was ok) and
  then "Environment (empty: stop)".  Now it says setup cannot make an
  environment for a development install, and asks again with the default.
* **Fixed after Darrell's second real run** (cvpost106, Lustre; log
  `cubevis-1.0.99.log`): `conda create` died in the solver with
  `OSError: [Errno 37] No locks available` -- conda locks its repodata
  state file in the package cache (`/home/zuul06/conda/pkgs/cache`), and the
  filesystem there does not lock.  The worker now notices that text in what
  create printed and runs create once more with `CONDA_PKGS_DIRS` set to a
  fresh scratch directory (first of `$TMPDIR`, `/tmp`, `/var/tmp`,
  `/dev/shm` where `lockf` works and 2 GB are free; removed afterwards --
  only python and pip go through conda's cache, the 1.5 GB is pip's).  No
  such directory: the reason says so.  The status now has a one-line
  `reason` (the last `...Error:`/`ERROR:` line of the failed command), shown
  instead of the log's last lines, which were conda's boilerplate ("Upload
  did not complete").  Tests: `test_a_package_cache_that_cannot_lock_...`,
  `test_a_failure_says_why_in_one_line`.  Not seen on a real node: whether
  mamba/micromamba behave the same with `CONDA_PKGS_DIRS`.  pip's own cache
  (`~/.cache/pip`) is not locked with `lockf`, so it should be fine.
* **Suite**: setup suite 195 passed, 1 skipped with the rig and venv-kernel
  (OpenSSH 9.6 only: the 8.0 client was not rebuilt this session; step 3
  adds nothing to the wrapper or ssh's options).
* **Not covered**: mamba and micromamba are driven the same way
  (`create -y -p`, `env remove -y -p`) but only conda's interface is
  faked; a real conda on the real network is the first check -- ask Darrell
  how a `setup HOST` on a node with no usable environment goes (the
  question's wording, the directory default on NRAO's conda, the time).

## 44. Real-node check of step 3; how the wait shows life (base `c405f16`, 2026-10-30)

* **Real run** (Darrell, `try-007A`, cvpost106, cubevis 1.0.101): setup made
  `/home/zuul06/conda/envs/cubevis-1.0.101` (the lock fallback of section 43
  worked), the kernel started, and a visplot session ran.  Step 3 is
  accepted as done.  It took **42 minutes** (conda 9, pip 33): the node's
  filesystem is slow (autofs / Lustre).  The wait printed a line a minute
  with the elapsed time and nothing else; it had two silent gaps (9->10 and
  14->22 minutes) -- polls that did not come back (each `status` call can
  take up to `CALL_TIMEOUT`, 120 s, and failures were not mentioned).
* **Wanted** (Darrell): busy-type feedback during a long wait, but only if
  it is tied to the real health and output of the remote work.
* **Built**, on that rule -- nothing is shown that the node did not report:
  * `envmake.activity_of` (in the node program's `status`): the log's size
    and age, and over the worker's live descendants (`/proc`, Linux; left
    out elsewhere) CPU seconds, bytes read and written (`rchar`+`wchar`, so
    network reads count), process names, and how many are in state D
    (uninterruptible disk wait -- what a stalled shared filesystem looks
    like).  Only for a running job.  Standard library, Python 3.6.
  * `hostsetup.Pulse`: compares two answers.  *moving* = CPU, data or the
    log grew.  Words: `working (pip)`; `quiet for 1:20 (pip is running)`
    after 20 s without movement; `waiting on NODE's disk for 5:00 (...)`
    when some process is in state D; `no output, CPU or data for 6:00,
    though pip is still running -- slow or stuck; it is not stopped` after
    3 minutes.  No report (not Linux): no verdict, as before.
  * `wait_for_job`: the minute line now carries the verdict (and the last
    line of output when not "working"); a line is also printed when the
    verdict turns to or from waiting/no-output; **a poll that does not come
    back** says "NODE is slow to answer (...); the work goes on there" once,
    and "answers again (after M:SS)" later (the give-up rule, 6 failed polls
    in a row, is unchanged).  On a terminal, one line rewritten at each poll
    (`ui.live`, `ui.live_clear`; any other message clears it first):
    `| installing cubevis (pip)   14:03   working (pip)   Downloading ...`;
    the spinner turns **only** when the node reported movement, so a frozen
    spinner means nothing is happening.  Not a terminal: lines only.
* **Brittleness**: `/proc` field positions (`stat`: state after the closing
  parenthesis of the name; utime, stime, cutime, cstime = fields 14-17) and
  `/proc/PID/io` are stable Linux interfaces; any failure leaves `activity`
  smaller or absent, never an error.  Counters of finished children fold
  into their parent's `cutime`, so CPU does not fall as pip's helpers exit;
  `rchar`/`wchar` can, so only an increase counts as movement.  A process
  that is slow but alive is reported as such ("slow or stuck"), not as a
  failure: the verdict never stops the work or gives up.
* **Suite**: setup suite 214 passed, 1 skipped (rig, venv-kernel, OpenSSH 9.6).
* **Tests**: `test_envmake.py` (the activity of a running job: names,
  CPU, data, disk wait 0, log age; gone when done; also under Python 3.6),
  `test_hostsetup.py` (Pulse moving / not; the words for working, quiet,
  waiting on disk, stuck; the lines of a wait; a node that stops answering
  and returns; the live line on a terminal only).  Not tested: a real
  state-D process (the wording is checked with the count as the node would
  send it).
* **Not covered**: pip's own progress (its output goes to the log; with no
  terminal it prints "Downloading X (size)" lines, which the last-output
  text shows); a verdict for macOS/BSD nodes; and whether 20 s / 3 min are
  the right thresholds for NRAO's filesystems -- ask Darrell.

## 45. Stage 2c step 4: the user's ssh as the reference, `diagnose`, `--2fa` (base `d3c99fb`, 2026-10-31)

Design 7c, "The user's ssh configuration is the reference", "Two-factor" and
"Diagnostics, on both sides".  Section 40 had taken defaults from `ssh -G`;
this is the rest.

* **Login options copied** (`cubevis/setup/sshopts.py`, new).  `ssh -G NAME`
  (the user's `~/.ssh/config` *and* `/etc/ssh/ssh_config`) against `ssh -F
  /dev/null -G NAME` (ssh's own defaults); the options that differ, among
  those that affect logging in, go into the host's block of the generated
  `ssh/config` under `# from your ssh configuration (ssh -G NAME)`:
  identity and certificate files, `IdentitiesOnly`, `IdentityAgent`,
  `gssapi*` (Kerberos; Red Hat's extra GSSAPI options too), PKCS#11 and
  security-key providers, the authentication methods and prompts, the
  algorithm lists (RHEL's crypto policies arrive this way), known-hosts
  files, `HostKeyAlias`, `CheckHostIP`, `VerifyHostKeyDNS`, `AddressFamily`;
  `ProxyCommand` only where the route is the user's (a network without a
  login host).  Never copied: host name, user, port (setup's fields),
  `ProxyJump` (setup's login host), connection sharing and keep-alives
  (setup's).  A path with spaces is quoted.
  - **Nothing is stored**: the options are read from `ssh -G` each time the
    file is written (every save; cached per process by the files' times), so
    an edit to `~/.ssh/config` reaches it at the next setup run, and each
    computer sharing a directory writes from its own ssh -- the option names
    are that ssh's (OpenSSH 8.0 `pubkeyacceptedkeytypes`, 9.x
    `pubkeyacceptedalgorithms`).
  - **The names the user's ssh knows**: nodes and login hosts now record
    `ssh_name` (what the user typed, or the ProxyJump hop's name: `cvpost106`,
    `ssh`), and `ssh -G` is asked about that name (its port then comes from
    the user's configuration too); without one, the real name and setup's
    port.
* **The route, adopted** (`hostsetup.new_network`).  A `ProxyJump` in the
  user's configuration becomes the login host without questions (no
  "intermediate host?", no user names: the configured ones), said in one
  line.  Several hops (`gw,ssh`): the last is the login host, the earlier
  ones its own `ProxyJump` (`login.via`), written as real `user@host:port`
  (`sshopts.resolve_jump`: the generated file knows none of the user's
  aliases; the user only when it is not ssh's default).  Earlier hops log in
  by key or Kerberos only (no rules for them).
  - **No ProxyJump**: a login with the user's ssh is tried
    (`sshopts.try_login`: `BatchMode`, `ControlMaster=no`,
    `StrictHostKeyChecking=yes` -- it never asks, never becomes a master,
    never records a host key) and sorted into ok / answers (reachable, wants
    something typed or an unknown key) / unreachable.  Reached: "Do you also
    log in to HOST from elsewhere (off site, say) through an intermediate
    host? [y/N]"; not reached: the old question, default yes.  Still asked
    (not skipped) when reached: a laptop on site at setup time is off site
    later -- the inside ranges then decide.  Skipped when `via_login` is
    answered (tests).  With exactly one known login host, it is the default
    name.
  - **User names**: not asked when the user's configuration sets the host
    (as `ssh -G` reports it); asked as before otherwise.
* **The login is tested before anything is kept** (`hostsetup.forget_new`).
  When the login to a host placed in this run fails, its node -- and its
  network, when new and now empty -- are removed again and saved ("forgot:
  node X, network Y (the login did not work)"; "Nothing about HOST was kept
  ... asks again").  A password already stored stays in its store.  Not for
  a two-factor network (the user logs in next, with the command setup
  printed, which names the generated alias).  The old test
  (`test_unattended_failure_...`) expected the node kept, flagged: changed.
* **Your ssh logs in, setup's does not** (`steps.hop_steps`): before offering
  to store a password, a hop without one tries the user's own ssh; when that
  logs in without asking, setup says the generated file lacks something,
  names `diagnose`, and offers no password.
* **`python -m cubevis.setup diagnose HOST`** (`cubevis/setup/diagnose.py`,
  new; read-only, one login attempt each way).  Sections: (1) what setup
  knows (node, network, cluster, the user's names, how each hop logs in,
  status); (2) per hop, the user's `ssh -G` and setup's `ssh -F ... -G
  ALIAS` (route as decided now), the login options carried, differences in
  host, user, port, and `PROBLEM: setup's lacks OPTION VALUE` with the
  advice to run setup again; (3) the route (inside/outside and why, the
  override variable); (4) the wrapper's `--explain` (rules, stored password,
  shared connections); (5) a login with the user's ssh (never asking) and
  with setup's -- on failure the wrapper's notes and the last 15 useful
  `debug1:` lines of `ssh -v`; (6) the kernel-side code here and in each
  environment recorded on the node (`discover --only`).  A host setup does
  not know: only the user's view (exit 1 when it is not reachable).  Exit 0
  when setup's login works.  Meant to be pasted to whoever helps (no
  secrets: only whether a password is stored).
* **`--2fa` / `--no-2fa`** (`setup HOST --2fa`): marks the hop whose login
  asks for a second factor -- the network's login host, or its nodes when it
  has none (`twofactor` in the record).
  - **Rules**: `HOST  twofactor=yes` instead of a stored-password rule (the
    password rule of a two-factor hop is left out; the "no store chosen yet"
    case now still writes the other rules).
  - **The wrapper**: a `twofactor=yes` hop is never logged in to with a
    password.  When its shared connection is not up: with a terminal, it
    runs `ssh -fN` there (BatchMode off, ControlMaster auto,
    ControlPersist), so the user answers ssh's own prompts once, then goes
    on; without one (sshpyk, setup's checks) it stops: "asks for a second
    factor, and there is no shared connection to it now ... Log in once in a
    terminal -- it is then shared:" and the command (`<wrapper> -F
    <config> ALIAS true`).  `--explain` says "second factor: you log in".
  - **Setup**: the hop step says why nothing is stored and runs that command
    with the terminal (the user logs in); without a terminal, prints it and
    stops (the network is kept).  `status` shows "login asks for a second
    factor".  Opening it from a notebook stays a stage 3/5 question.
* **Tests**: `test_sshopts.py` (new): which options are copied and which
  never; nothing from an empty configuration; jumps to real names;
  adoption, with the generated file checked by `ssh -F ... -G` (identity
  file with a space, GSSAPI, setup's ControlPath and keep-alives); a changed
  user configuration followed at the next write; several hops; a
  ProxyCommand route on a direct network; `try_login` (refused, unresolvable;
  the rig: answers); the direct-login default (rig); "your ssh logs in but
  setup's does not".  `test_cli.py`: the route from a user configuration end
  to end (no questions, the option carried, `ssh_name` kept); a failed login
  keeps nothing; the two-factor login host (no terminal: setup and the
  wrapper name the command; typed through a pseudo-terminal, the
  connection is shared and setup and the node's stored-password login ride
  on it; `--no-2fa`); `diagnose` (healthy, a user option added later, a
  failing login, an unknown host; state and generated file unchanged); the
  interactive run's question changed (the rig's node answers directly:
  default no).  `conftest.py` (new): every setup test runs with
  `CUBEVIS_SETUP_USER_SSH_CONFIG=/dev/null` unless it sets its own --
  otherwise this machine's `/etc/ssh/ssh_config` (`GSSAPIAuthentication yes`
  on Ubuntu) leaks into the generated files.
* **Decisions beyond the design text -- for Darrell to confirm**:
  1. A host reached directly is still asked about a login host (default
     no), rather than recorded as direct.
  2. A failed first login keeps nothing (instead of keeping it flagged).
  3. Options are read from `ssh -G` at every write, never stored.
  4. Several ProxyJump hops: the last is setup's login host; earlier hops
     are key/Kerberos only.
  5. With a terminal, the wrapper itself lets the user log in to a
     two-factor hop (it is also the user's ssh in a terminal, when on PATH).
* **macOS**: Apple's ssh prints `usekeychain` (a key's passphrase from the
  Keychain, without an agent): copied too; other builds never print it.
* **Found on the way**: section 42's stale-connection test froze, then
  terminated, *every* `sshd: alice` session on the rig -- other tests'
  connections too, when they ran alongside (`-n 2`); the new tests shifted
  the schedule so the cluster test did, and failed at discovery with a bare
  `ssh exit 255`.  It now freezes only its own master's session (the parent
  of `echo $PPID` run over it).  The test kit's OpenSSH 8.0 client is built
  without GSSAPI and prints no `gssapi*` options: the option tests use
  `PreferredAuthentications` and check GSSAPI only where ssh has it.
* **Not tried on a real network**: Kerberos (GSSAPI) logins at NRAO through
  the generated file; macOS's ssh; a real second factor.

## 46. Stage 2d: development installs -- `--sync-checkout`, `--wheel` (base `d3c99fb`, 2026-10-31)

Design 7c, "Fingerprint override" (`--sync-checkout`) and section 37's plan
(an environment from a local wheel).  Until now a development install here
got advice only ("install the same checkout there, then run this again").

* **What is copied** (`cubevis/setup/sync.py`, new): the checkout this cubevis
  is *imported from* (`sync.checkout_root`: the directory above the package,
  when its `pyproject.toml` names cubevis -- an editable install or
  `PYTHONPATH`; not `direct_url.json`, so what is copied is the code that
  runs here): the `cubevis` package, everything in it but byte code, caches,
  dot-directories and editor files -- tracked by git or not (a module just
  written is code the kernels may run) -- plus `pyproject.toml`,
  `README.rst`, `LICENSE`.  This checkout: about 2 MB compressed.  Not
  `casalib/`, `cubevisjs/`, docs, tests, demos.
* **How**: a gzip tar stream (`tarfile` here: no AppleDouble files from
  macOS's tar, owners blanked) on the stdin of setup's own ssh (the wrapper,
  the generated configuration), unpacked on the node by one `/bin/sh` line
  (`sync._PUSH_SCRIPT`, one line for csh, paths as arguments) into `DEST.new.$$`,
  then `DEST` -> `DEST.old.$$`, `DEST.new` -> `DEST`, the old removed: never a
  half-copied tree, and files deleted here are gone there.  It reports whether
  `pyproject.toml` changed (`cmp`).  Needs `tar`, `sh`, `mv` there -- no rsync
  (macOS's openrsync, and nodes without rsync).
* **`setup HOST --sync-checkout`**:
  - **First time**: setup's own environment `cubevis-dev-<this computer>`
    (`envmake.dev_name`; `-2`... when taken), made like section 43's (the
    same question, place, background job), from the copy: setup pushes the
    code to `<envs dir>/.<name>.cubevis-src` (`envmake.staged_dir`: beside
    the prefix, same filesystem) before starting; the worker, after
    `create`, moves it to `PREFIX/cubevis-src` and runs `PREFIX/bin/python -m
    pip install -e PREFIX/cubevis-src[visplot]`.  The build takes its
    version from git, which the copy lacks: `PDM_PEP517_SCM_VERSION` (and
    `PDM_BUILD_SCM_VERSION`, `SETUPTOOLS_SCM_PRETEND_VERSION_FOR_CUBEVIS`, for
    a later backend) = this cubevis's version (tested by hand: without it
    pdm-pep517 says 0.0.0; the editable install is a plain `.pth` to the
    directory, so later copies take effect at once, new modules included).
    The marker gains `source: checkout` and `from: <checkout>`.
  - **Again** (the environment is found by its marker: `source: checkout`,
    made from this computer): the code is copied straight into
    `PREFIX/cubevis-src`; pip runs again (in the foreground,
    `sync.pip_install`) only when `pyproject.toml` changed; discovery checks
    it (`--only`).  The one environment of setup's that changes after
    creation -- by design.
  - **The host's kernels**: each on another environment is offered the move
    ("Kernel visplot uses cubevis-1.0.101, whose kernel-side code differs
    from here. Use the checkout's environment ... for it?"; default yes when
    its code differs, else no); then their launchers are rewritten and run,
    as on any rerun.  On a host without kernels, the usual kernel questions.
  - **Cleanup**: never offered as superseded (after a local edit it differs
    until the next sync); `--cleanup` lists it as "a checkout's copy
    (--sync-checkout brings it up to date)", not selected by default.
  - Refused unless cubevis is imported from a checkout (argument error).
* **`setup HOST --wheel PATH`**: the file (`cubevis-VERSION-...whl`, checked
  by name) is copied to the staging directory, moved in by the worker, and
  installed as `PREFIX/cubevis-src/FILE[visplot]`; the environment is named
  for the wheel's version (`cubevis-1.0.103.dev7`), never changed afterwards
  (a release-like environment); marker `source: wheel`.  Used when setup
  makes an environment (none usable, or `new`).  **Without the flag**, a
  development install that was installed *from a wheel still on disk*
  (`direct_url.json`, same version) uses that wheel automatically.
* **Advice** for a development install with neither now names both flags
  (`hostsetup.development_advice`), and `CUBEVIS_ALLOW_MISMATCH`.
* **envmake**: the start request may carry `source` (`kind`, `staged`,
  `file`, `from`) and `pip_env`; a retry after an install failure takes the
  newer staged copy (the old `cubevis-src` replaced); a staged copy that is
  missing fails with a reason; `remove` also removes a staged copy a failed
  try left beside the prefix (setup's own; never an environment).  The test
  install hook sees the copy as `$SOURCE`.
* **Tests**: `test_sync.py` (new): what a checkout's copy holds (and what
  not), this checkout found, the tar stream, wheel versions, the node-side
  line run here with `sh` (whole replacement, deleted files gone, nothing left
  beside, the pyproject flag; a broken stream leaves the old copy), the pip
  command after a dependency change.  `test_envmake.py`: a checkout moved in
  and installed from there (`$SOURCE`, the version variable, the marker), a
  wheel by its file with the extra (and no `-e`), a missing copy, a retry
  taking the newer copy, `remove` taking a leftover copy (also under Python
  3.6).  `test_cli.py` (rig): the development loop -- a kernel on `hrs`, the
  code changed, `--sync-checkout` makes `cubevis-dev-gui1`, moves the
  kernel, the kernel starts with the copied code; changed again, copied
  again (same environment, no pip), the kernel matches; changed and not
  copied, the kernel fails on the kernel-side code; `--cleanup` does not
  select it -- and an environment from a wheel.  The rig's fake environments
  have no pip: the real `pip install -e` of a copy was tried by hand only
  (above), with `--no-deps`.
* **Found on the way**: `python -m` puts the current directory first on
  `sys.path`, ahead of `PYTHONPATH`: the rig test runs setup from its
  temporary home, or the repository's own cubevis is imported (`_run(cwd=)`).
* **Left from 2d** (design section 13): `prune`, `setup --local` (the plotting
  computer's own kernels), and the final kernel + plot test -- ask Darrell
  whether any is wanted before stage 3; `CUBEVIS_ALLOW_MISMATCH` /
  `allow_mismatch=` at run time is stage 3's.
* **Suite** (sections 45 and 46 together): setup suite 245 passed, 1 skipped,
  with OpenSSH 9.6 and with the 8.0p1 client (rig, venv-kernel, `-n 2`);
  `test_envmake.py` also under Python 3.6.15.
* **Not tried on a real network**: a real `pip install -e` of the copy with
  its dependencies on a node; macOS as the plotting computer (the tar stream
  is built in Python for that reason).

## 47. A checkout and the release it equals: the fingerprint fixed (base `c78df91`, 2026-11-01)

Darrell's first run of section 45/46 (macOS, cubevis used as a git clone
without installing it: `tests/manual/visplot/cubevis -> ../../../cubevis`,
at `v1.0.103`): setup listed `cvpost106`'s `cubevis-1.0.102` (made by setup
from PyPI) as "kernel-side code differs (there k1-e222..., here
k1-91e9...)", called the checkout "version unknown, not a release" and
offered only `--sync-checkout` / `--wheel`.

* **The cause was the fingerprint, not the checkout.**  The kernel-side
  source of v1.0.100 through v1.0.103 is identical (fingerprinted from git:
  all `k1-91e9...`, like the checkout), but the 1.0.102 *wheel* gave
  `k1-e222...`: the import tracing counted `cubevis/__version__.py`, which
  the build writes into the package (`pyproject.toml`, `write_to`) -- in
  every wheel, never in a checkout, and different in every release.  So no
  checkout ever matched a release, and no release matched another: section
  7c's "a GUI-only release keeps the fingerprint and reuses environments"
  never held.
* **Fixed** (`cubevis/_fingerprint.py`): `GENERATED` modules
  (`cubevis.__version__`) are left out; `ALGORITHM` is now `k2` (the same
  files can hash differently from k1, so the two must not be compared as
  equals; `compatible` says "computed another way ... run setup for this
  host again").  Checkout, the 1.0.102 wheel and the 1.0.103 tag: all
  `k2-67f9938a2b6ba9ce4a63`.
* **The node computes with this computer's code** (`_fingerprint.source()`):
  `envcheck` gets `FINGERPRINT_SOURCE` prepended (`discover.with_fingerprint`,
  in the payload and when run by hand from an installed cubevis), runs it in a
  fresh namespace over the environment's files (found with
  `importlib.util.find_spec("cubevis")`; nothing of cubevis imported for
  it); without it (an older setup), the environment's own copy as before.
  The launcher test likewise: `TEST_CODE` is now `import
  sys;exec(sys.stdin.read())` and `launcher.test_program()` (the kernel
  imports, then the fingerprint with the sent code) goes on stdin -- a short
  command line, nothing for csh to mangle.  So environments made before a
  rule change (the 1.0.102 environment above) are judged by today's rules:
  it is usable again with no change on the node.  **Stage 3**: the worker's
  own run-time report must hash the same way -- a worker from before `k2`
  reports `k1-...`, which `compatible` names.
* **A checkout's version** (`hostsetup.local_version`): cubevis imported
  from a checkout that is not installed (or installed from elsewhere) takes
  its version from git, as the build names it (`sync.git_version`): at an
  unchanged tag `1.0.103`; with uncommitted changes to tracked files
  `1.0.103+gHASH.dYYYYMMDD`; N commits on `1.0.104.devN+gHASH`.  An
  editable install of the checkout keeps the installed metadata's version.
  `local_is_development`: a checkout is a release only at an exact,
  unchanged release tag.
* **A checkout with a release's kernel code** (`sync.release_with_same_code`,
  `hostsetup.same_release`, once per run): the nearest tag's `cubevis/`
  from `git archive`, fingerprinted here, compared with the checkout's.
  Equal: setup's environment is that release from PyPI ("cubevis 1.0.103
  (cubevis[visplot]==1.0.103, from PyPI; this checkout (...) has the same
  kernel-side code)"), named `cubevis-1.0.103`, and the declined advice gives
  its commands -- no copy, no "development install" message.  Different:
  section 46's `--sync-checkout` / `--wheel` advice as before.  PyPI is never
  asked for fingerprints (it has none to give): git has the release's code.
* **Tests**: `test_fingerprint.py` (the build's version file does not count;
  another algorithm said plainly; the sent source gives the same answer);
  `test_discover.py` (an environment whose own `_fingerprint` answers
  `k1-old` and that has a `__version__.py` is hashed with the code sent;
  without it, its own answer; the csh launcher test with the program on
  stdin); `test_launcher.py` (the program on stdin); `test_sync.py` (versions
  from git at the tag, changed, one commit on; the release with the same
  kernel code, and none after a kernel-side change); `test_hostsetup.py`
  (an equal checkout gets the release from PyPI).

## 48. Releases carry their fingerprint: pdm-backend and a build hook; setup reads PyPI (base `c78df91`, 2026-11-01)

Agreed with Darrell after section 47: a release's kernel-side fingerprint
should come from *what was published* (section 47's bug was exactly a
difference between the published wheel and its tagged source), stamped by
the build itself -- not by a CI step, which a local build would skip, and not
by making the README a generated file (GitHub shows the committed README; a
checkout's build or editable install needs the file; a stale copy would
carry a wrong value).

* **The build backend is now pdm-backend** (`pyproject.toml`:
  `requires = ["pdm-backend>=2.4"]`, `build-backend = "pdm.backend"`).
  pdm-pep517 (its predecessor, same project) has no build hooks.  The
  `[tool.pdm.build]` excludes and `[tool.pdm.version]` (`source = "scm"`,
  `write_to`, `write_template`) carry over unchanged; `publish.yml` runs
  `pdm build` as before.  Compared by hand, `python -m build` of v1.0.103
  with each backend: the wheel has the same 227 files (plus an empty
  `entry_points.txt`), the same metadata fields (other order; `Author-Email`
  spelled so), the description identical but for the stamp line.  The
  sdist now also holds `cubevis/__version__.py` and `pdm_build.py`, and
  `README.rst` only through `source-includes` (the readme travels as
  metadata text).  Versions: at a tag `1.0.103`; two commits on
  `1.0.104.dev2+gHASH`; uncommitted changes at a tag `1.0.103+dYYYYMMDD`
  (`sync.git_version` now spells them the same way).  An editable install
  of a copy without `.git` takes `PDM_BUILD_SCM_VERSION` (already among the
  variables `--sync-checkout` passes).
* **The hook** (`pdm_build.py`, repository root; pdm-backend runs it on every
  build path -- CI, a local build, a wheel from the sdist, an editable
  install): `pdm_build_initialize` reads the readme file (or, for a wheel
  built from the sdist, the text the sdist's metadata already holds),
  removes any earlier stamp, appends `.. cubevis-kernel-fingerprint: k2-...`
  (a reST comment: not shown on PyPI) computed from the tree being built by
  that tree's own `cubevis/_fingerprint.py` (loaded by path: standard
  library only), and puts the result in the metadata as text.  The README
  file is never changed.
* **Setup reads it** (`cubevis/setup/releases.py`): PyPI's JSON
  (`https://pypi.org/pypi/cubevis/json`, then `/<version>/json`'s
  `info.description`) for the newest final, non-yanked releases (12 at
  most), newest first, until one is stamped with this computer's
  fingerprint.  No network or no answer: skipped.
  `CUBEVIS_SETUP_PYPI` names a mirror, or `off`.  Releases from before the
  hook carry no stamp; for them, section 47's git check (the checkout's
  nearest tag, fingerprinted here) remains the fallback
  (`hostsetup.same_release`: PyPI first, then git).
* **Tests**: `test_build_hook.py` (the description stamped with the tree's
  fingerprint, once; a wheel from the sdist re-stamped, not twice; the stamp
  is what setup parses; the backend), `test_releases.py` (newest matching
  release, stopping there; unstamped releases do not count; final,
  non-yanked releases in numeric order; no network, `off`, a mirror).
* **For Darrell**: the next release (1.0.104) is the first with a stamp;
  until then setup relies on the git check.  Worth a look at
  `https://pypi.org/project/cubevis/` after it: the page should look as
  before.
* **Suite** (sections 47 and 48): setup suite 262 passed, 1 skipped with
  OpenSSH 9.6, run as `-n 2 --dist loadfile` (each file on one worker).
  Plain `-n 2` now shows clashes between `test_cli.py` tests running
  side by side -- they share alice's account and the computer id `gui1`:
  kernel `compute`'s launcher rewritten by another test between a run and
  its rerun ("Changed in this run: wrote launcher"), and environment
  numbers shifted by another test's new environment (the mismatch test then
  picks a usable one).  Old hazards that a new schedule exposed; each test
  passes alone.  `--dist loadfile` keeps a file's tests in order on one
  worker and costs nothing (7 minutes).


## 49. visplot checks the kernel by fingerprint, not by version (base `98fda78`, 2026-11-01)

Darrell's rerun after sections 47/48 (`try-009A`): setup found
`cvpost106`'s `cubevis-1.0.102` usable ("same kernel-side code as here"),
made kernel `visplot` on it, and visplot plotted -- but the terminal and the
status line said "kernel cubevis 1.0.102 differs from local unknown (git
98fda78373+dirty); results may differ.  Install the same version where the
kernel runs".  That is the 2026-10-12 check (`plot_summary.version_mismatch`),
which compares version strings: a checkout used without installing it has
none, and a release whose kernel side did not change has another.

* **Now** (`visibility_plotter.kernel_code_check`, local side only): the
  kernel-side fingerprints decide.  `RemoteReductionContext.kernel_fingerprint()`
  runs, in the Jupyter kernel, this computer's `_fingerprint` code sent as
  text over the kernel's cubevis files (as setup's `envcheck` does on the
  node), on a client of its own that reads only the replies to its own
  request (comm traffic on the same kernel cannot end it early).  Same:
  nothing said (an INFO line in the log); different: "its kernel-side code
  differs (there ..., here ...); results may differ or calls fail.  python -m
  cubevis.setup HOST makes or chooses a matching environment".  The
  versions are compared, as before, only when the kernel's fingerprint
  cannot be had.  The kernel's versions are still collected for the plot
  summary.
* **Why not a worker method**: `plot_summary`, `remote_registrations` and
  `cubevis.remote._link` are kernel-side (in the fingerprint); adding a
  method there would change the fingerprint and make every existing
  environment -- Darrell's 1.0.102 among them -- a mismatch.  Asking the
  Jupyter kernel itself needs no kernel-side change: the fingerprint is
  still `k2-67f9938a2b6ba9ce4a63`.  (Design 7c's "the kernel side reports
  its fingerprint in the opening configure round trip" for stage 3 can be
  this same request.)
* **Tests**: `test_kernel_check.py` (same code under another version: no
  warning; other code: warned by fingerprint; no fingerprint from the
  kernel, or a context from before: the versions decide);
  `test_cli.py::test_rig_visplot_asks_the_kernel_for_its_fingerprint` (a
  kernel set up on the rig, started through its Jupyter kernel definition --
  sshpyk, the wrapper, the launcher -- reports the fingerprint of here).
  The rig's test measurement set was not in this sandbox (it restarted
  twice), so the plotter itself was not started here; the round trip it
  uses was.
* **Found in Darrell's next run (`try-010A`)**: the check passed ("kernel-side
  code identical") and then visplot hung on its first worker call
  (`axis_info`).  Reproduced with a local kernel and the real worker
  (`cubevis.remote._test_registrations`, `Counter`): even a second client
  that only ran `wait_for_ready()` made the next worker reply vanish; a
  second client in another process, or one with a `Session` of its own, did
  not.  `km.client()` gives every client the manager's own `Session`
  object, and a Session rejects a message whose signature it has already
  seen (its replay check, `digest_history`): iopub messages reach both
  clients, so the link's client dropped the worker's replies.  Fixed:
  `kernel_fingerprint` makes its client with a new `Session` (same key,
  signature scheme and user name).  Test:
  `tests/manual/remote/test_kernel_fingerprint.py` (a local kernel, a real
  worker: calls before, between and after two fingerprint requests; it
  times out without the fix).  The rest of `tests/manual/remote`: 40 passed
  (`test_query_raster*.py` need a Jupyter kernel definition this sandbox
  lacks; not run).  **For stage 3**: anything else that talks to a kernel
  beside the link must not share the kernel manager's Session either.
* **Cost of the check** (asked by Darrell): measured with a local kernel,
  `kernel_fingerprint()` takes 0.8 s (2.6 s on a kernel's first request,
  while it warms up): 0.2 s for the extra client (connect, ready; no new
  kernel, no new ssh login -- the tunnels sshpyk opened are reused) and the
  rest hashing the ~80 kernel-side modules in the kernel, which on
  cvpost106's filesystem may take longer.  Agreed: the local fingerprint is
  now computed once per Python session (`visibility_plotter.local_fingerprint`;
  it was ~0.7 s per plotter), and the check's time is logged ("kernel-side
  code check took N s (kernel N s, here N s)") so a real run shows it.
  Running the check alongside `create_object` stays possible if the real
  number turns out to matter.
* **Darrell's `try-011A` (2026-11-01)**: with the v3 zip, `setup cvpost106`
  chose `cubevis-1.0.102` by fingerprint and visplot plotted on it: "kernel-
  side code check took 1.1s (kernel 0.9s, here 0.3s)", "identical", no
  warning, no hang.  Section 49 confirmed on the real network.  A long
  pause follows the check: the first build of the plots (`_build_panels`:
  axis_info, the first raster and scatter queries and renders), which logs
  no timing -- not the check.  Phase timing for it offered, not yet asked.

## 50. `--sync-checkout` and `--wheel` removed (base `940c10b`, 2026-11-02)

Darrell's decision, after section 46 was built and used once on the rig only:

* **Why** (his reasons, which Claude agreed with): configuring a node over
  ssh is error-prone; it is one more configuration path, orthogonal to the
  rest; its users are the few developers of the kernel side, who are also
  the ones best able to set up a compatible conda environment that setup
  then finds by fingerprint; it would see little use and so drift out of
  date (its riskiest part, the real `pip install -e` of a copy, could not even
  be tested on the rig); and on NRAO's cluster nodes, conda on an
  autofs-mounted drive is very slow anyway.  `--wheel` falls under the same
  reasoning.
* **Removed**: the `--sync-checkout` and `--wheel` options; in `hostsetup`
  `sync_checkout_step`, `move_kernels_step`, `own_checkout_env`,
  `creation_source` (also the automatic use of the wheel cubevis was
  installed from) and `offer_creation`'s `source`; the cleanup labels for a
  checkout's or a wheel's environment; `sync.py`'s copying (tar stream,
  `push`, `pip_install`, staging).  `envmake.py` and `test_envmake.py` are back
  to their `d3c99fb` text (their only changes since were these).  Tests:
  `test_sync.py` and the two rig tests (the development loop, the wheel).
* **Kept**: `checkout.py` (new; from `sync.py`): `checkout_root`,
  `git_version`, `release_with_same_code` -- a checkout used without
  installing it gets its version from git, and one whose kernel-side code
  is a release's is served by that release from PyPI (sections 47, 48); its
  tests in `test_checkout.py` (plus: a package without a cubevis
  `pyproject.toml` above it is not a checkout).
* **Instead, when nothing on the node matches a development install**:
  `hostsetup.development_advice` says to put this checkout (a clone of the
  same commit, or a copy) where the node sees it, `pip install -e` it in a
  conda environment there, and run `setup HOST` again -- setup finds the
  environment by its kernel-side code; or `CUBEVIS_ALLOW_MISMATCH=1`.
* **`setup --local`** (design 7c, "the plotting computer's own kernels";
  never built): raised by Darrell as possibly of the same flavor.  Claude's
  view: yes -- visplot already runs on the plotting computer without a
  kernel or setup; a local kernel would only serve a backend in a
  *different* local Python environment, which nothing needs today.
  Proposed: dropped from the plan (nothing to remove); Darrell to confirm.
* **Suite** (2026-11-02): 252 passed, 1 skipped with OpenSSH 9.6 and with
  the 8.0p1 client (`-n 2 --dist loadfile`).

## 51. Wrapping up setup, round 1: the open decisions (base `4037f02`, 2026-11-02)

Darrell's `try-012A`: `python -m cubevis.setup diagnose cvpost106` from his
Mac -- every section as expected (route adopted from his `ProxyJump`,
setup's configuration carrying all his login options, both logins working,
`cubevis-1.0.102` identical by fingerprint to his 1.0.107 checkout).  The
PyPI stamp (section 48) is on 1.0.104 and 1.0.107
(`k2-67f9938a2b6ba9ce4a63`).  The release run of 1.0.104 waited about
20 minutes for a GitHub runner ("Starting job"): GitHub's side.

**Decisions of sections 43 and 45, settled with Darrell:**

* *A host reached directly is still asked about a login host*: kept, the
  question reworded -- "When this computer is away from HOST's network
  (off site, a visitor network), do you reach HOST through an intermediate
  host?", after a line saying it is used only from outside.
* *A failed first login keeps nothing*: kept for the state and the ssh
  files, but the answers are now kept as a **draft** (the network's
  record, in this computer's `local.json` under `drafts`, by host name);
  the next `setup HOST` shows them and asks "Use those answers again?"
  (default yes; `--answer use_draft=n` declines); a successful login, or
  declining, drops it.  `hostsetup.save_draft` / `draft_network` /
  `drop_draft`.
* *ssh options read fresh at every write*: to change (round 2): stored per
  host, a fresh `ssh -G` compared with them at each run, a difference
  reported (log, `status`, `diagnose`) and adopted only by `setup HOST`.
  Comparing `ssh -G`'s output rather than `~/.ssh/config`'s time stamp:
  it also sees `Include`d files, `/etc/ssh/ssh_config` and agent or
  Kerberos changes.
* *Several ProxyJump hops*: to change (round 2): each jump host its own
  entry, with its login and its route; the generated configuration builds
  the chain.
* *`create -p` with an explicit path*, *the user's conda channels*,
  *unpinned dependencies*: kept as they are.
* *Removal only for environments this computer made*: **changed** -- any
  environment with setup's marker, from any computer, is offered
  (`offer_cleanup`) and listed with the default to remove when unused and
  superseded (`--cleanup HOST`), each with which computer made it and when
  (`made_by`: "made from gui2, 2026-10-22").  Never one a kernel here uses.
  Darrell: environments can vanish anyway (removed by hand, or by setup
  from another computer), so kernels must cope with that -- next item.
* *The host-key question* (a node reached straight away on a new account
  or from another computer sharing the configuration): round 2 -- setup
  keeps its own `known_hosts` in the configuration directory, filled while
  the user runs setup (fingerprints shown), listed in the generated
  configuration beside the user's; kernels connect with strict checking,
  and an unknown or changed key stops them with a message naming `setup
  HOST` / `diagnose`.
* *A dead worker leaves the plotter waiting forever*: round 3 (it is in
  `cubevis.remote`): reproduce on the rig (the worker, the kernel, the ssh
  connection killed), report in the GUI and the terminal within seconds.
* `prune`, `setup --local`, the final kernel + plot test: **dropped**
  (design section 13).

**Built in this round:**

* **A kernel whose environment is gone**: the launcher checks the
  environment's python first and stops with `cubevis: the environment of
  kernel K, PREFIX, is gone from this node (removed?): python -m
  cubevis.setup HOST gives the kernel another` (`launcher.ENV_GONE`);
  `setup check` flags the kernel with it, `diagnose` says "not found on
  NODE -- removed?" with the same advice.  `setup HOST` then asks "[r]eplace
  its environment (choose another; the kernel's name, directory and
  variables stay), [f]orget the kernel, or [k]eep it as it is?" (default
  r; key `env_gone`): replace runs the usual environment choice (making
  setup's own when nothing is usable) and re-tests the kernel; forget
  removes the kernel's record, its Jupyter kernels here and its
  environment's record (`forget_kernel`, `State.remove_kernel`); nothing
  on the node is touched.
* **Inside ranges** contained in another are dropped
  (`state.collapse_ranges`): in what `setup HOST` finds and offers, in what
  it records, in `--inside-here`, in `status` and `diagnose`; ranges
  recorded before are simplified at the next `setup HOST` ("simplified
  inside ranges").  Darrell's cvpost106: `10.7.7.0/24, 10.7.17.0/24,
  10.7.0.0/16` -> `10.7.0.0/16`.
* `cubevis/setup/sync.py` and `tests/manual/setup/test_sync.py`, still in
  `4037f02` (a zip cannot delete), are deleted again in this round's list.
* **Tests**: `test_inside.py` (collapsing); `test_hostsetup.py` (the
  removal offer from any computer); `test_cli.py` -- the failed first
  login keeps a draft, which is offered and reused (rig) or declined; the
  cleanup of environments made from two computers; a kernel whose
  environment is gone: check, keep, diagnose, replace (setup's own made),
  forget; the reworded question (`test_sshopts.py` too).
* **Suite** (2026-11-02): 254 passed, 1 skipped with OpenSSH 9.6 and with
  the 8.0p1 client (`-n 2 --dist loadfile`; the rig rebuilt after a sandbox
  restart).

## 52. `--pythonhome`: Python's slow start on autofs (base `4037f02`, after section 51; 2026-11-02)

Darrell: sshpyk now lets `PYTHONHOME` be set before the remote Pythons
start (casangi/sshpyk PR 13, in sshpyk 1.25; PR 12 the first attempt).
Without it, CPython looks for its own installation by probing landmark
files up the directory tree; on an automounted `/home` (zuul06) a probe
such as `/home/lib/python312.zip` blocks for up to ~50 s.  sshpyk has two
settings because its launcher Python (`remote_python`, running
`SSHKernelApp`) and the kernel's (the remote kernelspec's `argv`) may be
different installations: `remote_pythonhome` (set while the launcher
starts, then restored) and `remote_kernel_pythonhome` (kept for the
kernel and its children).

**For setup's kernels both are the same environment** -- `remote_python`
is setup's launcher, which activates the environment and `exec`s its
python; the remote kernelspec is `python3`, ipykernel's native one
(`sys.executable`).  So setup sets it **in its own launcher**, once, and
both processes get it, with any sshpyk (no new minimum version); setup's
own launcher test (the slow start shows there first) benefits too.  The
cost is the same as sshpyk's kernel setting: processes the kernel starts
inherit `PYTHONHOME` (a different Python started from the kernel would
need it unset) -- hence off by default.

* **The value**: the interpreter's own `sys.base_prefix`, as its check
  reports it (`envcheck` already did; now recorded on the environment,
  `base_prefix`, a check field).  For conda environments that is the
  prefix; a venv's is the Python it was made from, and the venv still works
  with it (tested: its `sys.prefix` and packages stay).  For a conda
  environment checked before this, its prefix (`launcher.home_of`).
* **Per kernel** (kernels carry the launch settings, design 7c 4a):
  `pythonhome` in the kernel's record (default off).  `setup HOST
  --pythonhome` / `--no-pythonhome` sets it for the host's kernels (with
  `--kernel NAME`, that one); the launchers are rewritten and tested in the
  same run.  The line is `export PYTHONHOME=...` after activation and before
  the kernel's own variables (a `PYTHONHOME` among those still wins -- that
  was already a way to do this by hand).
* **Found, not guessed**: setup times its launcher test; slower than 15 s
  (`SLOW_START`) without the setting, it says so and names `--pythonhome`.
  `diagnose HOST` (section 6) now times the environment's python on the
  node twice, without and with `PYTHONHOME`, prints both, and says
  "slow without PYTHONHOME: python -m cubevis.setup HOST --pythonhome" when
  the first is over 5 s and the second under a third of it.  `status`
  shows "sets PYTHONHOME" on such kernels.
* Not used: sshpyk's own two settings (they would need sshpyk >= 1.25, and
  for setup's kernels say the same thing twice).  Stage 3's in-memory
  kernel spec can pass them through if a kernel ever runs from an
  environment other than its launcher's.
* Not covered: discovery's checks of the node's environments (each python
  started once by `setup HOST`) still pay the slow start; they run once
  per setup, not per kernel.  The setting could be applied there too
  (conda environments only) if zuul06's setup runs are too slow.
* **Also in this round** (the launcher): a gone environment's message
  (section 51) is now tested in `test_launcher.py` too.
* **Tests**: `test_launcher.py` -- the value is the base prefix, the venv
  still works with it, off by default, the kernel's own variable wins,
  unknown -> not set; `test_cli.py` -- `--pythonhome` on the rig (launcher
  line, kernel still starts, `status`, `diagnose`'s timings, then
  `--no-pythonhome`), and `--pythonhome` refused without a host.
* **Suite** (2026-11-02, sections 51 and 52): 257 passed, 1 skipped with
  OpenSSH 9.6 and with the 8.0p1 client (`-n 2 --dist loadfile`).

## 53. PYTHONHOME when the start is slow, and in setup's own Python runs (base `eaa9dd2`, 2026-11-02)

Darrell's `try-013A` (`setup zuul06` from a fresh state, then `diagnose
zuul06`): the launcher test took 52 s and setup named `--pythonhome`;
`diagnose` timed the environment's python at **49.3 s without
`PYTHONHOME`, 0.0 s with it**.  Two more pauses in that run had the same
cause: "Looking at zuul06 ..." (discovery starts each environment with
cubevis once: three there, ~50 s each) and `diagnose`'s section 6 (its own
check of the kernel's environment, then the timing).  Darrell: set it when
a slow start is detected.  Agreed: the one cost (processes the kernel
starts inherit `PYTHONHOME`) is not worth a minute per kernel start.

* **Kernels: tried when slow** (`write_and_test`): a launcher test slower
  than 15 s (`SLOW_START`) without `PYTHONHOME` is run again with it
  (setup says so); kept when it at least halves the time ("with PYTHONHOME
  it starts in 0.6 s: kernel K sets it (... --no-pythonhome takes it
  out)", recorded as a change), else taken out again and the launcher
  rewritten.  The kernel's record says who chose (`pythonhome_by`:
  `setup` or `you`); setup never overrides `--pythonhome` /
  `--no-pythonhome`.  Applies on every `setup HOST` run, so a kernel made
  before (Darrell's zuul06 one) gets it at the next run.
* **Discovery's checks** (`discover.check(..., home=)`): an environment
  that is a Python installation of its own -- its standard library there
  (`lib/python3*/os.py`, CPython's landmark) and no `pyvenv.cfg` -- is
  checked with `PYTHONHOME=prefix` (`discover.own_home`), which is exactly
  what Python would find by searching; if that gives no report, it is
  checked again without.  The report then carries `pythonhome`.  venvs and
  odd layouts are checked as before.  This removes the "Looking at ..."
  pause on autofs (and `diagnose`'s own check, which uses discovery).
* **Setup's own environments** (`envmake`'s worker): the pip install runs
  with `PYTHONHOME=PREFIX` (the same test: a new conda environment
  always passes it).  pip starts several interpreters; on cvpost106 the
  pip step took 32 of the 37-42 minutes (section 44's "slow filesystem"),
  probably this.  To be seen in the next environment setup makes there.
* **`diagnose`**: says, while it times ("timing Python's start there,
  without PYTHONHOME and with it (a slow start takes up to a minute)"),
  and which kernels set it and why ("set by setup, the start being slow" /
  "your choice").  The timing without `PYTHONHOME` still costs its minute:
  it is the measurement.
* Not changed: the node's own `python3` that runs discovery, envmake and
  the launcher-install program (zuul06: 3.8.17, presumably the system's,
  not on autofs).
* **Tests**: `test_discover.py` (`own_home`; a check with `PYTHONHOME`, and
  again without it when a wrong one stops Python); `test_hostsetup.py` (a
  slow start tried with `PYTHONHOME` and kept; dropped when it does not
  help; `--no-pythonhome` not overridden; a fast start left alone -- a
  fake clock).  envmake's use is not exercised (the rig's fake conda makes
  venvs).
* **Suite** (2026-11-02): 261 passed, 1 skipped with OpenSSH 9.6 and with
  the 8.0p1 client (`-n 2 --dist loadfile`).

## 54. `diagnose` does not time a slow start again (base `a12adbe`, 2026-11-02)

Darrell's `try-014A` (from an empty `~/.config/cubevis`: `setup zuul06`,
then visplot, then `diagnose zuul06`): discovery no longer paused; the
launcher test took 52 s, setup tried `PYTHONHOME`, 2.6 s, kept it ("start
52 s -> 2.6 s"); visplot started the kernel in 2.1 s and plotted.  The one
pause left was `diagnose`'s timing of the start without `PYTHONHOME`
(49.3 s), which tells nothing new once the kernels set it.

* `diagnose HOST` times Python's start only for environments with a
  kernel that does not set `PYTHONHOME`; otherwise it says "kernel K sets
  PYTHONHOME=... (K: set by setup, the start being slow, 52 s -> 2.6 s);
  Python's start not timed again (diagnose HOST --full does)".
  `diagnose HOST --full` times it as before.
* The times setup measured when it chose `PYTHONHOME` are kept on the
  kernel (`pythonhome_times`, a check field), dropped when the user decides
  with `--pythonhome` / `--no-pythonhome`.
* Seen in the same run, for round 2 (the ssh records): sshpyk warns
  `[SSHPYK144] ... batchmode: Recommended to be 'yes', not 'no'` for both
  hosts of setup's generated configuration -- it reads `ssh -G` of the
  aliases, and the generated blocks do not set `BatchMode` (setup's own
  calls pass `-o BatchMode=yes`; the wrapper passes `BatchMode=no` where it
  logs in with a stored password or lets the user log in).  `BatchMode yes`
  in the generated blocks would silence it; to be checked against the
  wrapper's and `--2fa`'s own logins first.
* **Tests**: `test_cli.py`'s `--pythonhome` rig test (not timed, then
  `--full`); `test_hostsetup.py` (the times kept).
* **Suite** (2026-11-02): 261 passed, 1 skipped with OpenSSH 9.6 and with
  the 8.0p1 client (`-n 2 --dist loadfile`).

## 55. Round 2, the ssh records: host keys, recorded options, hops (base `7d551ef`, 2026-11-02)

The three ssh decisions of section 51, and sshpyk's `batchmode` warning
(section 54).  No state schema change: the new fields are optional, and an
older cubevis sharing the directory ignores them (`via` is kept).

**Host keys** (the open point "first contact with a login host, straight
to a node", 2026-10-19; Darrell 2026-11-02: the best experience without a
security issue):

* Setup keeps its own known-hosts file, `~/.config/cubevis/ssh_known_hosts`
  (`sshgen.known_hosts_path`) -- in the configuration directory, so the
  computers sharing it share the keys: a key recorded from one is known to
  all, and a kernel started from a second laptop or a new account no
  longer fails with "Host key verification failed".
* Every generated block: `UserKnownHostsFile <setup's> <the user's>` (the
  user's from `ssh -G`, by default `~/.ssh/known_hosts
  ~/.ssh/known_hosts2`, so keys the user accepted before still count) and
  `StrictHostKeyChecking accept-new`.  New keys go to setup's file (ssh
  writes to the first).
* **accept-new rather than `yes`** (a change from what I proposed on
  2026-11-02, and why): a cluster's node is usable without running setup
  for it (design 7c), so a kernel may well be the first to reach
  cvpost107; with `yes` it would fail until setup visited it.
  `accept-new` is what OpenSSH added for unattended use: a host never seen
  is recorded on first contact, a *changed* key is refused -- the attack
  `StrictHostKeyChecking` exists for.  The first contact itself is
  trust-on-first-use either way (the user would type "yes" just as
  blindly).
* Shown when recorded: setup's login step notes the keys its file gained
  ("first contact with HOST from setup's configuration: its host key was
  recorded (ED25519 SHA256:...) in ~/.config/cubevis/ssh_known_hosts, which
  kernels check from now on ...") and records the change.
* A changed key: setup names the files ssh named (or setup's and
  `~/.ssh/known_hosts`), says to ask the host's administrators and how to
  remove the old key (`ssh-keygen -R HOST -f FILE`), and never overrides
  it; the wrapper says "the host key of ... is not the one recorded
  (known_hosts) ... `python -m cubevis.setup diagnose HOST` names the
  file" instead of "check the host name, the network".
* Tests: `CUBEVIS_SETUP_KNOWN_HOSTS` now *is* setup's file (the tests'
  own), and the user's files are added only when the user configured
  them -- the `Host *` block the tests appended is gone (a per-host
  `UserKnownHostsFile` would have overridden it anyway).

**`BatchMode yes`** in every generated block (sshpyk's
`[SSHPYK144] batchmode: Recommended to be 'yes'`): nothing in a kernel's ssh
can answer a question.  The wrapper's own logins (stored password, the
`--2fa` terminal login) already pass `-o BatchMode=no` ahead of everything
(ssh keeps the first value), and setup's checks pass `BatchMode=yes`.

**Recorded options** (Darrell: stored per host, a change "considered but
not taken for truth"):

* Per computer, in `local.json` (`ssh_options`, by setup's alias): what
  the user's `ssh -G` gave -- the login options (`sshopts.login_options`),
  the user's known-hosts files, the name asked about and when
  (`sshopts.snapshot`).  The generated file is written from that
  (`sshgen.options_for`; the block's comment says "recorded DATE").  A host
  without a record (all, after upgrading) is recorded at the next save as
  it is then (`sshgen.fill_missing`, from `Ctx.save`).
* Compared with `ssh -G` now (`sshgen.changes`, `sshopts.differ`), not with
  `~/.ssh/config`'s time stamp: that also sees `Include`d files,
  `/etc/ssh/ssh_config` and Match blocks.  A difference is logged at each
  `setup HOST` run, listed by `status` ("your ssh configuration changed
  since setup recorded it ... : node compute (ssh -G compute): now X; no
  longer Y"), and shown by `diagnose` (section 2: "setup recorded yours
  on DATE", "PROBLEM: changed since: ... -- python -m cubevis.setup HOST
  asks to take it over"); what nothing explains is now called a bug.
* Adopted only by `setup HOST` (that network's hops, login host and the
  node) or `network NAME` (the whole network), after showing it: "Your ssh
  configuration changed ... Use your ssh configuration as it is now for
  them? [Y/n]" (key `adopt_ssh`); the login tests that follow try it at
  once (`hostsetup.adopt_step`, `sshgen.adopt`).

**Hops** (Darrell: "each jump host has its own entry and each includes the
jumps to get there ... about the correct record keeping"):

* The login host's earlier hops are recorded in the shared state
  (`login.hops`: `ssh_name`, `ssh_port` as the user wrote them; real
  `host`, `user`, `port`), from `via` or the user's own ProxyJump for the
  login host (`sshgen.live_hops`, `sshopts.parse_hops`).  A route change
  there is reported and adopted like the options.
* Each hop is a block of its own, `<login alias>-hop1`, `-hop2`, ... with
  its own recorded options, known hosts, BatchMode and shared connection,
  each `ProxyJump` the one before; the login host jumps through the last
  (`sshgen.entries`, which `config_text`, `fill_missing`, `changes` and
  `adopt` all walk).  Before, the hops were a resolved `ProxyJump`
  string on the login host, read from the user's ssh at every write.
* Still key/Kerberos only for the earlier hops (no password rules for
  them): NRAO's routes have one hop.

* **Tests**: `test_sshopts.py` -- several hops as entries (real name, user,
  port; the chain), recorded at the first save and then not following the
  user's file, a route change reported and adopted; options recorded,
  a change reported not followed, adopted; known hosts and BatchMode in
  every block (setup's file first, the user's after; the tests' file
  alone).  `test_cli.py` (rig) -- the route test: both blocks carry the
  known hosts, accept-new and BatchMode; the first contact's keys shown;
  then the user's file changed: `status` lists it, `diagnose` shows it,
  the generated file keeps the recorded value, `setup HOST` asks and
  adopts; a changed host key refused, with setup's and the wrapper's
  messages.
* `test_rig_diagnose` follows: "setup recorded yours on", and a later
  change shown as "changed since ... asks to take it over" (not "lacks").
* **Suite** (2026-11-02): 264 passed, 1 skipped with OpenSSH 9.6 and with
  the 8.0p1 client (`-n 2 --dist loadfile`).

## 56. Round 3: a dead worker or kernel is reported, not waited for (base `45f403c`, 2026-11-02)

Darrell's `try-015A` (a new macOS account, `setup cvpost106` from
nothing: login host by password into the Keychain, the same password for
the node, the cluster, `cubevis-1.0.102` by fingerprint, sshpyk installed,
visplot plotted; the kernel started in 2.5 s): section 55 as intended --
cvpost106's host key recorded and shown, ssh.cv.nrao.edu's found in the
account's own `known_hosts`; no SSHPYK144.  Two small things from it are
below (the note's place, SSHPYK640).

**The problem** (section 36, found 2026-10-26): a worker killed by the
memory limit during a big scatter left the plotter waiting.  The chain:
the plotter's call (`RemoteReductionContext._call`, data budget 600 s) ->
`dispatch_fast` to the kernel -> the supervisor's
`_handle_dispatch_fast` -> `request()` to the worker, whose reply never
comes; the supervisor never looked at the worker process, so it never
answered, and the plotter waited out its 600 s for a bare timeout.
Reproduced first (`test_worker_death.py`: SIGKILL mid-call -> the wait ran
to the test's own 120 s timeout).

**Kernel side** (`cubevis/remote`):

* `WorkerProcessTransport` notes when the worker's stdout closes
  (`wait_exited`) and can say why (`exit_reason`): pid and host, the exit
  status or the signal -- SIGKILL "most often the system's memory limit
  (the out-of-memory killer); a narrower selection (one field, fewer
  SPWs) needs less" -- and its last stderr lines ("Its last words: ...").
  An unexpected exit is also logged in the kernel.
* `_supervisor.relay` races each relayed request against the worker's
  exit; the worker gone (before or during the request) raises
  `WorkerDiedError`, which `CommMgr` answers as an `{'error', 'traceback'}`
  reply -- at once.  A reply that arrived before the exit still counts.
  `dispatch_fast` and create_context's opening `configure` use it (a
  worker that dies on start-up -- a broken import, say -- is now reported
  with its last words instead of after the 90 s configure budget).

**Plotter side** (`RemoteReductionContext`):

* A `WorkerDiedError` reply becomes `RemoteWorkerDiedError`: the reason,
  then "the data's worker on kernel K is gone; the next Plot starts a new
  one (state held only by the old one, such as flags not yet saved, is
  lost)".  The next call does that (`_arestart_worker`: a new execution
  context with the same configuration, the backend object opened again
  on the same path and kind; the cached metadata stays), so after an
  out-of-memory kill the user narrows the selection and presses Plot.
  The Plot handler's existing error path shows the message (notify +
  status, "⚠ Panel N ... error: ...") and logs the traceback.
* Every call in flight is watched (`_watched`): every 5 s
  (`LIVENESS_INTERVAL`, `CUBEVIS_REMOTE_LIVENESS`) the kernel manager's
  `is_alive()` (sshpyk: the remote kernel process, over ssh; it reopens
  the tunnels too) and the kernel's heartbeat (the transport's client's
  `hb_channel`, answered by ipykernel's own heartbeat thread, so a busy
  kernel still beats).  A kernel that stopped: `RemoteKernelLostError`
  "the kernel K has stopped (its process is gone: killed, or its computer
  restarted). Close this visplot and start it again."; a heartbeat silent
  for 30 s (`HEARTBEAT_GRACE`): "no answer from the kernel K for 30 s:
  the connection to it was lost (the network or VPN, or this computer
  slept) ...".  sshpyk's poll says "alive" when it cannot reach the host
  (it assumes the best), hence the heartbeat for a lost connection.  Not
  restarted automatically: a new kernel means a new link (stage 3's
  Session can, with detach/reattach in stage 5).
* Not covered: `dispatch_async` / `job_status` (unused by visplot; its
  `JobRegistry` already reports a dead worker as `died`).

**From try-015A**:

* The host-key note now comes when the login step succeeds, beside its
  "ok" line: setup's first, silent try of a step was recording the key,
  and the note came before the step's heading ("4. Log in to the node
  ...") -- it looked as if it belonged to the step before.
* sshpyk's `[SSHPYK640] ... identityfile: Likely missing in your ssh
  config. Multiple values: [...]` is dropped for kernels whose ssh
  configuration is setup's (`_quiet_setup_kernel_hints`, a logging filter
  on the kernel manager's logger, which sshpyk's provisioner logs
  through): setup leaves ssh's default identity files on purpose (one
  `IdentityFile` would stop ssh trying the others; `none` is an error to
  sshpyk), and for a password host the hint is noise.  Other kernels keep
  it.  (Alternatively sshpyk could log it at debug level when only ssh's
  defaults are in effect; Darrell's call -- the filter can then go.)

* **Tests**: `tests/manual/remote/test_worker_death.py` (local `python3`
  kernel): a worker SIGKILLed mid-call is reported within seconds, with
  SIGKILL, "memory" and its pid, and later calls too; a worker that exits
  says its status and its last stderr line; the kernel SIGKILLed mid-call
  ends the plotter's wait (`RemoteKernelLostError`); a silent heartbeat
  counts as a lost connection, a beating one does not; a dead worker's
  `RemoteWorkerDiedError` and the new worker on the next call (fakes);
  the SSHPYK640 filter only for setup's kernels.  The remote suite: 46
  passed (40 before) without the two files that need Darrell's kernel.
  Not tried with an MS here (none in the sandbox): on cvpost106,
  `pkill -9 -f cubevis.remote.worker_main` while a Plot runs should show
  the message at once, and the next Plot should work.
* **Suite** (2026-11-02): setup 264 passed, 1 skipped with OpenSSH 9.6 and
  with the 8.0p1 client; remote 46 passed; visplot's remote tests 35
  passed, 13 skipped (no MS).

## 57. `--redo` and a kernel whose code differs (base `ec6dd8b`, 2026-11-03)

Darrell, after section 56 (its kernel-side change moved the fingerprint
from `k2-67f9938a2b6ba9ce4a63` to `k2-d19f4c5e00b7234d880a`):
`setup zuul06` found the kernel's environment (`cubevis-1.0.102`) with
other kernel-side code and suggested `--redo`; `setup --redo zuul06` then
failed at the logins: "Your own ssh logs in to ssh.cv.nrao.edu without a
password, but setup's configuration does not (it refused the login)",
`FAIL ... -- with an ssh key` -- a working login reported refused.

* **Why**: `--redo` skips each step's check and runs its `do()`; a login
  step's `do()` repairs a login that failed, reading the last ssh output
  -- here there was no failure (and stale output), so for a key login it
  took the "your ssh works, setup's does not" branch and returned False;
  `run_step` then reported FAIL even though the check that followed
  passed (the message after FAIL was the check's own "with an ssh key").
* **Fixes**: the login steps are `look_only` (never redone: a working
  login has nothing to ask; a changed stored password shows as a failing
  login, whose repair runs as before); and `run_step` reports a step whose
  `do()` declined but whose check passes as done, not FAIL.  `--redo` still
  asks the wrapper, the inside ranges and the kernels' settings again.
* **The real need behind it**: moving an existing kernel to an
  environment with this computer's kernel-side code.  `setup HOST` now
  offers, for a kernel whose code differs, the same choice as for one
  whose environment is gone (section 51): "[r]eplace its environment
  (choose another; the kernel's name, directory and variables stay),
  [f]orget the kernel, or [k]eep it" (key `env_differs`; default replace),
  after saying that the list shows each environment's code -- one the user
  installed this cubevis in, or setup's own for a release.  The old
  environment's record stays (it still exists).
* For Darrell's test of section 56 on zuul06: install the checkout at
  that commit in an environment there (`pip install -e`), run `setup
  zuul06`, answer `r` and choose it.
* **Tests**: `test_hostsetup.py` -- a working key login under `--redo`
  passes without the repair (failed before the fix); a step whose `do()`
  declines but whose check passes is done.  `test_cli.py` (rig) -- `--redo`
  on a set-up host asks the kernel's settings and no login; the mismatch
  test goes on to `setup HOST`, which replaces the kernel's environment
  (alt -> hrs) and keeps alt's record.
* **Suite** (2026-11-03): 267 passed, 1 skipped with OpenSSH 9.6 and with
  the 8.0p1 client (`-n 2 --dist loadfile`).

## 58. A worker gone between calls: the call goes to a new one; quieter reasons (base `ec6dd8b`, after section 57; 2026-11-03)

Darrell's test of section 56 on zuul06 (`cubevis-1.0.109`, fingerprint
`k2-d19f4c5e00b7234d880a`): the worker SIGKILLed while idle; the next
call (`axis_info`) was answered at once with the reason, the plot went on
with "unresolved labels" (that call failed), and the call after started a
new worker; plotting continued.  Two refinements, agreed:

* **A call that never reached the worker is sent to a new one.** The
  supervisor tells the two cases apart: a worker already gone when the
  request comes raises `WorkerGoneError` (a subclass of
  `WorkerDiedError`); one that dies while handling it, `WorkerDiedError`.
  For `WorkerGoneError` the plotter (`RemoteReductionContext._acall`)
  starts a new worker and sends the same call to it -- once -- logging
  "visplot: <reason>; starting a new worker on kernel K and sending
  <method> to it"; the plot comes out whole.  A death during a call is
  still reported (`RemoteWorkerDiedError`, shown by Plot): that is most
  often the call itself running out of memory, and sending it again
  would do the same.  A new worker that is gone again at once is
  reported, not retried.
* **The reason without noise.** Darrell's "last words" were xarray-ms's
  warnings from opening the MS (`IrregularSubtableGridWarning`,
  `FrameConversionWarning`, the `warnings.warn(` lines).  A process killed
  by a signal said nothing about it, so for a signal none is shown; for
  a worker that exited by itself, its last stderr lines other than
  Python's warnings (`_worker_transport._last_words`).
* The kernel-side code changed again (`_supervisor.py`,
  `_worker_transport.py`): fingerprint `k2-8c32a07ef87455eb3cd6`.
* **Tests** (`tests/manual/remote/test_worker_death.py`): a worker killed
  during a call is reported without last words and not as gone; later
  requests are marked `WorkerGoneError`; a worker that exits gives its
  last line but not a warning written after it; the plotter sends a call
  that met a gone worker to a new one, and reports a second death at
  once; `_last_words` leaves out warnings.  Remote suite 48 passed;
  visplot's remote tests 35 passed, 13 skipped; setup (no rig) 223 passed.

## 59. Parking remote sessions: the Scenario A experiment, session records, the quick tests (base `3a5d69b`, 2026-11-04)

**Scenario A tried** (Darrell's `try-016A`, zuul06, cubevis 1.0.111,
`k2-8c32a07ef87455eb3cd6`).  A laptop asleep overnight with a remote
session open carried on when opened again (same network).  Started on the
VPN (setup's route: direct, "inside cv"), plotting, then the VPN
disconnected: the next GUI update hung with no message, the spinner on,
until after 600 s "the kernel 'cubevis-zuul06' did not answer query_raster
within 600 s"; every later request the same; the Plot button stayed orange
after the failure.  (The two SIGKILLs at the start of that session were
Darrell's own `pkill`.)

Why no "connection lost" (section 56's watch) came: `_watched` asks
`km.is_alive()` every 5 s, and sshpyk's `_poll` first re-opens the
kernel's tunnels; with the control socket gone it calls
`check_control_sockets`, which runs **`verify_ssh_connection` -- a
blocking `subprocess.run`** -- inside the event loop, with
`timeout=launch_timeout` (setup writes 120 s), up to 3 tries.  That loop
is the one every remote call and the watch itself run on, so all of it
stops; `asyncio.wait_for(..., 30)` cannot fire either.  Only the 600 s
limit, measured from the calling thread (`SyncBridge.run`), ends the
wait.  (sshpyk also answers "alive" whenever its `ps` over ssh fails:
`_poll` returns None on `not success`.)  The heartbeat itself is fine:
with a local kernel stopped by SIGSTOP, `hb_channel.is_beating()` went
false within 2 s and true again after SIGCONT (jupyter_client 8.10).
Unknown still: whether ssh reconnected through the outside route at all,
and whether the kernel survived -- Darrell's three checks for the next
try are in the session-management handoff
(`hrs_handoff_2026-11-04_sessions.md`).

**Session records** (`cubevis/setup/sessions.py`, new): the `sessions`
section of `hosts/<id>/local.json`, reserved since stage 2a, is now
written.  `RemoteReductionContext` records a session after it connects to
a kernel that sshpyk started (`kernel_facts`: the provisioner's
`ssh_host_alias`, `persistent_file`, `rem_pid_k`; local kernels are not
recorded) and forgets it in `close()`:
`{app, kernel, jupyter_kernel, node, local_pid, started, persistent_file,
remote_pid, data, context_id, handle, cubevis}` (the last two updated when a new worker replaces a dead one).  The kernel's ports and signing key are not
copied: sshpyk keeps them in its `persistent_file` (Jupyter's runtime
directory, mode 600), which is what its reattach (`existing=`) reads; it
deletes it on a clean shutdown, keeps it when the Python dies.  `status`
lists the records, each "running here" or "left over" (its Python gone;
the kernel may still run on the node).  Recording never fails a session
(errors logged at debug).  Decided with this: no `persistent` flag in the
Jupyter kernel definitions (decided per session at run time, when a later
stage needs it); `KernelPersistenceManifest` (`cubevis/remote/_persistence.py`,
one unlocked file in the runtime directory) is superseded by these records
but left in place; the user-facing call `visplot(..., backend="remote",
kernel_name=...)` is the contract later stages keep.  Kernel-side code
unchanged (`k2-8c32a07ef87455eb3cd6`).

**The quick tests** (`tests/ci/`, `.github/workflows/quick-tests.yml`):
`run.py` runs the entries of `suites.toml` one by one (each its own
pytest, with its directory's `pytest.ini`), skips a suite whose modules
are missing (fails it with `--strict`, as in Actions), and summarises;
adding tests is adding a `[[suite]]`.  Today: `setup` (no rig), `remote`
(local kernel, `test_query_raster*` left out: they need zuul06),
`tiny-ms` + `visplot-remote` (`make_tiny_ms.py`: xradio's generator, then
real integrations, UVWs, data, flags and channel frequencies -- the
generator's own data are mostly zero and all its channels at 10 Hz, which
drew empty panels; `remote_visplot_check.py` on it: 22 steps, local
against `backend="remote"`), and a `[[lint]]` hook (`compileall` only;
a stricter checker is one more entry).  A fresh Python 3.12 venv with `pip
install -e ".[visplot]" -r tests/ci/requirements.txt`: all five passed in
about 4 minutes.  `.gitignore` gains `cubevis/__version__.py` (an editable
install writes it).

**Tests**: `tests/manual/setup/test_sessions.py` (record, list, forget,
left over, `status`, `kernel_facts`, recording never fails,
`RemoteReductionContext` records only sshpyk kernels).  Setup (no rig)
228 passed, 45 skipped; remote 48 passed; the quick tests 5 of 5.

**Parked**: what is left is in two handoffs, both 2026-11-04 --
`hrs_handoff_2026-11-04_sessions.md` (remote session management:
Scenario A first) and `hrs_handoff_2026-11-04_iclean_remote.md` (iclean
with `gclean` and the image backend on the remote).

## 60. iclean on remote kernels, step 1: the imaging layer, local (base `ff0d810`, 2026-11-04)

Design: `devel/notes/drs/iclean/iclean_imaging_layer_design.md` (step 1 of
its §4).  Interactive clean's GUI now reaches the imager and its images
only through an `ImagingSession`; nothing remote yet, and no change in
behaviour.

* **New package `cubevis.imaging`** (imports no CASA; casatools when a
  store first opens an image, casatasks when a session is made from
  iclean's arguments):
  * `_protocols.py`: `ImagingSession`, `ImageCube` (exported as
    `ImageCubeProtocol`), `ImageStore`, `Deconvolver` (runtime-checkable
    `Protocol`s); `CycleResult` (a `NamedTuple`, so the UI's six-value
    unpacking of gclean's tuple is unchanged).
  * `CasatoolsImageStore`: every casatools call `ImagePipe` made, moved
    unchanged (getchunk/putchunk, `pixelvalue`, mask0 by `getregion`, the
    one-use image tool for statistics -- CAS-13625 -- and for `coordsys`).
    Opens the image on first use: iclean makes its cubes before gclean has
    written the residual.  Reopens itself (mask included) after `close()`.
  * `ImageCube`: `ImagePipe`'s numpy logic, moved unchanged (plane cache,
    quantization to uint8, histogram, spectrum, statistics and their
    sorting, mask planes, mask0 cache), plus `channel_reply` (plane, mask,
    mask0, histogram and statistics in one call: one round trip per
    channel step when remote).  Colormap bounds/transfer are passed in
    (`quant`), not kept.  Freshness: `track="generation"` for cubes a
    session serves (the session increments `generation` after `start`
    and each cycle -- no ctime polling), `track="ctime"` for cubes opened
    from a path (createmask, createregion: as before).
  * `LocalImagingSession`: a gclean (`resolve_deconvolver(iclean_backend)`
    -- "PROD" or `_gclean_NAME`, the logic that was in both iclean entry
    points) and a cube per field; `start`, `update`, `run_cycle`,
    `arun_cycle` (gclean's own `__anext__`, its thread), `has_next`,
    `log`, `restore`, `stop_descriptions` (the deconvolver's own if it has
    one, else casatasks' `ImagingDict`), `fields`, `cube`, `close`.
    `as_imaging_session` wraps a bare gclean; `open_image_cube(path, mask)`.
* **`ImagePipe`** keeps the Bokeh model, the browser's messages and the
  colormap state; takes `cube=` (or a path, as before, for which it makes
  one).  Its public methods are unchanged and delegate.
* **`CubeMask`** takes a path (as before) or an `ImageCube`; "does the mask
  exist" asks the pipe (`have_mask`) instead of `os.path.isdir` on the
  path; `path()` fixed (it returned `self._image_papth`: `AttributeError`).
* **`InteractiveCleanUI(session, user_args)`** (a gclean is still accepted,
  wrapped): fields from `session.fields()`, each `CubeMask` built on
  `session.cube(field)`, `session.start/update/arun_cycle/log/restore`,
  stop descriptions from the session -- the UI no longer imports casatasks.
  Removed, all dead: the `msg['value']['mask']` branch of `clean_handler`
  (the browser sends `masks`; the branch used `self._imagename` and
  `self._mask_history`, never set, and its `rmtree` ran on the laptop),
  `_gen_port_fwd_cmd` and `_is_remote` (the pre-multiplexing port
  forwarding, design §3 of the remote design), `_residual_path` /
  `_mask_path` (never called; would have failed on the dict they scan).
  `masks()` returns `[]` instead of raising `AttributeError`.
* **Both entry points** (`_interactiveclean.py`,
  `_interactivecleannotebook.py`) make a
  `LocalImagingSession.from_args(args, iclean_backend)`; `_gclean` and
  `_gclean_paths` are kept (from the session).

Kernel-side code unchanged (`k2-8c32a07ef87455eb3cd6`): nothing in
`_fingerprint.ROOTS` imports `cubevis.imaging` yet (step 5 adds the
registration module).

**Tests**:
* `tests/manual/iclean-imaging/test_imaging_layer.py` (13, no CASA): the
  import pulls in no CASA; the protocols; quantization (linear, log with
  bounds) against `quantize`; generation vs ctime rereads; mask planes
  (bool -> uint8, display transpose, `mask_value` -1 on error); spectra
  (clipping, one channel); statistics (mask only with a mask path, blc/trc
  last); `channel_reply`; the session over a scripted gclean (fields,
  cubes made once, `CycleResult` unpacking, a cycle rereads the residual,
  log, restore, stop descriptions, close); `as_imaging_session`;
  `resolve_deconvolver`/`from_args` with a `_gclean_NAME` module.  A
  quick-test suite, `imaging` (`tests/ci/suites.toml`).
* **Before/after, with CASA** (`record_iclean_handlers.py`,
  `compare_records.py`, `make_sim_ms.py` in the same directory): iclean
  driven headlessly through the browser's handlers -- 5 channel replies,
  spectrum, colormap log/sqrt/reset, pixel value, statistics, histogram,
  9 mask edits (chan/cube add and subtract by polygon and by source
  channel, invert twice, delete) with the whole mask cube saved after
  each, masked statistics, the spectrum fetch, two clean cycles (46 and 34
  iterations) with the plane and statistics after each, status, the
  convergence pipe -- 56 records.  Base `ff0d810` against this zip:
  identical (floats to 1e-6; two runs of the base were identical apart
  from FITS DATE/MEMAVAIL/IMAGENME/HISTORY cards, which are ignored).
  Also: `CubeMask` from paths, with and without a mask (createmask and
  createregion's use); the notebook entry point to a built GUI.
* The data: casa.nrao.edu is not reachable from where this was done, so
  `make_sim_ms.py` simulates an MS like `refim_point_withline.ms`
  (VLA D, five channels from 1 GHz by 0.2 GHz, two sources).  Worth
  repeating the before/after with `refim_point_withline.ms` itself.
  (Environment note: the casatools 6.7.6 wheel's bundled libssl and
  libldap need RHEL's OpenSSL; on Ubuntu 24.04 they were replaced by the
  system's for this test.)

**Next** (design §4): 1b, the coordinate description; 2, `edit_mask`
operations in place of `CubeMask`'s per-channel `mask`/`put_mask` loops.

## 61. iclean step 1, follow-up: templates, the notebook bridge, organization (base `ff0d810` + §60, 2026-11-05)

* **Templates.**  `_interactive_clean_ui.py`, `_interactiveclean.py` and
  `_interactivecleannotebook.py` are generated (`scripts/expand_templates`)
  from `.mustache` files; §60 edited only the `.py` files, so the next
  expansion would have undone step 1.  The three templates now carry the
  same changes.  Checked: `expand_templates` on a checkout of `ff0d810`
  reproduces the committed `.py` files byte for byte, and on this tree
  reproduces the §60 `.py` files byte for byte.  (The UI's template has no
  tags at all: design §6 proposes dropping it.)
* **JupyterLab "display_bridge() must be called before connect()".**  Not
  step 1: reproduced on `ff0d810` without it, in a kernel with no
  `anywidget`.  The bridge is displayed by a preflight callable at the end
  of the cell that constructs the application; `run_preflight_callables`
  catches its exception and logs only a WARNING there, then `show()` fails
  in the next cell with the message above.  `anywidget` is the optional
  `cubevis[notebook]` extra.  With `anywidget` installed, both the base and
  step 1 run the notebook's whole sequence (construct, show, get_result,
  construct, show) to the bridge display and the connect.
  `tests/manual/iclean-imaging/notebook_check.py` runs those cells in a
  real kernel, no browser.
* **Better message (separate patch, `cubevis-ff0d8108-bridge-error.zip`).**
  `CommsTransport.display_bridge` records why it failed; `connect()`
  raises "the notebook bridge could not be displayed: The 'anywidget'
  package is required ... pip install 'cubevis[notebook]'" from that
  cause.  `_low_level_transport.py` is in the kernel-side closure, so this
  changes the fingerprint (`k2-8c32a07ef87455eb3cd6` ->
  `k2-97336d2e11f1cef86a2d`): kept out of the main zip so that it can go
  with the next release.
* **Organization**: design §6 (proposal, not yet done): `cubevis/toolbox/
  iclean/` like `toolbox/visplot/` (one app template with the notebook class
  as a subclass, the UI as plain source, the widgets); `cubevis/imaging`
  stays top level with a no-Bokeh/no-CASA-at-import rule; old pieces
  removed.  Step 1c, before 1b.

## 62. iclean step 1c: package organization (base `ff0d810` + §60, §61, 2026-11-05)

Design `devel/notes/drs/iclean/iclean_imaging_layer_design.md` §6 (as done:
§6.5).  Interactive clean now has visplot's shape:

* **`cubevis/toolbox/iclean/`** (new; lazy `__init__` like `cubevis.toolbox`):
  * `_app.mustache` -> `_app.py` (generated): ONE template for
    `InteractiveClean` and `InteractiveCleanNotebook(InteractiveClean)`,
    which adds only its display (`DisplayContext`, `get_future`, its
    `__call__`).  Was `private/apps/_interactiveclean{,notebook}.mustache`.
    The notebook class now sets `_future = None` (`get_future()` before
    launch raised `AttributeError`, not its `RuntimeError`); its unused
    module-level imports (`ImagerParameters` from casatasks,
    `output_notebook`, `pprint`, `find_pkg`/`load_pkg`) are gone.
  * `_ui.py`: `InteractiveCleanUI`, plain source (was
    `toolbox/_interactive_clean_ui.py`; its tag-free `.mustache` deleted).
  * `_widgets.py`: `SharedWidgets` (was `toolbox/_interactiveclean_wrappers.py`).
* **Pointers**: `cubevis.toolbox` (`InteractiveCleanUI`) and
  `cubevis.private.apps` (`InteractiveClean`, `InteractiveCleanNotebook`)
  lazy tables; the three task-wrapper templates import from
  `cubevis.toolbox.iclean` (regenerated: one line each changed).
  `pyproject.toml` excludes: `cubevis/apps/*.mustache` (a directory that
  no longer exists, so the app and UI templates shipped in the wheel) ->
  `cubevis/toolbox/iclean/*.mustache`.
* **Removed**: `private/apps/_interactiveclean{,notebook}.{mustache,py}`,
  `toolbox/_interactive_clean_ui.mustache`, `private/_gclean.py` (unused,
  pre-`image_products` copy).  `tests/manual/iclean-remote/` stays until
  remote iclean works (design §6.2).
* **`cubevis.imaging` dependency rule**: it imports no other `cubevis`
  module at import (own `partition`; `cubevis.utils._casa` when a store
  first opens an image), so not Bokeh, astropy, regions or CASA.
* **Tests**: `test_imaging_layer.py` gains the dependency rule and the
  laziness of `cubevis.toolbox.iclean` (14).  `test_lazy_packages.py` and
  `test_fingerprint.py` name `cubevis.toolbox.iclean._ui` as front end the
  kernel side must not load.  `record_iclean_handlers.py` imports the
  public `cubevis.private.apps.InteractiveClean`.
* **Checks**: `expand_templates` regenerates `_app.py` and the three
  wrappers (and nothing else changes); before/after handler records,
  base `ff0d810` against this tree: identical, 56 of 56;
  `notebook_check.py`: both rounds reach the bridge and connect;
  public names (`cubevis.iclean`, `.notebook`, `cubevis.toolbox.InteractiveCleanUI`,
  `cubevis.private.apps.*`, the casashell layer with casashell installed)
  resolve, `InteractiveCleanNotebook` a subclass.  Quick tests: remote,
  imaging, syntax pass; setup 180 pass, 11 fail only for want of an
  `ssh` binary where this was run (`test_wrapper_reachable`, the CLI's
  tools check); tiny-ms skipped (no xradio/casacore here).
* **Fingerprint** `k2-8c32a07ef87455eb3cd6` -> `k2-e38a583701fa4183c928` (this slice:
  `cubevis/toolbox/__init__.py` is in the closure; plus §61's bridge-error
  patch, now included).  Needs a release before remote sessions match.

## 63. iclean step 1b: the coordinate description (base `99116cd`, 2026-11-06)

Design `devel/notes/drs/iclean/iclean_imaging_layer_design.md` §2.5, as done
§2.5.1.  World coordinates come from a coordinate description (plain,
JSON-safe, independent of the image format) instead of the FITS header (in
the browser) and a casatools `coordsys` (in Python).  No change in what the
GUI shows.

* **`cubevis/imaging/_coordinates.py`** (new): `from_casa_coordsys`,
  `astropy_wcs`, `beam_from_casa`, frame table; no CASA, astropy only
  inside `astropy_wcs`.  `CasatoolsImageStore.coordinates()`,
  `ImageCube.coordinates()` (once), in the `ImageStore`/`ImageCube`
  protocols; `cubevis.imaging` exports `astropy_wcs`, `from_casa_coordsys`.
  The record's `pc` is the transpose of FITS `PCi_j`; the description
  follows FITS.
* **Browser**: `ImagePipe.coords_json` (Python and TS) replaces
  `fits_header_json`; new `cubevisjs/src/bokeh/util/coordinate_description.ts`
  (`description_keywords`: the keywords coordtxl reads, `CD` matrix for a
  rotated image); `image_pipe.ts` builds the `WCSTransform` from it and
  adds `coordinates()`.  coordtxl unchanged.  `ImagePipe.fits_header()`
  (Python) is made on first use.
* **cubevisjs builds** (`cubevis/__js__/bokeh-*/cubevisjs.min.js`):
  first checked that this environment reproduces the committed files byte
  for byte (bokeh 3.9.2 -> the 3.6-3.9 file, bokeh 3.10.0 -> the 3.10
  file; `bokeh build` in `cubevisjs/`, `npm ci`), then rebuilt both.
  (`cubevisjs/package.json` has no `scripts`, so the README's
  `npm run build` is `bokeh build` + `node scripts/copy-versioned.js`.)
* **Python**: `convert_masks(coord='world')`'s WCS from the description
  (`ImageCube.coorddesc()` adds `coordinates`; a `coordsys`, its record or
  a description in `csys` also work) -- the old helper put CASA's 0-based
  reference pixel in FITS' 1-based `CRPIX` (one pixel off, 0.0044 deg on
  the test image) and always used SIN; createregion's `RegionList` gets
  `astropy_wcs(cube.coordinates())` (vertices as from the FITS header
  string to 1e-8"); `bokeh/utils/_axes_labels.py` (never imported) deleted.
  `CubeMask.coordinates()`.
* **Checks**:
  * `coordinates_parity.py` (new; casatools, node, tsc, the coordtxl npm
    package): seven images made from the test residual -- its own frame,
    ICRS, FK4/B1950, galactic, rotated 30°, TAN, 200×90 with an off-centre
    reference pixel.  coordtxl from the old header keywords against coordtxl
    from `description_keywords` (the compiled `.ts`), the GUI's strings,
    `astropy_wcs` and the header's astropy WCS against casatools:

    case                    coordtxl desc vs header  strings  astropy desc vs casatools  header WCS vs casatools
    own_frame                                    0"    36/36                         0"                1.21e-08"
    icrs                                         0"    36/36                         0"                1.21e-08"
    fk4_b1950                                    0"    36/36                         0"                1.21e-08"
    galactic                                     0"    36/36                         0"                1.21e-08"
    rotated_30                                 540"     0/36                         0"                1.21e-08"
    tan                                          0"    36/36                         0"                1.21e-08"
    nonsquare_offcentre                          0"    36/36                         0"                1.21e-08"
    
    coordtxl vs casatools (1-based)   header, 0.1.4  desc, 0.1.4  header, fixed  desc, fixed
    own_frame                                  190"         190"      1.66e-10"    1.66e-10"
    icrs                                       190"         190"      1.66e-10"    1.66e-10"
    fk4_b1950                                  190"         190"      2.97e+05"    2.97e+05"
    galactic                                   190"         190"      1.66e-10"    1.66e-10"
    rotated_30                                 683"         256"           571"    2.82e-10"
    tan                                        190"         190"      2.86e-10"    2.86e-10"
    nonsquare_offcentre                        546"         546"      2.94e-10"    2.94e-10"
    

    The rotated image differs by design: the header path never gave coordtxl
    the rotation (it ignores `PC`).  The second table is coordtxl against
    casatools; "fixed" is a copy of coordtxl with its projection parsing
    corrected (below).
  * `browser_check.py` (new; playwright with a Chrome): real iclean pages,
    base `99116cd` and this tree, in headless Chrome: no console errors,
    `ImagePipe.wcs()` world coordinates and strings identical, screenshots
    identical (0 pixels differ), the page has the spectral values.
  * Handler records against `ff0d810`: 54 identical, `fits_header_json`
    -> `coords_json` the only difference; `notebook_check.py`: both rounds.
  * `test_coordinates.py` (new, no CASA, 16) in the `imaging` suite:
    record -> description (frames, pc orientation, spectral, beams,
    JSON-safe), `astropy_wcs` against a FITS-built WCS (rotated too),
    `ImageCube.coordinates`/`coorddesc`, `convert_masks`' helper with each
    kind of `csys`, astropy not imported until used.  Single and per-plane
    beams also checked on CASA images.
* **coordtxl 0.1.4** (pre-existing, for the planned coordtxl change; design
  §2.5.1): (1) `WCSTransform` parses the projection with
  `wcstemp.substring(3, 7)` (`"--SI"`), so every image is linear: RA
  offsets lack 1/cos(dec) (190" at the test image's corners);
  `substring(4, 8)` fixes it.  (2) Then FK4 is ~82° off (its FK4
  conversion).  (3) coordtxl pixels are 1-based; the GUI passes 0-based
  indices.  (4) `NAXIS1` was 4 (the axis count) from the header path.
* **Fingerprint**: `cubevis.utils` (in the kernel closure) now refers to
  `cubevis.imaging._coordinates`, which brings `cubevis.imaging` into the
  closure: `k2-19e170a674fe895b4d7d`.  Delete `cubevis/bokeh/utils/_axes_labels.py`.

## 64. iclean step 1d: coordinate defects (base `9ba6093`, 2026-11-06)

Fixes two of the four coordtxl findings of §63: the projection parsing (in
coordtxl, released as 0.1.6) and the GUI's pixel convention.  FK4 waits for
the coordtxl feature work.  Design §2.5.2.

* **coordtxl** (casangi/coordtxl, separate change, for 0.1.6):
  `WCSTransform.java` parsed the projection with `wcstemp.substring(3, 7)`
  (`"--SI"`), so every image was treated as linear.  Now it skips the run
  of dashes after the axis name and takes the next three letters (`-SIN`,
  `-TAN` from `RA---TAN-SIP`, `GLON-SIN`).  New `ProjectionTypeTest`
  (seven CTYPEs, a SIN corner against astropy); the expected label in
  `CasaImageLabelTest` and `tstest.ts` becomes the SIN value
  `20:03:56.406, +39:52:17.78 J2000` (astropy; was the linear
  `20:02:54.100, +39:52:37.50`).  `package.json` 0.1.6.  The Java compiles
  and gives the same results as the JS patch and astropy; Gradle/JSweet
  could not run here (no Maven Central), so it is not built.  Two stray
  tracked files to delete there: `src/main/java/coordtxl/fits/.#KeywordProvider.java`
  (an Emacs lock file, which javac trips over) and
  `src/test/java/coordtxl/CasaImageLabelTest.java~`.
* **casalib**: with `node_modules` at the versions in `yarn.lock` (yarn 1
  resolves newer d3/hotkeys-js; corepack could not fetch Berry), the build
  reproduces the committed `casalib.min.js` byte for byte apart from the
  version string.  Two pre-existing bugs in `scripts/build-browser.js`:
  `__VERSION__` (read by `src/browser.ts`) was never defined, so a fresh
  bundle throws a ReferenceError on load; and the copy step looked for
  `dist/esbuild/casalib.min.js`, which the build does not write (it writes
  `casalib-<version>.min.js`), so nothing reached `cubevis/__js__`.  Now
  `define: {__VERSION__}` and the copy takes the versioned output.
  `cubevis/__js__/casalib.min.js` is rebuilt with coordtxl 0.1.5 plus the
  0.1.6 change applied by hand to its JS (what JSweet makes of the Java);
  it embeds version `1.0.113` (the highest git tag) where the old one had
  `0.0.8`.  `package.json` keeps `coordtxl ^0.1.4` until 0.1.6 is
  published: then `yarn up coordtxl@^0.1.6` and rebuild.
* **Pixel convention**: the image glyph is drawn at `x=0, y=0`, `dw, dh` =
  shape, so pixel i covers [i, i+1] and its centre is i + 0.5 in Bokeh data
  space.  Axis ticks and region centres (`to_world`/`from_world`) convert
  data-space positions; the cursor readout converted `Math.floor(x)`.
  coordtxl takes 1-based FITS pixels, so the axes were half a pixel off and
  the readout one pixel.  `coordinate_description.ts`: `FITS_PIXEL_CENTER`
  (1, the default) and `BOKEH_PIXEL_CENTER` (0.5);
  `description_keywords(desc, pixel_center)` gives CRPIX = reference pixel
  − origin + pixel_center.  `ImagePipe` builds its transform with
  `BOKEH_PIXEL_CENTER`; `_cube.py`'s `refresh_pixel_display` converts
  index + 0.5.  cubevisjs rebuilt (Bokeh 3.9.2 → 3.6–3.9, 3.10.0 → 3.10).
  Note: `scripts/copy-versioned.js` names the target from the `bokeh` on
  PATH, not the one that built; put the build's venv first on PATH.
* **Checks**:
  * `browser_check.py`: probes the page at pixel centres (i + 0.5, as the
    readout now does) and compares with casatools' `toworld` for the
    residual (`--tolerance`, default 0.01"; `--no-casatools`); absolute
    work paths.  This tree: max separation 2.8e-10" over 5 pixels, no
    console errors, axis labels consistent (first tick at the pixel corner,
    half a pixel from pixel 0's centre).  Base `9ba6093`: 189" at the
    corner, 7.5" at the reference pixel (the readout there was a further
    pixel off).
  * `coordinates_parity.py`: the "fixed" copy now gets the 0.1.6 change
    (FIXED_PARSE, as JSweet writes it) instead of `substring(4, 8)`, or is
    used as is when the coordtxl given already parses the projection; a
    "GUI (bokeh)" column (`BOKEH_PIXEL_CENTER` at the pixel centres); with
    the projection parsed, description and GUI must equal casatools to
    1e-3" except the known FK4 defect (then: no worse than the header).
    With 0.1.5 and with the patched copy: all checks pass.

    coordtxl vs casatools  header, as given desc, as given  header, fixed  desc, fixed  GUI (bokeh), fixed
    own_frame                         190"          190"      1.66e-10"    1.66e-10"           1.66e-10"
    icrs                              190"          190"      1.66e-10"    1.66e-10"           1.66e-10"
    fk4_b1950                         190"          190"      2.97e+05"    2.97e+05"           2.97e+05"   (known coordtxl defect)
    galactic                          190"          190"      1.66e-10"    1.66e-10"           1.66e-10"
    rotated_30                        683"          256"           571"    2.82e-10"           2.82e-10"
    tan                               190"          190"      2.86e-10"    2.86e-10"           2.86e-10"
    nonsquare_offcentre               546"          546"      2.94e-10"    2.94e-10"           2.94e-10"

  * `imaging` suite (30) and `test_fingerprint.py` pass; the fingerprint
    is unchanged (`k2-19e170a674fe895b4d7d`: none of the changed files is
    in the kernel closure).
* **Since** (2026-11-06, Darrell): coordtxl 0.1.6 published; casalib's
  `package.json` takes `coordtxl ^0.1.6`, and `casalib.min.js` rebuilt from
  it is identical to the hand-patched one above (`a5719c4`).

## 65. iclean step 2: mask edits as single operations (base `a5719c4`, 2026-11-07)

Design §2.2 and step 2: `CubeMask`'s `mask-mod` handler read and wrote the
mask one channel at a time through `ImagePipe` (`mask(index, True)` /
`put_mask`), 2×nchan calls for a cube-wide edit -- 2×nchan round trips once
the cube is remote.  Each edit is now one `ImageCube.edit_mask` call that
runs where the mask is stored.  No behaviour change; the browser and its
messages are unchanged (no cubevisjs rebuild).

* **`cubevis/imaging/_masks.py`** (new):
  * `polygon_pixels(xs, ys, shape) -> (ix, iy)`: `polygon_indexes`'
    selection, vectorized (`Path.contains_points` over the candidate grid
    instead of `filter(contains_point, product(...))`); matplotlib imported
    when first used, so `cubevis.imaging` still imports none of matplotlib,
    Bokeh, CASA.  0.02 s instead of 0.47 s for a 52k-pixel triangle.
    `region_pixels` adds the handler's "whole region inside one pixel"
    fallback.
  * `apply(op, planes, pixels=, modifier=)` on `[x, y]` or `[x, y, n]`
    planes; `MaskEditor.edit(op, scope, index, polygon=, src=)`: ops `add`,
    `subtract` (a region, or another channel's mask: `src`), `clear`, `fill`,
    `invert`; scopes `chan`, `cube` (every channel of `index`'s stokes).  The
    special cases kept: a channel with itself -- subtract clears it and
    returns `{'clear_region': True}`, add does nothing; cube-wide add/subtract
    of a channel's mask skips that channel.  Values written are 0/1 (bools
    as uint8, which putchunk needs).  Cube scope goes through
    `read_mask_chans`/`write_mask_chans` when the store has them (optional
    `ImageStore` primitives, in the protocol's docstring), in chunks of at
    most `CHUNK_BYTES` (64 MB, at least one channel), else plane by plane.
* `ImageCube.edit_mask` (and the `ImageCube` protocol), `ImagePipe.edit_mask`,
  `CasatoolsImageStore.read_mask_chans`/`write_mask_chans` (getchunk/putchunk
  over a channel range).  `mask(index, True)`/`put_mask` stay (tests, other
  callers outside iclean may use them).
* `_cube.py`: `mod_mask` maps the browser's actions (`addition`, `subtract`,
  `delete`, `not`) onto `edit_mask` -- ~120 lines of loops become ~30; the
  failure replies are the ones the loops gave, including their check order
  (e.g. add with a polygon and a bad scope replied `error: None`).
  `set_all_mask_pixels` is `fill`/`clear` per stokes.  `cubevis.utils.
  polygon_indexes` now calls `polygon_pixels` (same generator of int tuples).
* **Two defects in the selection, measured and kept** (a decision for
  Darrell; to fix, change `polygon_pixels` and its reference test):
  1. The last column and row are never selected: the ranges stop at
     `min(ceil(x1), nx - 1)`, exclusive, so a box drawn to the image's edge
     ends at pixel nx-2 (126 of 128).
  2. Non-box polygons test the pixel's *corner* `(i, j)`, not its centre
     `(i + 0.5, j + 0.5)` (§64: pixel i covers [i, i+1] in Bokeh data space):
     the recorded triangle is exactly the corner test, 51 of ~570 pixels
     differ from the centre test.  Boxes (`floor(x0) <= i < ceil(x1)`) are
     consistent with the centre convention.
* **Checks**:
  * `record_iclean_handlers.py`: 15 more mask edits (non-box polygons in
    both scopes, a one-pixel region, regions at the image's edge, cube-wide
    add of a channel's mask, a channel with itself both ways, cube invert
    and delete, a bad action and a bad scope).  Before (`a5719c4`) and after:
    84 entries, 0 differences (24 mask cubes, every reply).
  * `test_masks.py` (new, no CASA, 46) in the `imaging` suite:
    `polygon_pixels` against the old `polygon_indexes` (verbatim, as the
    reference) on 600 random polygons and boxes plus edge cases; each of 17
    edits against a numpy rendering of the old loops, on a store with and
    without the ranged primitives; a sequence of edits whole and in
    three-channel chunks; cube edits on a ranged store read no planes;
    errors; `cubevis.imaging` loads no matplotlib.  `imaging`, `syntax`,
    `remote` suites, `test_fingerprint.py`, `test_lazy_packages.py` pass;
    `browser_check.py`: no console errors, coordinates as §64.
  * 512×512×1×200 mask (casatools): cube-wide region add 2.80 s → 2.19 s,
    cube-wide invert 8.57 s → 2.35 s (one getchunk+putchunk of the whole
    mask: 3.29 s, so edits are now I/O bound).  The point is remote: one
    round trip instead of 400.
* **Fingerprint**: `cubevis.utils._regions` now imports
  `cubevis.imaging._masks`, which is in the closure: `k2-c82e095772c5b36bec49`
  (release still pending).

## 66. iclean step 2b: drawn regions and the mask outline on the pixel grid (base `6da8526`, 2026-11-07)

The two selection defects measured in §65, fixed at Darrell's request, and
the cause of the long-standing "mask contour off by one pixel on one side":
the outline the browser draws was half a pixel inside the mask, and drawn
polygons selected pixels half a pixel down and left of the polygon, so the
outline met the drawn line on the left and bottom and was one pixel inside it
on the right and top.  The convention is §64's: the image glyph is drawn at
`x=0, y=0`, pixel i covers [i, i+1] and its centre is i + 0.5.

* **Selection** (`cubevis/imaging/_masks.py`, `polygon_pixels`): a pixel is
  selected when its centre is inside the region, for every shape.  Boxes:
  `x0 <= i + 0.5 < x1` exactly (was every pixel the box overlapped,
  `floor(x0) <= i < ceil(x1)`); other polygons: `Path.contains_points` at
  `(i + 0.5, j + 0.5)` (was the corner `(i, j)`).  The last column and row
  can be selected (the ranges stopped at `nx - 1`, exclusive).  A region
  holding no pixel centre selects the pixel under the middle of its bounding
  box when that is on the plane (`region_pixels`; was: only when all
  vertices fell in one pixel, so a sliver across two pixels selected
  nothing).  `cubevis.utils.polygon_indexes` follows (it calls
  `polygon_pixels`).
* **Outline** (`cubevisjs/src/bokeh/sources/image_data_source.ts`,
  `_mask_contour`): d3-contour puts mask value i at i + 0.5 -- the pixel's
  centre in Bokeh space -- and the mask is 0/1, so `thresholds([1])` drew the
  outline through the centres of the edge pixels.  `thresholds([0.5])` puts
  it on the pixel edges, corners cut by half a pixel (marching squares; a
  single pixel is a diamond inside its square).  Drawn back as a region (the
  "copy the contour" path), that outline selects the same pixels by the
  centre rule (test).  cubevisjs rebuilt (Bokeh 3.9.2 → 3.6–3.9, 3.10.0 →
  3.10).
* **Checks**:
  * `test_masks.py` (50): `polygon_pixels` against a brute-force centre test
    with its own even-odd ray casting (no matplotlib) on 500 random polygons
    and boxes plus edge cases (replaces the old-selection reference); the
    whole plane including its last column/row; boxes by their centres; the
    d3 outline of a block gives the block back; the no-centre fallback (one
    pixel, a sliver across two, off the plane); the 17 edits × 2 stores now
    against the centre rule.
  * Handler records against step 2 (`6da8526`): only mask cubes differ (10
    entries, from the first non-box polygon on); every reply is unchanged.
    Replaying each recorded edit against the centre rule: all 23 steps match
    now; before, 3 did not -- the triangle (51 pixels), the box to the
    image's edge (37) and the triangle in the corner (405 over 5 channels).
  * `browser_check.py` now also hands the page's `_mask_contour` a 3×2 block
    (x 3..5, y 2..3) and fails unless the outline is x [3, 6], y [2, 4]:
    this tree x [3, 6], y [2, 4]; `6da8526` x [3.5, 5.5], y [2.5, 3.5].  No
    console errors, coordinates as §64.
  * `imaging`, `syntax`, `remote` suites, `test_fingerprint.py`,
    `test_lazy_packages.py` pass.  Fingerprint `k2-f28e9989ade14c5f8b72`
    (`_masks.py` is in the closure; release still pending).
* `tests/ci/README.md`: says that the tests stay with their subject under
  `tests/manual/` and `tests/ci/` holds the runner, the suite list and
  CI-only helpers (Darrell asked whether the tests belong under `tests/ci/`).

## 67. iclean step 3: the imaging layer on a Jupyter kernel (base `4eca91f`, 2026-11-07)

Design §3 and step 3: `RemoteImagingSession` (`cubevis/imaging/_remote_session.py`)
has `LocalImagingSession`'s surface, so `InteractiveCleanUI`, `CubeMask` and
`ImagePipe` run unchanged on it; everything runs in a worker on the kernel's
host.  Not yet in it: `iclean(kernel_name=)` and closing the session from the
GUI (step 5), stopping/progress/reattach during a cycle (step 4).

* **`cubevis/remote/_remote_object.py`** (new, exported by `cubevis.remote`):
  `RemoteObjectConnection(kernel_name, class_name, kwargs, register_function=,
  wire_types=, app=, data=, ...)` -- one object in a worker on a kernel: the one
  `SyncBridge` loop, kernel start, `RemoteAppLink.open`, `create_context`,
  `create_object`; `call` (blocking), `acall` (a coroutine on the bridge's loop),
  `submit` (a `concurrent.futures.Future` another loop awaits with
  `asyncio.wrap_future`), `run`; liveness (`_watched`: kernel stopped or
  heartbeat silent -> `RemoteKernelLostError`), error replies ->
  `RemoteCallError` with the remote traceback, a dead worker ->
  `RemoteWorkerDiedError` (restarted only with `restart_worker=True`), timeouts
  (`DEFAULT_CALL_TIMEOUT`, `data_methods` with `CUBEVIS_REMOTE_TIMEOUT`), the
  session record (`app=`), `eval_code`/`exec_code`, `close` (idempotent).  The
  method name is positional-only (`cube_call` has its own `method`).  It is
  the generic half of visplot's `RemoteReductionContext`, lifted: visplot
  moves onto it in step 5 -- until then the two carry the same logic.
* **`cubevis/imaging/remote_registrations.py`** (new; a fingerprint root):
  `ImagingSessionBackend(args, iclean_backend, deconvolver=, store=)` holds a
  plain `LocalImagingSession`.  Cycles run in a thread (`start_cycle(kind)`,
  `cycle_status(number)`), as `arun_cycle` does locally, because a worker
  answers one call at a time and the GUI keeps asking for channels during a
  cycle; `cube_info(field, image=)`, `cube_call(field, method, args, kwargs)`
  for the `CUBE_METHODS` the GUI uses (`coorddesc` without its casatools
  coordsys; `channel`'s dtype sent by name); `update`, `has_next`, `log`,
  `restore`, `stop_descriptions`, `fields`, `image_products`, `close`,
  `runtime_info`.  `CycleResult` travels as a list; `StopCodes` arrives as a
  plain tuple (the GUI indexes it).
* **`RemoteImagingSession(args, kernel_name, iclean_backend=, deconvolver=,
  store=)`**: `start`/`run_cycle` block the calling thread while polling the
  worker (`CYCLE_POLL` 0.25 s, no budget for a cycle as a whole; the
  connection watches the kernel); `arun_cycle` awaits on the caller's loop
  without blocking it; a dead worker is not replaced (tclean's state was in
  it).  `cube(field)` is a `RemoteImageCube`: paths at once, shape /
  coordinates / stokes labels on first use (the GUI makes its cubes before
  `start()` writes the images), everything else forwarded; `coorddesc()['csys']`
  is None.  `deconvolver` is None.
* **`cubevis/imaging/testing.py`** (new): `ScriptedDeconvolver` (gclean's
  surface; each cycle halves the residual; `cycle_seconds`, `fail_at`) and
  `numpy_store` (an in-memory `ImageStore` with the ranged mask primitives),
  importable by name in a worker.
* **Found and fixed on the way**:
  1. **The worker's stdout is its wire** to the supervisor, and tclean prints
     to stdout from Python and C++ (`Running initial major cycle ...`): the
     bytes corrupted the frame stream and the first cycle never answered.
     `worker_main.protect_wire()`: the wire gets a descriptor of its own,
     descriptor 1 and `sys.stdout` go to stderr (relayed);
     `WorkerCommTransport(wire_out=)`.  Applies to every worker, visplot's
     too (nothing it prints reaches the wire any more).  tclean in a thread
     does not hold the GIL (measured: the main thread's longest wait 0.1 s).
  2. **An `imagename` with a directory** (an absolute path -- what a remote
     kernel's paths usually are) failed at start-up, locally too and before
     the imaging layer: gclean keys its convergence record by `imagename` as
     given, `image_products()` names the field by its base name.
     `LocalImagingSession._result` re-keys the record to field ids.
  3. **Fingerprint of a cubevis without one of the roots** (any release
     before this one, now that interactive clean's is a root): `closure`
     raised there, `kernel_fingerprint` returned None and the check said "no
     kernel-side fingerprint (a cubevis from before 2026-10-23)".
     `fingerprint` now hashes a missing root as a marker: the value differs
     and the message is "kernel-side code differs".  Complete trees hash as
     before (`k2`).
* **Checks**:
  * `test_remote_session.py` (new, 7; quick suite **`imaging-remote`**, kernel
    "python3", no CASA): the scripted clean in the kernel and in this process
    compared call by call -- fields, products, stop descriptions, start, two
    cycles, update (accepted and refused), log, restore, and every cube call
    the GUI makes (channel replies with statistics and histograms, quantized,
    float and uint8 channels, spectrum, histogram, pixel and mask values,
    statistics with and without mask, six mask edits, `put_mask`) before and
    after editing and after each cycle; the worker answering during a
    2-second cycle (>= 10 calls, each < 1 s); a failing cycle raising
    `RemoteCallError` with the remote traceback, the session still usable;
    cube methods outside `CUBE_METHODS` refused; a killed worker reported as
    `RemoteWorkerDiedError` and not replaced; `close` idempotent.
  * Real tclean through kernel "python3": `record_iclean_handlers.py
    --kernel python3` against a local run with `--absolute` in the same
    directory (84 entries): identical except `coorddesc_csys` (None remotely,
    by design), the remote run's own `remote_call_stats`, and
    `fetch_spectrum`'s mask flags (numpy bool locally, bool remotely: the
    browser's JSON is identical).  The recorder reads the mask cube through
    the session's cube: casatools in another process waits for ever on the
    worker's lock.  The recorder and `browser_check.py` replace
    `InteractiveClean`'s `LocalImagingSession` with the remote one
    (`iclean(kernel_name=)` is step 5).
  * `browser_check.py --kernel python3`: the page from a remote session --
    image, axes, convergence plot -- no console errors, coordinates equal
    casatools' (2.8e-10"), the mask outline on the pixel edges.
  * Local iclean against step 2b's record: identical (84 entries).
  * Quick tests: `remote` (worker_main changed), `imaging`, `imaging-remote`,
    `syntax` pass; `setup` fails only the 11 tests that need `ssh`, as on
    the base; `test_fingerprint.py`, `test_checkout.py`,
    `test_lazy_packages.py` pass with `CUBEVIS_TEST_KERNEL_PYTHON` set
    (the roots-cover-the-imports check included); their small trees gain
    the new root, and a test for a tree without it.  `tiny-ms` and
    `visplot-remote` skip here (no xradio/casacore).
  * Timing, kernel on this computer: connect 4–7 s; a cube call ~5 ms; start
    of the 128×128×5 test 2.3 s with every poll answered within 10 ms.
* **Fingerprint** `k2-46c7c0ba50891e6131d4` (new root, `worker_main`,
  `_worker_transport`); the pending release takes it.

## 68. iclean step 4: cycles -- stopping, failures, the controls tested (base `ad4ac0f`, 2026-11-07)

Design step 4.  The cycle itself already runs as a job (step 3: a thread in
the worker, polled, awaited without blocking Bokeh); what remained was what
happens around it.

* **Stopping: after the cycle, as locally.**  gclean has no way to interrupt
  a cycle (no interrupt, abort or cancel anywhere in it: a cycle is
  deconvolve + tclean calls returning at the end), and the GUI's contract is
  already "stop after this cycle": the orange stop (during *finish*) stops the
  browser sending the next 'finish' and sends 'status', whose reply waits on
  the control comm's lock until the running cycle ends
  (`wait_for_tclean_stop`).  Nothing of it depends on where the imager runs;
  tested both ways.  Interrupting would mean killing the worker, which loses
  tclean's state.
* **Failures reach the GUI** (`_ui.py`, `clean_handler`, `_cycle_error`):
  an exception from `update`/`arun_cycle`/`log` -- the imager raising, or
  remotely `RemoteCallError`, `RemoteWorkerDiedError`, `RemoteKernelLostError`,
  a timeout -- is answered `result='error'` with a message for the status line
  (the exception's type and text, the remote traceback left in the log of
  the Python session; worker/kernel messages as they are), stop code
  `[-1, -1]` and the imager's log so far; the browser's `update_gui` already
  handled 'error' (controls enabled again).  Before, the handler raised,
  `CommMgr` replied `{'error', 'traceback'}`, which `update_gui` does not
  recognise: the browser stayed at "Running one set of deconvolution
  iterations" for ever -- locally too, for any gclean exception.
* **A value `update()` refuses is an error** (behaviour change): gclean
  checks every value and returns `(-1, "threshold must be a number, ...")`;
  `clean_handler` ignored it and ran a cycle with the previous values, so a
  typo in the threshold box silently did nothing.  Now the message is shown
  and no cycle runs.
* **`cubevis.imaging.testing.ScriptedDeconvolver`** now gives gclean's
  results: the convergence record per field (`major.cyclethreshold`,
  `chan[c][stokes]` with `modelFlux`, `iterations` (an array), `peakRes`,
  `stopCode`, `cycleThresh`, one entry more per cycle), stop codes and
  descriptions (iteration limit, threshold), `update` checking values as
  gclean does -- so `InteractiveCleanUI` runs on it without CASA.
* **Checks**:
  * `test_clean_controls.py` (new, 11; in the `imaging-remote` quick suite):
    the real `InteractiveCleanUI` on the scripted imager, here and on kernel
    "python3", through the handlers the browser uses -- continue (one cycle,
    convergence, log); finish repeated until converged (update, update,
    converged); a refused threshold (error, no cycle; the next press runs the
    first cycle); a cycle that raises (error with the message, no remote
    traceback in it, the log so far); stop during finish (the 'status'
    answered, the cycle finishes, Bokeh's loop free throughout); a killed
    worker (error "... worker ... is gone ...", and again on the next press).
    Passes with casatools/casatasks unimportable and without `BROWSER`
    (the test sets it).  Against `ad4ac0f`'s `_ui.py` the five failure
    tests fail.
  * `browser_check.py --controls` (real tclean, headless Chrome), here and
    with `--kernel python3`: threshold "abc" + continue -> the status line
    shows gclean's "threshold must be a number, or a number with units
    (Jy/mJy/uJy)" with continue enabled; "0.001Jy" + continue -> the cycle's
    own result ("Zero mask": nothing drawn) with continue enabled.  Buttons
    are clicked through their views (found from `Bokeh.index`'s roots).
  * Handler records: local identical to step 3's (84); remote against local
    the same three expected differences as §67.
  * Quick tests: `remote`, `imaging`, `imaging-remote`, `syntax` pass.
    Fingerprint unchanged (`k2-46c7c0ba50891e6131d4`): nothing kernel-side
    changed (`testing.py` is not reached from a root).
* **Open, for Darrell** (design §5):
  * *Progress during a cycle*: locally there is none either (the status line
    says "Running ..." until the cycle ends).  Remotely a cycle can take
    minutes over a slow link.  Cheapest useful form: the elapsed time in the
    status line, pushed by Python while it waits (`CommMgr.send` to a small
    browser handler); fuller: minor-cycle lines from tclean's log, relayed by
    the worker.
  * *Reattach*: a laptop that loses the kernel ends the session
    (`RemoteKernelLostError`, now shown in the GUI); picking a running clean up
    again from a new process would need the session record
    (`cubevis.setup.sessions`, written with `app="iclean"`) plus a way to
    rebuild the GUI's state from the worker.  Not needed for parity.

## 69. iclean step 4b: progress of a running cycle (base `ad4ac0f` + §68, 2026-11-07)

Darrell (on §68's questions): progress feedback for local and remote tclean
runs -- the GUI is disabled during a cycle, so a busy cursor says little;
progress that changes shows that tclean runs and is being followed.  And
session management (a later session) will let a user start iclean on a
cluster node, close the laptop, and reconnect from elsewhere with the GUI
showing the clean's current state: support it where it is cheap now, leave
the significant work for that session.

* **What there is to show** (measured): tclean writes to casalog *while* a
  cycle runs -- `Begin Task: deconvolve` / `tclean`, `Run Hogbom minor-cycle
  on 5 chans`, one line per channel as it finishes (`[bl:C2] iters=18->33
  [15], ... peakres=1.00951->0.42671`), `Completed 51 iterations`,
  `Run Major Cycle 2`.
* **`cubevis/imaging/_progress.py`** (new): `CycleProgress(log_path)` --
  `begin(kind)` / `end(ok)` around a cycle, `snapshot()` reads what the log
  gained since (whole lines, at most 256 kB a time, a log that starts again
  read from its start) and returns the state: `running`, `number`, `kind`,
  `seconds`, `phase` ('minor cycles' / 'major cycle'), `algorithm`,
  `channels` / `channels_done`, `iterations`, `peak_residual`,
  `major_cycle`, `activity` (lines read), `quiet_seconds`, `ok`.
  `describe(snapshot, label)` makes the status line: "Running one set of
  deconvolution iterations · 0:42 · minor cycles (Hogbom): 3 of 5 channels,
  33 iterations, peak residual 0.4267" (and "· tclean quiet for N s" after
  30 s without a line).  The log is casalog's (`casatasks`, imported when
  first needed), or a deconvolver's own `log_path()`; without one, only the
  elapsed time.
* **Progress is state, kept where the imager runs** -- what a reconnecting GUI
  will need: `LocalImagingSession.progress()` (begin/end around `start`,
  `run_cycle`, `arun_cycle`; `log_path=` or the deconvolver's own);
  the worker's `cycle_status` carries it and `ImagingSessionBackend.progress()`
  answers it at any time; `RemoteImagingSession.progress()` returns the last
  poll's while it polls a cycle (no extra round trip), else asks the worker.
* **The GUI** (`_ui.py`): a third comm, "cycle progress", opened with
  `squash_queue=True` (a slow link holds the newest update, not a backlog);
  `_report_progress(label)` runs while `clean_handler` awaits the cycle and
  every `PROGRESS_INTERVAL` (1 s) sends `{'text', 'progress'}`; cancelled
  when the cycle ends.  Browser (`'clean-progress'` snippet):
  `progress_watch()` -- called before each 'next'/'finish' is sent, the
  automatic 'finish' repeats included -- registers the handler once and sets
  `state.cycle_running`; the handler writes the text to the status line only
  while `cycle_running` (`update_gui` and `wait_for_tclean_stop` clear it), so
  an update arriving after the cycle's result is dropped.  Labels: "Running
  one set of deconvolution iterations" (continue), "Running multiple
  iterations" (finish), as the browser's own.
* **`ScriptedDeconvolver`** writes those casalog lines into its own log
  (`log_path()`) while it works, spread over `cycle_seconds`; names its field
  by the image's base name, as gclean does.
* **Checks**:
  * `test_progress.py` (new, 7; `imaging` suite): real tclean lines read as
    they are written (phase, algorithm, channels 3 of 5, iterations 33, peak
    residual, then the major cycle, totals), what came before the cycle
    skipped, a line written in two pieces read once, a log that starts again,
    no log (time only; a failing log source), the "quiet" note, the session
    following its cycles while they run, a failed cycle marked.
  * `test_clean_controls.py` (+2, 13): during a 3 s cycle, here and in the
    worker, at least four updates, the elapsed time rising, the channels
    moving, "minor cycles (Hogbom) ... of 4 channels"; none after the cycle;
    `progress()` still answers after it; 'finish' with its own label.
  * Real tclean (1024², 5 channels, a mask): snapshots every 0.1 s showed
    "minor cycles (Hogbom): 1 / 3 / 5 of 5 channels, 9 / 33 / 51
    iterations, peak residual ..." then "major cycle (51 iterations so far)",
    for two cycles.
  * `browser_check.py --controls --scripted 4` (headless Chrome), here and
    with `--kernel python3`: the status line showed 3 (local) and 4
    (remote) progress lines during the cycle -- "... · 0:01 · minor cycles
    (Hogbom): 1 of 5 channels, 2 iterations, peak residual 1.924", "... ·
    0:03 · major cycle (10 iterations so far)" -- then the cycle's result.
    `--scripted SECONDS` runs the scripted imager (no tclean, no casatools
    comparison).  Real tclean `--controls`, both ways: as §68.
  * Handler records: local and remote identical to step 4's.
  * Quick tests: `remote`, `imaging`, `imaging-remote`, `syntax` pass
    (without `BROWSER`); `test_fingerprint.py` with a kernel Python passes.
* **For reattach, in place**: a cycle's state and progress live in the
  worker (`cycle_status()` without a number answers the latest cycle, with
  its result once done; `progress()` at any time), so a new laptop process
  could learn what runs and what came of it.  **Left for the session work**:
  adopting a running worker (the session record, `app="iclean"`, has the
  kernel, context and handle), rebuilding the GUI (convergence history, the
  last `CycleResult`, the controls' state) from it, the browser registering
  the progress handler at page load rather than at the first click.
* **Fingerprint** `k2-eefb1f77fd564a7cf7ee` (session, `_progress`, the worker).

## 70. iclean step 5a: `iclean(kernel_name=)`, the end of a session; CI without python-casacore (base `48a51d7`, 2026-11-07)

Step 5 split (Darrell, 2026-11-07): 5a `kernel_name=` and closing the remote
session; 5b kernel capabilities in setup; 5c visplot onto
`RemoteObjectConnection`.  A cubevis release comes from `main`, so it waits
for the merge; until then a node runs this branch the way setup already
advises for a development install (this checkout installed with
`pip install -e` in an environment there; setup finds it by fingerprint).

* **`kernel_name=`** (like `iclean_backend`: accepted, not in the
  casashell `inp`/`go` listing): `InteractiveClean(..., iclean_backend="PROD",
  kernel_name=None)` makes a `RemoteImagingSession(args, kernel_name,
  iclean_backend=)` instead of a `LocalImagingSession`; the casatasks
  wrappers `iclean` and `iclean_notebook` pass it on.  Templates
  (`toolbox/iclean/_app.mustache`, `private/casatasks/iclean{,_notebook}.mustache`)
  and the generated `.py` (`scripts/expand_templates`; it reproduces the
  base byte for byte, so the generated diff is these lines only).  The
  casashell binding passes neither `iclean_backend` nor `kernel_name` (it
  never had the first).
* **The end of a session** (`_ui.py`, `_finish_session`): `_task_server`
  returns `result()` (which calls `restore()`), then keeps the imager's
  history (`log(True)`; `history()` returns it afterwards) and closes a
  remote session -- worker, link, kernel.  Only the GUI's real end comes
  here (`shutdown_handler` resolves the result future); a connection that
  drops (`connection_closed_handler`) leaves the session for the frontend to
  reconnect.  A local session is left as before.  Not done: closing when the
  Python process ends some other way (an exception, Ctrl-C) -- with session
  management a kernel left running may be wanted; decided there.
* **Remote result**: `restore()`'s `stopcode` is gclean's `StopCodes`
  namedtuple locally, a plain tuple remotely (indexing is the same,
  `.major` is not).
* **CI without python-casacore**: visplot itself never needed it (MSv2
  through xarray-ms/arcae; casacore only named as the table format).
  `tests/ci/make_tiny_ms.py` used `casacore.tables` to give the generated MS
  its times, UVWs and data; xradio 1.3.0 accesses tables through arcae by
  default (`xradio._utils._casacore.backend`: python-casacore's
  `tables`/`images` API emulated on arcae; python-casacore only through an
  unsupported `XRADIO_CASACORE_BACKEND` switch), and its generator writes
  with it -- `make_tiny_ms` now uses that `tables.table`.
  `tests/ci/requirements.txt`: `xradio>=1.3`, no `python-casacore`;
  `suites.toml`: `tiny-ms` and `visplot-remote` require `arcae` instead of
  `casacore`.  Here (xradio 1.3.0, arcae 0.5.8, xarray-ms 0.5.13, no
  python-casacore): both suites run and pass -- `visplot-remote` 22/22,
  which also confirms §67's `protect_wire` for visplot's workers.
* **Checks**:
  * `browser_check.py --controls --stop` (new `--stop`: the red stop, the
    confirmation accepted; the app must end with its result), with real
    tclean, here and with `--kernel python3` -- now through
    `InteractiveClean(kernel_name=)`, not a swapped-in session: controls as
    §68; stop: exit 0, the result (`'stopDescription': 'Zero mask'`),
    history 4 entries, `session closed: True` remotely; no kernel process
    left.
  * `record_iclean_handlers.py --kernel` now uses `kernel_name=`:
    identical to the swapped-in session's record (85 entries).
  * `test_clean_controls.py` (+2, 15): the end keeps the history and closes
    a remote session once; a local session stays usable.
  * Quick tests: `remote`, `imaging`, `imaging-remote`, `tiny-ms`,
    `visplot-remote`, `syntax` pass.  Fingerprint unchanged
    (`k2-eefb1f77fd564a7cf7ee`).

## 71. iclean step 5b: kernel capabilities in setup; CASA's data (base `1e0c6b2`, 2026-11-07)

Decisions (Darrell, 2026-11-07): two capabilities, `visplot` and `iclean`,
never assumed of each other (a site's CASA environment with cubevis is an
iclean kernel without xarray-ms); setup adds only to environments it made;
the extra is `cubevis[iclean]`; no warning about casa6 + xarray in one
environment (arcae was written to live beside casatools -- checked here:
casatools 6.7.6.14 and arcae 0.5.8 from pip read one MS in one process);
CASA's data: a private copy per environment (casadata), or the user's own
when casaconfig finds it -- the user chooses; the private copy reaches the
kernels as `CASASITECONFIG` through the kernel launcher's environment.

* **Capabilities** (`cubevis/setup/capabilities.py`, new):
  * `PROBE`, appended to the launcher test (`launcher.test_program`), so it
    runs in the kernel's own context (directory, variables,
    `CASASITECONFIG`): `visplot` -- `xarray_ms` imports; `iclean` --
    casatasks and casatools installed and casatasks' `_gclean.py` found,
    **not imported** (importing casatools writes `casa-*.log` into the
    current directory and may start CASA's data updates; the worker's real
    import reports failures in the GUI); `casa_data` -- casaconfig's
    resolved configuration (`casaconfig.config` alone reads the site and
    user files, changes nothing): `measurespath`, the files that set it,
    auto-update, `ready` when `measurespath/geodetic` exists, else why.
    One line, `CUBEVIS-CAPS {json}`; `remote.test_launcher` returns
    `(fingerprint, cwd, report)`.
  * Kernel records: `capabilities`, `capability_errors`, `casa_data`
    (health fields).  `envcheck` reports `iclean` per environment the same
    way (no import); environment records keep `iclean`.
  * `require(kernel_name, app)`: the laptop side.  `RemoteObjectConnection`
    (app `iclean`) and visplot's `RemoteReductionContext` refuse, before
    anything starts, a kernel setup recorded as unable to run the app
    (`MissingCapability`, with what to do).  A kernel setup does not know,
    or never checked, is not refused.
* **Setup** (`hostsetup.py`):
  * Making an environment asks "Should kernels on NODE run interactive
    clean?" (default yes; `cubevis[visplot,iclean]==V`, about 300 MB more).
  * `iclean_step` (after an environment is chosen, and on a rerun for each
    environment the host's kernels use): an environment that cannot run it
    -- its check, else its kernels' tests (`environment_iclean`: records from
    before 5b have no `iclean`) -- gets, if setup made it, "Add interactive
    clean to it?" (a no is kept, `iclean_declined`, until `--redo`); if it
    is the user's, the pip command.  Added: the kernels are tested again.
  * `casa_data_step` (after a kernel's test passes, it runs iclean): data
    found -- "[u]se it, or give the environment a [p]rivate copy?" (asked
    once per environment, `casa_data_choice`; `--redo` asks again); none
    found -- a private copy by default in setup's environment, what to do in
    the user's.  A private copy: `envmake` add of `casadata` plus
    `PREFIX/cubevis-casasiteconfig.py` (measurespath = casadata's, no
    auto-updates); the environment record's `casa_site_config` (from the
    marker); the launcher exports `CASASITECONFIG` (unless the kernel's
    variables set it); the kernels are tested again, and a user
    `~/.casa/config.py` whose measurespath overrides the copy is said.
  * `use_kernel` and `status` show what each kernel runs, the iclean call
    beside the visplot one, and CASA's data.
* **envmake**: `add` -- into an environment with setup's marker only: the
  same detached worker without the create step, pip with each requirement
  (`spec` now space-separated), the site configuration
  (`site-config` step), the marker rewritten last (`added`, `iclean`,
  `casa_site_config`).  Its docstring: setup changes its own environments
  only by adding to them when asked.
* **pyproject**: `iclean = ["cubevis[casa6,remote]"]` (a kernel's
  environment needs ipykernel as visplot's does); the casa6 + xarray
  warning replaced.
* **Checks**: setup suite: the 11 base failures (ssh) only, +24 tests
  (`test_capabilities.py` new: the probe in this Python -- no data, your
  configuration, setup's site configuration, your config overriding it,
  nothing written in the directory or home -- and in one without either;
  the launcher's `CASASITECONFIG`; `require`; `RemoteImagingSession` refused
  before a kernel manager is made.  `test_envmake.py`: add, the site
  configuration, pip with both requirements, never into an environment
  without the marker.  `test_hostsetup.py`: the question when making, the
  offer and its no, the user's environment, the add round trip, both data
  choices, an overriding config, the rerun path end to end).  Quick tests:
  `remote`, `imaging`, `imaging-remote`, `tiny-ms`, `visplot-remote`,
  `syntax` pass.  Fingerprint `k2-4e77b3fe294591fa0bd6`
  (`cubevis.setup.capabilities` joins the closure through
  `RemoteObjectConnection`, as `sessions` and `state` did).
* **Not done**: a real host (ssh, conda, PyPI's casatasks) -- the rig's
  tests skip here; the probe ran through a launcher on this computer
  (`test_launcher.py` with the kernel Python).
## 72. iclean with `kernel_name=`: parameters checked on the kernel's host; gclean's bare error code (base `1e0c6b2` + §71, 2026-11-09)

Darrell's first run on a cluster kernel (`cvpost106-iclean`, from a wheel of
this branch): `iclean(vis='refim_point_withline.ms', ..., kernel_name=...)`
failed **on the laptop** -- `AssertionError: all elements of the vis vector
parameter must be a path that exists`.  The task wrapper
(`private/casatasks/iclean{,_notebook}`) checks and coerces every
parameter with casatools' `CasaValidator` before anything else, and `vis`
is a `cReqPath`: it must exist *here*.  With a kernel the paths are the
host's.  (It also needed casatools and casatasks on the laptop; the
notebook wrapper imported them at module level.)

* **The check, generated once**: `cubevis/imaging/_tclean_params.mustache`
  (new; `TASK XML> tclean -argfilter=interactive,fullsummary`, as the
  wrappers) -> `_tclean_params.py`: `check(doc)` (the wrapper's schema and
  validation; the coerced document, or AssertionError with casatools'
  text) and `names()`.  casatools only inside `check`
  (`cubevis.imaging` still imports no CASA).  `scripts/expand_templates`
  regenerates every other file unchanged.
* **Wrappers**: with `kernel_name` the arguments go to the application as
  given -- no CASA imported, nothing logged here; otherwise `check` and the
  task log as before (the schema no longer inline; module-level casatools
  imports in `iclean_notebook` gone).
* **Worker**: `ImagingSessionBackend(check=True)` checks in its own
  working directory (relative paths relative to the kernel's, as the
  design says), logs `iclean` in the host's CASA log (begin; end at
  `close`), keeps the coerced arguments (`args()`).  A refusal is a
  ValueError beginning `PARAMS_ERROR` with the host and directory;
  `RemoteImagingSession` raises it as `ValueError("kernel 'K': iclean's
  parameters (checked on HOST, in DIR): ...")`, without the worker's
  traceback.  `check` defaults to True unless a test deconvolver is given.
  The app takes `session.args` (coerced on the host) for the GUI's values.
* **gclean's bare error code**: when gclean's cycle raises, it returns
  stopcode `-1` (an int) with the exception's text as the description;
  `clean_handler` indexed `stopcode[0]` and failed with "'int' object is
  not subscriptable", so the error reached nobody and the page kept
  "Running one set of deconvolution iterations".  `LocalImagingSession`
  now makes a bare code a pair, `(-1, 0)`: the GUI shows gclean's message.
* **Found, in casatasks' gclean (not fixed here)**:
  `_update_peakres_masksum_multifield` runs `imstat` on
  `product['residualname']` / `['maskname']` -- base names, without
  `imagepath` -- so they are looked up in the **current directory**.  An
  `imagename` whose directory is not the working directory fails at the
  first cycle ("the imagename parameter must be a path that exists
  ('br.residual' does not exist)") -- or, worse, silently reads a
  same-named image there.  Locally too.  Seen as: the browser check with
  an absolute imagename on a kernel started elsewhere; a relative
  `imagename` (images in the kernel's directory, Darrell's case) works.
  Fix belongs in casatasks: join `imagepath`.
* **Checks**: the case reproduced with a kernelspec that starts in another
  directory (`hostdir`, `cd /tmp/hostdir`): the wrapper from a directory
  without the MS -> the GUI starts on the kernel; `vis` not on the host ->
  the plain ValueError; locally -> casatools' AssertionError as before.
  Relative and absolute imagename cycles (`Zero mask`).  New
  `test_task_params.py` (imaging suite): the wrapper's remote branch with
  casatools/casatasks unimportable (both wrappers), `check`, the worker's
  check in its directory, the refusal through a kernel.
  `test_imaging_layer.py` +1 (the pair).  `browser_check.py --controls
  --stop --kernel hostdir` with the absolute imagename now shows gclean's
  message in the status line, the buttons enabled again (before: stuck on
  "Running ...").  Quick suites (`remote`, `imaging`, `imaging-remote`,
  `tiny-ms`, `visplot-remote`, `syntax`) pass; setup: the 11 ssh failures
  only.  Fingerprint `k2-4649a3d7dc3c3e260b98` (with §71).
* **Delivery**: cumulative on `1e0c6b2` -- §71's files (not yet pushed
  then) and these.

## 73. iclean takes setup's kernel name (2026-11-09)

Darrell's next run: setup made kernel `cvpost106-iclean` and printed
`iclean(..., kernel_name="cvpost106-iclean")`; iclean failed with
jupyter_client's `NoSuchKernel: No such kernel named cvpost106-iclean`.
Setup writes the Jupyter kernel as `cubevis-NAME`; visplot has resolved
setup's name to it (`VisibilityPlotter._resolve_kernel_name`),
but `RemoteObjectConnection` (iclean) passed the name to jupyter_client
as given.

* The resolver moves to `cubevis.remote.resolve_kernel_name`
  (`_remote_object.py`; the text unchanged), and `RemoteObjectConnection`
  resolves every kernel name -- after the capability check, which knows
  setup's names (so a kernel that cannot run the app is refused as that,
  not as an unknown name).  `visibility_plotter._resolve_kernel_name`
  delegates to it (its tests unchanged).
* Test: `test_capabilities.py` +1 -- `RemoteImagingSession` with
  `"cvpost106-iclean"` starts `cubevis-cvpost106-iclean`; an unknown name
  is refused before a kernel manager is made.
* Quick suites pass; setup: the 11 ssh failures only.  Fingerprint
  `k2-1d0eb7578783471039fe`.

## 74. The image no longer resizes as the status line changes (2026-11-09)

Darrell, on the cluster kernel: during a cycle the image plot shrank a
little and came back.  The status line is a `Div` (width 230) in the top
row beside the clean buttons (40 px); the progress text (§69) ran to 3-4
lines, the row grew with it, and the image below lost the height.  Two
lines (the old "Running one set of deconvolution iterations") fit beside
the buttons, which is why it was never seen before progress.  Measured in
`browser_check --scripted 6 --controls`: status 37 / 51 / 69 px -> image
plot 875 / 860 / 843 px high, top 96 / 111 / 128.

* The status line has a fixed height (`InteractiveCleanUI.STATUS_HEIGHT`
  = 40, the buttons'; `height_policy='fixed'`, `overflow-y: auto`): a
  longer status (an error) scrolls inside it.
* Progress uses a short form that fits two lines --
  `describe(progress, label, short=True)`: "0:42 · minor cycles: 3/5
  chans, 1520 iter, peak 0.0213", "0:42 · major cycle", "· quiet 42 s" --
  with the whole text (label and all) as its tooltip (`<span title=...>`).
* `browser_check.py --controls` now records the image plot's box and the
  status line's height while the cycle runs, fails when the plot moves or
  changes size, and lists status texts too long for the line.  Scripted
  (6 s cycles) and real tclean: the plot one box throughout
  ([0, 99, 1050, 872]), the status line 40 px, nothing scrolling.
* Tests: `test_progress.py` +1 (the short form); `test_clean_controls.py`
  (the pushed text is the short form with the tooltip).  Quick suites pass;
  setup: the 11 ssh failures only.  Fingerprint `k2-dc6e5294be51545a1070`.

## 75. iclean step 5c: visplot on the shared connection; kernels on other cluster nodes; `setup kernel NAME` (base `ca3f47b`, 2026-11-09)

* **visplot's `RemoteReductionContext` uses `RemoteObjectConnection`**
  (`self._conn`): the kernel, link, worker, liveness watch, session record
  and kernel-name resolution are one code path for visplot and interactive
  clean.  `remote_reduction_context.py` 1281 -> 887 lines.  What the shared
  connection gained from visplot's copy, for both: each connect phase's
  time in the log; with `restart_worker`, a call that never reached a
  worker gone between calls is sent to a new one, once; the session record
  follows a new worker; `recreate(kwargs)` (visplot's `reopen`); the
  decode time in `CLIENT_STATS`; `kernel_fingerprint()`;
  `kernel_manager`, `session_id`; `restart_hint` (visplot: "the next Plot
  starts a new one ...").  Kept in visplot: metadata after connecting (a
  failure there closes the connection), the memory guard's refusal as
  `SelectionTooLargeError`, the slow-call message with its hint, the memo
  and pending-flag bookkeeping, `call_stats()` over `conn.call_stats`.
  `RemoteBackendError`, `RemoteWorkerDiedError`, `RemoteKernelLostError`,
  `HEARTBEAT_GRACE`, `_quiet_setup_kernel_hints` stay importable from the
  module (the shared ones).  Tests that set visplot's internals
  (`test_worker_death`, `test_kernel_fingerprint`, `test_sessions`) now set
  the connection's, plus: interactive clean's connection (no restart)
  reports a gone worker at once.
* **Fingerprint**: `RemoteObjectConnection.kernel_fingerprint` imports
  `cubevis._fingerprint`, which put that module in the kernel-side closure
  -- an environment's own copy of it then changed the hash, though it never
  decides anything (setup and the connection send their copy).
  `_fingerprint.SENT` leaves it out (`test_discover`'s
  `test_environments_are_fingerprinted_with_the_code_setup_sends` found
  it).  Fingerprint `k2-046c84b6febd6d1717d6`.
* **Kernels on another node of a cluster** (`setup jupyter NAME NODE` ->
  `cubevis-NAME-NODE`): `sessions.setup_kernel_name(name, st)` knows them as
  NAME when NAME is a kernel and NODE a node; the capability check and the
  session record use it (before, such a kernel was never refused, and its
  session recorded as `NAME-NODE`).
* **`python -m cubevis.setup kernel NAME [--workdir DIR] [--env VARS]
  [--environment ENV]`** (Darrell: change one thing without the host's
  whole walk): logs in to the node the kernel was last checked on, changes
  only what is given (no option: working directory and variables asked,
  defaulting to the kernel's), records the change, rewrites and runs the
  launcher; a cluster whose defaults were the kernel's old settings is
  asked whether they follow (`cluster_defaults`); a new environment gets the
  CASA-data step.  The end of a host's setup names the command.
* **Checks**: quick suites pass; setup: the 11 ssh failures only (+7
  tests: the command, the node names).  `test_remote_reduction_context.py`
  on `sim_point_withline.ms`: the same 9 pass / 3 fail before and after
  (the 3 compare `antenna_labels` and columns on this simulated MS; not
  this change).

## 76. setup: another kernel on a host set up before; the help lists every command (base `2b385ab`, 2026-11-09)

Darrell: (1) `--help` did not show the kernel command's options; (2) a
rerun of `setup cvpost106` (a working kernel there) gave no way to add a
kernel in another environment -- now that kernels serve two applications,
that matters.

* **Another kernel**: a rerun of a host whose kernels exist checks them, as
  before, then asks "Add another kernel?" (default no, `another_kernel`);
  yes chooses its environment (any of the node's, 'new', or a path), the
  interactive clean offer for it, then the kernel's settings and name.
* **`HOST --kernel NAME`**: a *new* NAME now always chooses its environment
  (the default is the one the host's kernels use).  Before,
  `environment_step(reuse=True)` took that one without asking whenever a
  single environment was in use -- the reason no other could be chosen.
  An existing NAME keeps its environment (its settings asked again);
  `setup kernel NAME --environment ENV` moves it.
* **`--help`** prints the commands' table from the module docstring (every
  command and its options, the kernel command's included) above argparse's
  list (`RawDescriptionHelpFormatter`).
* Tests (+3): the rerun's offer (no / yes), `--kernel` new and existing,
  the help text.  Setup: the 11 ssh failures only; syntax passes.
  Fingerprint unchanged (setup's modules are not kernel-side).

## 77. A kernel's capability record is tried, not trusted; the visplot tests take setup's kernel names (base `128cbc1`, 2026-11-09)

Darrell's full visplot run against `CUBEVIS_TEST_KERNEL=cvpost106-visplot`
(setup-test-run-005): 7 failed, 81 errors, two kinds:

* **`MissingCapability: kernel 'cvpost106-visplot' cannot run visplot:
  ModuleNotFoundError: No module named 'xarray_ms'`** (every test through
  `RemoteReductionContext`).  Setup records what a kernel runs when it
  tests the launcher; this kernel's environment (`cubevis-devel`) got its
  visplot packages after that, but the record still said no, and the
  connection refused on the record alone.  Now:
  * `RemoteObjectConnection` does not refuse on a record: a negative one is
    logged ("setup recorded that kernel ... cannot run visplot (...);
    trying it") and the kernel is tried.
  * When the worker cannot make the application's object because it
    cannot import a module (`ModuleNotFoundError` / `ImportError` the last
    line of the remote traceback), `MissingCapability` says why and what to
    do -- as the refusal did, now from what the worker found -- and the
    record is set to it (`capabilities.mark`).
  * A connection that works where the record said no sets it to yes: the
    record follows the environment (`setup status` shows it).
  * `capabilities.require` stays, for an explicit check; connections no
    longer call it.
* **`NoSuchKernel: No such kernel named cvpost106-visplot`**
  (`test_remote_eval_exec`, `test_remote_kernel_link`,
  `test_remote_object_registry`): those tests start `AsyncKernelManager`
  with `CUBEVIS_TEST_KERNEL` as given; setup's name needs
  `cubevis.remote.resolve_kernel_name` (Jupyter knows `cubevis-NAME`).  The
  seven visplot test modules that read `CUBEVIS_TEST_KERNEL` now resolve it.
* **Checks**: `test_capabilities.py` (a negative record tried with a
  warning; a missing module reported as the capability and recorded; a
  working connection correcting the record); a real kernel recorded
  "visplot: no" connects and the record becomes yes; the visplot remote
  tests with a setup-style name (`CUBEVIS_TEST_KERNEL=localtest`, Jupyter
  `cubevis-localtest`): eval/exec 5/5, reduction context as before (the
  same 3 failures on the simulated MS, before and after).  Quick suites
  pass; setup: the 11 ssh failures only.  Fingerprint
  `k2-c26b085ba19057418f44`.
