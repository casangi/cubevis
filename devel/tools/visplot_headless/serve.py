"""Run visplot as the casatask does -- browser GUI plus a live websocket
kernel -- but record the page URL instead of opening a browser.

    python serve.py ms=<path> [VisibilityPlotter key=value ...]

Values "True"/"False" become booleans; everything else is passed as a
string, as the task's string arguments are.  `live.sh` runs this with
BROWSER pointing at `record_url.sh`, which writes the URL to url.txt.
"""
import os, sys, warnings
warnings.filterwarnings("ignore")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.environ.get("CUBEVIS_ROOT", os.path.abspath(os.path.join(HERE, "..", "..", ".."))))
from uuid import uuid4
from cubevis import exe
from cubevis.toolbox.visplot.visibility_plotter import VisibilityPlotter

kw = {}
for a in sys.argv[1:]:
    k, v = a.split("=", 1)
    if v in ("True", "False"):
        v = v == "True"
    kw[k] = v
# SLOW_PLOT=<seconds>: delay every Plot reply, to check behaviour (e.g. the
# busy indicator) when a plot takes longer than the browser's own timers.
_slow = float(os.environ.get("SLOW_PLOT", "0") or 0)
if _slow > 0:
    import asyncio
    _orig_plot = VisibilityPlotter._handle_plot
    async def _slow_plot(self, msg, context=None):
        await asyncio.sleep(_slow)
        return await _orig_plot(self, msg, context=context)
    VisibilityPlotter._handle_plot = _slow_plot
app = VisibilityPlotter(headless=False, enable_flagging=True, **kw)
ctx = exe.Context(exe.Mode.SYNC)
ui, task = app(ctx, uuid4())
ui.show()
print("SERVING", flush=True)
ctx.execute(task, uuid4())
