# Starting iclean

`iclean` is interactive clean: it runs CASA's image reconstruction,
`tclean`, one cycle at a time, and shows the residual image, the mask and
the convergence in a window where you can change the mask and the
iteration control between cycles.

`iclean` takes `tclean`'s parameters, and its documentation is `tclean`'s:

```{task-doc} iclean
```

## Before you start

- An environment with `cubevis[iclean]`, which brings in CASA6's
  `casatasks` and `casatools`, and `arcae` for reading the images: see
  [](installing.md).
- A calibrated measurement set.

## Starting

`iclean` is called as `tclean` is. `vis` and `imagename` are required:

```python
from cubevis import iclean

iclean( vis='refim_twopoints_twochan.ms', imagename='test',
        imsize=100, cell='8.0arcsec',
        phasecenter="J2000 19:59:28.500 +40.44.01.50",
        niter=50, cycleniter=10, deconvolver='hogbom',
        specmode='mfs', spw='0:0' )
```

The call shows the window in your web browser and runs until you stop
interactive clean there, then returns its result. The images are written
as `tclean` writes them, named after `imagename`.

The measurement set of this example can be
[downloaded](https://casa.nrao.edu/download/devel/casavis/data/refim_twopoints_twochan-ms.tar.gz).

:::{note}
A screenshot of the window, and a tour of its controls, will be added
here when the layout is final.
:::

```{task-params} iclean
:only: vis, imagename
```

### In a Jupyter notebook

`iclean.notebook(...)` takes the same parameters and shows the window in
the notebook cell. It needs the `notebook` extra as well:
`pip install "cubevis[iclean,notebook]"`.

### Data on another computer

When the data is on another computer, name a kernel that was set up there
and give `vis` and `imagename` as they are on that computer:

```python
iclean( vis="twhya.ms", imagename="twhya", ..., kernel_name="cvpost106-iclean" )
```

`tclean` and the image access then run on that computer and only the
window runs on yours, which needs no CASA. Setting up the kernel, and what
to know when using one, is described in [](remote_kernels.md).

```{task-params} iclean
:only: kernel_name, iclean_backend
```

## The parameters used most

These are `tclean`'s descriptions. Each opens to the full text with
**more**.

### The image

```{task-params} iclean
:only: imsize, cell, phasecenter, stokes, specmode, deconvolver, weighting
```

### Iteration control

The values given here are what the window's controls start with; they can
be changed between cycles.

```{task-params} iclean
:only: niter, cycleniter, threshold, nsigma, gain, nmajor
```

### Masks

```{task-params} iclean
:only: usemask, mask
```

Every parameter is listed in [](iclean_parameters.md). The task pages of
`tclean` in [CASA Docs](https://casadocs.readthedocs.io) describe the
algorithms behind them.
