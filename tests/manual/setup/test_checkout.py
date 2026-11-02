"""cubevis used from a git checkout (``cubevis.setup.checkout``; notes
sections 47 and 50): the checkout found, its version from git as the build
would name it, and the release whose kernel-side code is the same.

    pytest tests/manual/setup/test_checkout.py
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from cubevis.setup import checkout


def _repo(tmp_path):
    """A small checkout with a release tag v1.0.5: a kernel side (the
    fingerprint's roots) and a module it does not reach."""
    import shutil as _sh
    if not _sh.which("git"):
        pytest.skip("needs git")
    root = tmp_path / "repo"
    files = {"pyproject.toml": '[project]\nname = "cubevis"\n',
             "cubevis/__init__.py": "", "cubevis/remote/__init__.py": "",
             "cubevis/remote/worker_main.py": "W = 1\n", "cubevis/toolbox/__init__.py": "",
             "cubevis/toolbox/visplot/__init__.py": "",
             "cubevis/toolbox/visplot/remote_registrations.py": "R = 1\n",
             "cubevis/gui.py": "G = 1\n"}
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)

    def git(*a):
        subprocess.run(["git", "-C", str(root), *a], check=True, capture_output=True,
                       env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "PATH": "/usr/bin:/bin",
                            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x", "HOME": str(tmp_path)})
    git("init", "-q")
    git("add", ".")
    git("commit", "-qm", "one")
    git("tag", "v1.0.5")
    return root, git


def test_a_checkouts_version_from_git(tmp_path):
    root, git = _repo(tmp_path)
    assert checkout.git_version(root) == "1.0.5"                         # at the tag
    (root / "cubevis" / "gui.py").write_text("G = 2\n")
    v = checkout.git_version(root)                                       # changed, not committed
    import re
    assert re.match(r"^1\.0\.5\+d\d{8}$", v), v
    git("commit", "-qam", "two")
    v = checkout.git_version(root)
    assert re.match(r"^1\.0\.6\.dev1\+g[0-9a-f]+$", v), v
    assert checkout.git_version(tmp_path) is None                        # not a git checkout


def test_the_release_with_the_same_kernel_code(tmp_path):
    """Past the tag with changes only outside the kernel side: that release
    (its code from git, fingerprinted here).  A kernel-side change: none."""
    from cubevis import _fingerprint as F
    root, git = _repo(tmp_path)
    (root / "cubevis" / "gui.py").write_text("G = 2\n")
    git("commit", "-qam", "gui only")
    assert checkout.release_with_same_code(root, F.fingerprint(root=str(root))) == "1.0.5"
    (root / "cubevis" / "remote" / "worker_main.py").write_text("W = 2\n")      # not even committed
    assert checkout.release_with_same_code(root, F.fingerprint(root=str(root))) is None
    assert checkout.release_with_same_code(tmp_path, "k2-x") is None              # no git there


def test_this_checkout_is_found():
    import cubevis
    root = checkout.checkout_root()
    assert root is not None and Path(cubevis.__file__).resolve().parent.parent == root


def test_a_directory_that_is_not_a_cubevis_checkout(tmp_path, monkeypatch):
    """checkout_root looks above the imported package for a pyproject.toml
    naming cubevis; a package without one (an installed wheel) has none."""
    import cubevis
    pkg = tmp_path / "site" / "cubevis"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    monkeypatch.setattr(cubevis, "__file__", str(pkg / "__init__.py"))
    assert checkout.checkout_root() is None
    (tmp_path / "site" / "pyproject.toml").write_text('[project]\nname = "other"\n')
    assert checkout.checkout_root() is None
    (tmp_path / "site" / "pyproject.toml").write_text('[project]\nname = "cubevis"\n')
    assert checkout.checkout_root() == (tmp_path / "site").resolve()
