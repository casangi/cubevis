# cubevis

`cubevis` provides graphical user interfaces for radio astronomy data:
interactive image reconstruction (`iclean`) and visibility inspection and
flagging (`visplot`). The interfaces are built with
[Bokeh](https://bokeh.org), display in a web browser, and are started from
Python or CASA with a single function call.

```python
from cubevis import visplot

visplot(vis="twhya_selfcal_contsub_ms", field="5", spw="0")
```

## User guide

For astronomers using the applications.

```{toctree}
:maxdepth: 2
:caption: User guide

applications/installing
applications/remote_kernels
applications/visplot_starting
applications/visplot
applications/interactive_clean
```

## Slides

Illustrated overviews of how `cubevis` is put together and what `visplot`
does.

```{toctree}
:maxdepth: 1
:caption: Slides

slides
```

## Design and development

The motivation, architecture and long-term goals of `cubevis`, and how to
work on this documentation. Notes for developers working on `cubevis`
itself are in the
[`devel`](https://github.com/casangi/cubevis/tree/main/devel) directory of
the repository.

```{toctree}
:maxdepth: 2
:caption: Design and development

design/intro
contributing
```
