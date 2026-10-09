# Timeline of the busy indicator (window.__cvSetBusy calls, the busy
# overlay) against image-source updates, after pressing Plot with scatter
# averaging set.  AT / AC set "Average over time" / "over channels"; WAIT
# is how long to record (ms).  Written for the 2026-10 busy-cursor report.
#   AT=scan ./live.sh examples/busy_timeline.py /tmp/x ms=<ms> field=3c279 kind=scatter scatter_x=CHANNEL
def setsel(title, v):
    return pg.evaluate("""([t, v]) => { let n = 0; for (const m of Bokeh.documents[0]._all_models.values())
        if (m.title === t && m.options) { try { m.value = v; n++; } catch (e) {} } return n; }""", [title, v])
pg.evaluate("""() => {
  window.__log = []; const T0 = performance.now();
  const sb = window.__cvSetBusy;
  window.__cvSetBusy = function(on) { const r = sb.apply(this, arguments);
     window.__log.push([Math.round(performance.now() - T0), 'busy(' + on + ') n=' + window.__cvBusyN + ' overlay=' + !!document.getElementById('__cv_busy_overlay')]); return r; };
  for (const m of Bokeh.documents[0]._all_models.values())
    if (m.data && m.data.image) m.change.connect(() => window.__log.push([Math.round(performance.now() - T0), 'image ' + m.id]));
  const obs = new MutationObserver(() => window.__log.push([Math.round(performance.now() - T0), 'overlay ' + (!!document.getElementById('__cv_busy_overlay'))]));
  obs.observe(document.body, {childList: true});
}""")
print("set", setsel("Average over time", os.environ.get("AT", "scan")), setsel("Average over channels", os.environ.get("AC", "off")))
plot = pg.evaluate("""() => { for (const m of Bokeh.documents[0]._all_models.values()) if (m.label && String(m.label).startsWith('Plot')) return m.id; }""")
pg.evaluate("""(id) => { const b = Bokeh.documents[0].get_model_by_id(id);
   for (const cb of (b.js_event_callbacks['button_click'] || [])) cb.execute(b); }""", plot)
pg.wait_for_timeout(int(os.environ.get("WAIT", "20000")))
for t, e in pg.evaluate("() => window.__log"):
    print(t, e)
