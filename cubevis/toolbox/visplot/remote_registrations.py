"""remote_registrations.py
=========================
``register_function`` for the ``visplot`` remote data path (Chunk 2).

This module runs **inside the worker subprocess** — see the developer
guide §1's "which tier does this code run in": this is tier 3, a plain
OS process spawned by the supervisor kernel, one per execution context.
It must be importable cold, from a fresh interpreter, with nothing but
``cubevis`` on ``sys.path`` — see the developer guide §3's registration-
module rules and ``_test_registrations.py``'s docstring for exactly why
that constraint exists (this subprocess never sees anything from
whatever process constructed it, only the supervisor kernel's own
``PYTHONPATH``).

``VisplotRemoteBackend`` is deliberately a thin wrapper: it constructs
and opens a real ``MSv2Backend``/``MSv4Backend`` and a
``LocalVisibilityReader`` around it, then forwards every call.  From
this object's own point of view it is running completely locally — it
does not import anything from ``cubevis.remote`` and does not know it is
being driven remotely, per the developer guide §1's explicit warning
against leaking the remote abstraction into application-level classes.
``RemoteReductionContext`` (the ``P_local``-side half of this pair, in
``remote_reduction_context.py``) is this class's only caller; method
names and signatures below intentionally mirror ``LocalVisibilityReader``
exactly.

Package location (proposed)
----------------------------
``cubevis/cubevis/toolbox/visplot/remote_registrations.py`` — resolved
as ``"cubevis.toolbox.visplot.remote_registrations:register"``, matching
``remote_reduction_context.py``'s ``DEFAULT_REGISTER_FUNCTION``.

Reuse for iclean / gclean (Chunk 3)
-------------------------------------
This file is intentionally visplot-specific — the kickoff note for this
chunk asked that ``cubevis.remote`` itself stay application-agnostic,
which it does: nothing here is imported by or coupled to
``cubevis.remote``.  A `gclean` registration module for Chunk 3 would be
a sibling file in the ``iclean`` package following the exact same shape
(a thin worker-side wrapper class + a ``register(comm, registry,
**kwargs)`` function), not a change to this one.  Whether that shape is
worth generating from a template (the ``sync_layers``/``.j2`` idea
raised alongside this chunk's kickoff) is a separate, open question —
see the accompanying chunk2-status.md for the reasoning on why this pass
hand-writes it instead.
"""

from __future__ import annotations

from typing import Any, Optional

# Registers Serializer/Deserializer support for xr.DataArray and
# pd.DataFrame (see _wire_types.py's own docstring) in THIS process.
# Mirrors remote_reduction_context.py's identical import on the
# P_local side -- each side of the wire needs its own explicit import
# to register serialization in that process; one process importing
# _wire_types has no effect on any other process's registry. Without
# this, query_raster()'s xr.DataArray reply serializes via whatever
# generic fallback Bokeh's serializer gives an array-like object --
# which drops the DataArray wrapper (dims/coords) and arrives at
# P_local as a bare ndarray with a matching .shape but no .values,
# exactly the failure mode this import prevents.
from . import _wire_types  # noqa: F401


class VisplotRemoteBackend:
    """Worker-side object registered under ``"VisplotRemoteBackend"``.

    Constructed once per remote session (one ``create_object`` call from
    ``RemoteReductionContext.__init__``), lives for the life of the
    execution context, and is called many times via ``call_method`` —
    exactly the ``Counter``/``NumpyEcho`` shape from
    ``_test_registrations.py``, just backed by a real MS instead of a
    toy in-memory value.

    Parameters
    ----------
    path : str
        Path to the MS / Processing Set, resolved on **this** (the
        worker's) host.
    backend_kind : str
        ``"msv2"`` or ``"msv4"``.
    """

    def __init__(self, path: str, backend_kind: str = "msv2") -> None:
        # Local imports: keep worker startup fast for whichever backend
        # ISN'T being used, and keep this module importable even in an
        # environment where one of the two backend implementations isn't
        # installed (mirrors visibility_plotter.py's own lazy-import
        # convention in open_ms/open_ps).
        from cubevis.toolbox.visplot.local_visibility_reader import LocalVisibilityReader

        if backend_kind == "msv2":
            from cubevis.toolbox.visplot.data.msv2_backend import MSv2Backend
            backend = MSv2Backend(path)
        elif backend_kind == "msv4":
            from cubevis.toolbox.visplot.data.msv4_backend import MSv4Backend
            backend = MSv4Backend(path)
        else:
            raise ValueError(
                f"backend_kind must be 'msv2' or 'msv4'; got {backend_kind!r}"
            )

        backend.open()
        self._backend = backend
        self._reader = LocalVisibilityReader(backend)

    # ------------------------------------------------------------------ #
    # VisibilityReader protocol -- forwarded verbatim                     #
    # ------------------------------------------------------------------ #

    def query_raster(self, y_dim, x_dim, quantity, selection, polarization=None,
                      max_cells: int = 2_000_000):
        return self._reader.query_raster(
            y_dim=y_dim, x_dim=x_dim, quantity=quantity, selection=selection,
            polarization=polarization, max_cells=max_cells,
        )

    def query_columns(self, xaxis, layers, selection, *,
                       x_range=None, y_range=None, color_mode="global",
                       width: int = 800, height: int = 600):
        # See RemoteReductionContext.query_columns's comment: this now
        # relays a bounded ScatterRenderResult, not raw DataFrames --
        # MSv2Backend.query_columns does the binning+shading (2026-09).
        return self._reader.query_columns(
            xaxis, layers, selection,
            x_range=x_range, y_range=y_range, color_mode=color_mode,
            width=width, height=height,
        )

    def probe_raster_pixel(self, raw_grid, gx: int, gy: int, selection,
                            polarization=None):
        # Same pre-existing gap noted in remote_reduction_context.py's
        # matching relay -- fixed here too for the same reason.
        return self._reader.probe_raster_pixel(
            raw_grid, gx, gy, selection, polarization=polarization,
        )

    def probe_scatter_pixel(self, canvas_agg, px: int, py: int, selection, scatter_df):
        return self._reader.probe_scatter_pixel(canvas_agg, px, py, selection, scatter_df)

    def identity_tables(self, selection, *, polarization=None):
        return self._reader.identity_tables(selection, polarization=polarization)

    # ------------------------------------------------------------------ #
    # Extra methods LocalVisibilityReader also exposes                    #
    # ------------------------------------------------------------------ #

    def metadata(self) -> dict:
        return self._reader.metadata()

    def axis_info(self, axis, selection=None, query: str = "columns"):
        return self._reader.axis_info(axis, selection, query)

    def available_axes(self):
        return self._reader.available_axes()

    # ------------------------------------------------------------------ #
    # Worker-local cleanup -- NOT part of VisibilityReader; called only  #
    # if a future version of the framework grows an explicit             #
    # dispose-time hook. dispose_object() today just drops the Python    #
    # reference (see ObjectRegistry.dispose_object) -- it does not call  #
    # any method on the instance, so this exists for a caller that       #
    # wants to close explicitly via call_method("close") before          #
    # disposing, not because the framework invokes it automatically.     #
    # ------------------------------------------------------------------ #

    def close(self) -> None:
        self._backend.close()


def register(comm, registry, **kwargs: Any) -> None:
    """``register_function`` entry point.

    Signature matches ``worker_main.py``'s ``handle_configure`` contract
    exactly: ``register_function(comm, registry, **kwargs)``, called
    once per worker subprocess, at ``configure``-message time.  ``comm``
    is accepted (per that contract) but unused here — this application
    has no need for worker-initiated push messages yet, unlike Chunk 3's
    ``gclean`` convergence-update case (see the developer guide §4).
    """
    registry.register_class("VisplotRemoteBackend", VisplotRemoteBackend)
