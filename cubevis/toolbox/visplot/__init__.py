"""``cubevis.toolbox.visplot`` — interactive visibility data plotting.

This subpackage implements the PlotMS/msview replacement described in
``devel/docs/msvis_design.md``.

Astronomer-facing API
---------------------
``VisibilityPlotter``
    Combined raster + scatter visibility inspection and flagging tool.
    Accepts strings, numbers, and lists — no internal objects required.
    The primary entry point for interactive use::

        from cubevis.toolbox.visplot import VisibilityPlotter

        plotter = VisibilityPlotter(
            ms          = "sis14_twhya_calibrated_flagged.ms",
            field       = "0637-752",
            spw         = "0,1,2,3",
            correlation = "XX,YY",
        )
        plotter.show()

``ReductionBackend``
    ``str`` enum controlling which ``ReductionContext`` is constructed.
    Accepted as a plain string by ``VisibilityPlotter``'s ``backend=``
    parameter: ``"auto"`` | ``"casa6"`` | ``"radps"`` | ``"remote"``
    | ``"null"``.

Programmer-facing composable API
---------------------------------
The lower-level components are also importable directly for embedding in
pipelines or custom tools:

``VisibilityRaster``
    Datashader-rendered 2D heatmap of a visibility quantity over two
    native axes (time × channel, baseline × time, etc.).

``VisibilityScatter``
    Multi-layer Datashader scatter plot of one or more y-axis quantities
    vs a free x-axis.

``ScatterLayer``
    Dataclass specifying one scatter layer (y-axis, polarization,
    colour map, alpha, scaling).

``LocalVisibilityReader``
    ``VisibilityReader`` implementation backed by a local
    ``XArrayReader`` (``MSv2Backend`` or ``MSv4Backend``).

``FlagDB``
    In-memory flag accumulation layer.  ``append()`` / ``pop()`` /
    ``commit(context)`` / ``peek()`` / ``overlay_deltas()``.

``SelectionSpec``
    Portable, human-readable data selection specification shared across
    GUI controls, the reader, and ``FlagDB``.

``Axis`` / ``AxisType``
    The authoritative axis vocabulary.  Every layer uses ``Axis``
    members rather than bare strings.

``data.XArrayReader``
    Abstract base class for the MSv2 and MSv4 data reading layers.

``data.MSv2Backend``
    ``XArrayReader`` backed by ``xarray-ms`` + ``arcae`` (MSv2 files).

``data.MSv4Backend``
    ``XArrayReader`` backed by ``xradio`` (MSv4 Zarr).

Internal helpers (not public API)
----------------------------------
``open_ms`` / ``open_ps``
    Factory functions used internally by ``VisibilityPlotter.__init__``.
    Importable directly for developer / testing use but not part of the
    astronomer-facing contract.

References
----------
devel/docs/msvis_design.md
devel/docs/visibility_plotter_implementation_plan.md
devel/docs/visibility_plotter_preview.md
"""

from .axes import Axis, AxisType
from .selection import SelectionSpec
from .flag_db import FlagDB
from .local_visibility_reader import LocalVisibilityReader
from .visibility_raster import VisibilityRaster
from .visibility_scatter import VisibilityScatter, ScatterLayer
from .visibility_plotter import VisibilityPlotter
from .reduction_context import ReductionBackend
from . import data

__all__ = [
    # Astronomer-facing
    "VisibilityPlotter",
    "ReductionBackend",
    # Programmer-facing composable layer
    "VisibilityRaster",
    "VisibilityScatter",
    "ScatterLayer",
    "LocalVisibilityReader",
    "FlagDB",
    "SelectionSpec",
    "Axis",
    "AxisType",
    "data",
]
