# Creating an environment for testing

``visplot`` requires:

- ``xarray-ms``, the current version of which is currently built with ``pyarrow=23``
- ``zarr``
- ``dask``
- ``datashader``

The issue that can arise is version skew for ``libprotobuf`` between the ``xarray-ms`` backend for ``xarray`` and ``casatools``. I build my development version of ``casatools`` using [casa6-dev](https://github.com/schiebel/casa6-dev) which uses ``pixi``. This ``pixi`` build setup includes the dependency for the version of ``pyarrow`` to ensure that ``casatools`` is built with the same version. Then to ensure that the right version of ``libprotobuf`` is installed for my testing environment, I use:

```bash
PROTOBUF_VER=$(cd ~/develop/casa/casa6-dev && pixi list 2>/dev/null | awk '$1=="libprotobuf"{print $2; exit}') && \
    mamba create -n ms4-py312 python=3.12 ipython websockets anywidget bokeh=3.9 scipy regions certifi xarray zarr \
    'pyarrow=23' "libprotobuf=$PROTOBUF_VER" dask datashader pytest nodejs
```

Running tests in this directory requires many file descriptors so you must do the equivalent of:

```bash
bash$ ulimit -n 4096 && MS=sis14_twhya_calibrated_flagged.ms python3 test_visibility_raster.py
```
