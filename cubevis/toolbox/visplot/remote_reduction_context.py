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

3. **Wire serialization of ``Axis``/``SelectionSpec``, confirmed correct
   (2026-09, Chunk 2d).** ``test_remote_reduction_context.py::test_
   query_raster_matches_local`` round-trips both through the real
   ``cubevis.utils.serialize``/``deserialize`` path and asserts the
   remote result is numerically identical to the equivalent local call
   -- confirmed on both MSv2 and MSv4. First confirmed against a local
   kernel standing in for a real one (see ``ways-of-working.md``: an
   acceptable proxy, modulo the latency a real remote connection would
   add); a first real-``zuul06`` run of this suite that same week
   failed earlier, at ``create_object()`` (a real MS-path-not-
   resolvable-on-the-remote-host problem, unrelated to serialization --
   ``create_object`` only ever sends plain strings, no
   ``Axis``/``SelectionSpec`` involved -- fixed by the ``CUBEVIS_TEST_KERNEL_MS``/
   ``CUBEVIS_TEST_KERNEL_PS`` environment variables that test file's own docstring now
   documents). With that fixed, a subsequent real-``zuul06`` run of the
   full suite passed this test along with everything else -- this
   mechanism is now confirmed correct against a genuine remote cluster
   kernel, not just the local-kernel proxy.

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

from . import _wire_types  # noqa: F401 -- import for its Serializer/Deserializer
                            # registration side effect (xr.DataArray wire
                            # support), not for any name used directly here.
                            # Must happen before the first real call crosses
                            # the wire; module-import caching makes this a
                            # no-op on any subsequent import within the
                            # process. See _wire_types.py's own docstring for
                            # why this lives here and not in
                            # cubevis.utils._conversion.

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

# Matches _link.py's own _DEFAULT_CALL_TIMEOUT -- dispatch_fast's
# built-in default -- so leaving this alone changes nothing about
# today's behavior. Exposed here (Chunk 2d) after an unrestricted
# query_raster() was observed to occasionally exceed it on a cold
# worker (first MS open in a fresh subprocess) even though the
# equivalent local call was fast -- see test_remote_reduction_context.py's
# module docstring for the investigation. Before this, RemoteReductionContext
# had no way to raise this budget from its public API at all: dispatch_fast's
# timeout was hardwired past _acall/_call with no override.
DEFAULT_CALL_TIMEOUT = 30.0


class UserFilterNotRemoteError(RuntimeError):
    """A user-supplied (Python callable) flag filter was used with remote
    data.  Callables never cross the wire; built-in filters run remotely."""


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
    create_context_timeout, call_timeout, max_cells :
        See the corresponding constants above / ``cubevis.remote``
        defaults.  Exposed as constructor arguments so a caller with an
        unusually slow host, or a customized registration function, can
        override them without subclassing.  ``call_timeout`` sets the
        default budget for every ``VisibilityReader``/``ReductionContext``
        method below (``query_raster``, ``metadata``, etc.) -- each of
        those also accepts its own ``timeout=`` to override just that
        one call without changing the instance-wide default.
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
        call_timeout: float = DEFAULT_CALL_TIMEOUT,
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
        self._call_timeout = call_timeout
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
                    config={
                        "register_function": register_function,
                        # Same reasoning as register_function, one layer
                        # up: the supervisor relays every message between
                        # P_local and the worker and must be able to
                        # deserialize() them to do so, including this
                        # application's own wire types (xr.DataArray) --
                        # see _supervisor.py's _handle_create_context and
                        # _wire_types.py's own docstring for why this
                        # can't just be hardcoded into the generic
                        # supervisor instead.
                        "wire_types": ["cubevis.toolbox.visplot._wire_types"],
                    },
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

            # Fetched once, up front -- open_ms/open_ps need an
            # ObservationMetadata immediately, and every ReductionContext
            # list_*() method below is served from this same cached copy
            # rather than a fresh remote round trip per call. If metadata
            # can change server-side during a session (e.g. after a future
            # split()), this will need an explicit refresh() -- not needed
            # for Chunk 2's read-only scope.
            #
            # INSIDE the try block deliberately (moved here 2026-09,
            # Chunk 2d): this call was previously issued after the
            # try/except below, so a failure here -- including, now, a
            # too-tight call_timeout= -- skipped the same-shaped cleanup
            # every earlier failure in this constructor already gets,
            # leaking the kernel and worker subprocess. Confirmed as a
            # real leak (not just theoretical) by triggering it directly
            # against a local kernel: the kernel process was still alive
            # after the exception propagated, and only exited because it
            # happened to notice its parent process exit -- a real
            # long-lived caller (VisibilityPlotter itself) would have
            # leaked it indefinitely.
            self._meta = ObservationMetadata.from_backend_metadata(
                self._call("metadata"), source_path=path
            )
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

    async def _acall(self, method: str, *, timeout: Optional[float] = None,
                      **kwargs: Any) -> Any:
        reply = await self._ctx.dispatch_fast(
            "call_method",
            {"handle": self._handle, "method": method, "args": [], "kwargs": kwargs,
             "pre_encoded": True},
            timeout=timeout,
        )
        if isinstance(reply, dict) and "error" in reply:
            raise RemoteBackendError(method, reply)
        if isinstance(reply, dict) and "__cv_pre_encoded__" in reply:
            # Worker-encoded result passed through the kernel untouched
            # (see worker_main.handle_call_method): decode it here, once.
            from cubevis.utils import remote_deserialize
            t0 = time.perf_counter()
            reply = remote_deserialize(reply["__cv_pre_encoded__"])
            try:
                from cubevis.remote._kernel_transport import CLIENT_STATS
                CLIENT_STATS["decode_s"] += time.perf_counter() - t0
            except Exception:
                pass
        return reply

    def _call(self, method: str, *, timeout: Optional[float] = None,
              **kwargs: Any) -> Any:
        """Synchronous entry point every VisibilityReader/ReductionContext
        method below uses -- see module docstring, point 1.

        ``timeout=None`` (the default every call site below uses unless
        its own ``timeout=`` was given) resolves to this instance's
        ``_call_timeout`` (``call_timeout=`` at construction, itself
        defaulting to ``DEFAULT_CALL_TIMEOUT``) -- so leaving both alone
        changes nothing about today's behavior. A caller that knows one
        particular call needs longer (or shorter) can pass ``timeout=``
        on that call alone without touching the instance-wide default.
        """
        effective_timeout = self._call_timeout if timeout is None else timeout
        t0 = time.perf_counter()
        ok = False
        try:
            out = self._bridge.run(self._acall(method, timeout=effective_timeout, **kwargs))
            ok = True
            return out
        finally:
            dt = time.perf_counter() - t0
            stats = self.__dict__.setdefault("_cv_call_stats", {})
            n, tot, mx, last = stats.get(method, (0, 0.0, 0.0, 0.0))
            stats[method] = (n + 1, tot + dt, max(mx, dt), dt)
            log.debug("remote %s: %.4fs%s", method, dt, "" if ok else " (FAILED)")

    def runtime_info(self, timeout: Optional[float] = None) -> dict:
        """What the remote worker is running (paths, versions, whether the
        relay counters exist).  ``{"error": ...}`` if the remote cubevis is
        too old to answer."""
        try:
            return self._call("runtime_info", timeout=timeout)
        except Exception as exc:
            return {"error": f"remote cubevis has no runtime_info ({exc}); it predates "
                             "this client -- update the kernel environment"}

    # ------------------------------------------------------------------ #
    # Worker-side code and re-targeting (tests, diagnostics)              #
    # ------------------------------------------------------------------ #

    def eval_code(self, code: str, timeout: Optional[float] = None):
        """Evaluate a Python expression in the worker (on the kernel host)
        and return its value -- e.g. to inspect or prepare files there."""
        import asyncio
        return self._bridge.run(asyncio.wait_for(self._ctx.eval_code(code),
                                                 timeout or self._call_timeout or 600))

    def exec_code(self, code: str, timeout: Optional[float] = None):
        """Execute Python statements in the worker; ``_result`` is returned."""
        import asyncio
        return self._bridge.run(asyncio.wait_for(self._ctx.exec_code(code),
                                                 timeout or self._call_timeout or 600))

    def reopen(self, path: str, backend_kind: Optional[str] = None) -> None:
        """Point this session at another data set on the kernel host,
        reusing the running kernel and worker (no new connection: on
        zuul06 that saves ~2.5 minutes).  Caches tied to the old data are
        dropped and metadata is refreshed."""
        kind = backend_kind or self._backend_kind
        self._handle = self._bridge.run(self._acreate_object(
            _BACKEND_CLASS_NAME, kwargs={"path": path, "backend_kind": kind}))
        self._path, self._backend_kind = path, kind
        for k in ("_cv_memo", "_cv_sent_ids"):
            self.__dict__.pop(k, None)
        self._meta = ObservationMetadata.from_backend_metadata(
            self._call("metadata"), source_path=path)

    def call_stats(self, reset: bool = False, timeout: Optional[float] = None) -> dict:
        """Round-trip timing per remote method, with the worker's own
        compute time for the same methods.

        ``{"client": {method: [count, total_s, max_s, last_s]},
        "worker": {...same, measured inside the worker...},
        "overhead": {method: mean client - mean worker seconds}}``.  The
        overhead is what the Jupyter-kernel execution model costs per call
        (transport, serialization, dispatch) on top of the computation.
        """
        worker = self._bridge.run(self._acall(
            "call_stats", timeout=self._call_timeout if timeout is None else timeout,
            reset=reset)) or {}
        frames = worker.pop("__frames__", None) if isinstance(worker, dict) else None
        client = {k: list(v) for k, v in self.__dict__.get("_cv_call_stats", {}).items()}
        try:
            from cubevis.remote._kernel_transport import CLIENT_STATS
            relay = {"worker": frames, "client": dict(CLIENT_STATS)}
            if reset:
                for k in CLIENT_STATS:
                    CLIENT_STATS[k] = 0
        except Exception:
            relay = {"worker": frames}
        overhead = {}
        for m, (n, tot, _mx, _l) in client.items():
            w = worker.get(m)
            if w and n and w[0]:
                overhead[m] = tot / n - w[1] / w[0]
        if reset:
            self.__dict__["_cv_call_stats"] = {}
        return {"client": client, "worker": worker, "overhead": overhead, "relay": relay}

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
        timeout: Optional[float] = None,
    ) -> tuple:
        return self._call(
            "query_raster",
            y_dim=y_dim, x_dim=x_dim, quantity=quantity, selection=selection,
            polarization=polarization, max_cells=max_cells,
            timeout=timeout,
        )

    def query_columns(
        self,
        xaxis: "Axis",
        layers: list,
        selection: "SelectionSpec",
        *,
        x_range: Optional[tuple] = None,
        y_range: Optional[tuple] = None,
        color_mode: str = "global",
        width: int = 800,
        height: int = 600,
        probe_grid_max_cells: int = 3072,
        ref_scale: Optional[float] = None,
        timeout: Optional[float] = None,
    ):
        # STRAIGHT RELAY -- and correctly so now. MSv2Backend.query_columns
        # (2026-09 redesign) bins and shades server-side and returns a
        # bounded ScatterRenderResult (an RGBA image + a few small
        # arrays per layer), so a straight relay no longer ships raw
        # rows over the wire the way it did before that redesign -- see
        # ScatterRenderResult's docstring in data/reader.py.
        # MSv4Backend.query_columns matches this contract too (2026-09).
        # probe_grid_max_cells added (2026-09, hover-probe redesign
        # piece 2) -- forwarded like every other keyword here.
        # ref_scale added (2026-09, two-level rendering): when set, each
        # returned layer's ScatterLayerRender.reference carries
        # xr.DataArray fields (the cached aggregations
        # VisibilityScatter resamples LOCALLY for every subsequent
        # pan/zoom -- see the scatter two-level rendering handoff notes
        # §1/§7 for why this must cross the wire ONCE here rather than
        # being rebuilt backend-side on every viewport change). No new
        # wire-type work needed: xr.DataArray already has an encoder/
        # decoder registered by _wire_types.py (imported on both ends --
        # see that module's own docstring), and ScatterLayerReference is
        # a plain dataclass, which composes with it via the existing
        # generic dataclass wire support the same way ScatterLayerSpec
        # already does.
        return self._call(
            "query_columns",
            xaxis=xaxis, layers=layers, selection=selection,
            x_range=x_range, y_range=y_range, color_mode=color_mode,
            width=width, height=height,
            probe_grid_max_cells=probe_grid_max_cells,
            ref_scale=ref_scale,
            timeout=timeout,
        )

    def probe_scatter_region(
        self,
        x_axis: "Axis",
        yaxes: list,
        selection: "SelectionSpec",
        x_range: tuple,
        y_range: tuple,
        max_samples: int = 200_000,
        timeout: Optional[float] = None,
    ) -> dict:
        # STRAIGHT RELAY. Every argument is a plain tuple/list/str/int,
        # an Axis (Enum), or a SelectionSpec (plain dataclass) -- the
        # same wire-safety profile query_raster/query_columns already
        # rely on (see the module docstring's point 3), and the result
        # is a plain dict of dicts, no xr.DataArray/pd.DataFrame
        # involved -- unlike the old probe_scatter_pixel this replaces,
        # no _wire_types registration is needed for this method.
        return self._call(
            "probe_scatter_region", x_axis=x_axis, yaxes=yaxes,
            selection=selection, x_range=x_range, y_range=y_range,
            max_samples=max_samples, timeout=timeout,
        )

    def identity_tables(
        self,
        selection: "SelectionSpec",
        *,
        polarization: Optional[str] = None,
        timeout: Optional[float] = None,
    ):
        return self._memo_call("identity_tables", selection, (polarization,),
                               dict(selection=selection, polarization=polarization,
                                    timeout=timeout))

    # ------------------------------------------------------------------ #
    # Extra methods LocalVisibilityReader also exposes (not part of the  #
    # formal VisibilityReader Protocol, but required -- see the handoff  #
    # §2: "Two more methods are needed beyond the formal protocol").     #
    # ------------------------------------------------------------------ #

    # ------------------------------------------------------------------ #
    # FlagDB v2                                                            #
    # ------------------------------------------------------------------ #

    def set_pending_flags(self, deltas, version: int = 0, apply: bool = True,
                          proposal=None, timeout: Optional[float] = None) -> None:
        """Send the ordered pending deltas (and an optional proposal under
        review) to the worker, where every render applies them.

        Incremental: deltas are immutable and identified by ``delta_id``, so
        after the first full send only the ids (in order) plus the deltas
        the worker has not seen cross the wire (``sync_pending_flags``).  A
        session with many -- or large sample-set -- pending operations no
        longer re-encodes and re-sends all of them on every flag, undo or
        preview.  Any failure falls back to a full ``set_pending_flags``.
        """
        import json
        t0 = time.perf_counter()
        deltas = list(deltas or ())

        def enc(d):
            return d.to_dict(json_safe=True) if hasattr(d, "to_dict") else d

        def did(d):
            return getattr(d, "delta_id", None) or (d.get("delta_id") if isinstance(d, dict) else None)
        ids = [did(d) for d in deltas]
        prop = enc(proposal) if proposal is not None else None
        prop_json = json.dumps(prop) if prop is not None else None
        sent = self.__dict__.get("_cv_sent_ids")
        if sent is not None and all(ids):
            new = [enc(d) for d, i in zip(deltas, ids) if i not in sent]
            new_json = json.dumps(new)
            try:
                self._call("sync_pending_flags", order=ids, new_json=new_json,
                           version=int(version), proposal_json=prop_json, timeout=timeout)
                self.__dict__["_cv_sent_ids"] = set(ids)
                log.debug("remote sync_pending_flags: %d delta(s), %d new, %d bytes%s, %.3fs",
                          len(ids), len(new), len(new_json),
                          " + proposal" if prop is not None else "", time.perf_counter() - t0)
                return
            except Exception as exc:
                log.debug("sync_pending_flags failed (%s); sending full state", exc)
        payload = json.dumps([enc(d) for d in deltas])
        self._call("set_pending_flags", deltas_json=payload, version=int(version),
                   apply=bool(apply), proposal_json=prop_json, timeout=timeout)
        self.__dict__["_cv_sent_ids"] = set(i for i in ids if i)
        log.debug("remote set_pending_flags (full): %d delta(s), %d bytes%s, %.3fs",
                  len(deltas), len(payload), " + proposal" if prop is not None else "",
                  time.perf_counter() - t0)

    def evaluate_flag_request(self, request: dict, timeout: Optional[float] = None) -> dict:
        """Resolve a flag box in the worker (built-in filters only)."""
        fobj = request.get("filter_obj")
        if fobj is not None and not getattr(fobj, "builtin", True):
            raise UserFilterNotRemoteError(
                f"flag filter {fobj.name!r} is a user-supplied Python function; it "
                "runs only with local data (this session's data are in a remote "
                "worker). Choose a built-in filter.")
        req = {k: v for k, v in request.items() if k != "filter_obj"}
        t0 = time.perf_counter()
        out = self._call("evaluate_flag_request", request=req, timeout=timeout)
        if isinstance(out, str):            # compact JSON form (see worker)
            import json
            out = json.loads(out)
        counts = (out or {}).get("counts") or {}
        log.debug("remote evaluate_flag_request: kind=%s filter=%s -> %s, %s changed, %.3fs",
                  req.get("kind"), (req.get("filter") or {}).get("name"),
                  "delta" if (out or {}).get("delta") else "nothing",
                  counts.get("n_changed"), time.perf_counter() - t0)
        return out

    def flag_commit_capabilities(self, timeout: Optional[float] = None) -> dict:
        try:
            return self._call("flag_commit_capabilities", timeout=timeout)
        except Exception as exc:
            return {"format": "?", "write": False, "script": False,
                    "write_reason": f"remote cubevis cannot report commit capabilities ({exc})"}

    def commit_pending_flags(self, deltas, timeout: Optional[float] = None, **options) -> dict:
        import json
        wire = [d.to_dict(json_safe=True) if hasattr(d, "to_dict") else d for d in deltas]
        out = self._call("commit_pending_flags", deltas_json=json.dumps(wire),
                         options_json=json.dumps(options),
                         timeout=timeout if timeout is not None else max(self._call_timeout or 0, 3600.0))
        self.__dict__["_cv_sent_ids"] = set()          # worker pending state was cleared
        self.__dict__.pop("_cv_memo", None)
        return out

    def set_frame_cache_limit_mb(self, mb: float, timeout: Optional[float] = None) -> None:
        """Frame cache budget of the WORKER (where the frames live)."""
        self._call("set_frame_cache_limit_mb", mb=float(mb), timeout=timeout)

    def list_flag_backups(self, timeout: Optional[float] = None) -> list:
        try:
            return self._call("list_flag_backups", timeout=timeout) or []
        except Exception:
            return []

    def restore_flag_backup(self, backup_path: str, timeout: Optional[float] = None) -> dict:
        self.__dict__.pop("_cv_memo", None)
        return self._call("restore_flag_backup", backup_path=backup_path,
                          timeout=timeout if timeout is not None else max(self._call_timeout or 0, 3600.0))

    def probe_flag_region(self, request: dict, timeout: Optional[float] = None) -> dict:
        t0 = time.perf_counter()
        out = self._call("probe_flag_region", request=request, timeout=timeout)
        log.debug("remote probe_flag_region: flag_n=%s unflag_n=%s, %.3fs",
                  (out or {}).get("flag_n"), (out or {}).get("unflag_n"),
                  time.perf_counter() - t0)
        return out

    def spw_casa_ids(self, timeout: Optional[float] = None) -> dict:
        from .flag_model import SpwKey
        pairs = self._call("spw_casa_ids", timeout=timeout) or []
        return {SpwKey.from_dict(k): int(v) for k, v in pairs}

    def flag_spw_table(self, timeout: Optional[float] = None) -> list:
        return self._call("flag_spw_table", timeout=timeout) or []

    def metadata(self, timeout: Optional[float] = None) -> dict:
        return self._call("metadata", timeout=timeout)

    def axis_info(self, axis: "Axis", selection: Optional["SelectionSpec"] = None,
                  query: str = "columns", timeout: Optional[float] = None):
        return self._memo_call("axis_info", selection, (axis, query),
                               dict(axis=axis, selection=selection, query=query,
                                    timeout=timeout))

    # ------------------------------------------------------------------ #
    # Client-side memo for coordinate-only queries (2026-09-30)           #
    # ------------------------------------------------------------------ #
    # axis_info and identity_tables read partition COORDINATES only (no
    # visibilities, no flags), so their answer depends only on their
    # arguments, the selection's row constraints and the data generation
    # (Reload).  A redraw asked the worker the same five questions every
    # time -- ~5 x 22-25 ms of pure round trip on zuul06/cvpost140
    # (bench_remote_overhead.py --gui, 2026-09-30).  Pending flags and the
    # flag view are deliberately NOT part of the key: they cannot change
    # coordinates.

    _MEMO_MAX = 256

    def _memo_call(self, method: str, selection, extra: tuple, kwargs: dict):
        from .data.reader import _selection_fingerprint
        fp = _selection_fingerprint(selection) if selection is not None else None
        if fp is not None:     # the flag view cannot change coordinates
            fp = tuple(kv for kv in fp if not (isinstance(kv, tuple) and kv and kv[0] == "flag_view"))
        if selection is not None and fp is None:
            return self._call(method, **kwargs)       # unhashable selection: no memo
        gen = int(getattr(selection, "cache_generation", 0) or 0) if selection is not None else 0
        try:
            key = (method, fp, gen, extra)
            hash(key)
        except TypeError:
            return self._call(method, **kwargs)
        memo = self.__dict__.setdefault("_cv_memo", {})
        if key in memo:
            stats = self.__dict__.setdefault("_cv_memo_hits", {})
            stats[method] = stats.get(method, 0) + 1
            return memo[key]
        out = self._call(method, **kwargs)
        if len(memo) >= self._MEMO_MAX:
            memo.clear()
        memo[key] = out
        return out

    def clear_memo(self) -> None:
        """Forget memoised coordinate queries (e.g. after the data changed
        on disk without a Reload)."""
        self.__dict__.pop("_cv_memo", None)

    def available_axes(self, timeout: Optional[float] = None):
        return self._call("available_axes", timeout=timeout)

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
