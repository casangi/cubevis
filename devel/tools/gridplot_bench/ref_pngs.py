"""Reference PNGs: every visplot preset, light and dark, on two generated
measurement sets, written with ``plotfile=`` and hashed.  A refactor that
should not change what visplot draws must leave the hashes as they were.

    python devel/tools/gridplot_bench/ref_pngs.py OUTDIR > before.txt
    ... change the code ...
    python devel/tools/gridplot_bench/ref_pngs.py OUTDIR > after.txt
    diff before.txt after.txt

The two sets are made in OUTDIR when missing: ``ci.ms``, the quick tests'
tiny MS (``tests/ci/make_tiny_ms.py``: 5 antennas, 2 SPW of 8 channels),
and ``g0.ms``, G0's VLBA-sized one (``bench_g0.py --make``: 10 antennas,
64 channels, 600 times).  Each preset runs in a process of its own, as a
user's call would.  10 presets x 2 sets x 2 themes = 40 files when this
was written (2026-11-13); the hashes are only comparable on one machine
and one set of library versions.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))


def one(out: str, ms: str, preset: str) -> None:
    """Child: one preset on one MS, both themes; prints name and hash."""
    import logging
    import warnings
    warnings.filterwarnings("ignore")
    logging.disable(logging.WARNING)
    from cubevis.toolbox.visplot.visibility_plotter import VisibilityPlotter
    for theme in ("light", "dark"):
        name = f"{os.path.basename(ms)}_{preset}_{theme}.png"
        path = os.path.join(out, name)
        try:
            p = VisibilityPlotter(ms=ms, headless=True, preset=preset, theme=theme)
            p(plotfile=path)
            p.close()
        except Exception as exc:                            # noqa: BLE001 -- reported
            print(name, "ERROR", type(exc).__name__, str(exc)[:80], flush=True)
            continue
        with open(path, "rb") as f:
            print(name, hashlib.sha256(f.read()).hexdigest()[:16], flush=True)


def make_sets(out: str) -> list:
    sys.path.insert(0, os.path.join(ROOT, "tests", "ci"))
    sys.path.insert(0, HERE)
    sets = []
    ci = os.path.join(out, "ci.ms")
    if not os.path.isdir(ci):
        import make_tiny_ms
        make_tiny_ms.make(ci)
    sets.append(ci)
    g0 = os.path.join(out, "g0.ms")
    if not os.path.isdir(g0):
        import contextlib
        import bench_g0
        with contextlib.redirect_stdout(sys.stderr):        # stdout is the hashes
            bench_g0.make_ms(g0, nant=10, nchan=64, ntimes=600, nspw=1)
    sets.append(g0)
    return sets


def main() -> None:
    if len(sys.argv) == 5 and sys.argv[1] == "--one":
        one(*sys.argv[2:5])
        return
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    out = os.path.abspath(sys.argv[1])
    os.makedirs(out, exist_ok=True)
    sets = make_sets(out)
    from cubevis.toolbox.visplot.visibility_plotter import _PRESETS
    env = dict(os.environ, PYTHONPATH=ROOT + os.pathsep + os.environ.get("PYTHONPATH", ""))
    for ms in sets:
        for preset in _PRESETS:
            subprocess.run([sys.executable, __file__, "--one", out, ms, preset],
                           env=env, stderr=subprocess.DEVNULL, check=False)


if __name__ == "__main__":
    main()
