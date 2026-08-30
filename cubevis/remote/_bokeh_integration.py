"""
Chunk 1b, Task 6. Unchanged by Chunk 1c -- out of scope for this chunk
per the kickoff doc (no application-specific/Bokeh-integration changes
here); `RemoteAppLink.close()`'s contract (tear down everything this
link owns, confirm actual exit) is the same shape Chunk 1c's multi-
context version still satisfies, so this cascade needed no changes.

Investigation finding (Task 6 required reading `_bokeh_app_context.py`
closely enough to answer this before writing any code): `show()`'s call
to `BokehInit.clear_app_context()` fires immediately after HTML
generation/serialization -- it is NOT an app-closed signal, just the
"currently active context" registry pointer (used during `save()`) being
cleared once serialization no longer needs it. It has nothing to do with
whether the app is still running.

The real per-app teardown signal is `CommMgr.shutdown()`: it fires the
`on_shutdown` constructor callback exactly once -- whether triggered by
an explicit call or by the reconnect watchdog after the frontend fails
to return within `reconnect_grace_period`/`reconnect_timeout` -- before
tearing down transport and state. That's where `remote_link.close()`
belongs, not `show()`.

One property worth stating plainly rather than glossing over:
`on_shutdown` is a *synchronous* callback, but confirming the worker
subprocess actually exited is inherently async work (per
`RemoteAppLink.close()`'s own contract). The cascade below schedules
`link.close()` as a background task from within that sync callback
rather than awaiting it -- so `CommMgr.shutdown()` itself returns before
the worker teardown is necessarily complete. That's a real, deliberate
trade-off (there is no other awaited hook inside `shutdown()` to use
instead without a more invasive change to `_comm_mgr.py`, which is out
of this chat's scope), not an oversight.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Optional, Tuple

from cubevis.bokeh.transport import CommMgr
from cubevis.bokeh.models import BokehAppContext
from ._link import RemoteAppLink

logger = logging.getLogger(__name__)


class _RemoteLinkHolder:
    """Mutable box so the `on_shutdown` callback -- registered at
    `CommMgr` construction time, before any `RemoteAppLink` exists yet
    -- can reach whichever link ends up attached later."""

    def __init__(self) -> None:
        self.link: Optional[RemoteAppLink] = None


def new_comm_mgr_with_remote_teardown(
    on_shutdown: Optional[Callable] = None, **comm_mgr_kwargs: Any
) -> Tuple[CommMgr, _RemoteLinkHolder]:
    """
    Construct a `CommMgr` whose `on_shutdown` cascades into
    `remote_link.close()` once a link is attached -- via the supported
    integration point (`on_shutdown` is already a legitimate constructor
    parameter) rather than reaching past `CommMgr`'s underscore-prefixed
    internals to patch it in after construction.

    Returns ``(comm_mgr, holder)``. Construct `BokehAppContext` with
    this `comm_mgr`, then once a link exists call
    ``attach_remote_link(app_context, link, holder)``.
    """
    holder = _RemoteLinkHolder()

    def _cascade(reason=None, description: str = "") -> None:
        if on_shutdown is not None:
            try:
                on_shutdown(reason=reason, description=description)
            except Exception:
                logger.exception("new_comm_mgr_with_remote_teardown: user on_shutdown raised")
        if holder.link is not None:
            logger.debug("CommMgr.shutdown -> cascading into remote_link.close()")
            asyncio.ensure_future(holder.link.close())

    mgr = CommMgr(on_shutdown=_cascade, **comm_mgr_kwargs)
    return mgr, holder


def attach_remote_link(app_context: BokehAppContext, link: RemoteAppLink,
                        holder: _RemoteLinkHolder) -> None:
    """
    Attach an already-opened `RemoteAppLink` to `app_context` as a plain
    Python attribute -- deliberately **not** a Bokeh `Instance(...)`
    Property: it holds a transport, an event loop, and a subprocess
    handle, none of which are JS-serializable or have any business being
    JS-visible.
    """
    app_context.remote_link = link
    holder.link = link
