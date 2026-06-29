"""local_visibility_reader.py
============================
``LocalVisibilityReader`` — ``VisibilityReader`` implementation for
in-process data access.

Wraps any ``XArrayReader`` subclass (``MSv2Backend`` or ``MSv4Backend``)
and exposes the four-method ``VisibilityReader`` protocol that
``VisibilityRaster`` and ``VisibilityScatter`` depend on.

This is the concrete implementation used in all local sessions — desktop
Jupyter notebooks, CASA6 terminal sessions, and AstroVIPER environments
where the Processing Set is accessible from the same process.  For remote
cluster scenarios where the data lives on a different host, the
``RemoteReductionContext`` (future) will implement the same protocol via
RPC dispatch instead.

Relationship to ``XArrayReader``
---------------------------------
``XArrayReader`` is the abstract base class for the two backends.  Its
interface is *broader* than ``VisibilityReader``: it includes lifecycle
methods (``open``, ``close``), a ``metadata()`` method used by
``ObservationMetadata``, and ``available_axes()``.  ``LocalVisibilityReader``
wraps it and presents only the four methods that the display widgets need,
enforcing the boundary cleanly.

The backends are **not** modified.  They continue to implement
``XArrayReader`` directly and can be used independently (e.g. in tests
or scripting) without going through this wrapper.

Usage
-----
::

    from cubevis.toolbox.visplot.msv2_backend import MSv2Backend
    from cubevis.toolbox.visplot.local_visibility_reader import (
        LocalVisibilityReader,
    )

    backend = MSv2Backend("/data/obs.ms")
    backend.open()
    reader = LocalVisibilityReader(backend)

    # Pass reader to display widgets:
    raster = VisibilityRaster(reader, selection, ...)
    scatter = VisibilityScatter(reader, selection, ...)

Context manager
---------------
``LocalVisibilityReader`` forwards ``__enter__`` / ``__exit__`` to the
underlying backend so it can be used as a context manager even though
``VisibilityReader`` itself does not define lifecycle methods::

    with LocalVisibilityReader(MSv2Backend(path)) as reader:
        raster = VisibilityRaster(reader, ...)

Package location
----------------
``cubevis/cubevis/toolbox/visplot/local_visibility_reader.py``
"""

from __future__ import annotations

import logging
from typing import Optional, TYPE_CHECKING

import pandas as pd
import xarray as xr

from .data import XArrayReader
from .visibility_reader import VisibilityReader

if TYPE_CHECKING:
    from ..axes import Axis
    from ..selection import SelectionSpec

log = logging.getLogger(__name__)


class LocalVisibilityReader:
    """``VisibilityReader`` implementation backed by a local ``XArrayReader``.

    All four protocol methods delegate directly to the wrapped backend.
    No data transformation or caching is performed here — this class is a
    pure adapter.

    Parameters
    ----------
    backend : XArrayReader
        An already-constructed (but not necessarily open) backend.
        ``MSv2Backend`` or ``MSv4Backend`` are the expected concrete types.
        The backend must be open before any query method is called; use
        the context manager form or call ``backend.open()`` explicitly.
    """

    def __init__(self, backend: XArrayReader) -> None:
        if not isinstance(backend, XArrayReader):
            raise TypeError(
                f"backend must be an XArrayReader subclass; "
                f"got {type(backend).__name__!r}"
            )
        self._backend = backend

    # ------------------------------------------------------------------ #
    # Lifecycle forwarding (convenience, not part of VisibilityReader)     #
    # ------------------------------------------------------------------ #

    def open(self) -> None:
        """Open the underlying backend.  Idempotent."""
        self._backend.open()

    def close(self) -> None:
        """Close the underlying backend."""
        self._backend.close()

    def __enter__(self) -> "LocalVisibilityReader":
        self._backend.open()
        return self

    def __exit__(self, *_: object) -> None:
        self._backend.close()

    # ------------------------------------------------------------------ #
    # VisibilityReader protocol                                            #
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
        """Delegate to ``backend.query_raster``."""
        return self._backend.query_raster(
            y_dim=y_dim,
            x_dim=x_dim,
            quantity=quantity,
            selection=selection,
            polarization=polarization,
            max_cells=max_cells,
        )

    def query_columns(
        self,
        xaxis: "Axis",
        yaxes: list[tuple["Axis", str]],
        selection: "SelectionSpec",
        *,
        canvas_width: int = 800,
        canvas_height: int = 600,
    ) -> dict[tuple["Axis", str], pd.DataFrame]:
        """Delegate to ``backend.query_columns``."""
        return self._backend.query_columns(
            xaxis,
            yaxes,
            selection,
            canvas_width=canvas_width,
            canvas_height=canvas_height,
        )

    def probe_raster_pixel(
        self,
        raw_grid: xr.DataArray,
        gx: int,
        gy: int,
        selection: "SelectionSpec",
    ) -> dict:
        """Delegate to ``backend.probe_raster_pixel``."""
        return self._backend.probe_raster_pixel(raw_grid, gx, gy, selection)

    def probe_scatter_pixel(
        self,
        canvas_agg: xr.DataArray,
        px: int,
        py: int,
        selection: "SelectionSpec",
        scatter_df: pd.DataFrame,
    ) -> dict:
        """Delegate to ``backend.probe_scatter_pixel``."""
        return self._backend.probe_scatter_pixel(
            canvas_agg, px, py, selection, scatter_df
        )

    # ------------------------------------------------------------------ #
    # Pass-through for ObservationMetadata construction                    #
    # ------------------------------------------------------------------ #

    def metadata(self) -> dict:
        """Return the backend's metadata dict for ``ObservationMetadata``.

        Not part of ``VisibilityReader`` — called only by the
        ``open_ms`` / ``open_ps`` factory functions that construct
        ``ObservationMetadata``.
        """
        return self._backend.metadata()

    def available_axes(self):
        """Forward to the backend's ``available_axes`` method."""
        return self._backend.available_axes()

    # ------------------------------------------------------------------ #
    # Repr                                                                 #
    # ------------------------------------------------------------------ #

    def __repr__(self) -> str:  # pragma: no cover
        return f"LocalVisibilityReader({self._backend!r})"


# ======================================================================
# Runtime assertion that LocalVisibilityReader satisfies the protocol
# ======================================================================

def _assert_protocol() -> None:  # pragma: no cover
    """Checked at import time in debug builds.

    Raises ``TypeError`` if ``LocalVisibilityReader`` does not satisfy
    ``VisibilityReader`` at the structural level.  This will catch any
    accidental signature drift before tests run.
    """
    assert isinstance(
        LocalVisibilityReader.__new__(LocalVisibilityReader),
        VisibilityReader,
    ), (
        "LocalVisibilityReader does not satisfy the VisibilityReader protocol. "
        "Check method signatures in visibility_reader.py."
    )
