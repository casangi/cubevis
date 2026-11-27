# A page reload while an iclean cycle runs (issue 1, 2026-11-25).  Needs a
# notebook like reload_state.py's whose cell 0 also slows the cycle down:
#     from cubevis.imaging import LocalImagingSession
#     _orig = LocalImagingSession.arun_cycle
#     async def _slow(self): await asyncio.sleep(25); return await _orig(self)
#     LocalImagingSession.arun_cycle = _slow
# Starts a cycle, saves and reloads while it runs: the reloaded page should show
# that a cycle is running with the clean buttons disabled, then, when the cycle
# ends, its result (niter left, status, two convergence points), buttons enabled.
SNAP = r"""() => { const docs = Bokeh.documents; if (!docs.length) return 'no bokeh docs';
  const d = docs[docs.length - 1]; const r = {};
  for (const m of d._all_models.values()) {
    if (m.title === 'niter' || m.title === 'cycleniter') r[m.title] = m.value;
    if (m.name === 'continue' && m.js_event_callbacks && m.js_event_callbacks['button_click']) r['continue_disabled'] = m.disabled;
    if (m.data && m.data.iterations && m.data.values) r['conv_points'] = Math.max(r['conv_points'] || 0, m.data.iterations.length);
    if (m.text !== undefined && typeof m.text === 'string' && /residual|cycle|iteration|stop|running/i.test(m.text) && m.text.length < 600) r['status'] = m.text.replace(/<[^>]+>/g, ' ').trim().slice(0, 120);
  }
  return r }"""
def snap(label):
    try: print("STEP", label, json.dumps(pg.evaluate(SNAP)))
    except Exception as e: print("STEP", label, "ERR", e)
run_cell(0, timeout=300000)
run_cell(1, timeout=300000)
time.sleep(15)
snap("initial")
pg.evaluate("""() => { const d = Bokeh.documents[Bokeh.documents.length - 1];
  for (const m of d._all_models.values()) { if (m.title === 'niter') m.value = '400';
                                            if (m.title === 'cycleniter') m.value = '15' } }""")
print('STEP continue:', pg.evaluate("""() => { const d = Bokeh.documents[Bokeh.documents.length - 1];
  for (const m of d._all_models.values()) if (m.name === 'continue' && m.js_event_callbacks['button_click']) {
    const v = Bokeh.index.find_one ? Bokeh.index.find_one(m) : null;
    if (v) { v.click(); return 'clicked' } return 'no view' } }"""))
time.sleep(4)
pg.evaluate("() => window.jupyterapp.commands.execute('docmanager:save')")
time.sleep(2)
reload()
time.sleep(8)
snap("reloaded while running (want continue_disabled true, cycleniter 15, a progress status)")
time.sleep(40)
snap("after the cycle (want niter 345 or so, conv_points 2, continue_disabled false)")
print("STEP page errors:", [c.splitlines()[0] for c in console if "PAGEERROR" in c][:4])
pg.screenshot(path=out + "_after.png")
