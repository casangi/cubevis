# Starting visplot

`visplot` displays visibility data as rasters and scatter plots and flags
it interactively. It reads MSv2 measurement sets and MSv4 / Processing
Sets, and replaces CASA6's `plotms` and `msview` for this purpose.

```{task-doc} visplot
```

## Before you start

- An environment with `cubevis[visplot]`: see [](installing.md).
- An interactive Python or CASA6 session: `ipython`, or a `casa` prompt.
- A measurement set (MSv2) or an MSv4 / Processing Set.

:::{note}
Jupyter notebooks are not yet supported. `visplot.notebook()` exists but
raises `NotImplementedError` saying so.
:::

## Opening a data set

`visplot` is called as a CASA-style task:

```python
from cubevis import visplot

visplot(vis="twhya_selfcal_contsub_ms")
```

The call does everything: it opens the data, builds the window, shows it
in your web browser and runs until the window is closed. Nothing is
plotted until **Plot ▶** is pressed, so the selection can be narrowed
first.

:::{note}
A screenshot of the window as it first opens will be added here when the
layout is final.
:::

The arguments that say which data to open:

```{task-params} visplot
:only: vis, ms, ps, kernel_name, backend
```

### Data on another computer

When the data is on another computer, name a kernel that was set up there
and give the path as it is on that computer:

```python
visplot(vis="sis14_twhya_calibrated_flagged.ms", kernel_name="cvpost106-visplot")
```

The window runs on your computer and the data access on the other one.
Setting up the kernel is described in [](remote_kernels.md).

## Selecting data

The selection arguments use CASA's selection syntax and set what the
sidebar's *Data selection* controls start with. Everything can be changed
in the window afterwards.

```python
visplot(
    vis         = "twhya_selfcal_contsub_ms",
    field       = "5",
    spw         = "0",
    correlation = "XX,YY",
    datacolumn  = "data",
)
```

Text that does not parse, or that matches nothing in the data, is refused
with a message saying what is accepted.

```{task-params} visplot
:only: field, spw, antenna, scan, timerange, uvrange, autocorr, correlation, datacolumn, time_range, freq_range, uvdist_range
```

## Choosing what is plotted

The window holds one or two panels, each a raster or a scatter plot, or
a grid of plots: one plot repeated for each baseline, antenna, spectral
window, field, scan or correlation in the selection, page by page, as
`plotms`'s `iteraxis` does. A *preset* sets both panels at once to a view
that is useful for one job; the same presets are buttons in the window's
toolbar.

```python
visplot(vis="twhya_selfcal_contsub_ms", field="5", preset="radplot")
visplot(vis="twhya_selfcal_contsub_ms", field="5", layout="grid",
        iterate_by="baseline", gridrows=3, gridcols=4)
```

```{task-params} visplot
:only: preset, layout, kind, panels, raster_y, raster_x, raster_qty, scatter_x, scatter_y, iterate_by, gridrows, gridcols, gridcolor, grid_panel, pin_layout, time_labels
```

What each view is for, and the order in which they are useful on a new
observation, is described in [](visplot.rst). A list of everything `visplot` can do,
one capability per page, is in [](capabilities/index.md).

## Writing a plot to a file

With `plotfile`, `visplot` writes a PNG instead of opening the window and
returns the file's path:

```python
path = visplot(vis="twhya_selfcal_contsub_ms", field="5",
               preset="radplot", plotfile="twhya_radplot.png")
```

```{task-params} visplot
:only: plotfile, exprange, row_height, column_width, theme
```

## From the CASA shell

`visplot` is also generated as a CASA task and for the CASA shell's
`inp` / `go` interface, with the same parameters and descriptions as
above.

:::{note}
Instructions for the `inp` / `go` interface will be added here.
:::

## All other parameters

The remaining parameters set the initial state of controls that are in
the window: how raster cells are averaged, the statistics windows, colour
maps and flagging. They are listed here for reference; most sessions do
not need them.

```{task-params} visplot
:all:
```
