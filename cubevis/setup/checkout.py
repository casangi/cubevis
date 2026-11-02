"""cubevis used from a git checkout (remote session design 7c; notes sections
47 and 50).

A checkout imported without being installed -- ``PYTHONPATH``, or a symbolic
link to the package beside a script -- has no installed version, and its
code is not a release's.  But its kernel-side code may well be a release's:
when it is, that release from PyPI serves its kernels.  This module finds the
checkout (``checkout_root``), names its version as the build would
(``git_version``), and compares its kernel-side code with that of its nearest
release tag (``release_with_same_code``: the tag's code from ``git archive``,
fingerprinted here).  The releases' own published fingerprints are in
``releases``.

Copying a checkout or a wheel to a node (``--sync-checkout``, ``--wheel``,
2026-10-31) was removed on 2026-11-02 (notes section 50): a developer who
needs other code there installs it in an environment there, and setup finds
that environment by its fingerprint.
"""
from __future__ import annotations

import io
import re
import subprocess
import tarfile
from pathlib import Path
from typing import Optional

PACKAGE = "cubevis"


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
