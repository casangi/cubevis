"""
cubevis.remote -- remote execution of cubevis GUIs against a Jupyter
kernel on a cluster node.

Scoped as its own subpackage so that (a) work here doesn't interfere with
concurrent work elsewhere in `cubevis`, and (b) it stays in reasonable
shape to be extracted as a standalone package later, should the
multiplexed-request/response-over-a-Jupyter-kernel machinery turn out to
be useful outside cubevis specifically.

Public surface:

    request(comm, message_id, payload, timeout=None)
        Async request/response over a Comm. For call sites with a running
        event loop.

    SyncBridge
        Dedicated background thread + persistent event loop, for call
        sites with none (construction-time calls, `next(gclean)`-shaped
        cases).

    KernelClientTransport
        P_local-side TransportBase, frontend role, backed by
        jupyter_client.

    KernelCommTransport
        Remote-kernel-side TransportBase, headless (no browser/anywidget
        coupling -- see its docstring for why CommsTransport can't serve
        this role).

    open_remote_kernel_link(kernel_manager, target_name=..., ...)
        One-call convenience: builds and connects a mirrored CommMgr +
        KernelClientTransport pair for P_local's side.

    ensure_remote_worker(build_worker, target_name=..., namespace=...)
        Idempotent bootstrap for the remote kernel's worker -- safe to
        call on every P_local (re)connect; only builds once per kernel
        process. Intended to run *inside* the remote kernel.

    DEFAULT_TARGET_NAME
        The well-known comm target name both sides use by default.

Depends on `cubevis.bokeh.transport`'s public surface (`CommMgr`, `Comm`,
`TransportBase`, `AppState`) as its one fundamental, permanent coupling
point -- that's the thing being bridged to a remote kernel. Two narrower
seams remain (`cubevis.utils.serialize`/`deserialize`, and one private
module `cubevis.bokeh.transport._environment`) -- see
`_kernel_transport.py`'s module docstring for what they are and what
extracting this package standalone would need to do about them.

`cubevis.remote.testing` (imported separately, not part of this
`__init__`) has a `LoopbackTransport`/`wire_loopback_pair` test double for
exercising mirrored-role `CommMgr` wiring without a real kernel.
"""
from ._bridge import request, SyncBridge
from ._kernel_transport import KernelClientTransport, KernelCommTransport
from ._link import open_remote_kernel_link
from ._worker import ensure_remote_worker, RemoteWorkerHandle, DEFAULT_TARGET_NAME

__all__ = [
    "request",
    "SyncBridge",
    "KernelClientTransport",
    "KernelCommTransport",
    "open_remote_kernel_link",
    "ensure_remote_worker",
    "RemoteWorkerHandle",
    "DEFAULT_TARGET_NAME",
]
