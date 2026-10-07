# HRS H2: phase rms and coherence (slices 1-4: raster quantities, windows, scatter, scatter controls)

*2026-10-05. Built against `main` at `aba8829`; in `main` as of `9ab33e0`. Plan:
`hrs_visplot_plan.md` (milestone H2). Background:
`hrs_commissioning_workflow_survey.md` section 2.*

Requirement (CASR-385): "Ability to plot phase rms vs time and frequency."

## What this slice adds

Two raster quantities, in the *Raster quantity* dropdown and as
`raster_qty="PHASE_RMS"` / `"COHERENCE"`:

| Quantity | Definition | Stable phase | Pure noise |
|---|---|---|---|
| Phase RMS (deg) | RMS of each sample's phase about the cell's mean phase direction; differences wrapped into (-180, 180]; every unflagged sample weighted equally | 0 | about 104 (180/sqrt 3) |
| Coherence | abs(mean V) / mean(abs V) | 1 | about 1/sqrt(N) |

For phase noise of sigma radians, Coherence is about exp(-sigma^2/2): the
two carry the same information, Coherence as "fraction of signal that
survives averaging". It is what AIPS shows as EDITR's coherence display
and IBLED's decorrelation index.

A raster cell covers the samples along the dimension that is not
displayed, so the requirement's two views are the existing axis choices:

| Raster axes | Each cell is | Statistic taken over | Reads as |
|---|---|---|---|
| Baseline x Time | one integration, one baseline | the band (channels) | phase rms vs time |
| Baseline x Channel | one channel, one baseline | the selected time range | phase rms vs frequency |

## Slope removal (`detrend`)

Before fringe fitting, data have a residual delay (phase linear in
frequency) and rate (phase linear in time). Left in, the slope swamps the
statistic: three turns across the band read 104 deg and zero coherence
with no noise at all. With `detrend` on (the default) a linear slope is
estimated and removed per cell along the reduced time or frequency
dimension first. With it off, the statistic is of the data as they are,
which is the view for "is there a delay?".

- Per raster panel, like averaging: *Phase slope (RMS / Coherence)* Select
  in each raster gear tab (Remove / Keep); `VisibilityRaster(detrend=)`,
  `update_axes(detrend=)`; `detrend=` on the plotter and task sets the
  initial value. Carried to the backend on `SelectionSpec.detrend`,
  stamped on a copy at query time.
- The title says which: "Phase RMS (slope removed)" or "(slope kept)".

Method, in `data/_raster_stats.py`: a ladder of mean lag-m products
(m = 1, 4, 16, ... up to a third of the window) gives a wrap-proof coarse
slope, each rung refining the last; a least-squares line fit to the
residual phases finishes it. Pairs are matched by real coordinate, so
gaps between scans and descending frequency are handled. The RMS divides
by (n minus fitted parameters), so short windows do not read low.

Two simpler estimators were tried and rejected on measured results: lag 1
alone (20 deg of true noise read 24) and lag 1 followed directly by a
long lag (45 deg over 256 channels read 61, the long lag aliasing).

### Measured accuracy (synthetic, known noise, steep delay present)

| Channels in window | 5 deg | 20 deg | 45 deg | 60 deg |
|---|---|---|---|---|
| 8 | 4.6 | 19.8 | 43.6 | 55.2 |
| 16 | 4.9 | 19.7 | 44.3 | 59.6 |
| 64 | 4.8 | 19.6 | 44.4 | 59.3 |
| 256 | 5.0 | 20.0 | 45.1 | 60.1 |
| 1024 | 5.0 | 20.0 | 44.9 | 59.7 |

Coherence matches exp(-sigma^2/2) to three decimals from 64 channels up;
with 8 to 16 channels it reads a few percent high (0.953 for an expected
0.941 at 20 deg), because the slope fit and the 1/sqrt(N) noise floor are
no longer negligible.

This is not a fringe fit. It needs enough signal per sample for adjacent
phases to be related; on pure noise there is no slope to find and the
result is the noise value either way.

## Windows (slice 2, 2026-10-05, against `452ed8f`)

By default a cell's statistic is taken over the samples it covers (the
undisplayed dimension). Two per-panel settings refine that.

| Setting | Values | Default |
|---|---|---|
| Time window | Auto, Off, Scan, or seconds (gear tab offers 10 s to 10 min) | Auto |
| Channel window | Off, or a channel count (gear tab offers 4 to 256) | Off |

How a window acts depends on whether its axis is on the plot:

- **Displayed axis: painted back.** The statistic is taken within each
  window and every cell in the window shows it. The grid does not change,
  so the flag overlay, the cursor readout and box flagging behave as
  before; the picture becomes blocky at the window size. This is what
  gives Phase RMS and Coherence on the Time x Channel waterfall, which
  is otherwise blank for them (one sample per baseline per cell).
- **Reduced axis: pooled.** Each window's scatter is measured about its
  own mean phase and slope, and the windows are combined (sums of squares
  and degrees of freedom added). So scan-to-scan phase jumps and changes
  of source do not count as scatter.

- **Baselines reduced into a cell: always pooled one by one.** The GUI's
  antenna filter selects every baseline of the named antennas (a single
  baseline cannot be selected there), so a Time x Channel raster normally
  has several baselines in each cell. Each has its own phase, so the
  scatter is measured per baseline and the baselines combined; the result
  is the phase stability of the selected baselines as a set. The first
  build of slice 2 lumped them instead, which read 80 to 90 deg for five
  baselines of 10 deg each; caught before delivery of this revision, when
  checking how a single baseline is selected (it is not).

**Auto** means Off where Time is a plot axis and Scan where it is not.
That changes slice 1's Baseline x Channel behaviour, deliberately: it used
the whole selected time range as one window, which read high whenever
more than one scan was selected (three scans with 10, 30 and 10 deg of
noise and different phase offsets: 67 deg as one window, 19 deg pooled
per scan, the correct pooled value being 19.1). Off restores the old
behaviour.

A "scan" is a contiguous run of integrations: the time axis is split
wherever consecutive samples are more than 1.5 median steps apart.
Windows in seconds are cut within each run and never span a gap; a final
piece shorter than half a window joins the one before it. The scan number
itself is not consulted, so two scans recorded back to back with no gap
count as one run.

The title names the window: "Phase RMS (slope removed, 60 s x 16 ch)",
"(slope removed, per scan)".

API: `stat_time_window=`, `stat_chan_window=` on `VisibilityPlotter`,
`visplot()`, `VisibilityRaster` and `update_axes`; carried on
`SelectionSpec` like `detrend`.

## Scatter (slice 3, 2026-10-06, against `ec5809d`)

*Phase RMS* and *Coherence* are now also in the scatter panel's Y list
(`scatter_y="PHASE_RMS"`). This is the requirement's literal form: phase
rms plotted against time, or against frequency.

A scatter has no undisplayed dimension to take the statistic over, so the
x axis decides the window, using the same two settings as the rasters:

| X axis | Time window | Channel window | One point per |
|---|---|---|---|
| Time | each integration | the whole band | baseline, integration |
| Frequency / Channel | each scan | each channel | baseline, channel, scan |
| UV distance, U, V | each scan | the whole band | baseline, scan |

Those are what the defaults (time Auto, channels Off) resolve to; an
explicit window is used as given, so "phase rms vs time in 60 s windows"
is `stat_time_window=60` with x = Time. Baselines are never mixed: each
has its own value. Phase RMS against UV distance is the standard array
phase-stability plot (scatter rising with baseline length is atmosphere;
one antenna standing out is equipment).

How it is built: every sample carries the statistic of the window it
falls in (`paint_phase_stat`), so the existing per-sample scatter
pipeline (binning, hover, flag views, colouring by axis) plots it
unchanged. Samples sharing a window land on the same point. "Phase rms vs
time" in a scatter equals the Baseline x Time raster, one point per cell;
a test checks that.

**No scatter gear-tab controls yet.** Slope removal and the two windows
for a scatter come from the constructor / task arguments (`detrend`,
`stat_time_window`, `stat_chan_window`) and cannot be changed from the
GUI; raster panels keep their own per-panel controls. The scatter's title
does not yet say which window was used.

## Conditional slope removal (2026-10-06, against `ec5809d`)

Found on TW Hya (Darrell, Phase RMS against Channel): where each window is
the few integrations of a scan and the samples are noise, the plot filled
continuously from 0 to about 110 deg instead of sitting near 104.

Cause: the slope search always returns *a* slope, and on noise it returns
whichever one lines the noise up best; removing it removes real scatter.
Pure noise, slope removal on, before the fix (median, deg):

| Samples in window | 3 | 10 | 20 | 50 |
|---|---|---|---|---|
| Before | 37 | 67 | 80 | 91 |
| Slope kept (reference) | 78 | 90 | 94 | 97 |

Fix: a slope is removed only where the data show one. After the slope is
taken out, the phasors must line up better than searched noise would: mean
resultant length at least `sqrt((ln n + 3) / n)`, and at least 4 samples.
Otherwise the samples are left as they are, and no slope is counted
against the degrees of freedom.

After the fix (median, deg), slope removal on:

| Samples | Pure noise | 10 deg, 3-turn slope | 45 deg + slope | 60 deg + slope |
|---|---|---|---|---|
| 4 | 81 | 8 | 92 | 89 |
| 6 | 86 | 9 | 82 | 86 |
| 10 | 89 | 10 | 43 | 85 |
| 20 | 93 | 10 | 44 | 59 |
| 50 | 97 | 10 | 45 | 59 |
| 384 | 101 | 10 | 45 | 60 |

Reading the table:

- Noise now reads the same with slope removal on as off. The shortfall
  from 104 that remains at short windows (81 at 4 samples) is the fitted
  mean phase, which the degrees-of-freedom correction only partly covers
  for wrapped phases; it is not the slope.
- A clean slope is still removed from 6 samples up.
- A slope under heavy noise needs more samples to be believed: about 20
  at 45 deg, about 50 at 60 deg. Below that it is left in and the value
  reads high. That is the trade the threshold makes; the constant (3) was
  chosen from this table, with 2 and 4 also measured.
- The whole-band case (hundreds of channels) is unchanged.

## Scatter controls (slice 4, 2026-10-06)

Each scatter gear tab now has the raster's three controls: *Phase slope*,
*Time window*, *Channel window*. Per panel, read when Plot is pressed. A
scatter that has not been given its own value shows and uses the
constructor's. The scatter title now says how the statistic was taken:
"Phase RMS XX  vs  Time  (slope removed, whole band)".

**Bug fixed:** the scatter's y-axis label was set once, from its first
quantity at construction, so after changing Y in the GUI it still said
"Amplitude" (over a Phase RMS plot in Darrell's screenshots; over anything
else too). It now follows the first layer's quantity.

## Status-area help (2026-10-06)

While the pointer is over one of these controls, the status area shows
what it does and when to use it, the same way the sidebar's inputs do:
raster *Averaging*, *Phase slope*, *Time window*, *Channel window*; the
scatter's three; and the Antenna table's *Either end / Both ends* switch.
No special widget class is needed: `VisibilityPlotter._attach_hint(widget,
name)` attaches `self._hint_<name>` to any Bokeh widget, and adding help
to another control is one hint Div in `_build_status_bar` plus one call.

## Browser session 2026-10-06 (Darrell): four findings

1. **Status-area help did not appear for the dropdowns.** My claim that
   no special widget was needed was wrong. Only cubevis's `EvTextInput`
   turns DOM mouse-enter / mouse-leave into the `MouseEnter` /
   `MouseLeave` model events the help listens for; a stock `Select`,
   `DataTable`, `CheckboxGroup` or `RadioButtonGroup` never emits them.
   So the help was inert on those, as `_focus_blur` had always been for
   the Field dropdown and the SPW table. My first answer, a Bokeh
   `description` tooltip, put a second kind of help in the application
   and was removed.
   **Fix:** a new wrapper model, `EvHover` (`cubevis/bokeh/models/
   _ev_hover.py`, `cubevisjs/src/bokeh/models/ev_hover.ts`): it holds one
   child and triggers the two events when the pointer crosses it, with
   `Tip`'s layout handling and no tooltip. `VisibilityPlotter._hover(
   widget, name)` wraps a control and wires `_hint_<name>`; the wrapper
   goes in the layout, the control itself stays what the Plot code reads.
   Wrapped now: raster Averaging, Phase slope, Time window, Channel
   window; the scatter's three; the Either end / Both ends switch; the
   Antenna, Baseline and SPW tables; Correlation; and Field (title and
   dropdown together). Showing one hint hides the others.
   The cubevisjs bundle was rebuilt (`bokeh build`, Bokeh 3.10.0) and, as
   with the previous bundle, the same file placed in all five
   `__js__/bokeh-3.x` directories. The build reports one existing type
   error, `visibility_raster.ts:63` (`static override __name__`), which
   does not stop the output. **The browser side is untested here.**
2. **Phase RMS vs Channel piled up to 130 deg.** Values that high need
   windows of two or three samples, so the gap rule was cutting scans into
   pieces. A scan is now a run of equal `scan_name` (or `scan_number`)
   labels when the data carry them; the gap rule is the fallback.
   Not re-checked on TW Hya.
3. **Baseline x Correlation raster failed** with a numpy ufunc error. The
   option has been in the axis lists since the first commit but the
   backend reduces one polarization at a time, so there is no correlation
   dimension to plot. It now says so ("Correlation is not available as a
   raster axis yet"). Not implemented.
4. **Black text on the dark sidebar** ("Changes apply when you press
   Plot", and the notes under the Antenna and Baseline tables): plain Div
   text had no colour rule in the shared widget stylesheet. Added to the
   dark and light sheets.

## Titles on two lines (2026-10-07)

With the statistic's settings in it, a default title no longer fitted
above a side-by-side panel. A default title longer than 64 characters is
now broken at whichever of its double-space joins leaves the two lines
most nearly equal (never straight after "vs"), and shown at 11px instead
of 13px:

    Phase RMS XX, Phase RMS YY  vs  UV Distance
    (slope removed, per scan x whole band)

`visibility_plot.wrap_title` / `title_font_size`; applied in both panels'
`_effective_title` and, for the font size, at figure creation and in the
plot-response handler. A title supplied by the caller is never wrapped.
**Not seen in a browser:** that Bokeh draws the line break in a plot title
is my understanding of Bokeh 3, not something tested here.

## Cost

A numpy kernel applied block by block through `xr.apply_ufunc`, lazy on
dask input; windows are a loop inside the kernel. Measured in this
sandbox:

| Case | Time |
|---|---|
| Amplitude, 200 x 45 x 512 samples | 2.6 s |
| Phase RMS, same data, slope removed | 2.6 s |
| Waterfall 600 x 2048, per scan | 0.3 s |
| Waterfall 600 x 2048, 60 s x 64 ch | 0.6 s |
| Waterfall 600 x 2048, 20 s x 16 ch (7680 windows) | 4.0 s |

(Slice 1 measured 12 s for the second row; slice 2 also rechunks the
batch dimensions into larger blocks, which removed most of that.)

Memory: the windowed time / frequency dimensions must be whole in each
block, and the batch dimensions are rechunked to keep a block under 8
million samples. So memory no longer grows with the number of baselines,
but one baseline's worth of the windowed dimensions must fit; for
Baseline x Channel that is the full selected time range times the
channels.

## Limits

1. **Multiple SPWs on Baseline x Time.** `_raster_merge` keeps the first
   non-NaN value where partitions overlap a cell, so the statistic shown
   is the first selected SPW's. True of every quantity on that view.
2. **Windows are blocks, not sliding.** Values step at window edges.
3. **Gap-based scans** (above).
4. **No presets** for the scatter views yet.
5. **Short windows of noisy data** read somewhat low and scattered even
   after the slope fix (see "Conditional slope removal").

## Verified

- `test_raster_phase_stats.py`: 322 passed (conditional slope removal,
  scatter settings, y label, titles and help wiring added), both backends, including the
  scatter form through the real backends and a real plotter on simulated
  data (90 of them for windows and pooled baselines: painted back, pooled, never across a gap, auto, flags,
  chunking, parity, panel and title). Slice 1: known answers,
  wrap, slope removal (steep delays, rate, scan gap, descending frequency,
  datetime64 time, per-cell slopes), flags and padding, too-few-samples,
  independence of chunking, laziness, MSv2 == MSv4, per-panel plumbing,
  titles.
- `test_raster_averaging.py`: 121 passed; `test_cursor_readout_reset.py`:
  4 passed (both unchanged).
- Rest of `tests/manual/visplot`, with and without: identical (1000
  passed; the same 11 failures and 3 collection errors that need packages
  or data absent in the sandbox; 679 skipped for lack of a real MS).
- `scripts/sync_layers --check` clean after regeneration.

## Verified in the browser (Darrell, 2026-10-05)

TW Hya, Baseline x Time: Phase RMS with slope kept and slope removed side
by side, and Phase RMS next to Coherence. The two dropdown entries, the
*Phase slope* control and the titles work. As expected on calibrated
data, keeping or removing the slope makes little difference; calibrator
scans read low rms and high coherence (up to about 0.83), target scans
read about 100 deg, i.e. noise per sample.

## Not verified

- MSv4 on real data; the remote path; memory on a long Baseline x Channel
  selection.
- How colormap scaling defaults suit these quantities; nothing was tuned
  (the screenshots used eq_hist).

## Not verified (slice 2)

- The two window controls in a browser (wired exactly like *Phase slope*).
- Windows on real data: whether gap-based scans match the MS's scans on
  TW Hya, and how the blocky waterfall reads in practice.

## Final slice (2026-10-07): presets and DIFF quantities

Presets `phaserms-time`, `phaserms-freq`, `phaserms-uvdist` (toolbar
buttons and `preset=`; underscores accepted). Each pairs a Phase RMS
raster with the Phase RMS scatter and resets slope removal and both
windows, in both gear tabs, to their defaults.

`AMP_VDIFF` and `PHASE_DIFF` raster quantities, in
`data/_raster_diff.py`. Choices made, all in that module's docstring:

- The reference is the vector mean of the *other* samples in the window
  (leave-one-out), per baseline, channel and polarization, along time.
  Including the sample biases every value low by 1 - 1/n and lets a
  strong outlier hide by pulling the mean toward itself.
- The window is the raster's *Time window* control; Auto and Off both
  mean one scan. Blocks, not a sliding buffer (AIPS uses a rolling
  buffer centred on the sample).
- Reduced to the displayed axes with a plain mean.
- Known property: where a window has no coherent mean (pure noise, or a
  phase that winds through a full turn inside the window), `PHASE_DIFF`
  reads above 90 deg rather than at 90, because the leave-one-out
  reference of a set that sums to about zero points away from each
  sample. Such data have no reference phase to differ from; `PHASE_RMS`
  is the quantity for them.
- Raster only. A scatter form would need the painted-back machinery of
  `paint_phase_stat`; not requested.

Verified: `test_hrs_presets_diff_phase.py` (56 tests): known answers,
leave-one-out, wrap, scans, seconds windows, flags, lone samples,
chunking, both backends through a real plotter on simulated data.
Not verified: real data; nothing was seen in a real session (the GUI
was rendered headlessly as a static page, without a Python connection).

## Remaining after H2

Nothing planned. Possible later: sliding windows; DIFF as scatter Y.
