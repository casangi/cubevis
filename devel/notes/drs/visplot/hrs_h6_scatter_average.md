# HRS H6, slice 1: averaged scatter points

*2026-10-09. Built against `main` at `84ae4bb`. Plan:
`hrs_visplot_plan.md` (H6). Agreed with Darrell: averaging controls of
their own, separate from the Phase RMS windows; a flag box on averaged
points flags the samples behind them; the Spectrum / Time series presets
(slice 2) may set both panels and the Over / Under layout.*

## What it is for

A commissioning spectrum (amplitude and phase against frequency, a
scan's integrations averaged: AIPS POSSM, plotms with time averaging) or
time series (amplitude and phase against time, the band averaged)
cannot be read sample by sample: the noise hides the shape. The scatter
panel can now draw one point per baseline and correlation per window.

## What the user sees

Scatter gear tab, under the X / Y axis choices:

- **Average over time**: Off (default) / Scan / 10 s ... 10 min. Windows
  start at each scan and never cross a scan boundary.
- **Average over channels**: Off (default) / 2 ... 256 channels / All
  (one point per spectral window).
- **Averaging**: Vector (default) / Scalar. Vector averages the complex
  visibilities and takes amplitude and phase from the mean (amplitude
  drops where the samples do not add up); scalar averages the
  amplitudes. Phase is the mean direction either way (of the vector mean,
  or of the unit phasors); Real and Imaginary are the means.

The title says it: "Amplitude XX, Amplitude YY vs Channel (vector avg:
scan)". One help text in the status area covers the three controls.
Constructor / task arguments: `scatter_avg_time`, `scatter_avg_chan`;
the vector / scalar start value is the existing `averaging` argument
(now documented as raster and scatter).

- Only Amplitude, Phase, Real and Imaginary are averaged. Phase RMS,
  Coherence, Z-Score, ... are drawn as before.
- X of a point is the mean X of its samples (mean time, frequency, UV
  distance, ...). Scan, field, baseline and spectral window (colouring,
  hover) are those of the window, which never mixes scans.
- Flagged samples are left out of the averages; in "Show in colour" or
  "Show flagged data" the coloured points are averages of the samples in
  that state.
- **Flagging**: a box takes every sample behind the points it encloses
  (for Unflag, the flagged samples behind the points of the flagged
  view). The count and the record are of those samples. Filters other
  than "All selected" are refused on an averaged scatter with a message
  (they read the data themselves and would judge samples that are not
  on the screen); switch averaging off to use them.

## How it works

`data/_scatter_average.py`: group the per-sample rows into windows
(`group_rows`: antenna pair, spectral window, time window, channel
window) and average (`average_frame`, with `numpy.bincount`).

It runs on the cached raw frames (`XArrayReader.averaged_view`), after
the flag view is applied: the frames of Real and Imaginary for the same
correlation hold the same samples in the same order (checked on every
use, `aligned`; if not, the samples are drawn). So:

- changing the averaging reads nothing from the data (the averaging
  fields and `averaging` are left out of the raw-frame cache key);
- the remote path needs nothing new (it all happens where the data are);
- the flag box resolves on the same rows the panel draws
  (`flag_engine._scatter_box_from_frames`).

The panel stamps its settings on its selection (`VisibilityScatter.
_with_stat_settings`), and a flag request now uses that stamped
selection too (it used the plotter's, so a scatter with its own Phase
RMS windows was flagged against unwindowed values).

## Fixed on the way

**The scatter and raster X axes were tied.** When the constructor gave
the raster and scatter the same X axis (`raster_x == scatter_x`), the
two figures shared one Range model, for good. Changing the scatter's X
with Plot (e.g. Channel to Time) then set the raster's Channel axis to
time values: the raster went blank. Found live while testing a time
series. Now no figures share a Range; a pan or zoom along X is copied to
the other panel on screen while both X axes have the same label
(quantity and units) -- the "spot it on the raster, confirm on the
scatter" link, but only while it is meaningful (`_X_SYNC_JS`).

## Verified

- `tests/manual/visplot/test_scatter_average.py`, 34 tests:
  - grouping: windows per scan, per length from the scan start, per
    channel block and all channels; a gap ends a window without scan
    information; vector and scalar Amplitude / Phase / Real / Imaginary
    against numpy, sample counts and mean X;
  - both backends on a simulated MS (3 scans, 16 channels, amplitudes
    and phases varying with time, channel and baseline): a spectrum per
    scan (vector and scalar) and a time series over all channels against
    the visibilities read directly; Phase RMS not averaged; changing the
    averaging re-reads nothing;
  - flagging: a box round one averaged point flags exactly that
    baseline, channel and scan's samples; samples flagged elsewhere drop
    out of the averages; other filters refused; an unaveraged scatter is
    as before;
  - plotter: title, gear controls, help, a Plot message changing the
    averaging, constructor validation;
  - the X link: no shared ranges; the link copies between visible
    panels with the same X label only (run under node).
- Live in headless Chromium against a running plotter on TW Hya 3c279:
  a scan-averaged spectrum; switching to a channel-averaged time series
  with Plot; a box on it flagged 39,552 samples behind the points.

## Not verified

- Speed on HRS-sized data: averaging is a pandas groupby over the
  cached samples of the layer (about 0.1 s per million rows here).
