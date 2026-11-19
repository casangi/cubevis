(cap-plots)=

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
- **Grid axes.** The plots in a grid can share one axis scale, so that
  zooming one zooms them all, or each can have its own. Pages can be
  stepped through, or played in turn.
- Dark and light themes are available.

(cap-plots-calc)=

## How the picture is drawn

What is on the screen is not always every cell at full resolution.
Knowing how it is reduced matters when looking for a single bad
integration.

**Very large rasters skip rows and columns.**

- **When.** A raster has a limit of 2 million cells. Above it, `visplot`
  keeps every *k*-th row and every *k*-th column, choosing *k* for each
  axis so that the result fits and keeps its shape. For example,
  351 baselines × 6,000 integrations is 2.1 million cells and would be
  thinned.
- **What is lost.** The skipped rows and columns are **not shown at
  all**: they are not averaged into the ones that are kept. On a thinned
  raster a single bad integration or baseline can be missing from the
  picture.
- **Zooming.** When you zoom in, the visible region is read again at
  full resolution, but only for Amplitude, Phase, Real, Imaginary and
  Flag. Z-Score, Phase RMS, Coherence, Amp V Diff and Phase Diff are
  computed against more than the visible cells (a whole scan or a whole
  selection), so they stay on the thinned picture even when zoomed.
- **Seeing everything.** Narrow the selection (time range, baselines,
  spectral windows) until the raster is under the limit.

:::{warning}
The plot description page currently reports a thinned raster as
"each cell aggregates several samples". That is not what happens: rows
and columns are skipped. This, and the thinning itself, is being
changed so that rows are combined rather than skipped.
:::

**More cells than screen pixels.** When there are more cells than
pixels (for example, many integrations across a narrow plot), each pixel
shows the **mean** of the cells it covers. Phase is averaged as a
direction. A spike is therefore diluted, not lost, until you zoom in. A
pixel whose phases cancel exactly is left blank. When a cell is larger
than a pixel, it is drawn with its own value: there is no interpolation
between cells, because neighbouring baselines are not related and a gap
between scans is not data.

**Colors stay put when you zoom.** The colors are fixed to the whole
raster, not to the region shown. The same value has the same color at
every zoom, and zooming in on a quiet region does not stretch its noise
across the whole colormap.

**Scatter plots** are not thinned: every selected sample is drawn.

- In the default coloring, a pixel's color is the **number of points**
  in it (density).
- Colored by a category, each category has its own color.
- A cursor readout reports the values under the pointer.

### Axis definitions

| Axis | Definition |
|---|---|
| Time | The data's own time stamps (the integration midpoints), in UTC |
| Frequency | Each channel's frequency as stored in the data; there is no frame conversion |
| Channel | The channel's index within its spectral window |
| UV distance (m) | $\sqrt{u^2 + v^2}$ from the data's UVW, in metres |
| UV distance (λ) | UV distance in metres × the *channel's own* frequency / c. (A UV range *selection* in wavelengths uses each spectral window's centre frequency instead.) |
| U, V | From the data's UVW, in metres |
| Baseline, ordered by length | Ordered by the physical distance between the two antennas' positions, not the projected baseline length |

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

(cap-plots-limits)=

## Limits

- Rasters over 2 million cells skip rows and columns (see
  {ref}`cap-plots-calc`); narrow the selection to see every cell.

- At most two independently configured panels; the grid repeats a single
  plot.
- Correlation cannot be a raster axis.
- Elevation, hour angle, azimuth and parallactic angle are not available
  as axes.

---

Coming from plotms or AIPS? See {ref}`cap-from-plotms` and {ref}`cap-from-aips`.
