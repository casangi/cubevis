# A page reload when the notebook was not saved since visplot was shown: the
# output is gone, the session runs on in the kernel.  vp.show(redisplay=True)
# shows it again and it carries on (2026-11-25).  Needs a notebook whose cell 0
# makes `vp` and cell 1 is `vp.show()` (not make_notebook.py's single cell).
run_cell(0, timeout=300000)
run_cell(1, timeout=300000)
time.sleep(8)
print("STEP before reload:", pg.evaluate("() => (window.__cvCommMgrs||[]).map(m => m.state)"))
reload()                                  # not saved
time.sleep(10)
print("STEP after reload (want []):", pg.evaluate("() => (window.__cvCommMgrs||[]).map(m => m.state)"))
print("STEP a plain vp.show():", repr(run_cell(add_cell("vp.show()"), timeout=300000)[:120]))
run_cell(add_cell("vp.show(redisplay=True)"), timeout=300000)
time.sleep(10)
print("STEP after redisplay (want ['running']):", pg.evaluate("() => (window.__cvCommMgrs||[]).map(m => m.state)"))
pg.evaluate("""() => { const docs = Bokeh.documents; const d = docs[docs.length - 1];
  for (const m of d._all_models.values()) if (m.label !== undefined && String(m.label).startsWith('Plot') && m.js_event_callbacks['button_click']) {
    for (const cb of m.js_event_callbacks['button_click']) cb.execute(m); return } }""")
time.sleep(6)
print("STEP Plot after redisplay (want busy 0): busy =", pg.evaluate("() => window.__cvBusyN || 0"))
