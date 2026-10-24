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

``FlagDB``
    In-memory flag accumulation layer.  ``append()`` / ``pop()`` /
    ``commit(context)`` / ``peek()`` / ``overlay_deltas()``.

``SelectionSpec``
    Portable, human-readable data selection specification shared across
    GUI controls, the reader, and ``FlagDB``.

``Axis`` / ``AxisType``
    The authoritative axis vocabulary.  Every layer uses ``Axis``
    members rather than bare strings.

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

# Lazy package (PEP 562).  The public names below are imported on first
# use, not when the package is: a remote kernel imports this package only
# because ``remote_registrations`` lives in it, and with eager imports it
# would load the whole plotting front end (``VisibilityPlotter``, the
# panels, the flag controls) on the node for nothing.  That made importing
# slower, and it made the kernel-side fingerprint (``cubevis/_fingerprint.py``,
# remote session design 7c) change with every front-end edit, so that
# environments on the nodes would be rebuilt for changes that never run
# there.  ``from cubevis.toolbox.visplot import VisibilityPlotter`` works as
# before; ``tests/manual/setup/test_lazy_packages.py`` checks every name.
# Keep new public names in ``_LAZY`` rather than importing them here.
import importlib as _importlib
from typing import TYPE_CHECKING

_LAZY = {
    "Axis": ".axes",
    "AxisType": ".axes",
    "SelectionSpec": ".selection",
    "FlagDB": ".flag_db",
    "FlagDelta": ".flag_model",
    "FlagFilter": ".flag_filters",
    "ParamSpec": ".flag_filters",
    "make_flag_filter": ".flag_filters",
    "VisibilityRaster": ".visibility_raster",
    "VisibilityScatter": ".visibility_scatter",
    "ScatterLayer": ".visibility_scatter",
    "VisibilityPlotter": ".visibility_plotter",
    "ReductionBackend": ".reduction_context",
}


def __getattr__(name):
    if name in _LAZY:
        value = getattr(_importlib.import_module(_LAZY[name], __name__), name)
        globals()[name] = value          # once imported, an ordinary attribute
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__():
    return sorted(set(globals()) | set(_LAZY))


if TYPE_CHECKING:                        # for type checkers and editors only
    from .axes import Axis, AxisType
    from .selection import SelectionSpec
    from .flag_db import FlagDB
    from .flag_model import FlagDelta
    from .flag_filters import FlagFilter, ParamSpec, make_flag_filter
    from .visibility_raster import VisibilityRaster
    from .visibility_scatter import VisibilityScatter, ScatterLayer
    from .visibility_plotter import VisibilityPlotter
    from .reduction_context import ReductionBackend

__all__ = [
    # Astronomer-facing
    "VisibilityPlotter",
    "ReductionBackend",
    # Programmer-facing composable layer
    "VisibilityRaster",
    "VisibilityScatter",
    "ScatterLayer",
    "FlagDB",
    "FlagDelta",
    "FlagFilter",
    "ParamSpec",
    "make_flag_filter",
    "SelectionSpec",
    "Axis",
    "AxisType",
]
