"""Copying a development install's code to a node (``cubevis.setup.sync``;
design 7c, stage 2d): what is copied from a checkout, the tar stream, and
the node side of the copy -- the shell line that unpacks beside the
destination and moves the result into place -- run here with ``sh``.  The
copy over ssh and the environments made from it are in ``test_cli.py``.

    pytest tests/manual/setup/test_sync.py
"""
from __future__ import annotations

import io
import subprocess
import tarfile
from pathlib import Path

import pytest

from cubevis.setup import sync


def _checkout(tmp_path) -> Path:
    root = tmp_path / "co"
    (root / "cubevis" / "remote" / "__pycache__").mkdir(parents=True)
    (root / "cubevis" / ".hidden").mkdir()
    (root / "pyproject.toml").write_text('[project]\nname = "cubevis"\n')
    (root / "README.rst").write_text("r")
    (root / "docs").mkdir()
    (root / "docs" / "big.ipynb").write_text("x" * 1000)
    (root / "cubevis" / "__init__.py").write_text("")
    (root / "cubevis" / "remote" / "worker_main.py").write_text("W = 1\n")
    (root / "cubevis" / "remote" / "new_module.py").write_text("N = 1\n")     # not in git yet
    (root / "cubevis" / "remote" / "__pycache__" / "w.cpython-312.pyc").write_bytes(b"x")
    (root / "cubevis" / "remote" / "w.py~").write_text("")
    (root / "cubevis" / ".hidden" / "x.py").write_text("")
    (root / "cubevis" / "data.js").write_text("js")
    return root


def test_what_a_checkout_copy_holds(tmp_path):
    names = [n for n, _ in sync.checkout_files(_checkout(tmp_path))]
    assert names == ["pyproject.toml", "README.rst", "cubevis/__init__.py", "cubevis/data.js",
                     "cubevis/remote/new_module.py", "cubevis/remote/worker_main.py"]


def test_not_a_checkout(tmp_path):
    (tmp_path / "cubevis").mkdir()
    with pytest.raises(sync.SyncError):
        sync.checkout_files(tmp_path)


def test_this_checkout_is_found():
    root = sync.checkout_root()
    import cubevis
    assert root is not None and Path(cubevis.__file__).resolve().parent.parent == root
    names = {n for n, _ in sync.checkout_files(root)}
    assert "cubevis/_fingerprint.py" in names and "pyproject.toml" in names
    assert not any("__pycache__" in n or n.startswith(("docs/", "tests/", "demos/")) for n in names)


def test_the_tar_stream(tmp_path):
    data = sync.tar_bytes(sync.checkout_files(_checkout(tmp_path)))
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as t:
        members = {m.name: m for m in t.getmembers()}
    assert members["cubevis/remote/worker_main.py"].uid == 0
    assert members["cubevis/remote/worker_main.py"].uname == ""
    assert not any(n.startswith("._") or "/._" in n for n in members)


def test_wheel_versions():
    assert sync.wheel_version("/x/cubevis-1.0.97.dev3+gabc-py3-none-any.whl") == "1.0.97.dev3+gabc"
    assert sync.wheel_version("cubevis-1.0.97-1-py3-none-any.whl") == "1.0.97"
    assert sync.wheel_version("cubevis-1.0.97.tar.gz") is None
    assert sync.wheel_version("cubevisx-1.0-py3-none-any.whl") is None


def _push_here(files, dest: Path) -> str:
    """The node side of ``sync.push``, run here: the same line, the same
    stream on stdin."""
    r = subprocess.run(["/bin/sh", "-c", sync._PUSH_SCRIPT, "sh", str(dest)],
                       input=sync.tar_bytes(files), capture_output=True)
    assert r.returncode == 0, r.stderr
    return r.stdout.decode()


def test_the_copy_replaces_the_destination_whole(tmp_path):
    root = _checkout(tmp_path)
    dest = tmp_path / "env" / "cubevis-src"
    out = _push_here(sync.checkout_files(root), dest)
    assert "CUBEVIS-PUSH ok deps_changed=yes" in out                 # nothing there before
    assert (dest / "cubevis" / "remote" / "new_module.py").read_text() == "N = 1\n"
    # A module deleted here is gone there too; an unchanged pyproject.toml
    # needs no pip.
    (root / "cubevis" / "remote" / "new_module.py").unlink()
    (root / "cubevis" / "remote" / "worker_main.py").write_text("W = 2\n")
    out = _push_here(sync.checkout_files(root), dest)
    assert "deps_changed=no" in out
    assert not (dest / "cubevis" / "remote" / "new_module.py").exists()
    assert (dest / "cubevis" / "remote" / "worker_main.py").read_text() == "W = 2\n"
    assert sorted(p.name for p in dest.parent.iterdir()) == ["cubevis-src"]     # nothing left beside
    (root / "pyproject.toml").write_text('[project]\nname = "cubevis"\ndependencies = ["x"]\n')
    assert "deps_changed=yes" in _push_here(sync.checkout_files(root), dest)


def test_a_broken_stream_leaves_the_destination_alone(tmp_path):
    root = _checkout(tmp_path)
    dest = tmp_path / "env" / "cubevis-src"
    _push_here(sync.checkout_files(root), dest)
    r = subprocess.run(["/bin/sh", "-c", sync._PUSH_SCRIPT, "sh", str(dest)],
                       input=b"not a tar stream", capture_output=True)
    assert r.returncode == 4
    assert (dest / "cubevis" / "remote" / "worker_main.py").exists()
    assert sorted(p.name for p in dest.parent.iterdir()) == ["cubevis-src"]


def test_pip_again_after_a_dependency_change_runs_in_the_environment():
    """``sync.pip_install``: the environment's own python, pip without
    prompts or user site, the version for a build without .git -- one
    command line, each word quoted for the node's shell (csh too)."""
    import shlex

    class C:
        last_out = ""

        def ssh(self, alias, *words, timeout=60, **kw):
            self.got = (alias, words)

            class R:
                returncode = 0
            self.last_out = "Successfully installed cubevis-1.0.97.dev3"
            return R()
    c = C()
    ok, why = sync.pip_install(c, "cv-n1", "/lustre/envs/cubevis-dev-gui1",
                               ["-e", "/lustre/envs/cubevis-dev-gui1/cubevis-src[visplot]"],
                               sync.scm_env("1.0.97.dev3"))
    assert ok and "Successfully" in why
    alias, words = c.got
    argv = shlex.split(" ".join(words))
    assert alias == "cv-n1" and argv[0] == "env"
    assert "PYTHONNOUSERSITE=1" in argv and "PDM_PEP517_SCM_VERSION=1.0.97.dev3" in argv
    i = argv.index("/lustre/envs/cubevis-dev-gui1/bin/python")
    assert argv[i:] == ["/lustre/envs/cubevis-dev-gui1/bin/python", "-m", "pip", "install", "-e",
                        "/lustre/envs/cubevis-dev-gui1/cubevis-src[visplot]"]
    assert words[-1] == "'/lustre/envs/cubevis-dev-gui1/cubevis-src[visplot]'"   # [ ] quoted
