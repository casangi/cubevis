(cap-colormaps)=

# Colormaps and color scaling

**Status:** available. A cyclic colormap is used automatically for
phase; it cannot yet be chosen for other quantities.

## What it is

- **Raster colormaps:** plasma (the default), viridis, inferno, cividis,
  plasma reversed, viridis reversed, and gray (reversed). All of them are
  perceptually uniform, and cividis is designed to be readable with
  color-vision deficiency.
- **A cyclic colormap for Phase.** Phase rasters are drawn with a
  colormap whose two ends are the same color, on a fixed −180° to 180°
  scale.
- **Scatter colormaps** show point density, in several color families,
  with light-theme versions.
- **Color scaling** is how values map onto the colormap: histogram
  equalized (the default), linear, logarithmic, square root, square,
  gamma or power, or threshold.

## What it is for

The AIPS cookbook advises, for editing by phase, a coloring that is
circular in color, because phase is circular. Phase −179° and +179° are
nearly the same direction. With an ordinary colormap they would be at
opposite ends, and a phase drifting across ±180° would draw a sharp
edge that isn't in the data. With a cyclic colormap the wrap is
invisible.

The colormap `visplot` uses for phase (based on cmocean's `phase` map)
also has constant lightness, so no phase value looks brighter, or more
important, than another.

Histogram-equalized scaling spreads the colors over the values that
actually occur, so faint structure is visible next to a few very bright
cells. Linear or logarithmic scaling is better for reading values.

## How it behaves

- The cyclic colormap applies to **Phase** automatically. The chosen
  colormap returns when the quantity changes.
- **Phase Diff** (0 to 180°) and Phase RMS do not wrap, so they use the
  ordinary colormap.
- The color range can be set explicitly, or taken from the data.
- Changing the colormap or the scaling redraws immediately, without
  reading the data again.
- Colormaps are adjusted for the theme so the low or high end never
  vanishes into the background.
- In a grid of plots, the plots on a page share one color range by
  default (see [](plot_types.md)).

## From Python

```python
visplot(vis="obs.ms", raster_cmap="cividis", theme="light")
visplot(vis="obs.ms", scatter_cmap="warm")
```

Raster names: `plasma`, `viridis`, `inferno`, `cividis`, `plasma_r`,
`viridis_r`, `gray_r`. Scatter names: `polar`, `warm`, `polar_light`,
`gray_light`.

(cap-colormaps-limits)=

## Limits

- The cyclic colormap cannot be chosen by name, or turned off for Phase.
- Scatter plots are colored by density, by a category (scan, field,
  antenna, baseline, spectral window) or by Z-Score, not by phase.

---

Coming from plotms or AIPS? See {ref}`cap-from-plotms` and {ref}`cap-from-aips`.
