"""Busy-indicator give-up watchdog (``_CV_SET_BUSY_JS``, visibility_plot.py).

2026-10-09: a fixed 30 s give-up ended the busy state while a long averaged
scatter was still being computed.  The watchdog now gives up only when no
request is awaiting a reply that can still arrive, as reported by the page's
CommMgr registry (``window.__cvCommMgrs``; ``inFlight()``, ``canReply()`` in
cubevisjs ``comm_mgr.ts``).

The script runs under node with a minimal DOM stand-in and a virtual clock,
so the 30 s / 5 s timers cost nothing.
"""
import json
import shutil
import subprocess

import pytest

from cubevis.toolbox.visplot.visibility_plot import _CV_SET_BUSY_JS

_NODE = shutil.which("node")

_HARNESS = r"""
let NOW = 0; const TIMERS = new Map(); let NEXT = 1;
global.setTimeout = (f, ms) => { const id = NEXT++; TIMERS.set(id, [NOW + (ms || 0), f]); return id; };
global.clearTimeout = (id) => { TIMERS.delete(id); };
function advance(ms) {
    const end = NOW + ms;
    for (;;) {
        let best = null;
        for (const [id, [t, f]] of TIMERS) if (t <= end && (best === null || t < best[1])) best = [id, t, f];
        if (best === null) break;
        TIMERS.delete(best[0]); NOW = best[1]; best[2]();
    }
    NOW = end;
}
const el = () => ({ style: {}, appendChild() {}, set innerHTML(v) {}, parentNode: null });
let OVERLAY = null;
global.document = {
    body: { style: {}, appendChild(o) { OVERLAY = o; o.parentNode = { removeChild() { OVERLAY = null; } }; } },
    head: { appendChild() {} },
    documentElement: { addEventListener() {} },
    addEventListener() {},
    getElementById(id) { return id === '__cv_busy_overlay' ? OVERLAY : null; },
    createElement: el,
};
global.window = global;
window.addEventListener = () => {};
"""

_SCENARIOS = r"""
const out = {};
function mgr(n, ok) { return { inFlight: () => n.v, canReply: () => ok.v }; }
function reset() { window.__cvBusyN = 0; OVERLAY = null; TIMERS.clear(); window.__cvBusyTimer = null; }

// 1. a long request: busy held past 30 s, cleared within 5 s of it ending
reset(); const n1 = {v: 1}, ok1 = {v: true}; window.__cvCommMgrs = [mgr(n1, ok1)];
window.__cvSetBusy(true);
advance(90000); out.long_held = window.__cvBusyN === 1 && OVERLAY !== null;
n1.v = 0; advance(5000 + 300); out.long_released = window.__cvBusyN === 0 && OVERLAY === null;

// 2. no registry (older bundle): give up at 30 s, as before
reset(); delete window.__cvCommMgrs;
window.__cvSetBusy(true);
advance(29000); out.noreg_before = window.__cvBusyN === 1;
advance(1500); out.noreg_after = window.__cvBusyN === 0 && OVERLAY === null;

// 3. nothing in flight (a reply that never cleared busy): give up at 30 s
reset(); window.__cvCommMgrs = [mgr({v: 0}, {v: true})];
window.__cvSetBusy(true);
advance(30500); out.leak_released = window.__cvBusyN === 0 && OVERLAY === null;

// 4. in flight but no reply can arrive (shut down / reconnection paused)
reset(); window.__cvCommMgrs = [mgr({v: 2}, {v: false})];
window.__cvSetBusy(true);
advance(30500); out.dead_released = window.__cvBusyN === 0 && OVERLAY === null;

// 5. a manager that throws does not hold busy
reset(); window.__cvCommMgrs = [{ inFlight() { throw new Error('x'); }, canReply: () => true }];
window.__cvSetBusy(true);
advance(30500); out.throw_released = window.__cvBusyN === 0;

console.log(JSON.stringify(out));
"""


@pytest.mark.skipif(_NODE is None, reason="node not available")
def test_busy_watchdog_holds_while_awaiting_reply():
    script = _HARNESS + _CV_SET_BUSY_JS + _SCENARIOS
    r = subprocess.run([_NODE, "-e", script], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert out == {
        "long_held": True, "long_released": True,
        "noreg_before": True, "noreg_after": True,
        "leak_released": True, "dead_released": True, "throw_released": True,
    }, out
