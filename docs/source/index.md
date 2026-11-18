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

To open this documentation, for the version of `cubevis` you have
installed, from Python or CASA:

```python
import cubevis

cubevis.docs()               # or a page: cubevis.docs('visplot')
```

The pages it knows by name are `'install'`, `'visplot'`, `'iclean'` and
`'remote'`. `docs()` opens the documentation of your `cubevis` release
(1.2.x opens the 1.2 documentation) when it is published, and this, the
latest documentation, otherwise. It uses the browser named by the
`BROWSER` environment variable, or your system's. In a Jupyter notebook it
opens no browser, since the notebook's Python may be running on another
computer; it shows the address as a link instead. It always returns the
address, and `cubevis.docs(launch=False)` returns it without opening
anything.

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
