# HRS H2: phase rms and coherence (slice 1: raster quantities)

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

## Cost

A numpy kernel applied block by block through `xr.apply_ufunc`, lazy on
dask input. Measured on 200 x 45 x 512 samples in this sandbox: Amplitude
3.4 s, Phase RMS 12.4 s with slope removal, 10.9 s without (about 3.5x).
A first version built from lazy xarray operations was correct but took
47 to 70 s on the same data and was replaced.

Each block must hold a cell's whole window, so the reduced dimension is
rechunked to one chunk. Cheap for Baseline x Time. For Baseline x Channel
every block spans the full selected time range, so memory grows with the
time range selected (Z-Score's per-baseline median has the same need).

## Limits of this slice

1. **The window is the undisplayed dimension, whole.** No sliding or
   per-scan windows yet. Consequences:
   - Baseline x Channel takes the statistic over the entire selected time
     range, across scans and fields. Select one field or scan for a
     meaningful number.
   - A single-baseline Time x Channel waterfall has one sample per cell,
     so both quantities are blank (NaN) there.
2. **Multiple SPWs on Baseline x Time.** `_raster_merge` keeps the first
   non-NaN value where partitions overlap a cell, so the statistic shown
   is the first selected SPW's, not all SPWs combined. That is how every
   quantity already behaves on that view; it matters more here. Select
   one SPW to be sure which is shown.
3. **Raster only.** The collapsed line plots (rms vs time, vs frequency),
   rms vs baseline length, and presets are later slices.

## Verified

- `test_raster_phase_stats.py`: 131 passed, both backends: known answers,
  wrap, slope removal (steep delays, rate, scan gap, descending frequency,
  datetime64 time, per-cell slopes), flags and padding, too-few-samples,
  independence of chunking, laziness, MSv2 == MSv4, per-panel plumbing,
  titles.
- `test_raster_averaging.py`: 121 passed (unchanged).
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

## Next slices

- 2: windows along the displayed axes (time length, channel count,
  per-scan), which also gives these quantities on the waterfall. Needs a
  check that the flag overlay copes with an aggregate coarser than the
  data.
- 3: collapsed scatter views (rms vs time, vs frequency, vs baseline
  length) and presets.
- 4: difference-from-running-mean displays (AMP V DIFF, PHASE DIFF).
