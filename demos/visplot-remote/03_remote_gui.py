"""Demo 3: the visplot window with a remote kernel, and what to try in it.

    python 03_remote_gui.py --ms /path/on/kernel/host.ms --kernel python3

Prints a short guided tour, then opens the window (the call returns when
the browser tab is closed).  For the full checklist used in testing see
``tests/manual/visplot/REMOTE_TESTING.md`` (layer 3).
"""
from __future__ import annotations

import os

from _demo import args, section

TOUR = """
  1. Status line (bottom): the data set and selection.  A yellow warning
     line there means the kernel runs a different cubevis.
  2. Press Plot.  The images are made in the kernel; only images cross.
  3. Data section: type '<200m' in UV range (no Plot yet).  The Baseline
     table dims the baselines with no data -- the answer came from the
     kernel.  Now press Baseline > : it skips those.  Press Plot.
  4. Time labels (Panels section): switch to Elapsed and back.  Labels
     only, nothing is read again.
  5. A plot's toolbar, the page icon beside the gear: 'Describe this plot'.
     The page names the kernel, lists the software on both sides, and its
     call includes backend='remote' and kernel_name.  Copy it.
  6. Draw a flag box (red flag tool), then Describe pending flags.  Nothing
     is written until 'Write to MS'.
  7. Close the browser tab: the worker and kernel stop.
"""


def main():
    a = args(__doc__.splitlines()[0])
    section("The tour", "Things to try once the window opens:")
    print(TOUR)
    if a.step:
        input("[Enter to open the window] ")
    kw = dict(backend="remote", kernel_name=a.kernel)
    if a.field:
        kw["field"] = a.field
    from cubevis import visplot              # the task, as users call it
    try:
        visplot(ms=a.remote_ms, **kw)
        return
    except ModuleNotFoundError as e:         # no casatasks here: run the
        if not (e.name or "").startswith("casa"):   # plotter the way the
            raise                                   # task does
    from uuid import uuid4
    from cubevis import exe
    from cubevis.toolbox.visplot.visibility_plotter import VisibilityPlotter
    app = VisibilityPlotter(ms=a.remote_ms, headless=False,
                            enable_flagging=True, **kw)
    ctx = exe.Context(exe.Mode.SYNC)
    ui, task = app(ctx, uuid4())
    ui.show()
    ctx.execute(task, uuid4())


if __name__ == "__main__":
    main()
