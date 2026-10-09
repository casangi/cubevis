# Turn on the raster Flag tool through its real toolbar button and drag
# boxes starting over data and over the gaps between baselines, with and
# without a pause before moving (the Bokeh press-gesture problem).  Shows
# finding a figure with FIND, clicking toolbar buttons, mouse drags, and
# reading pixels from a clipped screenshot.
#   ./live.sh examples/raster_flag_drag.py /tmp/x ms=<ms> field=3c279
import json
figs = pg.evaluate(FIND, None)
fr = [f for f in figs if f["panel"] == "raster"][0]
# instrument
pg.evaluate("""() => {
  window.__log = [];
  const sb = window.__cvSetBusy; window.__cvSetBusy = function(on) { window.__log.push('busy ' + on); return sb.apply(this, arguments); };
  const rs = window.__cvReleaseStuckDrag; window.__cvReleaseStuckDrag = function(r) { window.__log.push('release ' + r); return rs.apply(this, arguments); };
  for (const t of ['pointerdown','pointerup','pointercancel','lostpointercapture']) document.addEventListener(t, e => window.__log.push(t + ' id=' + e.pointerId + (e.isTrusted ? '' : ' SYNTH')), true);
}""")
# click the real toolbar button of the flag tool
btn = pg.evaluate("""(figid) => {
  const fig = Bokeh.documents[0].get_model_by_id(figid);
  const fv = Bokeh.index.find_one_by_id(fig.id);
  const tbv = Bokeh.index.find_one_by_id(fig.toolbar.id);
  if (!tbv) return null;
  const out = [];
  const walk = (v) => { for (const c of (v.child_views || v.children?.() || [])) {} };
  for (const [m, bv] of (tbv._tool_button_views || tbv.tool_button_views || new Map())) {
    const r = bv.el.getBoundingClientRect();
    out.push([m.tool ? (m.tool.description || m.tool.tool_name) : (m.description || m.tool_name || m.type), r.left + r.width/2, r.top + r.height/2]);
  }
  return out; }""", fr["fig"])
print("buttons", btn)
from PIL import Image
import io
fb = [b for b in btn if b[0] == "Flag"][0]
pg.mouse.click(fb[1], fb[2]); pg.wait_for_timeout(800)
print("flag tool active", pg.evaluate("(id) => Bokeh.documents[0].get_model_by_id(id).active", fr["id"]))
png = pg.screenshot(clip={"x": fr["x"], "y": fr["y"], "width": fr["w"], "height": fr["h"]})
im = Image.open(io.BytesIO(png)).convert("RGB")
cx = int(fr["w"] * 0.5)
dark = [y for y in range(5, int(fr["h"]) - 5) if sum(im.getpixel((cx, y))) < 30 and sum(im.getpixel((cx, y - 3))) < 30 and sum(im.getpixel((cx, y + 3))) < 30]
data = [y for y in range(5, int(fr["h"]) - 5) if sum(im.getpixel((cx, y))) > 120]
print("gap rows", dark[:5], "...", len(dark), " data rows", data[:3], len(data))
gy = fr["y"] + dark[len(dark) // 2]
dy = fr["y"] + data[len(data) // 2]
pg.screenshot(path=out + "_r.png", clip={"x": fr["x"] - 60, "y": fr["y"] - 20, "width": fr["w"] + 80, "height": fr["h"] + 60})
def drag(name, ystart, yend):
    pg.evaluate("() => { window.__log = []; }")
    n0 = pg.evaluate("() => (window.__sentN || 0)")
    x0 = fr["x"] + fr["w"] * 0.3
    pg.mouse.move(x0, ystart, steps=5); pg.wait_for_timeout(1500)   # hover there first, like a user
    pg.mouse.down(); pg.mouse.move(x0 + 60, yend, steps=8); pg.wait_for_timeout(200)
    vis = pg.evaluate("(id) => Bokeh.documents[0].get_model_by_id(id).overlay.visible", fr["id"])
    pg.mouse.up(); pg.wait_for_timeout(6000)
    nt = pg.evaluate("""() => { for (const m of Bokeh.documents[0]._all_models.values())
        if (m.text && typeof m.text === 'string' && /^(✓|⚠)/.test(m.text)) return m.text.slice(0, 80); return null; }""")
    print(f"{name}: box shown during drag={vis}  notify={nt}")
    print("   log:", pg.evaluate("() => window.__log.slice(0, 30)"))
def drag2(name, ystart, yend, pause):
    x0 = fr["x"] + fr["w"] * 0.3
    pg.mouse.move(x0, ystart, steps=5); pg.wait_for_timeout(800)
    pg.mouse.down(); pg.wait_for_timeout(pause); pg.mouse.move(x0 + 60, yend, steps=8); pg.wait_for_timeout(200)
    vis = pg.evaluate("(id) => Bokeh.documents[0].get_model_by_id(id).overlay.visible", fr["id"])
    pg.mouse.up(); pg.wait_for_timeout(5000)
    print(f"{name}: box shown={vis}")
drag2("gap,  no pause", gy, gy + 60, 0)
drag2("gap,  0.5 s still before moving", gy, gy + 60, 500)
drag2("data, 0.5 s still before moving", dy, dy + 30, 500)
drag2("data, 0.2 s still before moving", dy, dy + 30, 200)
drag2("gap,  2 s still before moving", gy, gy + 60, 2000)
print("press threshold while active", pg.evaluate("() => null"))
# a plain click still zooms to 1:1 (no drag): check the x range changes
r0 = pg.evaluate("(fid) => { const f = Bokeh.documents[0].get_model_by_id(fid); return [f.x_range.start, f.x_range.end]; }", fr["fig"])
pg.mouse.click(fr["x"] + fr["w"] * 0.5, dy); pg.wait_for_timeout(800)
r1 = pg.evaluate("(fid) => { const f = Bokeh.documents[0].get_model_by_id(fid); return [f.x_range.start, f.x_range.end]; }", fr["fig"])
print("click zoom", r0, "->", r1)
