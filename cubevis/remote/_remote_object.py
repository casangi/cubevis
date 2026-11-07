"""_remote_object.py
=================
``RemoteObjectConnection``: one object living in a worker on a (remote or
local) Jupyter kernel, and the plumbing to call it -- application-agnostic.

This is the generic half of visplot's ``RemoteReductionContext``
(``cubevis/toolbox/visplot/remote_reduction_context.py``), lifted so that
interactive clean's ``RemoteImagingSession`` (``cubevis/imaging``) uses the
same connection rather than a copy (iclean design §3, 2026-11-07).  visplot
moves onto it in design step 5; until then the two carry the same logic, and
a change to one belongs in the other.  What it does, in order of the
constructor:

* one ``SyncBridge``: everything below is made on, and driven from, its loop
  (visplot's module docstring, point 1: ``RemoteAppLink``'s own bridge runs
  another loop, and using it would deadlock silently);
* ``AsyncKernelManager(kernel_name)``: the kernel (sshpyk for a remote one,
  ``"python3"`` for one on this computer) -- the whole local/remote switch;
* ``RemoteAppLink.open`` and ``create_context`` with the application's
  ``register_function`` (resolved *in the worker*) and wire types;
* ``create_object(class_name, kwargs)``: the application's object.

Calls go through ``dispatch_fast("call_method", pre_encoded=True)`` and are
checked for an error reply (the convenience wrappers do not); each is
watched for the kernel stopping or its heartbeat going quiet
(``RemoteKernelLostError`` at once instead of after the call's budget).
A worker that died raises ``RemoteWorkerDiedError``; whether a new one is
started is the application's choice (``restart_worker``): visplot's data
can be reopened, a clean's state cannot.

``call`` blocks the calling thread (``SyncBridge.run``).  ``acall`` is a
coroutine on the bridge's loop; ``submit(coro)`` runs one there and returns a
``concurrent.futures.Future``, which another event loop -- Bokeh's -- awaits
with ``asyncio.wrap_future`` without blocking.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Dict, FrozenSet, Iterable, List, Optional
from uuid import uuid4

log = logging.getLogger(__name__)

DEFAULT_OPEN_TIMEOUT = 60.0
DEFAULT_CREATE_CONTEXT_TIMEOUT = 180.0
DEFAULT_CALL_TIMEOUT = 30.0
DEFAULT_DATA_CALL_TIMEOUT = 600.0
LIVENESS_INTERVAL = 5.0
HEARTBEAT_GRACE = 30.0


def liveness_interval() -> float:
    '''``CUBEVIS_REMOTE_LIVENESS`` (seconds) or ``LIVENESS_INTERVAL``.'''
    v = os.environ.get("CUBEVIS_REMOTE_LIVENESS", "").strip()
    try:
        return max(0.2, float(v)) if v else LIVENESS_INTERVAL
    except ValueError:
        return LIVENESS_INTERVAL


def data_call_timeout() -> float:
    '''``CUBEVIS_REMOTE_TIMEOUT`` (seconds) or ``DEFAULT_DATA_CALL_TIMEOUT``.'''
    v = os.environ.get("CUBEVIS_REMOTE_TIMEOUT", "").strip()
    try:
        return float(v) if v else DEFAULT_DATA_CALL_TIMEOUT
    except ValueError:
        log.warning("CUBEVIS_REMOTE_TIMEOUT=%r is not a number; using %.0f s",
                    v, DEFAULT_DATA_CALL_TIMEOUT)
        return DEFAULT_DATA_CALL_TIMEOUT


class RemoteCallError(RuntimeError):
    """The remote object's method raised (in the worker); carries the remote
    traceback.  Transport failures (timeouts, a lost connection) are other
    exceptions."""

    def __init__(self, method: str, reply: Dict[str, Any]):
        self.method = method
        self.remote_error = reply.get("error")
        self.remote_traceback = reply.get("traceback", "")
        msg = f"remote call to {method!r} failed: {self.remote_error}"
        if self.remote_traceback:
            msg += f"\n--- remote traceback ---\n{self.remote_traceback}"
        super().__init__(msg)


class RemoteWorkerDiedError(RuntimeError):
    """The worker holding the object on the kernel's host is gone (killed --
    most often by the system's memory limit --, crashed or exited)."""


class RemoteKernelLostError(RuntimeError):
    """The kernel stopped, or the connection to it was lost, while a call was
    waiting for it."""


class RemoteObjectConnection:
    """One object (``class_name(**kwargs)``) in a worker on the kernel
    ``kernel_name``, and calls to it.

    Parameters
    ----------
    kernel_name : str
        a kernelspec name (``jupyter kernelspec list``)
    class_name, kwargs :
        what ``register_function`` registered, and its constructor's arguments
        (resolved on the kernel's host)
    register_function : str
        ``"module:function"``, imported in the worker
    wire_types : iterable of str
        modules that register the application's wire types (both sides)
    app : str
        the application's name, for messages and the session record
        (``cubevis.setup.sessions``)
    data : str
        what the session works on, for the session record
    call_timeout : float
        budget for a call (``DEFAULT_CALL_TIMEOUT``); ``data_methods`` get
        ``data_timeout`` (``CUBEVIS_REMOTE_TIMEOUT`` or ten minutes)
    restart_worker : bool
        start a new worker (and object) when the old one died; False raises
        ``RemoteWorkerDiedError`` on every call after it
    """

    def __init__(self, kernel_name: str, class_name: str, kwargs: Optional[Dict[str, Any]] = None, *,
                 register_function: str, wire_types: Iterable[str] = (), app: str = "cubevis",
                 data: Optional[str] = None, worker_target_name: Optional[str] = None,
                 open_timeout: float = DEFAULT_OPEN_TIMEOUT,
                 create_context_timeout: float = DEFAULT_CREATE_CONTEXT_TIMEOUT,
                 call_timeout: Optional[float] = None,
                 data_methods: Iterable[str] = (), data_timeout: Optional[float] = None,
                 restart_worker: bool = False) -> None:
        if not kernel_name:
            raise ValueError(f"{app}: a remote session needs kernel_name= (a kernelspec name "
                             "resolvable by `jupyter kernelspec list`)")
        from jupyter_client import AsyncKernelManager
        from cubevis.remote import RemoteAppLink, SyncBridge, DEFAULT_WORKER_TARGET_NAME

        self._kernel_name = kernel_name
        self._class_name = class_name
        self._kwargs = dict(kwargs or {})
        self._app = app
        self._data = data
        self._call_timeout = DEFAULT_CALL_TIMEOUT if call_timeout is None else call_timeout
        self._data_methods: FrozenSet[str] = frozenset(data_methods)
        self._data_timeout = (data_timeout if data_timeout is not None
                              else call_timeout if call_timeout is not None else data_call_timeout())
        self._restart_worker = restart_worker
        self._worker_dead = False
        self._closed = False
        self._session_id = None
        self.call_stats: Dict[str, tuple] = {}

        self._bridge = SyncBridge(name=f"{app}-remote-{uuid4().hex[:8]}")
        self._bridge.start()
        from jupyter_core.paths import jupyter_runtime_dir
        os.makedirs(jupyter_runtime_dir(), mode=0o700, exist_ok=True)
        self._km = AsyncKernelManager(kernel_name=kernel_name)
        log.info("%s: connecting kernel_name=%r -- from a few seconds (a kernel on this "
                 "computer) to a couple of minutes (first connect to a cluster kernel)",
                 app, kernel_name)
        t0 = time.perf_counter()
        _quiet_setup_kernel_hints(self._km)
        self._bridge.run(self._km.start_kernel())
        try:
            self._link = self._bridge.run(RemoteAppLink.open(
                self._km, worker_target_name=worker_target_name or DEFAULT_WORKER_TARGET_NAME,
                timeout=open_timeout))
            self._context_config = {"register_function": register_function,
                                    "wire_types": list(wire_types)}
            self._ctx = self._bridge.run(self._link.create_context(
                config=self._context_config, timeout=create_context_timeout))
            self._handle = self._bridge.run(self._acreate_object())
        except BaseException:
            try:
                self._bridge.run(self._km.shutdown_kernel())
            except Exception:
                log.warning("%s: cleanup after a failed connect also failed", app, exc_info=True)
            self._bridge.stop()
            raise
        log.info("%s: connected in %.1fs", app, time.perf_counter() - t0)
        self._session_id = self._record_session()

    # ---- facts ------------------------------------------------------------

    @property
    def kernel_name(self) -> str:
        return self._kernel_name

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        '''the bridge's event loop, on which ``acall`` coroutines run'''
        return self._bridge.loop

    # ---- calls --------------------------------------------------------------

    async def _acreate_object(self) -> str:
        reply = await self._ctx.dispatch_fast(
            "create_object", {"class_name": self._class_name, "args": [], "kwargs": self._kwargs})
        if isinstance(reply, dict) and "error" in reply:
            self._raise_if_worker_died(reply)
            raise RemoteCallError(f"create_object({self._class_name!r})", reply)
        return reply["handle"]

    def _raise_if_worker_died(self, reply: Dict[str, Any]) -> None:
        tb = str(reply.get("traceback", ""))
        if "WorkerDiedError" not in tb and "WorkerGoneError" not in tb:
            return
        self._worker_dead = True
        what = ("the next call starts a new one" if self._restart_worker
                else f"close this {self._app} and start it again (what the worker held is lost; "
                     "what it wrote to disk stays)")
        raise RemoteWorkerDiedError(f"{reply.get('error')}. The {self._app} worker on kernel "
                                    f"{self._kernel_name!r} is gone; {what}.")

    async def _watched(self, aw):
        """Await *aw* while checking every ``liveness_interval()`` seconds
        that the kernel runs and answers its heartbeat."""
        task = asyncio.ensure_future(aw)
        interval = liveness_interval()
        silent_since = None
        try:
            while True:
                done, _ = await asyncio.wait({task}, timeout=interval)
                if task in done:
                    return task.result()
                try:
                    alive = await asyncio.wait_for(self._km.is_alive(), timeout=30)
                except Exception:
                    alive = True
                if not alive:
                    raise RemoteKernelLostError(
                        f"the kernel {self._kernel_name!r} has stopped (its process is gone: killed, "
                        f"or its computer restarted). Close this {self._app} and start it again.")
                hb = getattr(getattr(getattr(self, "_link", None), "transport", None), "_client", None)
                hb = getattr(hb, "hb_channel", None)
                beating = True
                try:
                    beating = hb is None or bool(hb.is_beating())
                except Exception:
                    pass
                now = time.monotonic()
                if beating:
                    silent_since = None
                elif silent_since is None:
                    silent_since = now
                elif now - silent_since >= HEARTBEAT_GRACE:
                    raise RemoteKernelLostError(
                        f"no answer from the kernel {self._kernel_name!r} for {now - silent_since:.0f} s: "
                        "the connection to it was lost (the network or VPN, or this computer slept). "
                        f"Close this {self._app} and start it again.")
        finally:
            if not task.done():
                task.cancel()

    async def _arestart(self) -> None:
        log.warning("%s: starting a new worker on kernel %r (the previous one is gone)",
                    self._app, self._kernel_name)
        try:
            await self._ctx.shutdown(timeout=10)
        except Exception:
            pass
        self._ctx = await self._link.create_context(config=self._context_config,
                                                    timeout=DEFAULT_CREATE_CONTEXT_TIMEOUT)
        self._handle = await self._acreate_object()
        self._worker_dead = False

    def _timeout_for(self, method: str, timeout: Optional[float]) -> float:
        if timeout is not None:
            return timeout
        return self._data_timeout if method in self._data_methods else self._call_timeout

    async def acall(self, method: str, /, *, timeout: Optional[float] = None, **kwargs: Any) -> Any:
        '''Call ``method(**kwargs)`` on the remote object (a coroutine on the
        bridge's loop: ``submit`` it from any other loop).'''
        if self._closed:
            raise RuntimeError(f"{self._app}: the remote session is closed")
        if self._worker_dead:
            if not self._restart_worker:
                raise RemoteWorkerDiedError(f"the {self._app} worker on kernel {self._kernel_name!r} "
                                            f"is gone; close this {self._app} and start it again")
            await self._watched(self._arestart())
        reply = await self._watched(self._ctx.dispatch_fast(
            "call_method",
            {"handle": self._handle, "method": method, "args": [], "kwargs": kwargs,
             "pre_encoded": True},
            timeout=self._timeout_for(method, timeout)))
        if isinstance(reply, dict) and "error" in reply:
            self._raise_if_worker_died(reply)
            raise RemoteCallError(method, reply)
        if isinstance(reply, dict) and "__cv_pre_encoded__" in reply:
            from cubevis.utils import remote_deserialize
            reply = remote_deserialize(reply["__cv_pre_encoded__"])
        return reply

    def call(self, method: str, /, *, timeout: Optional[float] = None, **kwargs: Any) -> Any:
        '''``acall`` from synchronous code (blocks the calling thread).'''
        budget = self._timeout_for(method, timeout)
        t0 = time.perf_counter()
        try:
            return self._bridge.run(self.acall(method, timeout=budget, **kwargs))
        except TimeoutError as exc:
            raise TimeoutError(
                f"the kernel {self._kernel_name!r} did not answer {method} within {budget:.0f} s "
                "(set CUBEVIS_REMOTE_TIMEOUT, in seconds, to allow longer)") from exc
        finally:
            dt = time.perf_counter() - t0
            n, tot, mx, _ = self.call_stats.get(method, (0, 0.0, 0.0, 0.0))
            self.call_stats[method] = (n + 1, tot + dt, max(mx, dt), dt)

    def submit(self, coro) -> "Future":
        '''Run *coro* (e.g. one using ``acall``) on the bridge's loop; the
        returned ``concurrent.futures.Future`` can be awaited from another
        loop with ``asyncio.wrap_future``.'''
        return asyncio.run_coroutine_threadsafe(coro, self._bridge.loop)

    def run(self, coro, timeout: Optional[float] = None):
        '''Run *coro* on the bridge's loop and wait for it.'''
        return self._bridge.run(coro, timeout=timeout)

    # ---- worker-side code (tests, diagnostics) ------------------------------

    def eval_code(self, code: str, timeout: Optional[float] = None):
        return self._bridge.run(asyncio.wait_for(self._ctx.eval_code(code), timeout or 600))

    def exec_code(self, code: str, timeout: Optional[float] = None):
        return self._bridge.run(asyncio.wait_for(self._ctx.exec_code(code), timeout or 600))

    # ---- session record, lifecycle -------------------------------------------

    def _record_session(self) -> Optional[str]:
        try:
            from cubevis.setup import sessions as _sessions
            facts = _sessions.kernel_facts(self._km)
            if not facts:
                return None
            cid = getattr(self._ctx, "context_id", None)
            return _sessions.record(app=self._app, jupyter_kernel=self._kernel_name, data=self._data,
                                    context_id=None if cid is None else str(cid),
                                    handle=self._handle, **facts)
        except Exception:
            log.debug("%s: session not recorded", self._app, exc_info=True)
            return None

    def close(self, timeout: float = 20.0) -> None:
        '''The worker, the link and the kernel; idempotent.'''
        if self._closed:
            return
        self._closed = True
        try:
            self._bridge.run(self._link.close(), timeout=timeout)
        except Exception:
            log.warning("%s: closing the link failed", self._app, exc_info=True)
        try:
            self._bridge.run(self._km.shutdown_kernel())
        except Exception:
            log.warning("%s: shutting the kernel down failed", self._app, exc_info=True)
        self._bridge.stop()
        try:
            from cubevis.setup import sessions as _sessions
            _sessions.forget(self._session_id)
        except Exception:
            pass

    def __repr__(self) -> str:  # pragma: no cover
        return (f"RemoteObjectConnection({self._class_name}, kernel_name={self._kernel_name!r}, "
                f"closed={self._closed})")


class _QuietIdentityFileHint(logging.Filter):
    """Drops sshpyk's ``[SSHPYK640] ... identityfile`` hint for kernels
    cubevis setup made (see visplot's remote_reduction_context.py)."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        return not ("[SSHPYK640]" in msg and "identityfile" in msg)


def _quiet_setup_kernel_hints(km) -> None:
    try:
        from cubevis.setup import sshgen
        cfg = ((km.kernel_spec.metadata or {}).get("kernel_provisioner") or {}).get("config") or {}
        if str(cfg.get("ssh_config") or "") != str(sshgen.config_path()):
            return
        lg = km.log
        if not any(isinstance(f, _QuietIdentityFileHint) for f in lg.filters):
            lg.addFilter(_QuietIdentityFileHint())
    except Exception:
        log.debug("could not quiet sshpyk's identityfile hint", exc_info=True)
