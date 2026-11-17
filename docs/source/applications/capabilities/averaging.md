# Averaging

**Status:** available. Averaging by a *number of integrations* has been
requested and is not yet available.

## What it is

Averaging works at three levels:

1. **How samples are averaged:**
   - **Vector**: average the complex visibilities, then take amplitude
     and phase. This is the default.
   - **Scalar**: average the amplitudes. Phase uses the circular mean.
2. **What is averaged together:**
   - **On a raster**, each cell combines the samples it covers along the
     dimensions that are not shown. For example, a Baseline × Time cell
     averages over channels.
   - **On a scatter plot**, points can be averaged over time (off, one
     scan, or a length in seconds) and over channels (off, a number of
     channels, or all channels in each spectral window).
3. **How baselines are combined**, for a raster cell that covers several
   baselines (any raster without Baseline as an axis):
   - **Mean** of the baselines' amplitudes: the general picture. A
     problem on a few baselines is diluted.
   - **Median**: what most baselines share, unaffected by a few bad ones.
   - **Maximum**: interference or a bad antenna shows even if only one
     baseline has it. A feature that appears with Maximum but not with
     Median is on only a few baselines.
   - **Coherent**: the visibilities of all baselines are added before the
     amplitude is taken. This is meaningful only for a calibrated point
     source; on anything else the baselines cancel and the plot reads
     low.

   Vector or scalar averaging still applies to the samples within each
   baseline.

## What it is for

- **Vector averaging** is a test in itself. Amplitude drops where the
  samples do not add up: noise, an uncalibrated phase, a delay across the
  averaged channels. **Scalar averaging** shows the amplitude whatever
  the phase does.
- **Scatter averaging** gives the standard reduced views: a spectrum per
  scan (as AIPS POSSM, averaged over time) or a time series (as AIPS
  VPLOT, averaged over channels).
- **Combining baselines** gives one image for many baselines, as in AIPS
  FTFLG.

## How it behaves

- Flagged samples are left out of every average.
- Time averaging starts at the beginning of each scan and never crosses a
  scan boundary.
- A flag box on an averaged plot flags every sample behind the points or
  cells it encloses.
- Each plot has its own averaging settings, and the raster's title names
  the averaging in use.
- The windows used by the [phase-stability](phase_stability.md) and
  [difference](vector_difference.md) quantities are separate from the
  averaging. They set what a statistic is measured over, not what a cell
  covers, so they do not reduce the time resolution of the plot.

## From Python

```python
visplot(vis="obs.ms", averaging="scalar")
visplot(vis="obs.ms", layout="one", kind="scatter", scatter_x="FREQUENCY",
        scatter_y="AMPLITUDE", scatter_avg_time="scan")       # spectrum per scan
visplot(vis="obs.ms", scatter_x="TIME", scatter_avg_chan="all",
        scatter_avg_time=30)                                   # 30 s time series
visplot(vis="obs.ms", raster_y="TIME", raster_x="CHANNEL",
        baseline_combine="max")
```

## Limits

- **Averaging by number of integrations is not yet available.** Time
  intervals, both for scatter averaging and for the statistics windows,
  are given as "off", "scan" or a length in seconds. AIPS counts in
  integrations, and since integration times differ between data sets, a
  count of integrations is often the more natural unit. This has been
  requested and is planned.
- Scatter averaging applies to Amplitude, Phase, Real and Imaginary only.
