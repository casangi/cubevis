"""visibility_reader.py
======================
``VisibilityReader`` — the narrow read protocol consumed by
``VisibilityRaster`` and ``VisibilityScatter``.

This module defines the *only* interface that the display widgets depend on
for data access.  Decoupling display from data access at this boundary is
what makes remote execution transparent: both ``LocalVisibilityReader``
(wrapping a local ``XArrayReader`` backend) and ``RemoteReductionContext``
(dispatching over a network) satisfy the same protocol, and the widget
classes never need to know which one they are talking to.

Design principles
-----------------
* **Four methods only** — ``query_raster``, ``query_columns``,
  ``probe_raster_pixel``, ``probe_scatter_pixel``.  These are exactly the
  methods called by ``VisibilityRaster`` and ``VisibilityScatter``; nothing
  more is exposed through this interface.
* **No lifecycle methods** — ``open`` / ``close`` are the backend's concern,
  not the widget's.  The widget receives an already-open reader.
* **No Bokeh dependency** — pure Python / xarray / pandas.
* **No write path** — flag persistence is ``FlagDB``'s responsibility.
* **Runtime-checkable Protocol** — ``isinstance(obj, VisibilityReader)``
  works without inheriting from the class.

Implementations
---------------
``LocalVisibilityReader``
    Wraps any ``XArrayReader`` (``MSv2Backend`` or ``MSv4Backend``) for
    in-process use.  Defined in ``local_visibility_reader.py``.

``ReductionContext`` (future)
    The ``RemoteReductionContext`` subclass will also implement this protocol
    so that ``VisibilityPlotter`` can pass the same object for both the
    ``reader`` and ``context`` constructor arguments when data lives on a
    remote cluster.  See ``reduction_context.py``.

Package location
----------------
``cubevis/cubevis/toolbox/visplot/visibility_reader.py``
"""

from __future__ import annotations

from typing import Optional, Protocol, runtime_checkable, TYPE_CHECKING

import pandas as pd
import xarray as xr

if TYPE_CHECKING:
    from ..axes import Axis
    from ..selection import SelectionSpec


@runtime_checkable
class VisibilityReader(Protocol):
    """Narrow read protocol for visibility display widgets.

    Any object that implements these four methods satisfies the protocol,
    regardless of inheritance.  The widget classes (``VisibilityRaster``,
    ``VisibilityScatter``) accept a ``VisibilityReader`` and call only
    these methods.

    All method signatures are identical to the corresponding abstract
    methods on ``XArrayReader``.  ``LocalVisibilityReader`` satisfies
    this protocol by delegation; ``RemoteReductionContext`` satisfies it
    by RPC dispatch.

    Notes
    -----
    ``@runtime_checkable`` enables ``isinstance(obj, VisibilityReader)``
    checks in ``VisibilityPlotter`` factory code without requiring
    subclassing.
    """

    # ------------------------------------------------------------------ #
    # Raster query                                                         #
    # ------------------------------------------------------------------ #

    def query_raster(
        self,
        y_dim: "Axis",
        x_dim: "Axis",
        quantity: "Axis",
        selection: "SelectionSpec",
        polarization: Optional[str] = None,
        max_cells: int = 2_000_000,
    ) -> tuple[xr.DataArray, tuple[float, float], tuple[float, float], bool]:
        """Return a computed 2D DataArray suitable for ``Canvas.raster()``.

        Signature is identical to ``XArrayReader.query_raster``.

        Returns
        -------
        agg : xr.DataArray
            Computed (not lazy) 2D float64 DataArray.
        x_range : tuple[float, float]
            Full extent of the x coordinate in the original unreduced data.
        y_range : tuple[float, float]
            Full extent of the y coordinate.
        is_decimated : bool
            ``True`` if a stride > 1 was applied; detail exists in the
            source data that is not in the agg.
        """
        ...

    # ------------------------------------------------------------------ #
    # Scatter / column query                                               #
    # ------------------------------------------------------------------ #

    def query_columns(
        self,
        xaxis: "Axis",
        yaxes: list[tuple["Axis", str]],
        selection: "SelectionSpec",
        *,
        canvas_width: int = 800,
        canvas_height: int = 600,
    ) -> dict[tuple["Axis", str], pd.DataFrame]:
        """Return flat DataFrames for scatter mode, one per (axis, pol) pair.

        Signature is identical to ``MSv2Backend.query_columns`` /
        ``MSv4Backend.query_columns``.

        Returns
        -------
        dict mapping each ``(Axis, polarization)`` key to a pandas
        DataFrame with columns ``"x"`` and ``"y"`` (NaN rows dropped).
        """
        ...

    # ------------------------------------------------------------------ #
    # Hover probes                                                         #
    # ------------------------------------------------------------------ #

    def probe_raster_pixel(
        self,
        raw_grid: xr.DataArray,
        gx: int,
        gy: int,
        selection: "SelectionSpec",
    ) -> dict:
        """Return metadata for a raster pixel at raw grid indices (gx, gy).

        Signature is identical to ``XArrayReader.probe_raster_pixel``.
        """
        ...

    def probe_scatter_pixel(
        self,
        canvas_agg: xr.DataArray,
        px: int,
        py: int,
        selection: "SelectionSpec",
        scatter_df: pd.DataFrame,
    ) -> dict:
        """Return metadata for a scatter pixel at canvas indices (px, py).

        Signature is identical to ``XArrayReader.probe_scatter_pixel``.
        """
        ...
