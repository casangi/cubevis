# Screenshot the page and print any success/warning notification text.
# ./live.sh examples/shot.py /tmp/x ms=<ms> field=3c279
pg.wait_for_timeout(3000)
pg.screenshot(path=out + "_shot.png")
for m in pg.evaluate("""() => { const o = []; for (const m of Bokeh.documents[0]._all_models.values())
   if (m.text && typeof m.text === 'string' && /^(✓|⚠)/.test(m.text)) o.push(m.text.slice(0, 150)); return o; }"""):
    print(m)
