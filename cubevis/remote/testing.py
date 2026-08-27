"""
A minimal `TransportBase` double that connects two `CommMgr` instances
directly in-process, with no real socket/kernel involved.

Public (not test-only) because it's independently useful: anyone writing
tests against `cubevis.remote`'s primitives -- Chunk 2/3's own test
suites, or a future standalone-package user's -- needs a way to validate
mirrored-role `CommMgr` wiring without spinning up a real Jupyter kernel
every time. This is that.

Usage::

    from cubevis.bokeh.transport import CommMgr
    from cubevis.remote.testing import wire_loopback_pair

    mgr_a = CommMgr(role=CommMgr.ROLE_MIRROR)
    mgr_b = CommMgr(role=CommMgr.ROLE_DEFAULT)
    wire_loopback_pair(mgr_a, mgr_b)
    # mgr_a/mgr_b can now .open(...)/.send(...)/.register(...) as if
    # mgr_a were P_local's kernel-facing side and mgr_b were the kernel
    # side, with no transport-level machinery involved.

Each `send_message` call schedules delivery to the peer's registered
callback on the next event-loop iteration (via `call_soon`), so this
behaves like a real async transport rather than a same-stack-frame call
-- which matters for reproducing races/ordering bugs faithfully.
"""
from __future__ import annotations

import asyncio
import copy
from typing import Any, Callable, Dict, Optional, Tuple

from cubevis.bokeh.transport import AppState, CommMgr

__all__ = ["LoopbackTransport", "wire_loopback_pair"]


class LoopbackTransport:
    """Implements the same duck-typed interface as TransportBase."""

    def __init__(self, name: str = ""):
        self.name = name
        self.peer: Optional["LoopbackTransport"] = None
        self._callback: Optional[Callable] = None
        self._connected = False
        self._closed = False
        self.sent: list = []  # record of everything sent, for assertions

    def set_message_callback(self, callback: Callable) -> None:
        self._callback = callback

    async def connect(self) -> None:
        self._connected = True

    def is_connected(self) -> bool:
        return self._connected and not self._closed

    async def send_message(self, message: Dict[str, Any]) -> None:
        if self._closed:
            raise RuntimeError(f"LoopbackTransport[{self.name}]: closed")
        self.sent.append(copy.deepcopy(message))
        if self.peer is None or self.peer._callback is None:
            return

        loop = asyncio.get_running_loop()

        async def _deliver():
            if self.peer is not None and self.peer._callback is not None:
                await self.peer._callback(copy.deepcopy(message))

        loop.call_soon(lambda: asyncio.ensure_future(_deliver()))

    async def run(self) -> None:
        while not self._closed:
            await asyncio.sleep(0.01)

    async def close(self) -> None:
        self._closed = True
        self._connected = False

    def close_was_clean(self) -> bool:
        return True


def wire_loopback_pair(
    mgr_a: CommMgr, mgr_b: CommMgr, name_a: str = "A", name_b: str = "B"
) -> Tuple[LoopbackTransport, LoopbackTransport]:
    """
    Connect two already-constructed CommMgrs with a loopback pair.

    Deliberately bypasses `CommMgr.initialize()` (which dispatches on
    `transport_type` and doesn't have a branch for a bare test double)
    and wires the transport + routing callback directly -- this is
    whitebox test-harness code, so reaching past the underscore here is
    the normal, expected thing, unlike in `cubevis.remote`'s own
    non-test modules (see `_link.py`, which goes through the public
    `initialize()` specifically to avoid this).
    """
    t_a = LoopbackTransport(name_a)
    t_b = LoopbackTransport(name_b)
    t_a.peer = t_b
    t_b.peer = t_a

    t_a._connected = True
    mgr_a._transport = t_a
    t_a.set_message_callback(mgr_a._route_message)
    mgr_a.state = AppState.RUNNING
    mgr_a._initialized = True

    t_b._connected = True
    mgr_b._transport = t_b
    t_b.set_message_callback(mgr_b._route_message)
    mgr_b.state = AppState.RUNNING
    mgr_b._initialized = True

    return t_a, t_b
