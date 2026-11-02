"""The launcher: a small script on the node that a kernel's ssh runs
instead of the environment's python -- one per kernel (design 7c, 4a).

sshpyk starts the remote kernel as ``$FP_PY -c '...'``, with ``FP_PY`` the
configured ``remote_python``, after checking only that it is an executable
file; it passes no working directory and no environment.  So setup writes,
on the node, ``<config>/launch/<gui>/<kernel>.sh`` (``<config>`` the node's
``~/.config/cubevis``, ``<gui>`` the id of the computer the plots are on,
so that two such computers with their own state never overwrite each
other's), and the kernel's record gives its path as ``remote_python``.
Before Python starts -- which matters: ``OMP_NUM_THREADS``,
``HDF5_USE_FILE_LOCKING`` and the like are read when the libraries load --
it

0. stops with a message when the environment's python is gone (removed by
   hand, or by setup from another computer): ``ENV_GONE`` -- setup offers
   to give the kernel another environment;
1. changes to the kernel's working directory (default: the home
   directory), so that relative paths (``ms="sis14.ms"``) are found there;
   a directory that is gone stops the kernel with a message, rather than
   starting it somewhere else;
2. activates the environment as conda or venv would: ``PATH``,
   ``CONDA_PREFIX`` / ``VIRTUAL_ENV``, and, for conda, the environment's
   ``etc/conda/activate.d/*.sh`` scripts (their output goes to stderr:
   sshpyk reads the launcher's stdout);
3. with the kernel's ``pythonhome`` setting, sets ``PYTHONHOME`` to the
   environment's base prefix (``home_of``): Python then skips the search
   for its own installation, which on an automounted filesystem (autofs:
   ``/home/lib/python312.zip`` looked up) can take most of a minute
   (zuul06, sshpyk PR 13).  For setup's kernels the launcher's Python and
   the kernel's are the same environment's, so one value serves both
   (sshpyk's ``remote_pythonhome`` / ``remote_kernel_pythonhome`` are for
   when they differ) and it works with any sshpyk.  Like sshpyk's kernel
   setting, it stays in the kernel's environment: processes the kernel
   starts inherit it.  Setup turns it on by itself when a kernel's start
   is slow and it helps (``hostsetup.write_and_test``, notes section 53);
   ``--pythonhome`` / ``--no-pythonhome`` decide instead;
4. sets the kernel's variables (after activation, so they win -- a
   ``PYTHONHOME`` given there too);
5. ``exec``s the python with the arguments it was given.

Only what the kernel's record holds goes in; nothing else comes from the
computer the plots are on (developers pass that explicitly, stage 3).
bash, not sh: conda's activation scripts are written for bash and zsh.

``install_script`` is the small Python program, run by the node's
``python3`` over ssh, that writes it there (atomically, mode 700, only
when its text changed); ``test_code`` is what setup runs through it to see
that the kernel can start: the directory, the activation, the imports, and
the kernel-side fingerprint.
"""
from __future__ import annotations

import os
import re
import shlex
import tempfile
from pathlib import Path
from typing import Optional

# In the launcher's message when the environment's python is gone.
ENV_GONE = "is gone from this node"

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


def home_of(e: dict) -> Optional[str]:
    """The ``PYTHONHOME`` for environment record *e*: its interpreter's
    ``sys.base_prefix`` as its check reported it (a venv's is the Python it
    was made from); for a conda environment checked before that was
    recorded, its prefix.  None when unknown."""
    if e.get("base_prefix"):
        return e["base_prefix"]
    if e.get("kind") == "conda" and e.get("prefix"):
        return e["prefix"]
    return None


def relpath(gui: str, kernel: str) -> str:
    """Where a kernel's launcher goes, relative to the node's configuration
    directory (``~/.config/cubevis``)."""
    return f"launch/{gui}/{kernel}.sh"


def text(kernel: str, k: dict, e: dict, home: str, gui: str, host: str = "HOST") -> str:
    """The launcher of *kernel* (its record *k*: ``workdir``, ``env``) in
    environment *e* (``python``, ``prefix``, ``kind``), on a node whose home
    is *home*, written by the computer *gui*."""
    q = shlex.quote
    prefix = e.get("prefix") or os.path.dirname(os.path.dirname(e["python"]))
    workdir = k.get("workdir") or home
    lines = [
        "#!/bin/bash",
        f"# Kernel {kernel}: written by python -m cubevis.setup on {gui}, again at each",
        f"# run (change it there: python -m cubevis.setup {host} --kernel {kernel}).",
        "# A kernel's ssh runs this instead of python: working directory, activation,",
        "# environment variables, then the environment's python with the arguments given.",
        f"[ -x {q(e['python'])} ] || {{ echo {q(f'cubevis: the environment of kernel {kernel}, {prefix}, {ENV_GONE} (removed?): python -m cubevis.setup {host} gives the kernel another')} >&2; exit 1; }}",
        f"cd {q(workdir)} || {{ echo {q(f'cubevis: cannot change to {workdir}, the working directory of kernel {kernel} (python -m cubevis.setup {host} --kernel {kernel} changes it)')} >&2; exit 1; }}",
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
    if k.get("pythonhome") and home_of(e):
        lines.append(f"export PYTHONHOME={q(home_of(e))}   # python -m cubevis.setup {host} "
                     "--no-pythonhome removes it")
    for name, v in sorted((k.get("env") or {}).items()):
        lines.append(f"export {check_var(name)}={q(v)}")
    lines.append(f"exec {q(e['python'])} \"$@\"")
    return "\n".join(lines) + "\n"


# Run by the node's python3 (3.6 or later) with the launcher's path and text
# filled in; prints CUBEVIS-LAUNCHER written|same|error <path or reason>.
_INSTALL = r'''
import os, sys, tempfile
path, text = %(path)r, %(text)r
try:
    try:
        with open(path) as f:
            same = f.read() == text
    except (OSError, ValueError):
        same = False
    if same:
        print("CUBEVIS-LAUNCHER same " + path)
    else:
        d = os.path.dirname(path)
        os.makedirs(d, mode=0o700, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix="." + os.path.basename(path) + ".", dir=d)
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.chmod(tmp, 0o700)
        os.replace(tmp, path)
        print("CUBEVIS-LAUNCHER written " + path)
except Exception as exc:
    print("CUBEVIS-LAUNCHER error %%s" %% exc)
'''

# Through the launcher: the kernel's directory, its imports, its fingerprint.
# One line without quotes, '$' or '!', so that any login shell (csh too)
# passes it through single quotes unchanged.
# Run through a launcher once (``remote.test_launcher``): the program comes
# on stdin (``test_program``), so the command line stays short and has no
# newline for a csh to mangle.
TEST_CODE = "import sys;exec(sys.stdin.read())"


def test_program() -> str:
    """What ``TEST_CODE`` runs: the kernel side imported as a kernel would,
    then the fingerprint of the cubevis it found -- with this computer's
    fingerprint code (``cubevis._fingerprint.source``), as ``envcheck`` does."""
    from .. import _fingerprint
    return ("import os, ipykernel, cubevis, cubevis.remote.worker_main\n"
            f"FINGERPRINT_SOURCE = {_fingerprint.source()!r}\n"
            "_ns = {'__name__': 'cubevis_setup_fingerprint'}\n"
            "exec(compile(FINGERPRINT_SOURCE, 'cubevis/_fingerprint.py (sent by setup)', 'exec'), _ns)\n"
            "_root = os.path.dirname(os.path.dirname(os.path.abspath(cubevis.__file__)))\n"
            "print(chr(10) + 'CUBEVIS-LAUNCH-OK', _ns['fingerprint'](root=_root), os.getcwd())\n")


def install_script(path: str, content: str) -> str:
    """The Python program that writes *content* to *path* on the node."""
    return _INSTALL % {"path": path, "text": content}


def parse_install(out: str) -> tuple:
    """``(status, detail)`` from the install program's output: status
    ``written``, ``same`` or ``error`` (None: it said nothing)."""
    for line in (out or "").splitlines():
        if line.startswith("CUBEVIS-LAUNCHER "):
            parts = line.split(" ", 2)
            return parts[1], (parts[2] if len(parts) > 2 else "")
    return None, ""


def parse_test(out: str) -> Optional[tuple]:
    """``(fingerprint, cwd)`` from a run of ``TEST_CODE``, or None."""
    for line in (out or "").splitlines():
        if line.startswith("CUBEVIS-LAUNCH-OK "):
            parts = line.split(" ", 2)
            if len(parts) == 3:
                return parts[1], parts[2]
    return None


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
