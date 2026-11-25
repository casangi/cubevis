# What iclean GUI state survives a page reload (2026-11-25).  MODE=saved: save,
# then reload (JupyterLab restores the output).  MODE=unsaved: reload without
# saving, then ic.show(redisplay=True).  Needs a notebook whose cell 0 makes
# `ic` (with a mask, e.g. mask='circle[[64pix,64pix],20pix]', so a cycle
# cleans) and cell 1 is `ic.show()`.  Runs one cycle with niter=40, then edits
# cycleniter without sending it, and compares the GUI before and after.
MODE = os.environ.get("MODE", "saved")
SNAP = r"""() => { const docs = Bokeh.documents; if (!docs.length) return 'no bokeh docs';
  const d = docs[docs.length - 1]; const r = {};
  for (const m of d._all_models.values()) {
    if (m.title === 'niter' || m.title === 'cycleniter') r[m.title] = m.value;
    if (m.data && m.data.iterations && m.data.values) r['conv_points'] = Math.max(r['conv_points'] || 0, m.data.iterations.length);
    if (m.text !== undefined && typeof m.text === 'string' && /residual|cycle|iteration|stop/i.test(m.text) && m.text.length < 400) r['status'] = m.text.replace(/<[^>]+>/g, ' ').trim().slice(0, 120);
  }
  r.ndocs = docs.length; return r }"""
def snap(label):
    try: print("STEP", label, json.dumps(pg.evaluate(SNAP)))
    except Exception as e: print("STEP", label, "ERR", e)
run_cell(0, timeout=300000)
run_cell(1, timeout=300000)
time.sleep(15)
snap("initial")
# One clean cycle with niter=40 (sent to the kernel), then change cycleniter to 7 (browser only).
pg.evaluate("""() => { const d = Bokeh.documents[Bokeh.documents.length - 1];
  for (const m of d._all_models.values()) if (m.title === 'niter') m.value = '40' }""")
print('STEP continue:', pg.evaluate("""() => { const d = Bokeh.documents[Bokeh.documents.length - 1];
  for (const m of d._all_models.values()) if (m.name === 'continue' && m.js_event_callbacks['button_click']) {
    const v = Bokeh.index.find_one ? Bokeh.index.find_one(m) : null;
    if (v) { v.click(); return 'clicked' } return 'no view' } }"""))
time.sleep(40)
pg.evaluate("""() => { const d = Bokeh.documents[Bokeh.documents.length - 1];
  for (const m of d._all_models.values()) if (m.title === 'cycleniter') m.value = '7' }""")
snap("after one cycle + cycleniter edit")
pg.screenshot(path=out + "_before.png")
if MODE == "saved":
    pg.evaluate("() => window.jupyterapp.commands.execute('docmanager:save')")
    time.sleep(2)
reload()
time.sleep(20)
if MODE != "saved":
    run_cell(add_cell("ic.show(redisplay=True)"), timeout=300000)
    time.sleep(20)
snap("after reload" + ("" if MODE == "saved" else " + redisplay"))
print("STEP page errors:", [c.splitlines()[0] for c in console if "PAGEERROR" in c][:4])
pg.screenshot(path=out + "_after.png")
