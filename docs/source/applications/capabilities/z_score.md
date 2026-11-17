(cap-zscore)=

# Z-Score

**Status:** available on rasters and scatter plots, and as a flag
filter.

## What it is

A robust statistical score of how unusual each sample is *for its own
baseline*, in the style of CASA's `rflag`. It is computed jointly from
the real and imaginary parts, against a per-baseline reference that uses
the median and the median absolute deviation (MAD). It is always 0 or
greater: it measures how anomalous a sample is, not in which direction.

## What it is for

It finds outliers without one threshold for every baseline. A short
baseline on a bright source and a long, noisy one are each judged
against themselves. On a Baseline × Time raster, isolated bright cells
are outliers in time.

## How it behaves

- **Already-flagged samples** are left out of the reference population,
  so existing flags do not distort it.
- **On a raster**, a cell that covers several samples shows the largest
  score among them, so a single outlier is not averaged away.
- **On a scatter plot** it can be plotted as the Y value, or used to
  color an amplitude or phase layer, so the outliers stand out in an
  ordinary plot.
- **As a flag filter**, a flag box then takes only the samples above a
  chosen score (see [](flagging.md)).

## From Python

```python
visplot(vis="obs.ms", preset="zscore")
visplot(vis="obs.ms", scatter_x="TIME", scatter_y="Z_SCORE")
```

---

Coming from plotms or AIPS? See {ref}`cap-from-plotms` and {ref}`cap-from-aips`.
