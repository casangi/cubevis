"""Open the live page in headless Chromium and run a steps file in it.

    STEPS=<steps.py> python drive.py <url> <out-prefix>

The steps file is exec()'d with these names in scope:
    pg    the Playwright page (already loaded, 8 s settle time)
    out   the output prefix, for screenshots (out + "_x.png")
    FIND  the JS of find.js: pg.evaluate(FIND, None) lists the visible
          flag-tool figures with their screen boxes
    os, json, time
Environment: VW / VH viewport width / height (default 1700 / 1100),
LOGLEN console line length (300), SHOWLOG substring of console lines to
print in addition to errors and warnings.
"""
import os, sys, json, time
from playwright.sync_api import sync_playwright

HERE = os.path.dirname(os.path.abspath(__file__))
url, out = sys.argv[1], sys.argv[2]
FIND = open(os.path.join(HERE, "find.js")).read()
with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page(viewport={"width": int(os.environ.get("VW", "1700")),
                              "height": int(os.environ.get("VH", "1100"))})
    msgs = []
    pg.on("console", lambda m: msgs.append(m.type + ": " + m.text[:int(os.environ.get("LOGLEN", "300"))]))
    pg.on("pageerror", lambda e: msgs.append("PAGEERROR: " + str(e)[:400]))
    pg.goto(url)
    pg.wait_for_timeout(8000)
    exec(open(os.environ["STEPS"]).read())
    show = os.environ.get("SHOWLOG")
    for m in msgs:
        if "error" in m.lower() or "PAGEERROR" in m or "warn" in m.lower() or (show and show in m):
            print(m)
    b.close()
