"""
Chunk 1c, Task 6 -- the cubevis-level kernel-persistence manifest.

Layered on top of `sshpyk`'s own real, already-verified mechanism (see
the implementation doc's Chunk 1 "Real-API verification" section) --
NOT a reimplementation of it. `sshpyk` gives the *mechanism*: every
kernel launch writes a `persistent_file` (JSON: `kernel_id`,
`rem_sys_name`, `rem_conn_info`, `rem_pid_k`, `rem_pid_ka`, `rem_conn_fp`,
`rem_proc_cmds`); that file is deleted on clean shutdown unless the
provisioner's `persistent` flag is set; reattaching is
`AsyncKernelManager(kernel_name=..., existing=<name or path>)`, which
resolves the file and re-verifies the remembered remote PIDs are still
real processes before trusting the reattach. All of that is `sshpyk`'s
job and is not touched here.

What `sshpyk` does NOT give `cubevis`: a way to *discover* which
`persistent_file`, if any, corresponds to a session `cubevis` itself
cares about, or any way to enumerate what is currently dangling --
`find_persistent_file()` resolves a name/pattern you already know, it
doesn't list what exists. This module fills exactly that gap, and no
more:

  - `record(label, persistent_file, kernel_name=...)` -- called
    whenever a kernel meant to survive `P_local` exiting is created
    (i.e. whenever the caller sets `persistent=True` on construction),
    *after* sshpyk has already written its own `persistent_file`.
    Records a *reference* to that file, not a copy of its contents.
  - `outstanding()` -- read at `cubevis.remote` startup (or on demand).
    Returns everything currently recorded: label, the referenced
    `persistent_file`/kernel name, and when it was created. Deliberately
    does NOT itself attempt a liveness check or a reattach -- see
    "Liveness is the caller's job," below.
  - `forget(label)` -- called when a kernel is deliberately shut down
    *through cubevis* (not merely disconnected from), removing that
    label's entry once the underlying kernel is confirmed gone.

Reuse vs. shutdown of an outstanding entry is the caller's/user's
decision, never automated here either way: silently reusing a stale
kernel could hand back unexpected state; silently killing one could
tear down a reserved node's walltime the user still needed.

## Liveness is the caller's job, deliberately

The design doc's own phrasing is "whatever liveness can be cheaply
checked (e.g. attempting the reattach and seeing whether sshpyk's own
PID reverification succeeds)". That reattach *is* sshpyk's mechanism
(`AsyncKernelManager(kernel_name=..., existing=...)`, plus a real SSH
connection for a genuine cluster kernel) -- reimplementing any part of
it here, even a lightweight probe, would duplicate logic this module is
explicitly layered on top of rather than owning. So this module stays
pure bookkeeping (record/outstanding/forget); a caller that wants a
liveness answer for one outstanding entry attempts the real reattach
itself, exactly as `test_persistence_manifest.py` does end-to-end
against a real (local, in this sandbox) kernel process standing in for
what would be a real sshpyk-provisioned one -- see that test's module
docstring for what is and is not faked.

## File format

A single JSON file, one entry per caller-chosen label::

    {
      "<label>": {
        "persistent_file": "<sshpyk persistent_file path or name>",
        "kernel_name": "<kernelspec name, for AsyncKernelManager(kernel_name=...)>",
        "created_at": "<ISO-8601 UTC timestamp>"
      },
      ...
    }

Default location: ``<jupyter_runtime_dir()>/cubevis-kernel-manifest.json``
-- the same directory sshpyk's own `persistent_file`s live in by
default, so both are easy to find together, but a genuinely separate
file (never duplicating sshpyk's own contents, per the design doc).
Writes are atomic (write to a temp file, then `os.replace`) so a crash
mid-write can't leave a half-written manifest behind.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

__all__ = ["KernelPersistenceManifest"]


def _default_manifest_path() -> str:
    from jupyter_core.paths import jupyter_runtime_dir

    return os.path.join(jupyter_runtime_dir(), "cubevis-kernel-manifest.json")


class KernelPersistenceManifest:
    """
    Pure bookkeeping over one JSON file -- see the module docstring for
    the format and for why liveness-checking is deliberately not this
    class's job.

    Each instance is stateless between calls (every method re-reads the
    file), so two independent `KernelPersistenceManifest(path=...)`
    instances pointed at the same path -- e.g. one from a `P_local`
    session that has since exited, and one from a freshly restarted
    `P_local` process -- see each other's writes correctly, which is the
    whole point: this is exactly the "did I leave a kernel dangling"
    check that must survive `P_local` itself restarting.
    """

    def __init__(self, path: Optional[str] = None):
        self._path = path or _default_manifest_path()

    @property
    def path(self) -> str:
        return self._path

    def _read(self) -> Dict[str, Dict[str, Any]]:
        if not os.path.exists(self._path):
            return {}
        try:
            with open(self._path, "r") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            logger.exception(
                f"KernelPersistenceManifest: failed to read {self._path!r}; "
                f"treating as empty rather than raising, so a corrupt manifest "
                f"doesn't block startup"
            )
            return {}
        return data if isinstance(data, dict) else {}

    def _write(self, data: Dict[str, Dict[str, Any]]) -> None:
        directory = os.path.dirname(self._path) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(prefix=".cubevis-kernel-manifest-", dir=directory)
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, indent=2, sort_keys=True)
            os.replace(tmp_path, self._path)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def record(self, label: str, persistent_file: str, kernel_name: Optional[str] = None) -> None:
        """Record that `label` now refers to `persistent_file` (and,
        when known, the kernelspec `kernel_name` needed to reattach via
        `AsyncKernelManager(kernel_name=..., existing=persistent_file)`).
        Overwrites any previous entry under the same label -- a caller
        that creates a new persistent kernel under a label that already
        had one is presumed to be superseding it, not appending."""
        data = self._read()
        data[label] = {
            "persistent_file": persistent_file,
            "kernel_name": kernel_name,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        self._write(data)
        logger.debug(
            f"KernelPersistenceManifest: recorded label={label!r} "
            f"persistent_file={persistent_file!r} kernel_name={kernel_name!r}"
        )

    def forget(self, label: str) -> bool:
        """Remove `label`'s entry -- called once the underlying kernel
        has been deliberately, successfully shut down through cubevis.
        Returns True if an entry was actually removed, False if there
        was nothing recorded under this label (not an error -- forgetting
        an already-forgotten label is a no-op, not a mistake worth
        raising over)."""
        data = self._read()
        if label not in data:
            return False
        del data[label]
        self._write(data)
        logger.debug(f"KernelPersistenceManifest: forgot label={label!r}")
        return True

    def outstanding(self) -> Dict[str, Dict[str, Any]]:
        """Everything currently recorded -- checked at `cubevis.remote`
        startup and surfaced to the caller/user. Reuse vs. shutdown of
        any entry is their decision; this method only reports, it never
        decides."""
        return self._read()

    def get(self, label: str) -> Optional[Dict[str, Any]]:
        """The single recorded entry for `label`, or None if there
        isn't one."""
        return self._read().get(label)
