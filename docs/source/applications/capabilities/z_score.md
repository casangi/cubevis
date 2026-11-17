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

(cap-zscore-calc)=

## How it is calculated

For each reference population (below), take the medians of the real and
imaginary parts separately, $m_R$ and $m_I$, of its unflagged samples.
For a sample $V$:

$$r = \sqrt{(\mathrm{Re}\,V - m_R)^2 + (\mathrm{Im}\,V - m_I)^2},
\qquad
Z = \frac{r}{\operatorname{median}(r)}\,\sqrt{2\ln 2}$$

where $\operatorname{median}(r)$ is taken over the reference population.

**What the numbers mean.** If the samples are complex Gaussian noise
about a constant value, $Z$ follows a Rayleigh distribution with
$P(Z > t) = e^{-t^2/2}$; the factor $\sqrt{2\ln 2}$ is chosen so that
this holds. So:

- the median $Z$ is 1.18;
- $Z > 3$ happens for about 1 % of samples;
- $Z > 3.5$ (the default cutoff of the Z-Score flag filter, after
  Iglewicz and Hoaglin) happens for about 0.2 %.

Because the reference uses medians, a minority of bad samples does not
move it.

**The reference population:**

- **On a scatter plot** it is one baseline and one correlation, over the
  whole current selection: all times, channels and spectral windows
  selected.
- **On a raster** it is one baseline, one correlation and one spectral
  window. A sample can therefore score differently on a raster than on a
  scatter plot when several spectral windows are selected.
- **In either case** the score depends on the selection: narrowing the
  selection changes the reference, and with it every score.
- **Blank scores.** A population with no spread, such as a single
  sample, gives no score.

**A raster cell** shows the *largest* $Z$ among its samples, so a single
outlier is not averaged away. The largest of many samples is naturally
higher than one sample's value. For that reason, the Z-Score flag filter
working on whole cells raises its cutoff with the number of samples $N$
per cell, so that a cell of pure noise is taken no more often than a
single sample would be. The per-sample tail probability $p$ is set so
that $1-(1-p)^N = e^{-c^2/2}$, where $c$ is the per-sample cutoff (a
Šidák correction). With 384 channels per cell, a cutoff of 3.5 becomes
about 4.9.

## From Python

```python
visplot(vis="obs.ms", preset="zscore")
visplot(vis="obs.ms", scatter_x="TIME", scatter_y="Z_SCORE")
```

---

Coming from plotms or AIPS? See {ref}`cap-from-plotms` and {ref}`cap-from-aips`.
