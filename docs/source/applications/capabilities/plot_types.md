# Rasters, scatter plots and layouts

**Status:** available.

## What it is

`visplot` draws two kinds of plot. The same application handles both,
so they share one selection, one set of flags and the same conventions:

- **Raster:** a 2-D image of a quantity (shown as color) against two of
  Time, Baseline, Channel, or Antenna 1 × Antenna 2. This is the AIPS
  TVFLG / SPFLG and CASA `msview` style of display.
- **Scatter:** a quantity against UV distance (in metres or
  wavelengths), Time, Frequency, Channel, U or V. Plotting U against V
  gives UV coverage. This is the CASA `plotms` and AIPS VPLOT / UVPLT
  style. Points are drawn as density, so millions of samples remain
  readable.

They can be arranged as:

- **one plot**,
- **two plots**, side by side or one above the other, each a raster or a
  scatter with its own settings, or
- **a grid**: one plot repeated for each baseline, antenna, spectral
  window, field, scan or correlation in the selection, page by page, as
  `plotms`'s `iteraxis` does. Two iteration axes can be combined, such as
  each antenna's spectral windows in turn.

## What it is for

A raster shows *where* a problem is: which baseline, when, at which
channel. A scatter shows *how much* and *what shape*. Seen side by side,
the raster finds a problem and the scatter confirms it, or the other way
round. The grid is for comparing many baselines or antennas at a glance.

## How it behaves

- **Real coordinates.** Raster cells are drawn where their coordinates
  are. A gap between scans or between spectral windows is left blank,
  not closed up, so rasters and scatter plots of the same data line up.
- **Baseline order.** A Baseline axis can be ordered by baseline number,
  or by length (shortest first). Ordered by length, trouble that grows
  along the axis is about distance (atmosphere, a resolved source).
  Trouble in scattered columns is about particular antennas.
- **One correlation per raster.** A raster shows one correlation or the
  mean of the parallel hands. Two rasters on RR and LL show a problem in
  one hand only.
- **Layers.** A scatter can overplot several layers (for example several
  correlations), each in its own color. A layer can instead be
  colored by scan, field, antenna, baseline or spectral window, or by
  [Z-Score](z_score.md).
- **Linked axes.** Pan and zoom are shared between the panels, and while
  both plot the same X quantity their X ranges move together. Zooming a
  raster re-samples the data already read; it does not re-read the data.
- **Time** is shown in UTC by default, using the AIPS-style
  `d/hh:mm:ss` when the data cross midnight. It can be shown as elapsed
  time instead.
- **Grid color range.** The plots on a page share one color range by
  default. Alternatively every plot in the grid can share one range, so
  that pages compare, or each plot can have its own.
- Dark and light themes are available.

## From Python

```python
visplot(vis="obs.ms", layout="side", raster_y="BASELINE", raster_x="TIME",
        raster_qty="AMPLITUDE", scatter_x="UVDIST", scatter_y="AMPLITUDE")

visplot(vis="obs.ms", layout="grid", iterate_by="antenna,spw",
        gridrows=3, gridcols=3, gridcolor="page")

visplot(vis="obs.ms", baseline_order="length", time_labels="elapsed",
        theme="light")
```

`panels=[{...}, {...}]` gives each panel's settings separately.

## Limits

- At most two independently configured panels; the grid repeats a single
  plot.
- Correlation cannot be a raster axis.
- Elevation, hour angle, azimuth and parallactic angle are not available
  as axes.
