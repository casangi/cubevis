"""This computer's records of the remote sessions it started: one per
plotter (or, later, iclean) connected to a remote kernel, in the
``sessions`` section of ``hosts/<id>/local.json`` (``state.py``; reserved
there since stage 2a, written from 2026-11-04, notes section 59).

A record is what a later stage needs to find a session again -- to say
what is still running after a crash, to reconnect a running Python to its
kernel after the network changed (Scenario A), or to reattach a new Python
to a kernel left running (design section 10) -- and nothing more::

    "sessions": {ID: {"app",            "visplot" (later "iclean")
                      "kernel",         setup's kernel name (or the Jupyter one)
                      "jupyter_kernel", the Jupyter kernel started (cubevis-NAME)
                      "node",           the ssh alias it runs on
                      "local_pid",      this computer's Python holding it
                      "started",        UTC, ISO 8601
                      "persistent_file",  sshpyk's file for the kernel
                      "remote_pid",     the kernel's process on the node
                      "data",           what it opened (the MS path)
                      "context_id",     the worker's execution context in the kernel
                      "handle",         the data object's handle in that worker
                      "cubevis"}}       this side's version

The connection itself -- the kernel's ports on the node and its signing
key -- is *not* copied here: sshpyk keeps it in ``persistent_file`` (in
Jupyter's runtime directory, mode 600), which is what its own reattach
(``AsyncKernelManager(..., existing=...)``) reads.  sshpyk deletes that
file on a clean shutdown; a Python that died keeps it, with the kernel
still running on the node.

Written when a session connects, removed when it closes.  A record whose
``local_pid`` is gone is *left over*: its kernel may still be running on
``node``.  ``status`` lists both kinds.  Recording never fails a session:
every error here is logged and ignored.
"""
from __future__ import annotations

import datetime as _dt
import logging
import os
import uuid
from typing import Optional

log = logging.getLogger(__name__)

FIELDS = ("app", "kernel", "jupyter_kernel", "node", "local_pid", "started",
          "persistent_file", "remote_pid", "data", "context_id", "handle", "cubevis")


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _version() -> Optional[str]:
    try:
        from cubevis import __version__
        return str(__version__)
    except Exception:
        return None


def kernel_facts(km) -> dict:
    """What a started kernel manager *km* says about its kernel: sshpyk's
    ``node`` (ssh alias), ``persistent_file`` and ``remote_pid``; empty for
    a kernel that is not sshpyk's (a local one)."""
    p = getattr(km, "provisioner", None)
    alias = getattr(p, "ssh_host_alias", None)
    if not alias:
        return {}
    return {"node": str(alias),
            "persistent_file": getattr(p, "persistent_file", None) or None,
            "remote_pid": getattr(p, "rem_pid_k", None)}


def setup_kernel_name(jupyter_kernel: str, st=None) -> str:
    """Setup's name for the Jupyter kernel *jupyter_kernel* (``cubevis-NAME``
    -> ``NAME``), else the Jupyter name itself.  With setup's state *st*, a
    kernel of a cluster on another of its nodes (``cubevis-NAME-NODE``,
    ``python -m cubevis.setup jupyter NAME NODE``) is NAME too, when NAME is
    one of its kernels and NODE one of its nodes."""
    if not jupyter_kernel.startswith("cubevis-"):
        return jupyter_kernel
    name = jupyter_kernel[len("cubevis-"):]
    if st is not None:
        try:
            kernels, nodes = st.section("kernels"), st.nodes()
        except Exception:
            return name
        if name not in kernels:
            for node in sorted(nodes, key=len, reverse=True):
                if name.endswith(f"-{node}") and name[:-len(node) - 1] in kernels:
                    return name[:-len(node) - 1]
    return name


def record(*, app: str, jupyter_kernel: str, node: str,
           persistent_file: Optional[str] = None, remote_pid=None,
           data: Optional[str] = None, kernel: Optional[str] = None,
           context_id: Optional[str] = None, handle: Optional[str] = None) -> Optional[str]:
    """Record a session that just connected; its id, or None when it could
    not be recorded (logged, never raised)."""
    try:
        from . import state
        sid = f"{app}-{uuid.uuid4().hex[:8]}"
        st = state.load()
        st.section("sessions")[sid] = {
            "app": app, "kernel": kernel or setup_kernel_name(jupyter_kernel, st),
            "jupyter_kernel": jupyter_kernel, "node": node,
            "local_pid": os.getpid(), "started": _now(),
            "persistent_file": persistent_file,
            "remote_pid": int(remote_pid) if remote_pid else None,
            "data": data, "context_id": context_id, "handle": handle,
            "cubevis": _version()}
        state.save(st)
        return sid
    except Exception as e:                       # never fails the session
        log.debug("could not record the session: %s", e, exc_info=True)
        return None


def update(sid: Optional[str], **fields) -> None:
    """Change fields of session *sid*'s record (a new worker: its
    ``context_id`` and ``handle``); logged, never raised."""
    if not sid:
        return
    try:
        from . import state
        st = state.load()
        rec = st.section("sessions").get(sid)
        if rec is None:
            return
        rec.update({k: v for k, v in fields.items() if k in FIELDS})
        state.save(st)
    except Exception as e:
        log.debug("could not update session %s: %s", sid, e, exc_info=True)


def forget(sid: Optional[str]) -> None:
    """Remove session *sid*'s record (closed); logged, never raised."""
    if not sid:
        return
    try:
        from . import state
        st = state.load()
        if st.section("sessions").pop(sid, None) is not None:
            state.save(st)
    except Exception as e:
        log.debug("could not forget session %s: %s", sid, e, exc_info=True)


def pid_running(pid) -> bool:
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (TypeError, ValueError, OSError):
        return False
    return True


def listing(st=None) -> list:
    """``[(id, record, live)]``, oldest first; *live*: its Python is still
    running on this computer (else the record is left over)."""
    if st is None:
        from . import state
        st = state.load()
    out = []
    for sid, rec in st.section("sessions").items():
        out.append((sid, rec, pid_running(rec.get("local_pid"))))
    out.sort(key=lambda x: (x[1].get("started") or "", x[0]))
    return out


def describe(sid: str, rec: dict, live: bool) -> str:
    """One line for ``status``."""
    what = rec.get("app") or "session"
    where = f"kernel {rec.get('kernel')} on {rec.get('node')}"
    data = f", {rec['data']}" if rec.get("data") else ""
    since = f", since {rec['started']}" if rec.get("started") else ""
    if live:
        return f"{sid}: {what}, {where}{data}{since} (running here, pid {rec.get('local_pid')})"
    return (f"{sid}: {what}, {where}{data}{since} -- left over: its Python (pid "
            f"{rec.get('local_pid')}) is gone; the kernel may still run on {rec.get('node')}"
            + (f" (pid {rec['remote_pid']})" if rec.get("remote_pid") else ""))
