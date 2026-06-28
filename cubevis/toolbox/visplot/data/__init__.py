"""``cubevis.toolbox.visplot.data`` — data reading layer for visibility plots.

This subpackage provides the ``XArrayReader`` abstract base class and
its two concrete backends:

``MSv2Backend``
    Backed by ``xarray-ms`` + ``arcae`` (casacore C++ bindings).
    Opens MSv2 ``.ms`` directories via ``xarray.open_datatree()`` with
    the ``xarray-ms`` engine, presenting a full MSv4-structured DataTree
    to all layers above.

``MSv4Backend``
    Backed by ``xradio``.
    Opens MSv4 Processing Sets (``*.ps.zarr``) via
    ``xradio.read_processing_set()``.

Both backends expose an identical interface and may be used
interchangeably by the ``VisibilityPlotter`` source classes.

Quick start::

    from cubevis.toolbox.visplot.data import MSv2Backend
    from cubevis.toolbox.visplot.axes import Axis
    from cubevis.toolbox.visplot.selection import SelectionSpec

    with MSv2Backend('my_dataset.ms') as reader:
        meta = reader.metadata()
        sel = SelectionSpec(scan=['3', '5'])
        ds = reader.query_columns(Axis.TIME, Axis.AMPLITUDE, sel)
        # ds is a lazy, Dask-backed xarray Dataset ready for Datashader
"""

from .reader import XArrayReader, _compute_axis_values
from .msv2_backend import MSv2Backend
from .msv4_backend import MSv4Backend

__all__ = [
    "XArrayReader",
    "MSv4Backend",
    "MSv2Backend",
    "_compute_axis_values",  # exposed for testing and FlagDB use
]
