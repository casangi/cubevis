"""Demo 1: the same plot made locally and through a remote kernel.

    python 01_local_and_remote.py --ms sis14_twhya_calibrated_flagged.ms \\
        --field 3c279 --step

    # data and kernel on another machine:
    python 01_local_and_remote.py --ms local/copy.ms \\
        --remote-ms /path/on/kernel/host.ms --kernel cvpost106_python312

What it shows, in order: where each half of visplot runs; the version
check between the two sides; that a selection (scan, time, UV range,
autocorrelations) is applied in the kernel; that the images are the
same; the plot summary of a remote plotter; and a PNG from each side.
Read alongside ``tests/manual/visplot/REMOTE_TESTING.md``.
"""
from __future__ import annotations

import os
import tempfile
import warnings

import numpy as np

from _demo import args, note, section, timed

warnings.filterwarnings("ignore")


def main():
    a = args(__doc__.splitlines()[0])
    from cubevis.toolbox.visplot import VisibilityPlotter
    from cubevis.toolbox.visplot.axes import Axis

    section("1. Two halves", """
        visplot has two halves.  The plotter -- panels, controls, the
        browser connection, the flag list -- always runs where you start
        it.  The data side -- opening the MS, selecting, averaging,
        aggregating to images, evaluating flag boxes -- runs either in the
        same process (local) or in a worker started by a Jupyter kernel
        (backend="remote", kernel_name=...).  Only small results cross:
        images, sample counts, flag deltas.  Here both are built headless
        (no browser) on the same data so they can be compared.""")
    kw = dict(headless=True, layout="side")
    if a.field:
        kw["field"] = a.field
    with timed("local plotter (open the MS, first render)"):
        lp = VisibilityPlotter(ms=a.ms, **kw)
    with timed(f"remote plotter (start kernel {a.kernel!r}, worker, open, first render)"):
        rp = VisibilityPlotter(ms=a.remote_ms, backend="remote",
                               kernel_name=a.kernel, **kw)
    note(f"local reader:  {type(lp._reader).__name__}")
    note(f"remote reader: {type(rp._reader).__name__} -- every data call "
         "goes to the kernel")

    section("2. Version check", """
        The kernel may run a different cubevis from the plotter (another
        machine, another environment).  When it does, results can differ in
        confusing ways, so visplot compares the two when it connects and
        says so in the log, the status line and the plot summary.""")
    kv = dict(rp._kernel_versions or [])
    note(f"kernel cubevis: {kv.get('cubevis')}")
    note(f"kernel python:  {kv.get('python')}")
    note("version warning: " + (rp._version_warning or "none -- the two match"))

    section("3. A selection is applied in the kernel", """
        Scan, Time range, UV range and the autocorrelation switch become
        fields of the selection sent with every query.  The worker applies
        them where the data are: here a UV range, which removes rows
        (baseline x time) whose projected length is outside it.  The local
        and remote images must be identical.""")
    import dataclasses as dc
    sel = dc.replace(rp._selection, uv_range=[[None, 200.0, "m"]])
    pol = rp._slots[0].raster._polarization
    imgs = []
    for vp, who in ((lp, "local"), (rp, "remote")):
        with timed(f"{who}: Baseline x Time amplitude, UV < 200 m"):
            g = np.asarray(vp._reader.query_raster(
                Axis.BASELINE, Axis.TIME, Axis.AMPLITUDE, sel, polarization=pol)[0], float)
        imgs.append(g)
        note(f"{who}: {int(np.isfinite(g).sum())} cells with data")
    same = imgs[0].shape == imgs[1].shape and np.allclose(
        imgs[0], imgs[1], equal_nan=True)
    note("identical: " + ("yes" if same else "NO -- report this"))

    section("4. Stepping's 'rows with data'", """
        Baseline and Antenna Prev / Next skip rows with no data.  When the
        Data section changes before Plot is pressed, the browser asks the
        plotter which rows have data now; with a remote kernel the answer
        comes from the worker.""")
    import asyncio
    for msg in ({}, {"uvrange": "<100m"}):
        r = asyncio.run(rp._handle_rows(dict(msg)))
        note(f"{msg or 'current selection'}: "
             f"{sum(r.get('bl_has_data') or [])} baselines with data")

    section("5. The plot summary of a remote plotter", """
        'Describe this plot' (the page icon on each plot's toolbar) is
        built in the plotter's process.  For a remote plotter it names the
        kernel, lists the software on both sides, and its reproducing call
        includes backend='remote' and kernel_name.""")
    print(rp.reproducing_call())
    out = tempfile.mkdtemp(prefix="visplot_remote_demo_")
    page = os.path.join(out, "summary_panel_A.html")
    with open(page, "w") as f:
        f.write(rp.describe("A", html=True))
    note(f"summary page written to {page} (open it in a browser)")

    section("6. A PNG from each side", """
        Export works the same way on both: the images are aggregated where
        the data are, the PNG is composed where the plotter runs.""")
    for vp, who in ((lp, "local"), (rp, "remote")):
        with timed(f"{who} PNG"):
            p = vp(plotfile=os.path.join(out, f"{who}.png"), theme="light")
        note(p)

    section("7. Close", """
        Closing the remote plotter stops its worker; the kernel is shut
        down with it.""")
    lp.close()
    with timed("remote close"):
        rp.close()
    note(f"outputs in {out}")


if __name__ == "__main__":
    main()
