(cap-averaging)=

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

(cap-averaging-calc)=

## How it is calculated

Only unflagged samples are averaged, and they are **not weighted**: the
data weights are not used. The vector and scalar averages of one cell or
one point are given in {ref}`cap-quantities-calc`.

**Several baselines in one raster cell.** Each baseline's samples are
first averaged on their own, by vector or scalar averaging. The
baselines are then combined:

| `baseline_combine` | Amplitude | Phase |
|---|---|---|
| `"mean"` | the mean of the baselines' amplitudes; each baseline counts once, however many samples it has | the mean direction of the baselines' phases, each baseline counted once |
| `"median"` | the median of the baselines' amplitudes | as for `"mean"` |
| `"max"` | the largest of the baselines' amplitudes | as for `"mean"` |
| `"coherent"` | all samples of all baselines in one average, as for a single baseline | the same |

The other quantities combine baselines as follows:

- **Real and Imaginary** are the mean over all samples of all baselines.
- **Amp V Diff and Phase Diff** are the mean over all samples. With
  `"max"` or `"median"`, each baseline is averaged first and the
  largest, or the median, baseline is shown.
- **Z-Score** is always the largest value in the cell.
- **Phase RMS and Coherence** are measured baseline by baseline and then
  pooled (see {ref}`cap-phasestab-calc`).

**Scatter averaging.** Points are averaged within groups of one
baseline, one correlation, one spectral window, one time window and one
channel window.

- **Time windows:**
  - `"scan"` gives one window per scan.
  - A number of seconds gives consecutive windows of that length,
    starting at the beginning of each scan. The last window of a scan
    may be shorter.
  - A window never spans two scans. Without scan information, a window
    never spans a gap in time.
- **Channel windows:**
  - A number of channels gives blocks of that many channels, counted
    from the first channel of the spectral window.
  - `"all"` gives the whole spectral window.
- **The averaged values:**
  - Amplitude and Phase are averaged by vector or scalar averaging, as
    for a raster cell.
  - Real and Imaginary are means.
  - Other Y quantities are not averaged.
- **The point's X value** is the mean of its samples' X values: mean
  time, mean frequency, mean UV distance.
- **Other point information:** the scan, field and other details used
  for coloring and the cursor readout are those of the window's first
  sample. A window never mixes scans, so they are the same for all its
  samples.

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

(cap-averaging-limits)=

## Limits

- **Averaging by number of integrations is not yet available.** Time
  intervals, both for scatter averaging and for the statistics windows,
  are given as "off", "scan" or a length in seconds. AIPS counts in
  integrations, and since integration times differ between data sets, a
  count of integrations is often the more natural unit. This has been
  requested and is planned.
- Scatter averaging applies to Amplitude, Phase, Real and Imaginary only.
- **Rasters are not averaged in time.** A raster with a Time axis shows
  every integration; AIPS's smoothing time has no equivalent yet.
- **No spectral smoothing.** Channels can be averaged on scatter plots,
  but not smoothed (AIPS SPFLG's Gaussian smoothing).

---

Coming from plotms or AIPS? See {ref}`cap-from-plotms` and {ref}`cap-from-aips`.
