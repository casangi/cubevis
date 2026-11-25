# The basic visplot flow inside a notebook: Plot with another quantity, the
# raster Flag tool, Accept, the side-by-side layout, and iteration.  Each step
# reports whether its request was answered (busy back to 0) and what changed.
# Uses ../visplot_headless/find.js.  Written for the 2026-11-24 notebook check.
import os, io
FIND = open(os.path.join(os.path.dirname(os.path.abspath(steps)), "..", "..",
                         "visplot_headless", "find.js")).read()
run_cell(0, timeout=120000)
time.sleep(6)

pg.evaluate("""() => {
  window.__imgN = 0;
  for (const d of Bokeh.documents) for (const m of d._all_models.values())
    if (m.data && m.data.image) m.change.connect(() => window.__imgN++);
}""")

def models(js_filter):
    return pg.evaluate(f"() => [...Bokeh.documents[0]._all_models.values()].filter({js_filter}).map(m => m.id)")

def setsel(title, v):
    return pg.evaluate("""([t, v]) => { let n = 0; for (const m of Bokeh.documents[0]._all_models.values())
        if (m.title === t && m.options) { try { m.value = v; n++; } catch (e) {} } return n; }""", [title, v])

def press(label_start):
    return pg.evaluate("""(l) => { for (const m of Bokeh.documents[0]._all_models.values())
        if (m.label !== undefined && String(m.label).startsWith(l) && m.js_event_callbacks['button_click']) {
          for (const cb of m.js_event_callbacks['button_click']) cb.execute(m); return m.id } return null }""", label_start)

def settle(what, timeout=30):
    t0 = time.time()
    time.sleep(1)
    while time.time() - t0 < timeout:
        if pg.evaluate("() => (window.__cvBusyN || 0)") == 0:
            break
        time.sleep(0.5)
    st = pg.evaluate("() => ({busy: window.__cvBusyN || 0, images: window.__imgN})")
    print(f"STEP {what}: answered={st['busy'] == 0} images updated so far={st['images']} ({time.time() - t0:.1f} s)")

def notify():
    return pg.evaluate("""() => { for (const m of Bokeh.documents[0]._all_models.values())
        if (m.text && typeof m.text === 'string' && /^(✓|⚠|✗)/.test(m.text)) return m.text.slice(0, 120); return null }""")

# 1. another quantity, Plot
print("STEP quantities:", pg.evaluate("""() => { for (const m of Bokeh.documents[0]._all_models.values())
    if (m.title === 'Raster quantity') return m.options }"""))
setsel("Raster quantity", "Phase")
press("Plot")
settle("Plot (Phase)")

# 2. the raster Flag tool, a drag over data, Accept
figs = pg.evaluate(FIND, None)
print("STEP figures:", [(f["panel"], round(f["w"]), round(f["h"])) for f in figs])
fr = [f for f in figs if f["panel"] == "raster"][0]
btn = pg.evaluate("""(figid) => {
  const fig = Bokeh.documents[0].get_model_by_id(figid);
  const tbv = Bokeh.index.find_one_by_id(fig.toolbar.id);
  const out = [];
  for (const [m, bv] of (tbv._tool_button_views || tbv.tool_button_views || new Map())) {
    const r = bv.el.getBoundingClientRect();
    out.push([m.tool ? (m.tool.description || m.tool.tool_name) : (m.description || m.tool_name || m.type), r.left + r.width/2, r.top + r.height/2]);
  }
  return out }""", fr["fig"])
fb = [b for b in btn if b[0] == "Flag"]
if fb:
    pg.mouse.click(fb[0][1], fb[0][2]); time.sleep(0.8)
    x0, y0 = fr["x"] + fr["w"] * 0.3, fr["y"] + fr["h"] * 0.3
    pg.mouse.move(x0, y0, steps=5); time.sleep(0.8)
    pg.mouse.down(); pg.mouse.move(x0 + 80, y0 + 60, steps=8); time.sleep(0.2); pg.mouse.up()
    settle("flag drag")
    print("STEP notification:", notify())
    if press("Accept"):
        settle("Accept")
        print("STEP notification:", notify())
else:
    print("STEP no Flag tool button found:", [b[0] for b in btn])
pg.screenshot(path=out + "_flag.png")

# 3. side-by-side layout, Plot
rbg = pg.evaluate("""() => { for (const m of Bokeh.documents[0]._all_models.values())
    if (m.type === 'RadioButtonGroup' && m.labels && m.labels.length >= 3) return [m.id, m.labels]; return null }""")
print("STEP layout buttons:", rbg)
if rbg:
    pg.evaluate("(id) => { Bokeh.documents[0].get_model_by_id(id).active = 1 }", rbg[0])
    press("Plot")
    settle("Plot (layout 1)")
    figs = pg.evaluate(FIND, None)
    print("STEP figures now:", [(f["panel"], round(f["w"]), round(f["h"])) for f in figs])
pg.screenshot(path=out + "_side.png")

# 4. iteration
opts = pg.evaluate("""() => { for (const m of Bokeh.documents[0]._all_models.values())
    if (m.title === 'Iterate by') return m.options }""")
print("STEP iterate by:", opts)
if opts and len(opts) > 1:
    o = opts[1] if not isinstance(opts[1], list) else opts[1][0]
    setsel("Iterate by", o)
    time.sleep(1)
    nxt = press("⟩") or press("▶") or press("Next")
    print("STEP next button pressed:", bool(nxt))
    settle("iteration step")
errs = [c for c in console if "PAGEERROR" in c or c.startswith("error")]
print("STEP page errors:", len(errs), errs[:5])
