(cap-phasestab)=

# Phase stability: Phase RMS and Coherence

**Status:** available on rasters and scatter plots.

## What it is

Two measures of how steady the phase is over a group of samples:

- **Phase RMS**: the RMS scatter of phase about its mean direction, in
  degrees. It is 0 for a perfectly stable phase. For pure noise it is
  close to 104° in long windows, and lower in short ones (see below).
- **Coherence**: the vector-averaged amplitude divided by the
  scalar-averaged amplitude, from 0 to 1. This is the fraction of the
  signal that survives averaging: 1 when the samples add up coherently,
  about $0.9/\sqrt{N}$ for noise.

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

(cap-phasestab-calc)=

## How it is calculated

**One window.** Take the $n$ unflagged samples $V_k$ of one baseline in
one window.

- **Phase RMS** uses the unit phasors $u_k = V_k/\lvert V_k\rvert$, so
  every sample counts equally whatever its amplitude. A sample with
  zero amplitude has no phase and is left out. With
  $\bar u = \frac{1}{n}\sum u_k$ and $r_k = \arg(u_k\,\bar u^{*})$ (each
  residual wrapped into (−180°, 180°]):

  $$\text{Phase RMS} = \sqrt{\frac{\sum_k r_k^2}{n - p}}$$

  where $p$ is the number of fitted parameters: 1 for the mean phase,
  plus 1 for each phase slope that was removed. Dividing by $n - p$
  rather than $n$ keeps short windows from reading low.

- **Coherence** uses the visibilities themselves:

  $$\text{Coherence} = \frac{\left\lvert\sum_k V_k\right\rvert}{\sum_k \lvert V_k\rvert}$$

  For small phase noise $\sigma$ (in radians), Coherence ≈
  $e^{-\sigma^2/2}$.

A window needs at least two samples, and more than $p$ for Phase RMS.
Otherwise it contributes nothing.

**Many windows or baselines in one cell.** When a cell covers several
time windows, or several baselines, each is measured about its *own*
mean phase (and slope). The results are then pooled by adding the
numerators and the denominators:

$$\text{Phase RMS} = \sqrt{\frac{\sum_w \sum_k r_k^2}{\sum_w (n_w - p_w)}},
\qquad
\text{Coherence} = \frac{\sum_w \left\lvert\sum_k V_k\right\rvert}{\sum_w \sum_k \lvert V_k\rvert}$$

So a phase jump between scans, or a phase difference between baselines,
does not count as scatter.

**Which samples a window holds:**

- The *time window* is a scan, or consecutive lengths of so many
  seconds from the start of each scan.
- The *channel window* is a number of channels.
- Windows never span a gap between scans.
- The default (`"auto"`) for time is no window when Time is a plot axis,
  and one scan when it is not.
- Along a displayed axis, every cell in a window shows the window's
  value, so the plot looks blocky at the window size.

**Removing a phase slope** (`detrend=True`, the default):

- **Where.** A straight line in phase is searched for along time (a
  residual rate) and along frequency (a residual delay), separately in
  each window with at least 3 samples.
- **How.** It is first estimated coarsely, from the mean products of
  samples a fixed number of steps apart. That needs no phase unwrapping
  and is not biased by the ±180° wrap. The estimate is then refined by a
  least-squares straight-line fit to the remaining phases.
- **When it is kept.** The slope is removed only if the phases then line
  up better than noise would by chance. The test is the length of the
  mean unit phasor, against a threshold of about
  $\sqrt{(\ln n + c)/n}$. So on pure noise no slope is removed.
- **Gaps.** Pairs of samples are taken by their real time or frequency
  separation, so gaps between scans do not bias the estimate.
- **Limits.** It is not a fringe fit. It needs enough signal for
  neighbouring phases to be related (it recovers the true scatter to
  within a few percent for up to about 45° of noise per sample). It
  cannot follow a slope steeper than half a turn per sample.

**Values on pure noise** depend on the number of samples in a window:

| Samples per window | 8 | 16 | 64 | 256 | many |
|---|---|---|---|---|---|
| Phase RMS | ≈ 87° | ≈ 92° | ≈ 98° | ≈ 101° | → 104° ($180°/\sqrt{3}$) |
| Coherence | ≈ 0.32 | ≈ 0.22 | ≈ 0.11 | ≈ 0.05 | ≈ $0.9/\sqrt{n}$ |

These values are measured with the code on simulated noise. They are
useful for telling whether a baseline is showing any signal at all.

## From Python

```python
visplot(vis="obs.ms", preset="phaserms-time")
visplot(vis="obs.ms", raster_y="BASELINE", raster_x="CHANNEL",
        raster_qty="COHERENCE", stat_time_window="scan", detrend=True)
visplot(vis="obs.ms", raster_qty="PHASE_RMS", stat_time_window=60)
```

(cap-phasestab-limits)=

## Limits

- **Windows are set in seconds or scans**, not in a number of
  integrations (requested; see [](averaging.md)).
- **There is no amplitude RMS.** For uncalibrated data, the AIPS
  cookbook suggests the RMS of amplitude over time intervals; `visplot`
  measures the scatter of phase only. If you need amplitude RMS, please
  ask.

---

Coming from plotms or AIPS? See {ref}`cap-from-plotms` and {ref}`cap-from-aips`.
