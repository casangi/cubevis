# cubevis

`cubevis` provides graphical tools for radio astronomy data processing
with [CASA6](https://casadocs.readthedocs.io) and
[Xradio](https://github.com/casangi/xradio). The tools are built with
[Bokeh](https://bokeh.org), display in a web browser, and are started from
Python or CASA with a single function call:

- **`iclean`**, interactive clean: observe and control CASA's image
  reconstruction (`tclean`) cycle by cycle.
- **`visplot`**: inspect and flag visibility data, in MSv2 measurement
  sets and in Xradio's MSv4 / Processing Sets.

```python
from cubevis import visplot

visplot(vis="twhya_selfcal_contsub_ms", field="5", spw="0")
```

Each tool can do its work on the data on another computer, with only its
window on yours.

## User guide

For astronomers using the tools.

```{toctree}
:maxdepth: 2
:caption: User guide

applications/installing
applications/remote_kernels
applications/visplot_starting
applications/visplot
applications/capabilities/index
applications/iclean_starting
applications/iclean_parameters
```

## Slides

Illustrated overviews of how `cubevis` is put together and what `visplot`
does.

```{toctree}
:maxdepth: 1
:caption: Slides

slides
```

## Background and development

How to work on this documentation, and the design documents of `casagui`,
the package `cubevis` grew out of. Notes for developers working on
`cubevis` itself are in the
[`devel`](https://github.com/casangi/cubevis/tree/main/devel) directory of
the repository.

```{toctree}
:maxdepth: 1
:caption: Background and development

contributing
historical/index
```
