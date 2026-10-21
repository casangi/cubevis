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
