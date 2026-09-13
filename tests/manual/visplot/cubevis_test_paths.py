"""
cubevis_test_paths.py
======================
Makes `cubevis` importable for standalone visplot test scripts, without
assuming they're co-located with the package. Call
`ensure_cubevis_importable()` before importing anything from `cubevis`.

Resolution order:
1. `CUBEVIS_SRC` environment variable, if set -- point this at the
   `cubevis` package directory itself (the one containing `toolbox/`,
   `bokeh/`, `data/`, etc.); its *parent* gets added to `sys.path`.
2. A `cubevis` directory next to *this* file -- convenient for a
   co-located checkout, with no env var required.
3. Otherwise, a clear, actionable RuntimeError naming both options
   above and exactly what path was checked, rather than a bare
   `ModuleNotFoundError: No module named 'cubevis'` with no hint why.

Usage, from any test script in this directory:

    from cubevis_test_paths import ensure_cubevis_importable
    ensure_cubevis_importable()

    from cubevis.toolbox.visplot.visibility_scatter import VisibilityScatter

To point at a real checkout instead of a co-located `cubevis/` tree:

    export CUBEVIS_SRC=/path/to/checkout/cubevis
"""
from __future__ import annotations

import os
import pathlib
import sys

_ENV_VAR = "CUBEVIS_SRC"


def ensure_cubevis_importable() -> None:
    env = os.environ.get(_ENV_VAR)
    if env:
        cubevis_dir = pathlib.Path(env).expanduser().resolve()
        if not cubevis_dir.is_dir():
            raise RuntimeError(
                f"{_ENV_VAR}={env!r} does not exist or is not a directory."
            )
    else:
        candidate = pathlib.Path(__file__).resolve().parent / "cubevis"
        if not candidate.is_dir():
            raise RuntimeError(
                "Could not locate the cubevis package.\n"
                f"Set {_ENV_VAR} to point at it, e.g.:\n"
                f"    export {_ENV_VAR}=/path/to/checkout/cubevis\n"
                f"(looked for a co-located tree at {candidate} and it "
                f"does not exist)"
            )
        cubevis_dir = candidate

    parent = str(cubevis_dir.parent)
    if parent not in sys.path:
        sys.path.insert(0, parent)
