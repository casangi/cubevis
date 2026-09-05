"""remote_reduction_context.py
==============================
``RemoteReductionContext`` — Chunk 2's implementation of the ``visplot``
remote data path.

Satisfies **both** ``ReductionContext`` (``reduction_context.py``) and
``VisibilityReader`` (``visibility_reader.py``) at once, per
``reduction_context.py``'s own docstring:  one object, passed as both
the ``reader`` and ``context`` arguments to ``VisibilityPlotter``.

Every method below is delegated, via ``call_method``, to a
``VisplotRemoteBackend`` instance living in a dedicated execution
context on a remote (or local, for testing) Jupyter kernel reached
through ``cubevis.remote``.  See ``remote_registrations.py`` for the
worker-side half of this pair.

Scope of this chunk
--------------------
Chunk 2 is the **data path** — reading and visualizing.  Calibration
and flag-writing (``bandpass``, ``commit_flags``, ``split``, ...) are
explicitly out of scope here and raise ``NotImplementedError``, exactly
as ``_make_casa6_context``/``_make_radps_context`` already do for their
own not-yet-implemented paths.  ``supports_calibration()`` returns
``False`` accordingly, so ``VisibilityPlotter`` disables the calibration
buttons for a remote session, same as it would for
``NullReductionContext``.

Three things worth reading before touching this file
------------------------------------------------------
1. **SyncBridge loop affinity (developer guide §6).**  Every method on
   ``VisibilityReader`` is a plain, synchronous ``def`` — ``VisibilityRaster``
   /``VisibilityScatter`` call them from ordinary (non-async) code and
   immediately unpack the return value.  So every method here blocks via
   ``SyncBridge.run(...)``.  The subtlety: ``RemoteAppLink.open()``
   builds its own internal ``SyncBridge`` (``link.sync_bridge``), but
   that bridge runs on a *different* event loop than the one
   ``mgr``/``transport`` were actually constructed on (whichever loop
   was running ``open()`` itself) — using ``link.sync_bridge`` to drive
   later calls would violate the "same loop, always" rule and deadlock
   silently (no exception — see developer guide §6). This class avoids
   that by owning **its own** ``SyncBridge`` and using it to *run*
   ``RemoteAppLink.open()`` in the first place, so ``mgr``/``transport``
   end up constructed on, and always driven from, this same bridge's
   loop.  ``link.sync_bridge`` is never touched.

2. **``call_method``'s convenience wrapper does not check for errors**
   (developer guide §3).  ``ExecutionContext.call_method()`` /
   ``create_object()`` return the raw reply from the worker, including
   an ``{"error": ..., "traceback": ...}`` shaped reply on the worker
   method raising — they do *not* raise a Python exception for you, and
   ``create_object()``'s own ``reply["handle"]`` will raise a confusing
   ``KeyError`` instead of surfacing the real remote error.  This class
   therefore calls ``dispatch_fast`` directly (bypassing both
   convenience wrappers) and checks for ``"error"`` itself, raising
   ``RemoteBackendError`` with the remote traceback attached.

3. **Wire serialization of ``Axis``/``SelectionSpec`` is UNVERIFIED.**
   ``cubevis.utils.serialize``/``deserialize`` is confirmed (by
   ``test_object_registry_e2e.py``) to round-trip a real numpy array.
   Whether it also round-trips a plain ``Axis`` (an ``Enum``, not a
   ``str`` subclass) and a ``SelectionSpec`` (a plain ``@dataclass``,
   not a numpy/pandas/JSON-primitive type) has **not** been checked
   against the actual serializer implementation as part of this pass —
   that file wasn't in scope here.  Run a `query_raster` round trip
   against a local kernel early (per the handoff's own "suggested first
   milestone") to confirm this before assuming the rest of this file is
   correct; if it turns out ``Axis``/``SelectionSpec`` don't round-trip
   as-is, the fix belongs in ``cubevis.utils.serialize`` (register a
   custom encoder) or in a thin encode/decode shim in this file and in
   ``remote_registrations.py`` — not by changing the ``VisibilityReader``
   protocol's signatures.

Construction blocks
--------------------
``RemoteReductionContext.__init__`` is itself synchronous and blocks
for the *entire* connect sequence: kernel start, ``RemoteAppLink.open()``
(bootstrap cell + comm handshake), ``create_context()`` (worker
subprocess spawn + its own opening ``configure`` round trip), and one
``create_object()`` call.  Against a local kernel this is on the order
of a few seconds; against a real ``sshpyk``-provisioned cluster kernel
on first connect, the developer guide §5 measured roughly two and a
half minutes.  This happens today inside ``VisibilityPlotter.__init__``
(``open_ms``/``open_ps`` are plain synchronous functions), so
constructing a remote-backed ``VisibilityPlotter(...)`` is expected to
block the caller for that long the first time.  There is deliberately
no async/lazy-connect path here yet — flagged as a possible follow-on,
not built speculatively ahead of it being needed.

Lifecycle
---------
Nothing calls ``close()`` automatically.  ``VisibilityPlotter`` doesn't
close backends on shutdown today even for the local case (file handles
close on process exit) — but a remote session holds a live SSH-tunneled
kernel plus a spawned worker subprocess on a cluster node, which will
leak indefinitely if nothing ever calls ``close()``.  See the patch
notes for wiring ``close()`` into ``VisibilityPlotter``'s
``_shutdown_handler`` (session ending for good) and specifically NOT
into ``_connection_closed_handler`` (transient disconnect — the whole
point of the reconnection design is that the remote session survives
those).

Package location (proposed)
----------------------------
``cubevis/cubevis/toolbox/visplot/remote_reduction_context.py``
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional, TYPE_CHECKING
from uuid import uuid4

import pandas as pd
import xarray as xr

from .reduction_context import (
    AntennaInfo,
    ApplycalParams,
    BandpassParams,
    CaltableInfo,
    FieldInfo,
    FlagDelta,
    FlagSummary,
    FlagVersionInfo,
    FluxscaleParams,
    GaincalParams,
    ObservationMetadata,
    ReductionContext,
    ReductionOperation,
    ReductionResult,
    ScanInfo,
    SplitParams,
)

if TYPE_CHECKING:
    from concurrent.futures import Future
    from ..axes import Axis
    from ..selection import SelectionSpec

log = logging.getLogger(__name__)

# Dotted path resolved *inside the worker subprocess* -- see
# remote_registrations.py's module docstring for why it lives in an
# installable location rather than under a tests/ directory.
DEFAULT_REGISTER_FUNCTION = (
    "cubevis.toolbox.visplot.remote_registrations:register"
)
_BACKEND_CLASS_NAME = "VisplotRemoteBackend"

# Handoff §3's suggestion: "pass a smaller max_cells by default when
# dispatching remotely than the local 2M default, trading resolution
# for bandwidth". This number is a placeholder, not a measured value --
# tune it against real remote bandwidth before shipping. NOTE: as
# written, VisibilityRaster always passes an explicit max_cells (it
# falls back to ITS OWN default, not this one, when the caller didn't
# override) -- see the patch notes for why this constant needs to be
# threaded into VisibilityRaster's own construction in
# VisibilityPlotter._build_panels(), not just kept here.
DEFAULT_REMOTE_MAX_CELLS = 500_000

# See class docstring, point 1: create_context can legitimately take
# much longer on a real cluster kernel than the framework's own default
# already assumes. Kept here as a named constant so a caller building a
# RemoteReductionContext for a known-slow host can raise it, without
# needing to know create_context()'s own default lives in _link.py.
DEFAULT_OPEN_TIMEOUT = 60.0
DEFAULT_CREATE_CONTEXT_TIMEOUT = 180.0


class RemoteBackendError(RuntimeError):
    """Raised when the remote ``VisplotRemoteBackend`` method call
    itself failed (an exception inside the worker subprocess) — as
    opposed to a transport-level failure (timeout, connection lost),
    which surfaces as whatever exception ``cubevis.remote`` itself
    raises (``asyncio.TimeoutError``, etc.).

    Carries the remote traceback text so it shows up in a local
    stack trace instead of vanishing into an opaque dict.
    """

    def __init__(self, method: str, reply: Dict[str, Any]):
        self.method = method
        self.remote_error = reply.get("error")
        self.remote_traceback = reply.get("traceback", "")
        msg = f"remote call to {method!r} failed: {self.remote_error}"
        if self.remote_traceback:
            msg += f"\n--- remote traceback ---\n{self.remote_traceback}"
        super().__init__(msg)


class RemoteReductionContext(ReductionContext):
    """Satisfies both ``ReductionContext`` and ``VisibilityReader`` by
    delegating to a ``VisplotRemoteBackend`` object living in a
    dedicated execution context on a remote kernel.

    Parameters
    ----------
    path : str
        Path to the MS / Processing Set, resolved **on the remote
        host**, not locally — this class never opens ``path`` itself.
    kernel_name : str
        A kernelspec name resolvable by ``jupyter kernelspec list`` on
        this machine (an ``sshpyk``-provisioned remote kernel, or
        ``"python3"`` for local testing).  Passed straight through to
        ``AsyncKernelManager(kernel_name=...)`` — per the project's own
        confirmed finding, this is the *entire* local/remote switch;
        nothing else about this class changes based on it.
    backend_kind : str
        ``"msv2"`` or ``"msv4"`` — which backend
        ``VisplotRemoteBackend`` should construct on the worker side.
    worker_target_name, register_function, open_timeout,
    create_context_timeout, max_cells :
        See the corresponding constants above / ``cubevis.remote``
        defaults.  Exposed as constructor arguments so a caller with an
        unusually slow host, or a customized registration function, can
        override them without subclassing.
    """

    def __init__(
        self,
        path: str,
        kernel_name: str,
        *,
        backend_kind: str = "msv2",
        worker_target_name: Optional[str] = None,
        register_function: str = DEFAULT_REGISTER_FUNCTION,
        max_cells: int = DEFAULT_REMOTE_MAX_CELLS,
        open_timeout: float = DEFAULT_OPEN_TIMEOUT,
        create_context_timeout: float = DEFAULT_CREATE_CONTEXT_TIMEOUT,
    ) -> None:
        if backend_kind not in ("msv2", "msv4"):
            raise ValueError(
                f"backend_kind must be 'msv2' or 'msv4'; got {backend_kind!r}"
            )
        if not kernel_name:
            raise ValueError(
                "RemoteReductionContext requires kernel_name= (a kernelspec "
                "name resolvable by `jupyter kernelspec list`)."
            )

        # Local imports -- these pull in jupyter_client/cubevis.remote,
        # which every OTHER backend (casa6/radps/null) has no reason to
        # import at all. Matches the existing lazy-import convention in
        # visibility_plotter.py's own _resolve_context_* functions.
        from jupyter_client import AsyncKernelManager
        from cubevis.remote import RemoteAppLink, SyncBridge, DEFAULT_WORKER_TARGET_NAME

        self._path = path
        self._kernel_name = kernel_name
        self._backend_kind = backend_kind
        self._max_cells = max_cells
        self._closed = False

        # See module docstring, point 1: this bridge is the ONE loop
        # everything below is constructed on and driven from. It is
        # deliberately NOT the same object as `self._link.sync_bridge`
        # (which RemoteAppLink.open() builds internally, on a different
        # loop, and which this class never touches).
        self._bridge = SyncBridge(name=f"visplot-remote-{uuid4().hex[:8]}")
        self._bridge.start()

        self._km = AsyncKernelManager(kernel_name=kernel_name)
        log.info(
            "RemoteReductionContext: connecting kernel_name=%r "
            "(path=%r, backend_kind=%r) -- this can take from a few "
            "seconds (local kernel) to a couple of minutes (first "
            "connect to a real cluster kernel)",
            kernel_name, path, backend_kind,
        )
        # Phase timing -- connect time turned out to NOT be dominated by
        # kernel startup alone (measured 51s of a 154s connect against a
        # real sshpyk cluster kernel); logging each phase separately here
        # is cheap and turns "connect is slow" into "THIS phase is slow",
        # rather than re-deriving it from raw sshpyk log timestamps by
        # hand every time, as this number was.
        t0 = time.perf_counter()
        self._bridge.run(self._km.start_kernel())
        t1 = time.perf_counter()
        log.info("RemoteReductionContext: start_kernel() took %.1fs", t1 - t0)
        try:
            self._link = self._bridge.run(
                RemoteAppLink.open(
                    self._km,
                    worker_target_name=worker_target_name or DEFAULT_WORKER_TARGET_NAME,
                    timeout=open_timeout,
                )
            )
            t2 = time.perf_counter()
            log.info("RemoteReductionContext: RemoteAppLink.open() took %.1fs", t2 - t1)
            self._ctx = self._bridge.run(
                self._link.create_context(
                    config={"register_function": register_function},
                    timeout=create_context_timeout,
                )
            )
            t3 = time.perf_counter()
            log.info("RemoteReductionContext: create_context() took %.1fs "
                      "(worker subprocess spawn + register_function import)", t3 - t2)
            self._handle = self._bridge.run(
                self._acreate_object(
                    _BACKEND_CLASS_NAME,
                    kwargs={"path": path, "backend_kind": backend_kind},
                )
            )
            t4 = time.perf_counter()
            log.info("RemoteReductionContext: create_object() took %.1fs "
                      "(includes opening the MS/PS on the remote host)", t4 - t3)
        except BaseException:
            # Don't leak a half-connected kernel if any step above
            # fails -- best-effort, and deliberately swallows its own
            # errors so the ORIGINAL failure is what the caller sees.
            try:
                self._bridge.run(self._km.shutdown_kernel())
            except Exception:
                log.warning(
                    "RemoteReductionContext: cleanup after failed connect "
                    "also failed", exc_info=True,
                )
            self._bridge.stop()
            raise

        # Fetched once, up front -- open_ms/open_ps need an
        # ObservationMetadata immediately, and every ReductionContext
        # list_*() method below is served from this same cached copy
        # rather than a fresh remote round trip per call. If metadata
        # can change server-side during a session (e.g. after a future
        # split()), this will need an explicit refresh() -- not needed
        # for Chunk 2's read-only scope.
        self._meta = ObservationMetadata.from_backend_metadata(
            self._call("metadata"), source_path=path
        )

    # ------------------------------------------------------------------ #
    # Internal call plumbing -- see module docstring, point 2            #
    # ------------------------------------------------------------------ #

    async def _acreate_object(self, class_name: str, args: Optional[List[Any]] = None,
                               kwargs: Optional[Dict[str, Any]] = None) -> str:
        reply = await self._ctx.dispatch_fast(
            "create_object",
            {"class_name": class_name, "args": args or [], "kwargs": kwargs or {}},
        )
        if isinstance(reply, dict) and "error" in reply:
            raise RemoteBackendError(f"create_object({class_name!r})", reply)
        return reply["handle"]

    async def _acall(self, method: str, **kwargs: Any) -> Any:
        reply = await self._ctx.dispatch_fast(
            "call_method",
            {"handle": self._handle, "method": method, "args": [], "kwargs": kwargs},
        )
        if isinstance(reply, dict) and "error" in reply:
            raise RemoteBackendError(method, reply)
        return reply

    def _call(self, method: str, **kwargs: Any) -> Any:
        """Synchronous entry point every VisibilityReader/ReductionContext
        method below uses -- see module docstring, point 1."""
        return self._bridge.run(self._acall(method, **kwargs))

    # ------------------------------------------------------------------ #
    # VisibilityReader protocol                                           #
    # ------------------------------------------------------------------ #

    def query_raster(
        self,
        y_dim: "Axis",
        x_dim: "Axis",
        quantity: "Axis",
        selection: "SelectionSpec",
        polarization: Optional[str] = None,
        max_cells: int = 2_000_000,
    ) -> tuple:
        return self._call(
            "query_raster",
            y_dim=y_dim, x_dim=x_dim, quantity=quantity, selection=selection,
            polarization=polarization, max_cells=max_cells,
        )

    def query_columns(
        self,
        xaxis: "Axis",
        yaxes: list,
        selection: "SelectionSpec",
        *,
        canvas_width: int = 800,
        canvas_height: int = 600,
    ) -> Dict[Any, pd.DataFrame]:
        # STRAIGHT RELAY -- NOT the handoff §4 fix. VisplotRemoteBackend's
        # query_columns is, as of this pass, still MSv2Backend's existing
        # eager, unbounded-materialization implementation, just running
        # on the remote host instead of locally. A broad scatter
        # selection against a huge remote MS is exactly as memory-unsafe
        # remotely as it already is locally today -- moving it off
        # P_local doesn't fix that, and this relay makes no attempt to.
        # The real fix (lazy Dask -> Canvas.points(), no intermediate
        # materialization) is separate, real design work -- see the
        # handoff's §4/§5 -- and belongs in MSv2Backend/MSv4Backend
        # itself, independent of remoting.
        return self._call(
            "query_columns",
            xaxis=xaxis, yaxes=yaxes, selection=selection,
            canvas_width=canvas_width, canvas_height=canvas_height,
        )

    def probe_raster_pixel(
        self,
        raw_grid: xr.DataArray,
        gx: int,
        gy: int,
        selection: "SelectionSpec",
    ) -> dict:
        return self._call(
            "probe_raster_pixel", raw_grid=raw_grid, gx=gx, gy=gy, selection=selection
        )

    def probe_scatter_pixel(
        self,
        canvas_agg: xr.DataArray,
        px: int,
        py: int,
        selection: "SelectionSpec",
        scatter_df: pd.DataFrame,
    ) -> dict:
        return self._call(
            "probe_scatter_pixel", canvas_agg=canvas_agg, px=px, py=py,
            selection=selection, scatter_df=scatter_df,
        )

    # ------------------------------------------------------------------ #
    # Extra methods LocalVisibilityReader also exposes (not part of the  #
    # formal VisibilityReader Protocol, but required -- see the handoff  #
    # §2: "Two more methods are needed beyond the formal protocol").     #
    # ------------------------------------------------------------------ #

    def metadata(self) -> dict:
        return self._call("metadata")

    def axis_info(self, axis: "Axis", selection: Optional["SelectionSpec"] = None,
                  query: str = "columns"):
        return self._call("axis_info", axis=axis, selection=selection, query=query)

    def available_axes(self):
        return self._call("available_axes")

    def metadata_dto(self) -> ObservationMetadata:
        """Cached ObservationMetadata built at construction time -- avoids
        a second remote round trip from open_ms()/open_ps()."""
        return self._meta

    # ------------------------------------------------------------------ #
    # ReductionContext -- observation metadata                            #
    # ------------------------------------------------------------------ #
    # Served from the ObservationMetadata cached at construction time     #
    # (see __init__) rather than a fresh remote round trip per call.      #

    def list_fields(self) -> List[FieldInfo]:
        return list(self._meta.fields)

    def list_spws(self):
        return list(self._meta.spws)

    def list_antennas(self) -> List[AntennaInfo]:
        return list(self._meta.antennas)

    def list_scans(self) -> List[ScanInfo]:
        return list(self._meta.scans)

    def list_data_columns(self) -> List[str]:
        return list(self._meta.data_columns)

    def list_caltables(self) -> List[CaltableInfo]:
        return []  # not yet implemented remotely -- calibration is out of Chunk 2 scope

    def list_flag_versions(self) -> List[FlagVersionInfo]:
        return []  # ditto

    # ------------------------------------------------------------------ #
    # ReductionContext -- out of scope for Chunk 2                        #
    # ------------------------------------------------------------------ #
    # visplot's remote DATA path (read + visualize) is this chunk's       #
    # whole scope. Flag-writing and calibration are real, just not here — #
    # raising NotImplementedError rather than silently no-op'ing, per     #
    # ReductionContext's own ABC docstring convention (matches            #
    # _make_casa6_context/_make_radps_context's existing style).          #

    def _not_implemented(self, name: str):
        raise NotImplementedError(
            f"RemoteReductionContext: {name}() is not implemented. "
            f"Chunk 2 scope is read-only remote visualization; "
            f"calibration/flag-writing is a future chunk."
        )

    def commit_flags(self, flag_deltas: List[FlagDelta]) -> FlagSummary:
        self._not_implemented("commit_flags")

    def save_flag_version(self, name: str, comment: str = "") -> None:
        self._not_implemented("save_flag_version")

    def restore_flag_version(self, name: str) -> None:
        self._not_implemented("restore_flag_version")

    def bandpass(self, params: BandpassParams) -> CaltableInfo:
        self._not_implemented("bandpass")

    def gaincal(self, params: GaincalParams) -> CaltableInfo:
        self._not_implemented("gaincal")

    def fluxscale(self, params: FluxscaleParams) -> CaltableInfo:
        self._not_implemented("fluxscale")

    def applycal(self, params: ApplycalParams) -> None:
        self._not_implemented("applycal")

    def split(self, params: SplitParams) -> "ReductionContext":
        self._not_implemented("split")

    def submit(self, operation: ReductionOperation) -> "Future[ReductionResult]":
        # Also genuinely undesigned per the handoff §2 -- the
        # Future-bridge mechanism this needs is real, first-time design
        # work, not a mechanical NotImplementedError like the methods
        # above. Left raising for now; revisit only if visplot's actual
        # usage needs submit() (check current call sites before
        # assuming it's in scope for this chunk).
        self._not_implemented("submit")

    def supports_calibration(self) -> bool:
        return False

    def supports_remote_execution(self) -> bool:
        return True

    # ------------------------------------------------------------------ #
    # Lifecycle                                                            #
    # ------------------------------------------------------------------ #

    def close(self, timeout: float = 20.0) -> None:
        """Tears down the remote worker subprocess, the supervisor-kernel
        link, and the kernel itself. Idempotent.

        Nothing calls this automatically today -- see the module
        docstring's "Lifecycle" section and the patch notes for wiring
        this into ``VisibilityPlotter``'s ``_shutdown_handler``.
        """
        if self._closed:
            return
        try:
            self._bridge.run(self._link.close(), timeout=timeout)
        except Exception:
            log.warning("RemoteReductionContext.close: link.close() failed",
                        exc_info=True)
        try:
            self._bridge.run(self._km.shutdown_kernel())
        except Exception:
            log.warning("RemoteReductionContext.close: shutdown_kernel() failed",
                        exc_info=True)
        self._bridge.stop()
        self._closed = True

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"RemoteReductionContext(path={self._path!r}, "
            f"kernel_name={self._kernel_name!r}, "
            f"backend_kind={self._backend_kind!r}, closed={self._closed})"
        )


# ======================================================================
# Note on protocol verification
# ======================================================================
# local_visibility_reader.py asserts isinstance(obj, VisibilityReader) at
# import time using LocalVisibilityReader.__new__(...) -- a real instance
# is never actually needed just to check structural conformance, since
# @runtime_checkable Protocol isinstance checks only look at attribute
# *names* being present, not at __init__ having run. The same trick does
# NOT carry over cleanly here: RemoteReductionContext.__new__(...) would
# still pass the same structural check (the methods are defined on the
# class either way), so a hypothetical _assert_protocol() here would be
# checking nothing __init__-specific and would just duplicate the local
# reader's check for no added confidence. The real conformance proof for
# THIS class is behavioral, not structural -- a live query_raster() round
# trip against a real (local, for CI) kernel, per the handoff's own
# definition of done ("confirmed against a real MS ... not a mock").
