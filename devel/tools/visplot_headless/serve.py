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
app = VisibilityPlotter(headless=False, enable_flagging=True, **kw)
ctx = exe.Context(exe.Mode.SYNC)
ui, task = app(ctx, uuid4())
ui.show()
print("SERVING", flush=True)
ctx.execute(task, uuid4())
