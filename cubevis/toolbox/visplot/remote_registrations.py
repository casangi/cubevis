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

import logging

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

    # ------------------------------------------------------------------ #
    # Per-method worker-side timing (performance/overhead estimation)     #
    # ------------------------------------------------------------------ #
    # Every public method call is timed here, in the worker, so the P_local
    # side (RemoteReductionContext.call_stats) can separate time spent
    # computing from time spent in transport, serialization and dispatch.

    def __getattribute__(self, name):
        attr = object.__getattribute__(self, name)
        if name.startswith("_") or name == "call_stats" or not callable(attr):
            return attr
        import time as _t

        def _timed(*args, **kwargs):
            t0 = _t.perf_counter()
            try:
                return attr(*args, **kwargs)
            finally:
                dt = _t.perf_counter() - t0
                try:
                    stats = object.__getattribute__(self, "_cv_stats")
                except AttributeError:
                    stats = {}
                    object.__setattr__(self, "_cv_stats", stats)
                n, tot, mx, last = stats.get(name, (0, 0.0, 0.0, 0.0))
                stats[name] = (n + 1, tot + dt, max(mx, dt), dt)
                logging.getLogger(__name__).debug(
                    "worker %s: %.4fs", name, dt)
        return _timed

    def runtime_info(self) -> dict:
        """Where this worker's code comes from -- so a benchmark or a bug
        report can tell which cubevis the REMOTE side is running (the kernel
        environment has its own installed copy, independent of P_local)."""
        import os
        import sys
        import cubevis
        import cubevis.remote._worker_transport as wt
        info = {"python": sys.version.split()[0], "executable": sys.executable,
                "cubevis_path": os.path.dirname(cubevis.__file__),
                "worker_transport": wt.__file__,
                "frame_stats": hasattr(wt, "FRAME_STATS"),
                "frame_debug": bool(getattr(wt, "_FRAME_DEBUG", True)),
                "pid": os.getpid()}
        try:
            info["cubevis_version"] = getattr(cubevis, "__version__", None)
        except Exception:
            pass
        dbg = "/tmp/cubevis_frame_debug2.log"
        info["frame_debug_log_bytes"] = os.path.getsize(dbg) if os.path.exists(dbg) else 0
        return info

    def call_stats(self, reset: bool = False) -> dict:
        """``{method: [count, total_s, max_s, last_s]}`` measured in the worker."""
        try:
            stats = object.__getattribute__(self, "_cv_stats")
        except AttributeError:
            stats = {}
        out = {k: list(v) for k, v in stats.items()}
        try:   # this worker's relay encode/decode totals (not per method)
            from cubevis.remote._worker_transport import FRAME_STATS
            out["__frames__"] = dict(FRAME_STATS)
            if reset:
                for k in FRAME_STATS:
                    FRAME_STATS[k] = 0
        except Exception:
            pass
        if reset:
            object.__setattr__(self, "_cv_stats", {})
        return out

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
                       width: int = 800, height: int = 600,
                       probe_grid_max_cells: int = 3072,
                       ref_scale: Optional[float] = None):
        # See RemoteReductionContext.query_columns's comment: this now
        # relays a bounded ScatterRenderResult, not raw DataFrames --
        # MSv2Backend.query_columns does the binning+shading (2026-09).
        # probe_grid_max_cells added (2026-09, hover-probe redesign
        # piece 2) -- forwarded like every other keyword here.
        # ref_scale added (2026-09, two-level rendering) -- forwarded the
        # same way. MISSED in that pass's first cut: this worker-side
        # wrapper is a separate, explicit signature from
        # LocalVisibilityReader.query_columns/MSv2Backend.query_columns
        # (deliberately -- see this class's docstring, "does not know it
        # is being driven remotely"), so adding a parameter to those
        # doesn't automatically reach here; it has to be added here too,
        # by hand, every time. Confirmed via a real remote-kernel test
        # failure (TypeError: got an unexpected keyword argument
        # 'ref_scale') rather than caught before shipping -- this file
        # runs only inside a worker subprocess, which nothing in the
        # sandbox that produced the rest of that change could reach.
        return self._reader.query_columns(
            xaxis, layers, selection,
            x_range=x_range, y_range=y_range, color_mode=color_mode,
            width=width, height=height,
            probe_grid_max_cells=probe_grid_max_cells,
            ref_scale=ref_scale,
        )

    def probe_scatter_region(self, x_axis, yaxes, selection, x_range, y_range,
                              max_samples: int = 200_000):
        return self._reader.probe_scatter_region(
            x_axis, yaxes, selection, x_range, y_range,
            max_samples=max_samples,
        )

    def identity_tables(self, selection, *, polarization=None):
        return self._reader.identity_tables(selection, polarization=polarization)

    # ------------------------------------------------------------------ #
    # FlagDB v2 -- pending flags live with the data (see flag_engine)     #
    # ------------------------------------------------------------------ #

    def set_pending_flags(self, deltas=None, version: int = 0, apply: bool = True,
                          proposal=None, deltas_json: Optional[str] = None,
                          proposal_json: Optional[str] = None):
        # Preferred form: one JSON string (``deltas_json``).  Nested lists of
        # dicts are slow through the Bokeh wire serializer -- 100 region
        # deltas naming 325 baselines each cost ~0.7 s that way
        # (bench_remote_overhead.py, 2026-09-28).
        import json
        from cubevis.toolbox.visplot.flag_model import FlagDelta
        if deltas_json is not None:
            deltas = json.loads(deltas_json)
        if proposal_json is not None:
            proposal = json.loads(proposal_json)
        objs = [d if isinstance(d, FlagDelta) else FlagDelta.from_dict(d)
                for d in (deltas or [])]
        # Full state: (re)seed the per-delta cache sync_pending_flags uses.
        object.__setattr__(self, "_cv_delta_cache", {d.delta_id: d for d in objs})
        self._reader.set_pending_flags(objs, version, apply, proposal)
        return True

    def sync_pending_flags(self, order, new_json: str = "[]", version: int = 0,
                           proposal_json: Optional[str] = None):
        """Incremental form of ``set_pending_flags``: *order* is the full
        ordered list of delta ids; only deltas this worker has not seen are
        sent (*new_json*).  Deltas are immutable, so an id always means the
        same delta.  Raises ``KeyError`` for an unknown id -- the client then
        falls back to a full ``set_pending_flags``."""
        import json
        from cubevis.toolbox.visplot.flag_model import FlagDelta
        try:
            cache = object.__getattribute__(self, "_cv_delta_cache")
        except AttributeError:
            cache = {}
        for d in json.loads(new_json or "[]"):
            fd = FlagDelta.from_dict(d)
            cache[fd.delta_id] = fd
        missing = [i for i in order if i not in cache]
        if missing:
            raise KeyError(f"unknown pending delta id(s): {missing[:3]}")
        # keep only what is still referenced (undone/cleared deltas drop out)
        keep = set(order)
        cache = {k: v for k, v in cache.items() if k in keep}
        object.__setattr__(self, "_cv_delta_cache", cache)
        proposal = json.loads(proposal_json) if proposal_json else None
        self._reader.set_pending_flags([cache[i] for i in order], version, True, proposal)
        return True

    def evaluate_flag_request(self, request: dict):
        # Only built-in filters can run here; a user callable never crosses
        # the wire (evaluate_request raises a clear KeyError for it).
        import logging, time as _t
        request = dict(request)
        request.pop("filter_obj", None)
        t0 = _t.perf_counter()
        result = self._reader.evaluate_flag_request(request)
        logging.getLogger(__name__).debug(
            "worker evaluate_flag_request: kind=%s %.3fs", request.get("kind"),
            _t.perf_counter() - t0)
        return _as_json(result)

    def flag_commit_capabilities(self):
        return self._reader.flag_commit_capabilities()

    def commit_pending_flags(self, deltas_json: str, options_json: str = "{}"):
        import json
        return _wire_safe(self._reader.commit_pending_flags(json.loads(deltas_json),
                                                            **json.loads(options_json)))

    def set_frame_cache_limit_mb(self, mb: float):
        self._reader.set_frame_cache_limit_mb(float(mb))
        return True

    def list_flag_backups(self):
        return _wire_safe(self._reader.list_flag_backups())

    def restore_flag_backup(self, backup_path: str):
        return _wire_safe(self._reader.restore_flag_backup(backup_path))

    def probe_flag_region(self, request: dict):
        return _wire_safe(self._reader.probe_flag_region(dict(request)))

    def spw_casa_ids(self):
        return [[k.to_dict(), int(v)] for k, v in self._reader.spw_casa_ids().items()]

    def flag_spw_table(self):
        return self._reader.flag_spw_table()

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


def _as_json(result: dict) -> str:
    """A flag-evaluation result as one JSON string (fast on the wire; the
    delta in its compact ``to_dict(json_safe=True)`` form)."""
    import json
    from cubevis.toolbox.visplot.flag_model import FlagDelta
    out = dict(result)
    d = out.get("delta")
    if d is not None:
        out["delta"] = FlagDelta.from_dict(d).to_dict(json_safe=True)
    return json.dumps(_wire_safe(out))


def _wire_safe(obj):
    """Convert a flag-evaluation result to JSON-like data for the wire."""
    import numpy as np
    if isinstance(obj, dict):
        return {str(k): _wire_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_wire_safe(v) for v in obj]
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    if isinstance(obj, np.generic):
        return obj.item()
    return obj


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
