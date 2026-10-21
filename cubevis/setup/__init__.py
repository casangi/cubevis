"""Setting up remote kernels for cubevis: networks of nodes behind a login
host, environments on their shared filesystems, kernels, and the sessions
that run them.

Design: ``devel/notes/drs/visplot/remote_session_design.md`` (section 7b
for the file layout).

* ``state``   -- what setup knows: ``state.json`` (shared) and this
                 computer's ``hosts/<id>/local.json``
* ``sshgen``  -- the ssh configuration and wrapper rules generated from it
* ``data/``   -- the ssh wrapper and the scripts run on remote hosts

The configuration directory may be a home directory shared by several
computers (a cluster's nodes, and a GUI host among them).  What belongs to
one computer lives under ``hosts/<id>/``; what is shared (``state.json``)
is written under a lock, merging (``state.save``).

Standard library only, so the tooling works before anything else is
installed.  ``python -m cubevis.setup`` is the user interface.
"""
from __future__ import annotations

import os
import platform
import re
import socket
import subprocess
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "data"


def data_file(name: str) -> Path:
    """A file shipped in ``cubevis/setup/data`` (``ssh``, ``probe.sh``,
    ``remote-setup.sh``)."""
    p = DATA_DIR / name
    if not p.is_file():
        raise FileNotFoundError(f"cubevis setup data file {name!r} is missing ({p})")
    return p


def config_dir() -> Path:
    """``$XDG_CONFIG_HOME/cubevis``, or ``~/.config/cubevis``."""
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return Path(base) / "cubevis"


_ID_RE = re.compile(r"[^a-z0-9_-]+")
_host_id_cache: dict = {}


def host_id() -> str:
    """This computer's name for its own directory, ``hosts/<id>``.

    It must stay the same while the computer moves between networks, so it
    is the short host name (first label, lower case): a Linux host may
    report either ``zuul06`` or ``zuul06.cv.nrao.edu``, and a Mac's
    ``hostname`` follows the network it is on, so on macOS the
    ``LocalHostName`` (System Settings, Sharing) is used.
    ``$CUBEVIS_HOST_ID`` overrides it (and the tests use that to play
    several computers in one directory)."""
    env = os.environ.get("CUBEVIS_HOST_ID")
    if env:
        return _clean_id(env)
    if "id" not in _host_id_cache:
        name = ""
        if platform.system() == "Darwin":
            try:
                name = subprocess.run(["scutil", "--get", "LocalHostName"], capture_output=True,
                                      text=True, timeout=5).stdout.strip()
            except (OSError, subprocess.SubprocessError):
                name = ""
        _host_id_cache["id"] = _clean_id(name or socket.gethostname() or "localhost")
    return _host_id_cache["id"]


def _clean_id(name: str) -> str:
    short = name.strip().lower().split(".")[0]
    return _ID_RE.sub("-", short).strip("-") or "localhost"


def host_dir() -> Path:
    """``<config>/hosts/<host_id>``: this computer's own files."""
    return config_dir() / "hosts" / host_id()


class RuntimeDirError(RuntimeError):
    """The directory for ssh's connection sockets cannot be used safely."""


def runtime_path() -> Path:
    """The path ``runtime_dir`` checks and creates (nothing checked)."""
    env = os.environ.get("CUBEVIS_RUNTIME_DIR")
    return Path(env) if env else Path("/tmp") / f"cubevis-{os.getuid()}"


def runtime_dir(create: bool = True) -> Path:
    """Where ssh's shared-connection sockets go: on local disk, never in the
    configuration directory (a Unix socket cannot live on NFS, which a
    shared home often is).

    ``$CUBEVIS_RUNTIME_DIR`` if set, else ``/tmp/cubevis-<uid>``.  Not
    ``$XDG_RUNTIME_DIR``: it is removed when the last login session ends,
    which would take the sockets from kernels still running.  The
    directory must belong to this user and be closed to others (anyone can
    create a name in /tmp first); otherwise ``RuntimeDirError`` says why.
    """
    p = runtime_path()
    env = os.environ.get("CUBEVIS_RUNTIME_DIR")
    if create and not os.path.lexists(p):
        try:
            p.mkdir(mode=0o700, parents=bool(env), exist_ok=True)
        except OSError as exc:
            raise RuntimeDirError(f"cannot create {p} for ssh's connection sockets: {exc}") from None
    if p.exists():
        st = os.lstat(p)
        import stat as _stat
        if not _stat.S_ISDIR(st.st_mode):
            raise RuntimeDirError(f"{p} is not a directory; remove it, or set "
                                  "CUBEVIS_RUNTIME_DIR to a directory of your own")
        if st.st_uid != os.getuid():
            raise RuntimeDirError(f"{p} belongs to someone else (uid {st.st_uid}); set "
                                  "CUBEVIS_RUNTIME_DIR to a directory of your own")
        if st.st_mode & 0o077:
            try:
                os.chmod(p, 0o700)
            except OSError as exc:
                raise RuntimeDirError(f"{p} is open to other users and cannot be closed "
                                      f"({exc})") from None
    return p
