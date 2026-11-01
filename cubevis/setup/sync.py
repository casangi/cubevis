"""Development installs on a node (remote session design 7c, stage 2d).

A release is installed on a node from PyPI by its version (``envmake``).  A
development install here -- a checkout used with ``pip install -e``, or a
wheel built from one -- cannot be: no package has that code, and the
kernel-side code must be identical on both sides (``cubevis._fingerprint``).
So setup copies the code:

* ``setup HOST --sync-checkout``: the checkout this cubevis is imported from
  (the ``cubevis`` package and the files its build needs) goes to the node,
  into setup's own environment ``cubevis-dev-<this computer>``, installed
  there editable (``pip install -e``).  Running it again copies the code
  again -- the environment stays, and so do its kernels; only when
  ``pyproject.toml`` changed (dependencies, say) is pip run again.  The
  version a copy without ``.git`` cannot work out is given to the build
  (``SCM_VARS``).
* A wheel (``--wheel PATH``, or the wheel this cubevis was installed from):
  copied to the node, and setup's environment is made from it like one from
  PyPI -- named for its version, never changed afterwards.

The copy is a gzip tar stream sent over setup's own ssh (the wrapper and its
configuration: the route and logins of the kernels), unpacked beside the
destination and moved into place, so the node never holds a half-copied
tree and files deleted here are gone there too.  Only ``tar``, ``/bin/sh``
and ``mv`` are needed on the node -- no rsync.
"""
from __future__ import annotations

import io
import os
import re
import shlex
import subprocess
import tarfile
from pathlib import Path
from typing import Optional

PACKAGE = "cubevis"
TOP_FILES = ("pyproject.toml", "README.rst", "LICENSE")
SRC_DIR = "cubevis-src"               # in a development environment's prefix
ANSWER = "CUBEVIS-PUSH "
PUSH_TIMEOUT = 900
# The build (pdm-pep517 here; the others for a later build backend) takes
# its version from git; a copy has no .git.
SCM_VARS = ("PDM_PEP517_SCM_VERSION", "PDM_BUILD_SCM_VERSION",
            "SETUPTOOLS_SCM_PRETEND_VERSION_FOR_CUBEVIS")
_SKIP_DIRS = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ipynb_checkpoints"}
_SKIP_SUFFIXES = (".pyc", ".pyo", "~", ".swp")


class SyncError(RuntimeError):
    """The code could not be copied; the message says why."""


def checkout_root() -> Optional[Path]:
    """The checkout this cubevis is imported from: the directory above the
    package, when it holds a ``pyproject.toml`` naming cubevis (an editable
    install, or ``PYTHONPATH`` pointing at a checkout); else None."""
    import cubevis
    root = Path(cubevis.__file__).resolve().parent.parent
    pp = root / "pyproject.toml"
    try:
        text = pp.read_text()
    except OSError:
        return None
    return root if re.search(r'(?m)^\s*name\s*=\s*"cubevis"\s*$', text) else None


def _git(root: Path, *args, binary: bool = False):
    """``git -C ROOT ARGS``'s output, None when git is missing or fails."""
    try:
        r = subprocess.run(["git", "-C", str(root), *args], capture_output=True, timeout=30,
                           stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    return r.stdout if binary else r.stdout.decode("utf-8", "replace").strip()


def git_version(root: Path) -> Optional[str]:
    """The version of a checkout from git, as the build would name it: at a
    tag ``v1.0.103`` with no changes, ``1.0.103``; N commits after it,
    ``1.0.104.devN+gHASH``; with uncommitted changes to tracked files, the
    date added (``1.0.103+d20261101``, ``1.0.104.devN+gHASH.d20261101``) --
    pdm-backend's own spelling.  None without git or a release tag."""
    d = _git(root, "describe", "--tags", "--long", "--dirty", "--match", "v[0-9]*")
    m = re.match(r"^v(\d+(?:\.\d+)*)-(\d+)-g([0-9a-f]+)(-dirty)?$", d or "")
    if not m:
        return None
    ver, n, h, dirty = m.group(1), int(m.group(2)), m.group(3), bool(m.group(4))
    if n == 0 and not dirty:
        return ver
    import datetime
    stamp = ".d" + datetime.date.today().strftime("%Y%m%d") if dirty else ""
    if n == 0:
        return f"{ver}+d{datetime.date.today().strftime('%Y%m%d')}"     # as pdm-backend
    parts = ver.split(".")
    parts[-1] = str(int(parts[-1]) + 1)
    return f"{'.'.join(parts)}.dev{n}+g{h}{stamp}"


def release_with_same_code(root: Path, here: str) -> Optional[str]:
    """The release (the checkout's nearest tag ``vX.Y.Z``) whose kernel-side
    code is the same as this checkout's, by fingerprint *here*: that
    release's ``cubevis`` from git (``git archive``), fingerprinted the same
    way -- so PyPI's package of it is code the kernels can use, though the
    checkout has moved on (changes elsewhere, or none at all).  None when it
    differs, or without git."""
    tag = _git(root, "describe", "--tags", "--abbrev=0", "--match", "v[0-9]*")
    if not tag:
        return None
    data = _git(root, "archive", "--format=tar", tag, PACKAGE, binary=True)
    if not data:
        return None
    import tempfile
    from .. import _fingerprint
    with tempfile.TemporaryDirectory(prefix="cubevis-release-") as d:
        try:
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as t:
                for m in t.getmembers():
                    if m.isfile() and m.name.endswith(".py") and ".." not in m.name.split("/"):
                        t.extract(m, d, filter="data")
            fp = _fingerprint.fingerprint(root=d)
        except (OSError, tarfile.TarError, SyntaxError, ValueError):
            return None
    return tag[1:] if fp == here else None


def installed_wheel() -> Optional[Path]:
    """The wheel this cubevis was installed from (``direct_url.json``), if
    that file is still there."""
    try:
        import json
        from importlib.metadata import distribution
        d = json.loads(distribution("cubevis").read_text("direct_url.json") or "{}")
    except Exception:
        return None
    url = d.get("url") or ""
    if url.startswith("file://") and url.endswith(".whl"):
        p = Path(url[len("file://"):])
        return p if p.is_file() else None
    return None


def wheel_version(path) -> Optional[str]:
    """The version in a wheel's file name (``cubevis-1.0.97.dev3-py3-none-any.whl``),
    None when it is not a cubevis wheel."""
    m = re.match(r"^cubevis-([^-]+)(-\d[^-]*)?-[^-]+-[^-]+-[^-]+\.whl$", Path(path).name)
    return m.group(1) if m else None


def checkout_files(root: Path) -> list:
    """``[(name in the copy, path here)]``: the package and the top-level
    files the build reads.  Byte code, caches and editor files are left out;
    everything else in the package goes, tracked by git or not (a module
    just written is code the kernels may run)."""
    out = []
    for name in TOP_FILES:
        p = root / name
        if p.is_file():
            out.append((name, p))
    pkg = root / PACKAGE
    if not (pkg / "__init__.py").is_file():
        raise SyncError(f"{pkg} is not a Python package")
    for dirpath, dirnames, filenames in os.walk(pkg):
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS and not d.startswith("."))
        for f in sorted(filenames):
            if f.endswith(_SKIP_SUFFIXES) or f.startswith((".#", "._")) or f == ".DS_Store":
                continue
            p = Path(dirpath) / f
            out.append((str(p.relative_to(root)), p))
    if not any(n == "pyproject.toml" for n, _ in out):
        raise SyncError(f"no pyproject.toml in {root}")
    return out


def tar_bytes(files) -> bytes:
    """A gzip tar of *files* (``[(name, path)]``), owned by nobody in
    particular (the node's user owns what it unpacks); no macOS metadata."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz", format=tarfile.PAX_FORMAT) as t:
        for name, path in files:
            info = t.gettarinfo(str(path), arcname=name)
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            if info.isfile():
                with open(path, "rb") as f:
                    t.addfile(info, f)
            elif info.issym():
                t.addfile(info)
    return buf.getvalue()


# One line (csh passes a quoted newline badly), paths as arguments.
_PUSH_SCRIPT = (
    'd="$1"; new="$1.new.$$"; old="$1.old.$$"; rm -rf "$new"; mkdir -p "$new" || exit 3; '
    'tar -C "$new" -xzf - || { rm -rf "$new"; exit 4; }; '
    'deps=yes; if [ -f "$d/pyproject.toml" ] && cmp -s "$d/pyproject.toml" "$new/pyproject.toml"; '
    'then deps=no; fi; '
    'if [ -e "$d" ]; then mv "$d" "$old" || exit 5; fi; mv "$new" "$d" || exit 6; rm -rf "$old"; '
    'echo "CUBEVIS-PUSH ok deps_changed=$deps"')


def push(c, alias: str, files, dest: str, timeout: float = PUSH_TIMEOUT) -> dict:
    """Copy *files* (``[(name, path)]``) to the node *alias* as the
    directory *dest*, replacing what was there.  ``{"bytes": n,
    "deps_changed": whether pyproject.toml differs from the one replaced}``;
    SyncError otherwise.  Through setup's ssh (``Ctx.ssh``'s command)."""
    from . import sshgen
    data = tar_bytes(files)
    cmd = [str(sshgen.wrapper_path()), "-F", str(sshgen.config_path()),
           "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
           "-o", "ConnectTimeout=30", alias,
           "/bin/sh", "-c", shlex.quote(_PUSH_SCRIPT), "sh", shlex.quote(dest)]
    c.ui.log(f"push: {len(files)} files, {len(data)} bytes -> {alias}:{dest}")
    try:
        r = subprocess.run(cmd, input=data, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise SyncError(f"copying took longer than {int(timeout)} s") from None
    out = r.stdout.decode("utf-8", "replace")
    err = r.stderr.decode("utf-8", "replace")
    c.last_out = out + err
    c.ui.log(f"  -> exit {r.returncode}: {(out + err).strip()[-500:]}")
    for line in out.splitlines():
        if line.startswith(ANSWER):
            return {"bytes": len(data), "deps_changed": "deps_changed=yes" in line}
    from .steps import last_line
    why = {3: f"cannot make {dest}.new there", 4: "tar there could not unpack the copy",
           5: f"cannot move the old {dest} aside", 6: f"cannot move the copy to {dest}"}
    raise SyncError(why.get(r.returncode) or last_line(out + err) or f"ssh exit {r.returncode}")


def pip_install(c, alias: str, prefix: str, args, env: dict, timeout: float = 3600) -> tuple:
    """``PREFIX/bin/python -m pip install ARGS`` on the node, in the
    foreground (a dependency change after a copy: usually quick).
    ``(ok, last line)``."""
    import posixpath
    words = ["env", "PYTHONNOUSERSITE=1", "PIP_NO_INPUT=1", "PIP_DISABLE_PIP_VERSION_CHECK=1"]
    words += [f"{k}={v}" for k, v in sorted(env.items())]
    words += [posixpath.join(prefix, "bin", "python"), "-m", "pip", "install", *args]
    r = c.ssh(alias, *[shlex.quote(w) for w in words], timeout=timeout)
    from .steps import last_line
    return r.returncode == 0, last_line(c.last_out)


def scm_env(version: str) -> dict:
    return {k: version for k in SCM_VARS}
