"""The kernel-side fingerprint (remote session design 7c): normalization,
the import closure, stability across Pythons, the comparison and its
override -- and the test that keeps the declared roots honest.

    pytest tests/manual/setup/test_fingerprint.py
    CUBEVIS_TEST_KERNEL_PYTHON=/path/to/python pytest ...   # + the import trace

``CUBEVIS_TEST_KERNEL_PYTHON``: a Python with cubevis' kernel side and the
visplot extras installed (``pip install -e ".[visplot]" ipykernel``).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import textwrap

import pytest

from cubevis import _fingerprint as F

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
KERNEL_PY = os.environ.get("CUBEVIS_TEST_KERNEL_PYTHON")


def test_normalize_ignores_what_does_not_run():
    a = '''
"""Module doc."""
import os  # a comment

def f(x):
    """Doc."""

    # another comment
    return x + 1   # trailing
'''
    b = 'import os\ndef f(x):\n  return x + 1\n'          # 2-space indent, no docs
    assert F.normalize(a) == F.normalize(b)
    assert F.normalize(b) != F.normalize(b.replace("x + 1", "x + 2"))
    assert F.normalize("s = 'doc'\n") != F.normalize("s = 'other'\n")   # an assigned string counts
    assert F.normalize("x = (1,\n     2)\n") == F.normalize("x = (1, 2)\n")


def _pkg(tmp_path, files: dict):
    for rel, text in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(text))
    return str(tmp_path)


def test_closure_follows_imports_relative_lazy_and_parents(tmp_path):
    root = _pkg(tmp_path, {
        "cubevis/__init__.py": "",
        "cubevis/remote/__init__.py": "from ._a import A\n",
        "cubevis/remote/_a.py": "A = 1\n",
        "cubevis/remote/worker.py": """
            from . import _b
            from ..utils import conv
            def later():
                import cubevis.lazy.mod
            """,
        "cubevis/remote/_b.py": "import os\n",
        "cubevis/utils/__init__.py": "",
        "cubevis/utils/conv.py": "",
        "cubevis/lazy/__init__.py": "",
        "cubevis/lazy/mod.py": "",
        "cubevis/gui/__init__.py": "",
        "cubevis/gui/panel.py": "",
    })
    got = F.closure(["cubevis.remote.worker"], root)
    assert got == ["cubevis", "cubevis.lazy", "cubevis.lazy.mod", "cubevis.remote",
                   "cubevis.remote._a", "cubevis.remote._b", "cubevis.remote.worker",
                   "cubevis.utils", "cubevis.utils.conv"]
    fp = F.fingerprint(["cubevis.remote.worker"], root)
    assert fp.startswith("k1-") and len(fp) == 23
    # A module the kernel side does not reach: no change.
    (tmp_path / "cubevis/gui/panel.py").write_text("X = 2\n")
    assert F.fingerprint(["cubevis.remote.worker"], root) == fp
    # A comment in a kernel-side module: no change; code: a change.
    (tmp_path / "cubevis/utils/conv.py").write_text("# note\n")
    assert F.fingerprint(["cubevis.remote.worker"], root) == fp
    (tmp_path / "cubevis/utils/conv.py").write_text("Y = 1\n")
    assert F.fingerprint(["cubevis.remote.worker"], root) != fp
    with pytest.raises(FileNotFoundError, match="kernel-side root"):
        F.closure(["cubevis.nope"], root)


def test_the_real_roots_exist_and_the_module_imports_nothing_else():
    mods = F.closure()
    assert set(F.ROOTS) <= set(mods) and "cubevis.remote" in mods
    r = subprocess.run([sys.executable, "-c", "import sys, cubevis._fingerprint; "
                        "print(sorted(m for m in sys.modules if m.startswith('cubevis')))"],
                       capture_output=True, text=True, env=dict(os.environ, PYTHONPATH=ROOT),
                       cwd="/tmp")
    assert r.stdout.strip() == "['cubevis', 'cubevis._fingerprint']", r.stderr


def _other_python():
    """A Python 3.12+ other than this one, to check the fingerprint does
    not depend on the interpreter."""
    me = "%d.%d" % sys.version_info[:2]
    for v in ("3.12", "3.13", "3.14"):
        if v != me and shutil.which(f"python{v}"):
            return shutil.which(f"python{v}")
    return None


@pytest.mark.skipif(_other_python() is None, reason="needs a second Python 3.12+")
def test_the_same_code_gives_the_same_fingerprint_under_another_python():
    r = subprocess.run([_other_python(), "-c", "import cubevis._fingerprint as F; print(F.fingerprint())"],
                       capture_output=True, text=True, env=dict(os.environ, PYTHONPATH=ROOT),
                       cwd="/tmp")
    assert r.stdout.strip() == F.fingerprint(), r.stderr


def test_compatible_and_the_override():
    assert F.compatible("k1-a", "k1-a") == (True, "kernel-side code identical (k1-a)")
    ok, why = F.compatible("k1-a", "k1-b", environ={})
    assert not ok and "differs (there k1-b, here k1-a)" in why
    ok, why = F.compatible("k1-a", None, environ={})
    assert not ok and "no kernel-side fingerprint" in why
    ok, why = F.compatible("k1-a", "k1-b", environ={"CUBEVIS_ALLOW_MISMATCH": "1"})
    assert ok and "used anyway" in why
    assert F.compatible("k1-a", "k1-b", allow=True, environ={})[0]
    assert not F.compatible("k1-a", "k1-b", allow=False,
                            environ={"CUBEVIS_ALLOW_MISMATCH": "1"})[0]


@pytest.mark.skipif(not KERNEL_PY, reason="set CUBEVIS_TEST_KERNEL_PYTHON (kernel side + visplot extras)")
def test_the_roots_cover_every_cubevis_module_the_kernel_side_imports():
    """Import the declared roots in a fresh interpreter, as a worker does:
    every cubevis module it imports must be in the closure.  A failure here
    means a kernel-side module the fingerprint does not see -- add its entry
    point to ROOTS (or make the import come from one)."""
    code = ("import importlib, json, sys\n"
            "for r in sys.argv[1:]: importlib.import_module(r)\n"
            "print(json.dumps(sorted(m for m in sys.modules if m.split('.')[0] == 'cubevis')))\n")
    r = subprocess.run([KERNEL_PY, "-c", code, *F.ROOTS], capture_output=True, text=True, cwd="/tmp",
                       env={k: v for k, v in os.environ.items() if k != "PYTHONPATH"})
    assert r.returncode == 0, r.stderr[-3000:]
    imported = set(json.loads(r.stdout.strip().splitlines()[-1]))
    missing = sorted(imported - set(F.closure()))
    assert not missing, f"imported on the kernel side but not fingerprinted: {missing}"


@pytest.mark.skipif(not KERNEL_PY, reason="set CUBEVIS_TEST_KERNEL_PYTHON")
def test_the_environment_check_reports_the_same_fingerprint():
    from cubevis.setup import envs
    rep = envs.check(KERNEL_PY)
    # The kernel environment runs this very checkout (editable): identical.
    assert rep["fingerprint"] == F.fingerprint(), rep.get("errors")


def test_imports_only_for_type_checkers_do_not_count(tmp_path):
    """The lazy package __init__s keep their names under `if TYPE_CHECKING:`
    for editors; those imports never run, so they are not kernel-side."""
    root = _pkg(tmp_path, {
        "cubevis/__init__.py": "",
        "cubevis/remote/__init__.py": """
            from typing import TYPE_CHECKING
            import typing
            if TYPE_CHECKING:
                from ..gui import panel
            if typing.TYPE_CHECKING:
                from ..gui import other
            else:
                from ..utils import conv
            """,
        "cubevis/remote/worker.py": "",
        "cubevis/gui/__init__.py": "",
        "cubevis/gui/panel.py": "",
        "cubevis/gui/other.py": "",
        "cubevis/utils/__init__.py": "",
        "cubevis/utils/conv.py": "",
    })
    got = F.closure(["cubevis.remote.worker"], root)
    assert "cubevis.gui.panel" not in got and "cubevis.gui.other" not in got
    assert "cubevis.utils.conv" in got                      # the else branch runs


def test_the_real_kernel_side_leaves_out_the_front_end():
    mods = set(F.closure())
    for front in ("cubevis.toolbox.visplot.visibility_plotter", "cubevis.toolbox._cube",
                  "cubevis.toolbox.visplot.flag_controls", "cubevis.toolbox._interactive_clean_ui"):
        assert front not in mods, front
