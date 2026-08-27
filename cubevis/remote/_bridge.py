########################################################################
# Calling-convention primitives for the P_local <-> remote-kernel link
# (Chunk 1, Task 4).
#
# Two ways to call across the wire, matching the two calling shapes
# Chunk 2 (RemoteReductionContext) and Chunk 3 (gclean proxy) need:
#
#   request(comm, message_id, payload)
#       async, Future-based. For call sites that already have a running
#       event loop -- e.g. a j2p handler, or VisibilityPlotter's
#       query_raster/query_columns/probe_* per the design doc's
#       method-to-primitive mapping.
#
#   SyncBridge
#       A dedicated background thread with its own event loop, for call
#       sites with NO running loop -- construction-time calls such as
#       gclean's `next(gclean)` in _setup(), or
#       VisibilityPlotter._build_panels() (visibility_plotter.py:1176),
#       which runs immediately after _build_comm() (line 1175) and before
#       _task_server's own loop exists.
#
# The sync-bridge is modeled on the thread+fresh-event-loop pattern
# already used in this codebase by
# Task._convert_asyncio_event_to_threading / bridge_events (_task.py) --
# NOT on Context's Mode.THREAD (_context.py), which submits work to a
# ThreadPoolExecutor and doesn't give it a *persistent* event loop of its
# own. A persistent loop matters here because a comm's reply callback
# needs somewhere to land whenever it fires, not just for the duration of
# one submitted call.
########################################################################
from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any, Awaitable, Callable, Coroutine, Optional, TypeVar

logger = logging.getLogger(__name__)

__all__ = ["request", "SyncBridge"]

T = TypeVar("T")


async def request(comm, message_id: str, payload: dict, timeout: Optional[float] = None) -> Any:
    """
    Async request/response over a `Comm` -- wraps `Comm.send()`'s existing
    callback mechanism in a `Future`.

    Use from any call site that already has a running event loop (e.g. a
    j2p handler dispatched by CommMgr._handle_request).

    The resolved value is whatever the peer's handler returned, exactly
    as `CommMgr._handle_response` hands it to `send()`'s callback -- this
    includes the peer's own error-reporting convention
    (`{'error': ..., 'traceback': ...}`) on the peer's exception path in
    `_handle_request`. `request()` does not inspect that or raise on it;
    callers that care should check for an `'error'` key themselves. This
    matches the design doc's sketch and keeps this primitive from
    guessing at a Chunk-2/3-specific error contract.

    `timeout`, if given, bounds how long to wait for a reply and raises
    `asyncio.TimeoutError` if it expires. The design doc's sketch has no
    timeout; it is optional here because a permanently-vanished peer with
    ``resend_inflight_on_reconnect=True`` (CommMgr's default) would
    otherwise leave the awaited Future pending forever -- CommMgr itself
    only resolves a stuck pending request early when
    ``resend_inflight_on_reconnect=False``.
    """
    loop = asyncio.get_running_loop()
    fut: "asyncio.Future" = loop.create_future()

    def _on_reply(msg):
        if not fut.done():
            fut.set_result(msg)

    await comm.send(message_id, payload, callback=_on_reply)

    if timeout is None:
        return await fut
    try:
        return await asyncio.wait_for(fut, timeout=timeout)
    except asyncio.TimeoutError:
        logger.warning(
            f"request(): timed out after {timeout}s waiting for a reply to "
            f"{comm.comm_id}.{message_id}"
        )
        raise


class SyncBridge:
    """
    A dedicated background thread running its own persistent asyncio
    event loop, so synchronous, no-running-loop call sites can still
    `await` coroutines against a comm -- the `next(gclean)`-shaped case.

    Usage::

        bridge = SyncBridge()
        bridge.start()                 # spins up the background loop
        ...
        result = bridge.run(request(comm, "metadata", {}))   # blocks
        ...
        bridge.stop()                  # tears the loop down cleanly

    `start()`/`stop()` are idempotent and safe to call from any thread.
    `run()` is the thread-safe entry point call sites actually use; it
    wraps `asyncio.run_coroutine_threadsafe(...).result()` exactly as
    sketched in the design doc.

    Not itself tied to any particular transport -- a `KernelClientTransport`
    that needs its `run()` read-loop alive before `_task_server`'s own
    loop exists is a natural thing to schedule on the *same* bridge via
    `run_background()`, so the transport and any construction-time
    `request()`/`SyncBridge.run()` calls share one loop rather than
    racing two independent ones.
    """

    def __init__(self, name: str = "cubevis-sync-bridge"):
        self._name = name
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._ready = threading.Event()
        self._stop_event: Optional[asyncio.Event] = None
        self._background_tasks: list = []  # keep references so they aren't GC'd

    @property
    def loop(self) -> Optional[asyncio.AbstractEventLoop]:
        return self._loop

    @property
    def is_running(self) -> bool:
        return self._loop is not None and self._thread is not None and self._thread.is_alive()

    def start(self, ready_timeout: float = 5.0) -> None:
        """Start the background thread + loop. Idempotent."""
        if self._thread is not None:
            return

        def _run():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
            self._stop_event = asyncio.Event()
            self._ready.set()
            logger.debug(f"SyncBridge[{self._name}]: background loop started")
            try:
                loop.run_until_complete(self._stop_event.wait())
            finally:
                pending = [t for t in asyncio.all_tasks(loop=loop) if not t.done()]
                for t in pending:
                    t.cancel()
                if pending:
                    loop.run_until_complete(
                        asyncio.gather(*pending, return_exceptions=True)
                    )
                loop.close()
                logger.debug(f"SyncBridge[{self._name}]: background loop stopped")

        self._thread = threading.Thread(target=_run, name=self._name, daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=ready_timeout):
            raise RuntimeError(
                f"SyncBridge[{self._name}]: background loop did not start "
                f"within {ready_timeout}s"
            )

    def run(self, coro: "Coroutine[Any, Any, T]", timeout: Optional[float] = None) -> T:
        """
        Run `coro` on the bridge's loop and block the calling thread for
        the result. Safe to call from any thread, with or without its own
        running loop -- this is the primitive `next(gclean)`-shaped call
        sites use.
        """
        if self._loop is None:
            raise RuntimeError(f"SyncBridge[{self._name}].run: start() was not called")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=timeout)

    def run_background(self, coro: "Coroutine[Any, Any, Any]") -> None:
        """
        Schedule `coro` to run on the bridge's loop without blocking the
        caller -- for long-running work like a transport's `run()`
        read-loop, which needs to be alive on this loop for `run()`
        (above) to ever get replies delivered to it.
        """
        if self._loop is None:
            raise RuntimeError(f"SyncBridge[{self._name}].run_background: start() was not called")

        def _schedule():
            task = self._loop.create_task(coro)
            self._background_tasks.append(task)

            def _on_done(t):
                try:
                    self._background_tasks.remove(t)
                except ValueError:
                    pass
                if not t.cancelled() and t.exception() is not None:
                    logger.error(
                        f"SyncBridge[{self._name}]: background task failed: {t.exception()}"
                    )

            task.add_done_callback(_on_done)

        self._loop.call_soon_threadsafe(_schedule)

    def stop(self, timeout: float = 5.0) -> None:
        """Signal the background loop to stop and join the thread. Idempotent."""
        if self._loop is None or self._stop_event is None:
            return
        loop, stop_event, thread = self._loop, self._stop_event, self._thread
        loop.call_soon_threadsafe(stop_event.set)
        if thread is not None:
            thread.join(timeout=timeout)
            if thread.is_alive():
                logger.warning(f"SyncBridge[{self._name}].stop: thread did not exit within {timeout}s")
        self._loop = None
        self._thread = None
        self._stop_event = None
        self._ready.clear()
