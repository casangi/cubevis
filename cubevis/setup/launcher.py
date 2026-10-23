"""The launcher: a small script on the node that a kernel's ssh runs
instead of the environment's python.

sshpyk starts the remote kernel as ``$FP_PY -c '...'``, with ``FP_PY`` the
configured ``remote_python``, after checking only that it is an executable
file; it passes no working directory and no environment.  So
``node-here`` writes, per environment, ``hosts/<id>/node/<network>/<env>.sh``
and the GUI side gives its path as ``remote_python``.  Before Python
starts -- which matters: ``OMP_NUM_THREADS``, ``HDF5_USE_FILE_LOCKING`` and
the like are read when the libraries load -- it

1. changes to the environment's working directory (default: the home
   directory), so that relative paths (``ms="sis14.ms"``) are found there;
   a directory that is gone stops the kernel with a message, rather than
   starting it somewhere else;
2. activates the environment as conda or venv would: ``PATH``,
   ``CONDA_PREFIX`` / ``VIRTUAL_ENV``, and, for conda, the environment's
   ``etc/conda/activate.d/*.sh`` scripts (their output goes to stderr:
   sshpyk reads the launcher's stdout);
3. sets the environment's variables (after activation, so they win);
4. ``exec``s the python with the arguments it was given.

Only what ``node-here`` recorded on the node goes in; nothing comes from
the computer the plots are on (developers pass that explicitly, stage 3).
bash, not sh: conda's activation scripts are written for bash and zsh.
"""
from __future__ import annotations

import os
import re
import shlex
import tempfile
from pathlib import Path
from typing import Optional

_VAR_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# Set by the launcher itself; a user value for them would break activation.
RESERVED = {"PATH", "CONDA_PREFIX", "CONDA_DEFAULT_ENV", "VIRTUAL_ENV", "PWD", "HOME"}


def check_var(name: str) -> str:
    if not _VAR_RE.match(name or ""):
        raise ValueError(f"{name!r} is not an environment variable name (letters, digits, _)")
    if name in RESERVED:
        raise ValueError(f"{name} is set by the launcher itself")
    return name


def parse_vars(text: str) -> dict:
    """``NAME=value NAME2='a b'`` (shell quoting) -> dict; ValueError on a
    word that is not NAME=value."""
    out = {}
    for word in shlex.split(text or ""):
        if "=" not in word:
            raise ValueError(f"{word!r}: give NAME=value")
        k, v = word.split("=", 1)
        out[check_var(k)] = v
    return out


def format_vars(env: dict) -> str:
    """The inverse of ``parse_vars``, for a default answer."""
    return " ".join(f"{k}={shlex.quote(v)}" for k, v in sorted((env or {}).items()))


def text(name: str, network: str, host: str, e: dict, home: str) -> str:
    q = shlex.quote
    prefix = e.get("prefix") or os.path.dirname(os.path.dirname(e["python"]))
    workdir = e.get("workdir") or home
    lines = [
        "#!/bin/bash",
        f"# Written by python -m cubevis.setup node-here on {host} for environment",
        f"# {name} of network {network}; written again at each run (change it there).",
        "# A kernel's ssh runs this instead of python: working directory, activation,",
        "# environment variables, then the environment's python with the arguments given.",
        f"cd {q(workdir)} || {{ echo {q(f'cubevis: cannot change to {workdir}, the working directory of environment {name} (python -m cubevis.setup --redo node-here {network})')} >&2; exit 1; }}",
    ]
    if e.get("kind") == "conda":
        lines += [
            f"export CONDA_PREFIX={q(prefix)}",
            f"export CONDA_DEFAULT_ENV={q(os.path.basename(prefix.rstrip('/')))}",
            f"export PATH={q(prefix + '/bin')}:\"$PATH\"",
            f"for f in {q(prefix)}/etc/conda/activate.d/*.sh; do",
            "    [ -r \"$f\" ] && . \"$f\" >&2",
            "done",
        ]
    elif e.get("kind") == "venv":
        lines += [
            f"export VIRTUAL_ENV={q(prefix)}",
            f"export PATH={q(prefix + '/bin')}:\"$PATH\"",
        ]
    for k, v in sorted((e.get("env") or {}).items()):
        lines.append(f"export {check_var(k)}={q(v)}")
    lines.append(f"exec {q(e['python'])} \"$@\"")
    return "\n".join(lines) + "\n"


def write(path: Path, content: str) -> bool:
    """Write the launcher (mode 700) if its text changed; True if it did."""
    path = Path(path)
    try:
        if path.read_text() == content:
            return False
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
        os.chmod(tmp, 0o700)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return True


def remove(path: Path) -> Optional[Path]:
    try:
        Path(path).unlink()
        return Path(path)
    except OSError:
        return None
