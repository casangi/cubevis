# visplot(...) run in a notebook cell opens its GUI in a browser tab.  Until
# 2026-11-25 that tab could not reach the kernel (its CommMgr chose Jupyter
# comms) and the first request -- stepping to the first Antenna -- never
# returned.  Needs a notebook whose cell 0 replaces webbrowser.open with one
# that records the URL into _opened, runs visplot(ms=...) and prints
# "OPENED <urls>"; cell 1 (optional) shows a VisibilityPlotter made with the
# default transport in a tab, which is now refused.
import re
o = run_cell(0, timeout=300000)
print("STEP cell 0:", o.strip().splitlines()[-1][:200] if o.strip() else repr(o))
url = re.search(r"OPENED \['([^']+)'\]", o).group(1)
tab = ctx.new_page()
tab.on("console", lambda m: console.append(f"TAB {m.type}: {m.text}"))
tab.on("pageerror", lambda e: console.append(f"TAB PAGEERROR: {e}"))
tab.goto(url)
tab.wait_for_function("() => window.Bokeh && Bokeh.documents && Bokeh.documents.length", timeout=60000)
time.sleep(6)
print("STEP tab CommMgrs:", tab.evaluate("() => (window.__cvCommMgrs||[]).map(m => [m.state, m.transport && m.transport.constructor.name])"))
# Step to the first Antenna the way a user does: the ▶ beside the Antenna heading.
arrows = tab.evaluate("""() => {
  const all = []; const walk = (root) => { for (const e of root.querySelectorAll('*')) { all.push(e); if (e.shadowRoot) walk(e.shadowRoot) } };
  walk(document);
  const box = (e) => { const r = e.getBoundingClientRect(); return {x: r.left, y: r.top, width: r.width, height: r.height} };
  const lab = all.find(e => e.children.length === 0 && e.textContent.trim() === 'Antenna' && e.getBoundingClientRect().width > 0);
  if (!lab) return [];
  const ly = box(lab).y + box(lab).height / 2;
  return all.filter(e => e.tagName === 'BUTTON' && e.textContent.trim() === '▶').map(box)
            .filter(b => b.width > 0 && Math.abs(b.y + b.height / 2 - ly) < 15) }""")
print("STEP Antenna arrows found:", len(arrows))
title0 = tab.evaluate("""() => [...Bokeh.documents[0]._all_models.values()].filter(m => m.type === 'Title' || (m.text !== undefined && m.align !== undefined)).map(m => m.text).filter(t => !t.includes('<') && /Amplitude/.test(t))""")
ab = arrows[-1]          # not `b`: drive.py's browser
tab.mouse.click(ab["x"] + ab["width"] / 2, ab["y"] + ab["height"] / 2)
t0 = time.time(); time.sleep(1)
while time.time() - t0 < 60 and tab.evaluate("() => window.__cvBusyN || 0"):
    time.sleep(0.5)
title1 = tab.evaluate("""() => [...Bokeh.documents[0]._all_models.values()].filter(m => m.type === 'Title' || (m.text !== undefined && m.align !== undefined)).map(m => m.text).filter(t => !t.includes('<') && /Amplitude/.test(t))""")
print(f"STEP Antenna ▶: answered={tab.evaluate('() => (window.__cvBusyN || 0) == 0')} ({time.time() - t0:.1f} s)")
print("STEP titles before:", title0)
print("STEP titles after: ", title1)
tab.screenshot(path=out + "_tab.png")
if pg.evaluate("() => window.jupyterapp.shell.currentWidget.content.widgets.length") > 1:
    print("STEP cell 1:", (run_cell(1, timeout=300000) or "").strip().splitlines()[-1][:240])
print("STEP tab errors:", [c.splitlines()[0] for c in console if "PAGEERROR" in c][:4])
