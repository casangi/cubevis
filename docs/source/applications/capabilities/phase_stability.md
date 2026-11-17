# Phase stability: Phase RMS and Coherence

**Status:** available on rasters and scatter plots.

## What it is

Two measures of how steady the phase is over a group of samples:

- **Phase RMS**: the RMS scatter of phase about its mean direction, in
  degrees. It is 0 for a perfectly stable phase and about 104° for pure
  noise.
- **Coherence**: the vector-averaged amplitude divided by the
  scalar-averaged amplitude, from 0 to 1. This is the fraction of the
  signal that survives averaging: 1 when the samples add up coherently,
  near 1/√N for noise.

Both carry the same information, read two ways.

## What it is for

Phase stability over time and across the band is a core commissioning
measurement:

- **Baseline × Time:** each baseline's phase scatter across the band in
  each integration.
- **Baseline × Channel:** stability over time in each channel (scan by
  scan by default).
- **Against UV distance:** a rise with baseline length is the
  atmosphere; one high baseline points to an antenna.

## How it behaves

- **Windows.** The statistic is taken within a window that is set
  separately from what the plot shows:
  - *Time window*: automatic, off, a scan, or a length in seconds.
  - *Channel window*: off, or a number of channels.

  Automatic means one integration when Time is a plot axis and one scan
  when it is not. Windows never span a gap between scans. Longer windows
  are steadier but blur changes in time, and on the plot they show as
  blocks. Because the window is separate from the cell, measuring
  phase stability does not coarsen the time resolution of the plot or
  of flags drawn on it.
- **Phase slope.** By default a linear phase slope is removed before the
  statistic is taken: a residual delay along frequency, or a residual
  rate along time. Without this, an otherwise steady phase with a
  residual delay would read as unstable. The slope removal can be turned
  off to measure the data exactly as they are.
- **Waterfalls.** With a window set, both quantities are also available
  on a Time × Channel waterfall. There each selected baseline is
  measured on its own and the results are then combined.
- **On a scatter plot**, the X axis decides what each point is:
  - against Time: one baseline's scatter across the band in one
    integration;
  - against Frequency or Channel: one baseline's scatter over a scan in
    one channel;
  - against UV distance: one baseline's scatter over a scan.

## From Python

```python
visplot(vis="obs.ms", preset="phaserms-time")
visplot(vis="obs.ms", raster_y="BASELINE", raster_x="CHANNEL",
        raster_qty="COHERENCE", stat_time_window="scan", detrend=True)
visplot(vis="obs.ms", raster_qty="PHASE_RMS", stat_time_window=60)
```

## Limits

- **Windows are set in seconds or scans**, not in a number of
  integrations (requested; see [](averaging.md)).
- **There is no amplitude RMS.** For uncalibrated data, the AIPS
  cookbook suggests the RMS of amplitude over time intervals; `visplot`
  measures the scatter of phase only. If you need amplitude RMS, please
  ask.
