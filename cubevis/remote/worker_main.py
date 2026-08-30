"""
Chunk 1b, Task 2 -- restructured by Chunk 1c, Tasks 3 and 4.

`python -m cubevis.remote.worker_main` -- the worker subprocess's entry
point. Constructs a `CommMgr(role=CommMgr.ROLE_DEFAULT)` +
`WorkerCommTransport`, reading/writing its own stdin/stdout.

Chunk 1c change (Task 3): this module no longer hardcodes
`_register_toy_handlers` at import/startup time. Instead:

  - A small, fixed set of *generic* wire handlers -- `configure`,
    `create_object`, `call_method`, `dispose_object`, `eval_code`,
    `exec_code`, and `worker_info` -- are registered immediately at
    startup, unconditionally. These are the reusable framework surface
    (Chunk 1c's Tasks 3/4) and are never application-specific, so there
    is nothing to defer them for.
  - `configure` is the *opening configuration message* the kickoff doc
    describes: `_supervisor.py`'s `create_context` always sends exactly
    one `configure` request to a freshly spawned worker (with an empty
    payload if the caller passed no `config`), and this handler is what
    interprets that payload. If it carries a `register_function` dotted
    path, that function is imported and called as
    `register_function(comm, registry, **kwargs)` -- mirroring
    `build_worker(mgr)`'s own shape (Chunk 1's `_worker.py`): handed the
    worker's own `Comm` and `ObjectRegistry` ("handle table"), free to
    register whatever classes/handlers it wants. If no
    `register_function` is given, the *default* registration -- Chunk
    1b's original toy handlers (`ping`/`add`/`slow_echo`/`crash`/
    `trigger_push`) -- is installed instead, so Chunk 1b's existing tests
    and demo keep working unmodified.

CRITICAL: nothing here may write to stdout except the framed protocol
messages `WorkerCommTransport` sends -- stdout is the wire. Anything a
handler (or a library it calls) prints accidentally would corrupt the
frame stream from the supervisor's point of view. Logging is configured
to stderr only, which `WorkerProcessTransport` on the supervisor side
captures and relays.
"""
from __future__ import annotations

import argparse
import asyncio
import importlib
import logging
import os
import socket
import sys
import time
from typing import Any, Dict

from cubevis.bokeh.transport import CommMgr, AppState
from cubevis.remote._worker_transport import WorkerCommTransport
from cubevis.remote._object_registry import ObjectRegistry

logger = logging.getLogger("cubevis.remote.worker_main")


def _load_dotted(path: str) -> Any:
    """
    Resolve a dotted import path to the object it names.

    Accepts either `"package.module:attr"` (colon form, the same
    convention Python entry points use -- unambiguous about where the
    module name ends and the attribute path begins) or plain
    `"package.module.attr"` (dotted form, resolved by splitting off the
    last component as the attribute -- ambiguous only if a package
    itself is callable, which registration functions are not expected to
    be).
    """
    if ":" in path:
        module_name, _, attr_path = path.partition(":")
    else:
        module_name, _, attr_path = path.rpartition(".")
    if not module_name:
        raise ValueError(f"cannot resolve dotted path {path!r}: no module component")
    obj = importlib.import_module(module_name)
    if attr_path:
        for part in attr_path.split("."):
            obj = getattr(obj, part)
    return obj


def _register_toy_handlers(comm) -> None:
    """Mirrors Chunk 1's demo_local_or_remote_kernel.py toy commands --
    not real backend objects, just enough surface to exercise the
    transport for real (request/response, push, a deliberately slow
    command for liveness tests, and a deliberate crash for stderr-relay
    tests). This is the *default* registration Chunk 1c's `configure`
    handler installs when no `register_function` is given -- see the
    module docstring."""

    def handle_ping(msg):
        return {"pong": True, "pid": os.getpid(), "hostname": socket.gethostname()}

    def handle_add(msg):
        return {"sum": msg["a"] + msg["b"]}

    async def handle_slow_echo(msg):
        """A command that blocks the *worker's* asyncio loop for real
        (a CPU-bound sleep, standing in for a GIL-holding C++ call) --
        for exercising the supervisor's background-dispatch path against
        genuinely long-running work."""
        duration = msg.get("duration", 1.0)
        time.sleep(duration)  # deliberately synchronous/blocking, not asyncio.sleep
        return {"echo": msg.get("value"), "slept": duration}

    def handle_crash(msg):
        raise RuntimeError(f"deliberate crash for testing: {msg.get('reason', 'no reason given')}")

    async def handle_trigger_push(msg):
        """Worker-initiated push: sends an unsolicited message on this
        same comm *before* replying to this request, exercising the
        other direction of traffic (not just replying to what it's
        asked) against a real subprocess."""
        await comm.send("worker_event", {"note": msg.get("note", "hello from worker"),
                                          "pid": os.getpid()})
        return {"triggered": True}

    comm.register("trigger_push", handle_trigger_push)

    comm.register("ping", handle_ping)
    comm.register("add", handle_add)
    comm.register("slow_echo", handle_slow_echo)
    comm.register("crash", handle_crash)


def _register_generic_handlers(comm, registry: ObjectRegistry, namespace: Dict[str, Any],
                                configured: asyncio.Event) -> None:
    """
    Chunk 1c, Tasks 3 + 4 -- the generic, always-present framework
    surface. Registered once, unconditionally, at worker startup --
    these are never application-specific, so (unlike the toy handlers)
    there is no "default vs configured" distinction for them.
    """

    def handle_configure(msg):
        """The opening configuration message (Task 3). `_supervisor.py`
        sends exactly one of these, immediately after spawning, whether
        or not the caller passed a `config` to `create_context` -- an
        empty/absent payload here means "no application configuration,
        use the Chunk 1b-compatible toy handlers."""
        config = msg or {}
        register_function_path = config.get("register_function")
        applied = None
        if register_function_path:
            register_function = _load_dotted(register_function_path)
            register_function(comm, registry, **(config.get("kwargs") or {}))
            applied = register_function_path
        else:
            _register_toy_handlers(comm)
        configured.set()
        return {
            "configured": True,
            "register_function": applied,
            "pid": os.getpid(),
            "hostname": socket.gethostname(),
        }

    def handle_worker_info(msg):
        """Generic introspection: pid/hostname/executable for this
        worker process, independent of whatever it was configured
        with -- used by the try_*.py demos to print "process, kernel,
        host" information once at startup, per the kickoff doc."""
        return {
            "pid": os.getpid(),
            "hostname": socket.gethostname(),
            "executable": sys.executable,
        }

    def handle_create_object(msg):
        handle = registry.create_object(
            msg["class_name"], msg.get("args") or [], msg.get("kwargs") or {}
        )
        return {"handle": handle}

    def handle_call_method(msg):
        # The *result* is the raw return value -- not wrapped in an
        # envelope dict -- matching call_method's documented contract
        # ("-> <result>"). A numpy array, a plain scalar, a dict: all
        # travel through cubevis.utils.serialize/deserialize exactly as
        # a toy handler's return value already does (Chunk 1b never
        # required its handlers to return a specific shape either).
        return registry.call_method(
            msg["handle"], msg["method"], msg.get("args") or [], msg.get("kwargs") or {}
        )

    def handle_dispose_object(msg):
        disposed = registry.dispose_object(msg["handle"])
        return {"disposed": disposed}

    def handle_eval_code(msg):
        """Genuine expression evaluation -- `eval(code, namespace)` --
        for the single-expression case (`eval_code("1 + 2")` -> `3`),
        matching plain Python `eval()` semantics. `namespace` is a
        persistent dict for the life of this worker process, shared
        with `exec_code` below and seeded with `_registry` so eval/exec
        snippets can reach objects created via `create_object` (e.g.
        `_registry.get_object(handle).some_attr`)."""
        return eval(msg["code"], namespace)

    def handle_exec_code(msg):
        """Multi-statement execution -- `exec(code, namespace)`. exec()
        itself has no return value, so the documented convention (see
        the implementation doc's Chunk 1c section) is: after executing,
        read back a designated variable, `_result`, from `namespace` --
        not derived from mirroring a notebook cell's last-expression
        display hook (tempting, since that IS the mechanism Chunk 1's
        own bootstrap cells rely on via `client.execute()`, but "the
        last line" is not reliably "the interesting value" once a
        caller is composing several statements together). `_result` is
        cleared before each exec_code call so a stale value from an
        earlier call can't be silently mistaken for this one's result.
        """
        namespace.pop("_result", None)
        exec(msg["code"], namespace)
        return namespace.get("_result")

    comm.register("configure", handle_configure)
    comm.register("worker_info", handle_worker_info)
    comm.register("create_object", handle_create_object)
    comm.register("call_method", handle_call_method)
    comm.register("dispose_object", handle_dispose_object)
    comm.register("eval_code", handle_eval_code)
    comm.register("exec_code", handle_exec_code)


async def _amain(comm_mgr_id: str) -> None:
    mgr = CommMgr(role=CommMgr.ROLE_DEFAULT, comm_mgr_id=comm_mgr_id,
                  transport_type="remote_kernel")
    comm = mgr.open("worker")

    registry = ObjectRegistry()
    namespace: Dict[str, Any] = {"__builtins__": __builtins__, "_registry": registry}
    configured = asyncio.Event()

    _register_generic_handlers(comm, registry, namespace, configured)

    transport = WorkerCommTransport(comm_mgr_id)
    await mgr.initialize(transport=transport)
    mgr.state = AppState.RUNNING

    logger.info(f"worker_main: ready, pid={os.getpid()}, comm_mgr_id={comm_mgr_id}, "
                f"hostname={socket.gethostname()}")
    await transport.run()
    logger.info("worker_main: transport closed, exiting")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--comm-mgr-id", required=True,
                         help="comm_mgr_id the supervisor expects this worker to use "
                              "(so both sides' role-direction tags line up)")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    # Logging to stderr ONLY -- stdout is the wire protocol.
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        stream=sys.stderr,
        format="%(asctime)s %(name)s %(levelname)s: %(message)s",
    )

    try:
        asyncio.run(_amain(args.comm_mgr_id))
    except Exception:
        logger.exception("worker_main: fatal error during startup/run")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
