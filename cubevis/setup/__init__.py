"""Setting up remote kernels for cubevis: networks of nodes behind a login
host, environments on their shared filesystems, kernels, and the sessions
that run them.

Design: ``devel/notes/drs/visplot/remote_session_design.md``.

* ``state``   -- ``~/.config/cubevis/state.json``, the source of truth
* ``sshgen``  -- the ssh configuration and wrapper rules generated from it
* ``data/``   -- the ssh wrapper and the scripts run on remote hosts

Standard library only, so the tooling works before anything else is
installed.  ``python -m cubevis.setup`` (stage 2) is the user interface.
"""
from __future__ import annotations

import os
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
