# Installing

`cubevis` needs Python 3.12 or later. Each application has its own extra,
so that installing one does not pull in what only the other needs:

| To use | Install | Brings in |
|---|---|---|
| `visplot` | `pip install "cubevis[visplot]"` | xarray-ms, zarr, dask, distributed, datashader |
| `iclean` (interactive clean) | `pip install "cubevis[iclean]"` | casatasks and casatools, arcae, xarray |
| both | `pip install "cubevis[visplot,iclean]"` | all of the above |
| only the window, with the data on another computer | `pip install "cubevis[remote]"` | what is needed to start and talk to a remote kernel |

The `visplot` and `iclean` extras include `remote`.

Add the `notebook` extra to use `iclean` in a Jupyter notebook:
`pip install "cubevis[iclean,notebook]"`.

## Where visplot can read data

`visplot` reads MSv2 measurement sets and MSv4 / Processing Sets through
`xarray-ms` and its casacore bindings, `arcae`. `arcae` is published for
Linux (x86_64 and aarch64) and macOS (Apple Silicon on macOS 14 or later,
Intel on macOS 15 or later). There is no Windows build.

On a computer where the data cannot be read, or when the data is on
another computer, the data access runs there and only the window runs on
your computer: see [](remote_kernels.md).

## How iclean reads images

`iclean` runs CASA's `tclean` through `casatasks`, but reads the residual
and reads and writes the mask with `arcae`, not `casatools`. It reads CASA
images (paged, or concatenated with `ia.imageconcat`) and
[xradio](https://xradio.readthedocs.io) images saved in Zarr. `arcae` has a
build everywhere `casatools` does, so nothing needs to be set.

To read the images with `casatools` instead, set an environment variable
where the images are (for a remote kernel, in that kernel's environment):

```bash
export CUBEVIS_IMAGE_STORE=casatools
```

With `CUBEVIS_DEBUG=1`, the log says once which one is in use. If `arcae`
or `xarray` cannot be imported, `casatools` is used and the log says so.

The same image access works on its own, with no CASA installed
(`pip install "cubevis[image]"`): `xr.open_dataset(path, engine="casa-image")`
after `import cubevis.xarray.image`, or `cubevis.imaging.ImageReader`.

## Checking the installation

```python
from cubevis import visplot      # or: from cubevis import iclean
```

The applications are loaded on first use, so an import error for a missing
package appears here and names the package.

`cubevis.docs()` then opens the documentation for the version installed.
