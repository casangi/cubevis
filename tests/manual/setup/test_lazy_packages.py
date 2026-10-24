"""The lazy package ``__init__``s (PEP 562) of ``cubevis.toolbox`` and
``cubevis.toolbox.visplot``: every public name still imports, as the same
object as from its module, and importing the package alone stays light
(remote session design 7c: the kernel side should not load the plotting
front end, which also kept the kernel-side fingerprint needlessly wide).

    CUBEVIS_TEST_KERNEL_PYTHON=/path/to/python pytest tests/manual/setup/test_lazy_packages.py

Needs a Python with cubevis' dependencies and the visplot extras (the
names import bokeh, datashader, ...); without it, only the source checks run.
"""
from __future__ import annotations

import ast
import json
import os
import subprocess

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
KERNEL_PY = os.environ.get("CUBEVIS_TEST_KERNEL_PYTHON")
needs_py = pytest.mark.skipif(not KERNEL_PY, reason="set CUBEVIS_TEST_KERNEL_PYTHON")
PACKAGES = ("cubevis.toolbox", "cubevis.toolbox.visplot")


def _lazy_table(package: str) -> dict:
    """``_LAZY`` read from the package's source (no import)."""
    path = os.path.join(ROOT, *package.split("."), "__init__.py")
    tree = ast.parse(open(path).read())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "_LAZY" for t in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError(f"{package} has no _LAZY table")


@pytest.mark.parametrize("package", PACKAGES)
def test_the_package_imports_nothing_eagerly(package):
    """Outside ``if TYPE_CHECKING:``, the ``__init__`` imports no submodule of its own."""
    path = os.path.join(ROOT, *package.split("."), "__init__.py")
    tree = ast.parse(open(path).read())
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.level:
            raise AssertionError(f"{package} imports .{node.module} eagerly (line {node.lineno})")
    names = _lazy_table(package)
    assert names and all(v.startswith(".") for v in names.values())


def _run(code: str) -> dict:
    r = subprocess.run([KERNEL_PY, "-c", code], capture_output=True, text=True, cwd="/tmp",
                       env={k: v for k, v in os.environ.items() if k != "PYTHONPATH"})
    assert r.returncode == 0, r.stderr[-3000:]
    return json.loads(r.stdout.strip().splitlines()[-1])


@needs_py
@pytest.mark.parametrize("package", PACKAGES)
def test_every_public_name_imports_as_before(package):
    names = _lazy_table(package)
    code = f"""
import importlib, json, sys
pkg = importlib.import_module({package!r})
light = sorted(m for m in sys.modules if m.startswith({package!r} + '.'))
out = {{"light": light, "same": {{}}, "dir": sorted(dir(pkg)), "all": getattr(pkg, '__all__', None)}}
for name, mod in {names!r}.items():
    obj = getattr(pkg, name)
    out["same"][name] = obj is getattr(importlib.import_module(mod, {package!r}), name)
ns = {{}}
exec("from {package} import *", ns)
out["star"] = sorted(k for k in ns if not k.startswith("__"))
try:
    getattr(pkg, "NoSuchName")
    out["missing"] = "no error"
except AttributeError as e:
    out["missing"] = str(e)
print(json.dumps(out))
"""
    got = _run(code)
    assert got["light"] == [], f"importing {package} imported {got['light']}"
    assert all(got["same"].values()), got["same"]
    assert set(names) <= set(got["dir"])
    if got["all"] is not None:
        assert set(got["star"]) == set(got["all"]) == set(names)
    assert "has no attribute 'NoSuchName'" in got["missing"]


@needs_py
def test_the_kernel_side_no_longer_loads_the_front_end():
    got = _run("import json, sys\n"
               "import cubevis.remote.worker_main, cubevis.toolbox.visplot.remote_registrations\n"
               "print(json.dumps(sorted(m for m in sys.modules if m.startswith('cubevis'))))")
    for heavy in ("cubevis.toolbox.visplot.visibility_plotter", "cubevis.toolbox._cube",
                  "cubevis.toolbox._interactive_clean_ui", "cubevis.toolbox.visplot.flag_controls"):
        assert heavy not in got, heavy
