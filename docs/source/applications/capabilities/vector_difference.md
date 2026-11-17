# Difference from the mean: Amp V Diff and Phase Diff

**Status:** available on rasters. Not yet on scatter plots.

## What it is

Two raster quantities, the counterparts of the AIPS TVFLG / SPFLG
display modes of the same names:

- **Amp V Diff**: |V − M|, the *amplitude of the vector difference*
  between a sample V and the vector mean M of the other samples in its
  time window.
- **Phase Diff**: |arg(V M\*)|, how far the sample's phase is from the
  phase of that mean, in degrees, from 0 to 180.

M is taken along time, separately for each baseline, channel and
polarization.

## What it is for

Amplitude and phase show what is there; these two show what *changed*.
A steady source, the bandpass shape or a constant phase offset all
subtract out. What is left is short-lived: an interference burst, a
phase jump, one bad integration. The AIPS cookbook's advice for TVFLG
is the same: amplitude finds long-lived problems, such as bad scans, and
the vector difference finds short excursions from the norm.

## How it behaves

- **The window** that M is taken over is one scan by default, or a
  length in seconds. It uses the same *Time window* setting as
  [Phase RMS](phase_stability.md). A window never spans a scan boundary.
- **The sample is left out of its own mean.** If it were included, every
  difference would read low, and a single strong outlier could pull the
  mean toward itself and hide. A window with fewer than two unflagged
  samples has no mean to compare with, and is left blank.
- **Fixed blocks, not a sliding buffer.** The mean is taken over
  consecutive blocks, so the reference steps at the window edges. AIPS
  uses a rolling buffer centred on each sample. The difference matters
  only when the source itself changes on the scale of one window.
- **Cells.** Where a raster cell covers several samples (for example all
  channels on a Baseline × Time raster), the per-sample values are
  averaged. Both quantities are non-negative, so nothing cancels.
- **Several baselines in one cell.** Where a cell covers several
  baselines, they can be combined as the mean, median or maximum (see
  [](averaging.md)). Maximum keeps a problem on one baseline visible.
- **Colors.** Phase Diff runs from 0 to 180° and does not wrap, so it
  uses the ordinary colormap, not the cyclic one used for Phase.

## From Python

```python
visplot(vis="obs.ms", raster_y="BASELINE", raster_x="TIME",
        raster_qty="AMP_VDIFF")                   # vs scan mean
visplot(vis="obs.ms", raster_y="TIME", raster_x="CHANNEL",
        raster_qty="PHASE_DIFF", stat_time_window=120)   # vs 2-minute mean
```

## Limits

- **Rasters only.** They are not yet offered on scatter plots.
- **The window is set in seconds or scans**, not in a number of
  integrations (requested; see [](averaging.md)).
