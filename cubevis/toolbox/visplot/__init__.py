"""``cubevis.toolbox.visplot`` — interactive visibility data plotting.

This subpackage implements the PlotMS/msview replacement described in
``devel/docs/msvis_design.md``.  The top-level public API is:

``Axis`` / ``AxisType``
    The authoritative axis vocabulary.  Every layer uses ``Axis``
    members rather than bare strings.

``SelectionSpec``
    Portable, human-readable data selection specification shared across
    GUI controls, the reader, and ``FlagDB``.

``data.XArrayReader``
    Abstract base class for the MSv2 and MSv4 data reading layers.

``data.MSv2Backend``
    ``XArrayReader`` backed by ``xarray-ms`` + ``arcae`` (MSv2 files).

``data.MSv4Backend``
    ``XArrayReader`` backed by ``xradio`` (MSv4 Zarr).

Planned (not yet implemented)::

    VisibilityPlotter      — user-facing Bokeh toolbox class
    VisibilityLineSource   — Bokeh Model for scatter/line mode
    VisibilityRasterSource — Bokeh Model for raster mode
    FlagDB                 — flag accumulation layer

References
----------
devel/docs/msvis_design.md
"""

from .axes import Axis, AxisType
from .selection import SelectionSpec
from . import data

__all__ = [
    "Axis",
    "AxisType",
    "SelectionSpec",
    "data",
]
