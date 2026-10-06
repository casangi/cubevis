# HRS H2: phase rms and coherence (slices 1-3: raster quantities, windows, scatter)

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
4. **Scatter windows are not adjustable in the GUI** (above), and there
   are no presets for these views yet.

## Verified

- `test_raster_phase_stats.py`: 253 passed, both backends, including the
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

## Next slices

- 4: scatter gear-tab controls for slope and windows; the window in the
  scatter title; presets (toolbar buttons) for phase rms vs time, vs
  frequency and vs UV distance.
- 5: difference-from-running-mean displays (AMP V DIFF, PHASE DIFF).
