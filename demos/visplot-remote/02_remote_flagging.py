"""Demo 2: flagging through a remote kernel, headless.

    python 02_remote_flagging.py --ms sis14_twhya_calibrated_flagged.ms \\
        --field 3c279 --step

What it shows: a flag box is resolved in the worker (only the delta and
counts come back); a UV range makes a box a set of samples rather than a
region; undo; the pending flags exported as JSON.  Nothing is written to
the MS -- writing is a deliberate step in the GUI (Write to MS), with a
backup.
"""
from __future__ import annotations

import asyncio
import os
import tempfile
import warnings

from _demo import args, note, section, timed

warnings.filterwarnings("ignore")


def box(obj, fy=(0.0, 1.0)):
    x0, x1 = obj._x_range
    y0, y1 = obj._y_range
    return dict(x0=x0, x1=x1, y0=y0 + (y1 - y0) * fy[0], y1=y0 + (y1 - y0) * fy[1])


def main():
    a = args(__doc__.splitlines()[0])
    from cubevis.toolbox.visplot import VisibilityPlotter

    section("1. A remote plotter", """
        Built headless with backend="remote".  Its flag list (the pending
        operations, undo / redo) lives here, in the plotter; evaluating a
        box against the data happens in the worker.""")
    kw = dict(headless=True, layout="side", raster_y="BASELINE", raster_x="TIME",
              backend="remote", kernel_name=a.kernel)
    if a.field:
        kw["field"] = a.field
    with timed("open"):
        vp = VisibilityPlotter(ms=a.remote_ms, **kw)
    raster = vp._slots[0].raster

    section("2. A box over the lower half of the baselines", """
        The same call the browser makes when a box is drawn with the flag
        tool.  The worker resolves the box against the data and returns a
        compact description (a coordinate region where possible) and the
        number of samples it changes.""")
    with timed("flag box"):
        r = asyncio.run(vp._handle_box_select(dict(flag=True, **box(raster, (0.0, 0.5))),
                                              "raster", raster))
    note(r.get("notify_text") or r.get("status_text") or str(r))
    note(f"pending operations: {len(vp.flag_db)}")

    section("3. The same box with a UV range", """
        A UV range selects rows by their projected length, which is not a
        coordinate box.  So the worker stores the box as the exact samples
        inside the range, and says so.""")
    # A plotter whose selection has the UV range (as after typing <150m
    # in the UV range box and pressing Plot).
    with timed("open with uvrange='<150m'"):
        vp2 = VisibilityPlotter(ms=a.remote_ms, uvrange="<150m", **kw)
    r2 = vp2._slots[0].raster
    with timed("flag box under UV < 150 m"):
        r = asyncio.run(vp2._handle_box_select(dict(flag=True, **box(r2, (0.0, 0.5))),
                                               "raster", r2))
    note(r.get("notify_text") or r.get("status_text") or str(r))
    vp2.close()

    section("4. The record of what is pending, then undo", """
        'Describe pending flags' in the GUI shows the report built here;
        the JSON export is the record to keep or apply elsewhere.  Undo
        removes the last operation (Redo puts it back).""")
    out = tempfile.mkdtemp(prefix="visplot_remote_flag_demo_")
    path = vp.export_flags(os.path.join(out, "pending.jsonl"))
    note(f"exported: {path}")
    report = os.path.join(out, "pending_report.html")
    with open(report, "w") as f:
        f.write(vp.flags.report_html())
    note(f"report: {report}")
    asyncio.run(vp.flags.handle_action({"action": "undo"}))
    note(f"after undo: {len(vp.flag_db)} pending operation(s)")
    asyncio.run(vp.flags.handle_action({"action": "redo"}))
    note(f"after redo: {len(vp.flag_db)} pending operation(s)")

    section("5. Close", "Nothing was written to the MS.")
    vp.close()


if __name__ == "__main__":
    main()
