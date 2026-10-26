"""What setup runs on a node, over ssh (the wrapper and the generated
configuration -- exactly what kernels use): the discovery script, the
launcher's installation, and a test run of a kernel's launcher.

Nothing here needs cubevis on the node.  Python programs go to the node's
own ``python3`` on stdin (``ssh NODE <python3> -``), so no quoting for the
remote shell is involved; the remote shell (bash, or csh at some sites)
sees only a program name, ``-`` and options.  RHEL 8 has no ``python3`` on
PATH unless installed, so the first look tries ``/usr/libexec/platform-python``
too (its 3.6 is what ``discover`` is written for).
"""
from __future__ import annotations

import json
import shlex
from typing import Optional

from . import discover as D
from . import launcher

DISCOVER_TIMEOUT = 900       # several environments, each importing cubevis from NFS
TEST_TIMEOUT = 300

# Tried in order on the node: a remote shell runs this one line.  No
# quotes, no '$': csh passes it unchanged too.
PYTHONS = ("python3", "/usr/libexec/platform-python", "python")


def _python_finder() -> str:
    """One command line for any login shell: the first of PYTHONS that
    exists runs the program on stdin.  ``sh -c`` with the list as words,
    so csh never parses the loop itself."""
    loop = ("for p in " + " ".join(PYTHONS) + "; do "
            "command -v $p >/dev/null 2>&1 && exec $p - \"$@\"; done; "
            "echo cubevis: no python3 on this host >&2; exit 127")
    return "/bin/sh -c " + shlex.quote(loop) + " sh"


class RemoteError(RuntimeError):
    """A round trip that gave no usable answer; the message says why."""


def discover(c, alias: str, paths=(), only: bool = False, python3: Optional[str] = None) -> dict:
    """Run the discovery script on *alias*; its report.  *python3*: the
    interpreter an earlier report named (skips the search)."""
    args = ["--json"] + (["--only"] if only else []) + list(paths)
    if python3:
        remote = [shlex.quote(python3), "-"] + [shlex.quote(a) for a in args]
    else:
        remote = [_python_finder()] + [shlex.quote(a) for a in args]
    r = c.ssh(alias, *remote, timeout=DISCOVER_TIMEOUT, input=D.payload())
    for line in (r.stdout or "").splitlines():
        if line.startswith(D.MARKER):
            try:
                return json.loads(line[len(D.MARKER):])
            except ValueError:
                break
    from .steps import last_line
    why = last_line(c.last_out) or f"ssh exit {r.returncode}"
    if r.returncode == 127 or "no python3" in c.last_out:
        why = "no python3 there (setup needs one to look around; any version from 3.6)"
    raise RemoteError(why)


def install_launcher(c, alias: str, python3: str, path: str, text: str) -> str:
    """Write the launcher *text* at *path* on *alias*: ``written`` or
    ``same``; RemoteError otherwise."""
    r = c.ssh(alias, shlex.quote(python3), "-", timeout=120,
              input=launcher.install_script(path, text))
    status, detail = launcher.parse_install(r.stdout)
    if status in ("written", "same"):
        return status
    from .steps import last_line
    raise RemoteError(detail if status == "error" else
                      (last_line(c.last_out) or f"ssh exit {r.returncode}"))


def test_launcher(c, alias: str, path: str) -> tuple:
    """Run ``launcher.TEST_CODE`` through the launcher at *path* on *alias*:
    ``(fingerprint, cwd)``; RemoteError with the reason otherwise (the
    launcher's own message for a missing directory, the import error)."""
    r = c.ssh(alias, shlex.quote(path), "-c", shlex.quote(launcher.TEST_CODE),
              timeout=TEST_TIMEOUT)
    got = launcher.parse_test(r.stdout)
    if got:
        return got
    out = c.last_out
    lines = [l for l in out.strip().splitlines() if l and not l.startswith("[ssh-wrapper]")]
    cub = [l for l in lines if l.startswith("cubevis: ")]
    if cub:
        raise RemoteError(cub[-1][len("cubevis: "):])
    err = [l for l in lines if "Error" in l or "error" in l]
    raise RemoteError((err or lines or [f"ssh exit {r.returncode}"])[-1])
