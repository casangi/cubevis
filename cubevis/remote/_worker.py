########################################################################
# Start-vs-reattach for the remote kernel side (Chunk 1, Task 6).
#
# Two layers, deliberately kept separate:
#
#   1. Process-level liveness/reattachment -- whether the remote kernel
#      *process* is still running, and how to reconnect jupyter_client to
#      it if so. This is sshpyk's job, and sshpyk already has a real,
#      working answer: a kernelspec's provisioner config can pass
#      `existing=<persistent_file>` instead of launching fresh, and
#      `SSHKernelProvisioner.pre_launch()` checks a saved PID/command
#      snapshot against what's actually running on the remote host before
#      accepting the reattach (provisioning.py, `pre_launch`/
#      `write_persistent_info`/`load_persistent_info`/`has_process`/
#      `poll`). Nothing in this module reimplements that -- see the
#      design doc addendum for what was verified there.
#
#   2. App-level state idempotency -- *given* a live kernel process
#      (fresh or reattached), has THIS application's worker (a CommMgr,
#      a KernelCommTransport, and whatever backend state Chunk 2/3 needs
#      -- an opened MS, a running `gclean`) already been constructed in
#      it? Re-running that construction on every reattach would discard
#      live state, which is exactly the failure this task exists to
#      avoid. sshpyk has no opinion on this layer -- it only manages the
#      process, not what's living inside it -- so this module supplies
#      it: `ensure_remote_worker()` is safe to call from a fresh
#      execute_request every time P_local (re)connects, and only builds
#      the worker the first time.
########################################################################
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Optional

from cubevis.bokeh.transport import AppState, CommMgr

from ._kernel_transport import KernelCommTransport

logger = logging.getLogger(__name__)

__all__ = ["ensure_remote_worker", "RemoteWorkerHandle", "DEFAULT_TARGET_NAME"]

# The design doc's multiplexing discipline is "exactly one Jupyter comm
# per kernel" -- there is only ever one cubevis worker per remote kernel
# process. This is about letting CommMgr's own sub-multiplexing (Comm/
# comm_id categories within one channel) do the work of addressing
# "which logical destination," rather than needing N independent
# AsyncKernelClients each polling the same kernel's iopub stream with
# their own reconnect bookkeeping -- see the design doc's §1/§2e for the
# full reasoning.
#
# target_name itself does NOT need to be a fixed, well-known string for
# reconnection to work, and defaulting it to one (an earlier version of
# this module did) was based on incorrect reasoning: a *new* P_local
# process, after a full restart, does not need to already know the name
# to ask for. The idempotency check below returns the existing worker's
# comm_mgr_id unconditionally, regardless of what target_name a later
# call passes -- so a reattaching P_local session just re-runs this same
# bootstrap and reads the identifier back from its own output, exactly
# like it already does for comm_mgr_id. Defaulting target_name to a
# value generated from the newly-constructed CommMgr's own comm_mgr_id
# (see below) removes the collision risk a shared global constant
# carried (two independent cubevis deployments bootstrapping into the
# same kernel would otherwise silently share one marker) without losing
# anything reconnection actually needs.
#
# DEFAULT_TARGET_NAME is kept as a named constant for callers who have a
# specific reason to want a fixed, predictable name (e.g. manual/CLI
# debugging against a known target) -- pass it explicitly if so. It is
# no longer ensure_remote_worker's own default.
DEFAULT_TARGET_NAME = "cubevis-remote-worker"

_NAMESPACE_KEY = "__cubevis_remote_worker__"


class RemoteWorkerHandle:
    """
    Bookkeeping for one bootstrapped remote worker, stashed in the
    kernel's own namespace under `_NAMESPACE_KEY` so it survives between
    separate `execute_request`s (and separate P_local sessions, as long
    as the kernel process itself is still alive).
    """

    __slots__ = ("target_name", "comm_mgr_id", "mgr", "transport", "worker")

    def __init__(self, target_name: str, comm_mgr_id: str, mgr, transport, worker: Any):
        self.target_name = target_name
        self.comm_mgr_id = comm_mgr_id
        self.mgr = mgr
        self.transport = transport
        self.worker = worker


def ensure_remote_worker(
    build_worker: Callable[[CommMgr], Any],
    *,
    target_name: Optional[str] = None,
    namespace: Optional[Dict[str, Any]] = None,
) -> str:
    """
    Idempotent bootstrap for the remote kernel's cubevis worker.

    Safe to call more than once in the same kernel process -- from a
    genuinely repeated request, or from an independent P_local session
    reattaching after the original one went away. The first call
    constructs a `CommMgr(role=CommMgr.ROLE_DEFAULT)`, a
    `KernelCommTransport` registered under `target_name`, and calls
    `build_worker(mgr)` -- Chunk 2/3's chance to `mgr.open(category)` and
    `.register(message_id, handler)` whatever protocol handlers their
    backend needs (an opened MS/`ReductionContext`, a `gclean` instance --
    this module has no opinion on what that is or what messages it
    answers), returning whatever object should be kept alive as "the
    worker". That return value, `mgr`, and `transport` are all stashed in
    a `RemoteWorkerHandle` under a namespace marker. Every subsequent call
    finds the marker and returns the existing `comm_mgr_id` unchanged,
    WITHOUT calling `build_worker` again -- so live worker state (and its
    registered handlers) is never silently discarded by a reattach.

    `build_worker` receives `mgr` rather than being called with no
    arguments specifically so it CAN register handlers as part of
    construction -- there is no other hook for that, since at the time
    `build_worker` runs, P_local hasn't connected yet and no `Comm` exists
    until something calls `mgr.open(...)`.

    Intended call shape, from a single-line `execute_request`::

        from cubevis.remote import ensure_remote_worker

        def build_worker(mgr):
            comm = mgr.open("query")
            comm.register("axis_info", lambda msg: {...})
            return my_backend_object   # e.g. an opened MS / ReductionContext

        comm_mgr_id = ensure_remote_worker(build_worker)
        print("COMM_MGR_ID=" + comm_mgr_id)

    `namespace`, if given, is the dict the marker is stored in -- pass
    `globals()` when testing this outside a real kernel. Defaults to the
    active IPython shell's user namespace (`shell.user_ns`), which is
    where a marker actually needs to live to survive from one
    `execute_request` to the next inside a real kernel.

    Note on comm-target re-registration: `register_target()` is only
    called on the first (bootstrapping) call. A reattaching P_local's
    fresh `comm_open` against the already-registered `target_name` is
    still handled correctly -- ipykernel's `comm_manager.comm_open`
    handler constructs a new peer-side `Comm` per `comm_open` regardless
    of whether the target was "just" registered or registered earlier,
    and `KernelCommTransport._on_comm_open` simply rebinds its `_comm`
    reference to whichever one most recently opened -- so this doesn't
    need special-casing here.
    """
    if namespace is None:
        # See _kernel_transport.py's module docstring -- this is the same
        # private-module seam KernelCommTransport.connect() has.
        from cubevis.bokeh.transport._environment import get_ipython_kernel_shell

        shell = get_ipython_kernel_shell()
        if shell is None:
            raise RuntimeError(
                "ensure_remote_worker: no active Jupyter kernel session, and "
                "no explicit `namespace` was given to store the marker in"
            )
        namespace = shell.user_ns

    existing = namespace.get(_NAMESPACE_KEY)
    if existing is not None:
        logger.debug(
            f"ensure_remote_worker: existing worker found "
            f"(target_name={existing.target_name!r}, comm_mgr_id={existing.comm_mgr_id}); "
            f"not reconstructing"
        )
        return existing.comm_mgr_id

    mgr = CommMgr(role=CommMgr.ROLE_DEFAULT)
    if target_name is None:
        # No fixed constant needed -- see the module comment above.
        # Self-referential (the comm_mgr_id doubles as its own target
        # name) rather than a second generated value, matching the
        # convention this module's own spike/validation code already
        # used at the protocol level.
        target_name = mgr.comm_mgr_id
    transport = KernelCommTransport(mgr.comm_mgr_id, target_name=target_name)
    mgr._transport = transport
    transport.set_message_callback(mgr._route_message)

    # Deliberately NOT using CommMgr.initialize() here (which the P_local
    # side's open_remote_kernel_link() does use) -- initialize() awaits
    # transport.connect(), and KernelCommTransport.connect() blocks until
    # the peer's comm_open actually arrives. Blocking here would stall
    # this execute_request cell until P_local gets around to dialing in,
    # which may be seconds away. Instead: register the target immediately
    # (non-blocking, below) and mark RUNNING right away; the transport's
    # `_open_event`/`_comm` binding completes asynchronously, whenever
    # the comm_open shell message actually gets processed -- no explicit
    # wait needed on this side.

    # Build the caller's actual worker state BEFORE marking anything
    # RUNNING or registering the comm target -- if `build_worker` raises
    # (e.g. the MS fails to open), nothing is left half-registered for a
    # later call to mistake for a healthy, already-bootstrapped worker.
    #
    # `mgr` is passed in specifically so build_worker can mgr.open(...)/
    # .register(...) its own protocol handlers -- see the docstring above.
    worker = build_worker(mgr)

    import comm as _comm_pkg

    _comm_pkg.get_comm_manager().register_target(target_name, transport._on_comm_open)
    mgr.state = AppState.RUNNING
    mgr._initialized = True

    handle = RemoteWorkerHandle(target_name, mgr.comm_mgr_id, mgr, transport, worker)
    namespace[_NAMESPACE_KEY] = handle
    logger.debug(
        f"ensure_remote_worker: bootstrapped new worker "
        f"(target_name={target_name!r}, comm_mgr_id={mgr.comm_mgr_id})"
    )
    return mgr.comm_mgr_id
