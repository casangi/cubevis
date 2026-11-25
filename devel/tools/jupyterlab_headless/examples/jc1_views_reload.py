# Transport review JC-1 (2026-11-24): a second view of the output, a short
# loss of the kernel connection, a page reload, then a re-run.
run_cell(0, timeout=120000)
time.sleep(6)
SETUP = r'''
import asyncio
from cubevis.bokeh.transport import Comm
mgr = vp._app_context.comm_mgr
cid = sorted(mgr._comms)[0]
probe = Comm(comm_id=cid, comm_mgr=mgr)
replies = {}
def ask(tag, delay=0.0, m=None, p=None):
    m = m or mgr; p = p or probe
    def go():
        asyncio.ensure_future(p.send("__whoami__", {"tag": tag}, callback=lambda r, t=tag: replies.__setitem__(t, r)))
    asyncio.get_event_loop().call_later(delay, go)
def diag(label, m=None):
    m = m or mgr
    t = m._transport
    print("DIAG", label, "| comms:", len(getattr(t, "_comm_objs", [])), "| connected:", t.is_connected(),
          "| pending:", len(m._pending), "| queued:", sum(len(v) for v in m._send_queue.values()),
          "| replies:", replies)
'''
run_cell(add_cell(SETUP))
# Each page-side CommMgr answers __whoami__ with its index.
LABEL = """() => { (window.__cvCommMgrs || []).forEach((m, i) => {
    for (const c of m.comms.values()) c.register('__whoami__', () => ({view: i})) }) }"""
pg.evaluate(LABEL)
print("STEP", run_cell(add_cell('ask("one view"); import time; time.sleep(0)'), timeout=30000))
time.sleep(2)
print("STEP", run_cell(add_cell('diag("one view")')).strip())

# --- second view
pg.evaluate("""() => { const nb = window.jupyterapp.shell.currentWidget.content; nb.activeCellIndex = 0;
               return window.jupyterapp.commands.execute('notebook:create-output-view') }""")
time.sleep(10)
pg.evaluate(LABEL)
print("STEP CommMgrs on the page:", pg.evaluate("() => (window.__cvCommMgrs||[]).map(m => m.state)"))
print("STEP notices (want 0):", pg.evaluate("() => [...document.querySelectorAll('[id^=cv-superseded]')].length"))
run_cell(add_cell('ask("two views")'))
time.sleep(2)
print("STEP", run_cell(add_cell('diag("two views")')).strip())
r = pg.evaluate("""() => new Promise(resolve => {
    const out = {};
    (window.__cvCommMgrs || []).forEach((m, i) => {
        const comm = [...m.comms.values()][0];
        comm.send('__probe__', {view: i}, r => { out['view' + i] = r && (r.error ? 'answered' : r) });
    });
    setTimeout(() => resolve(out), 4000);
})""")
print("STEP requests from each page CommMgr:", r)
pg.screenshot(path=out + "_views.png")

# --- short offline (regression)
run_cell(add_cell('ask("offline-8s", delay=2.0)'))
ctx.set_offline(True); time.sleep(8); ctx.set_offline(False)
wait_idle(90000); time.sleep(5)
print("STEP", run_cell(add_cell('diag("after 8 s offline")'), timeout=90000).strip())

# --- reload: the output does not come back; the old session must hold, not lose
pg.evaluate("() => window.jupyterapp.commands.execute('docmanager:save')")
time.sleep(0.5)
reload()
time.sleep(12)
print("STEP", run_cell(add_cell('ask("after reload"); diag("after reload, asked")')).strip())
time.sleep(3)
print("STEP", run_cell(add_cell('diag("after reload, 3 s later")')).strip())
# Re-run the visplot cell: a new session works.
run_cell(0, timeout=120000)
time.sleep(6)
pg.evaluate(LABEL)
print("STEP", run_cell(add_cell('mgr2 = vp._app_context.comm_mgr; probe2 = Comm(comm_id=sorted(mgr2._comms)[0], comm_mgr=mgr2); ask("new session", m=mgr2, p=probe2)')))
time.sleep(3)
print("STEP", run_cell(add_cell('diag("new session", m=mgr2)')).strip())
