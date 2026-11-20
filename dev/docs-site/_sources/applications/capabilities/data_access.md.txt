(cap-data)=

# Data formats and access

**Status:** available.

## What it is

`visplot` reads visibility data in two formats:

- **MSv2**: CASA measurement sets.
- **MSv4 / Processing Sets**: Xradio's Zarr-based format.

The format is detected from the data itself, so the same call works for
either. The data can be on your own computer or on another one. In the
second case only the window runs on your computer; reading, averaging
and rendering happen where the data is.

## What it is for

It plays the role of CASA's `plotms` and `msview`, and works on both the
current and the next-generation data format. Being able to work on data
that stays on a cluster or a remote host means a large observation never
needs to be copied to a laptop.

## How it behaves

- **Visibility column.** The *data*, *corrected* or *model* column can be
  plotted.
- **Remote data.** A kernel set up on the remote host is named when
  `visplot` starts. Paths are then paths on that host. Setting up a
  kernel is described in [](../remote_kernels.md).
- **Large selections are loaded, not refused.** A selection that is
  estimated to need more than 70 % of the available memory is still
  loaded (slowly, if the system has to swap) and a note goes to the log.
  Where swapping is not wanted, such as on a shared cluster node, the
  environment variable `CUBEVIS_VISPLOT_MEMORY_GUARD=<MB>` sets a limit
  instead: a selection estimated to need more is refused before anything
  is read, with a message saying what to narrow.

## From Python

```python
from cubevis import visplot

visplot(vis="twhya_selfcal_contsub_ms")                  # either format
visplot(vis="big.ms", kernel_name="cvpost106-visplot")   # data on another host
visplot(vis="twhya_selfcal_contsub_ms", datacolumn="corrected")
```

Give exactly one of `vis`, `ms` (MSv2 only) or `ps` (MSv4 only).

(cap-data-limits)=

## Limits

- Not yet available in Jupyter notebooks. Run `visplot` from `ipython` or
  a `casa` prompt.
- Calibration tables cannot be plotted yet.

---

Coming from plotms or AIPS? See {ref}`cap-from-plotms` and {ref}`cap-from-aips`.
