"""A saved cubevis page loads its scripts from beside it (2026-10-27).

Safari loads nothing outside a ``file://`` page's own directory, so pages
whose scripts were ``file://`` URLs into site-packages stayed blank there
(Chrome loaded them).  ``BokehAppContext.show`` now copies them into
``js/`` beside the page (``cubevis.bokeh.state._local_scripts``).

    pytest tests/manual/visplot/test_local_scripts.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import webbrowser

import pytest

from cubevis.bokeh.state._local_scripts import localize_scripts


def test_file_scripts_are_copied_beside_the_page(tmp_path):
    lib = tmp_path / "site packages" / "pkg"
    lib.mkdir(parents=True)
    (lib / "a.min.js").write_text("var a = 1;")
    (tmp_path / "other").mkdir()
    (tmp_path / "other" / "a.min.js").write_text("var a2 = 2;")      # same name, other file
    page = tmp_path / "page" / "p.html"
    page.parent.mkdir()
    a_url = "file://" + str(lib / "a.min.js").replace(" ", "%20")
    page.write_text(
        "<html><head>\n"
        f'<script src="{a_url}"></script>\n'
        f"<script type='text/javascript' src='file://{tmp_path}/other/a.min.js'></script>\n"
        '<script src="https://cdn.bokeh.org/bokeh/release/bokeh-3.10.0.min.js"></script>\n'
        '<script src="file:///no/such/file.js"></script>\n'
        f'<script src="{a_url}"></script>\n'
        "</head></html>\n")
    copied = localize_scripts(str(page))
    text = page.read_text()
    assert len(copied) == 2
    assert text.count('src="js/a.min.js"') == 2                     # one copy, used twice
    assert "src='js/a.min-2.js'" in text
    assert "https://cdn.bokeh.org/bokeh/release/bokeh-3.10.0.min.js" in text
    assert 'src="file:///no/such/file.js"' in text                   # missing: left alone
    assert (page.parent / "js" / "a.min.js").read_text() == "var a = 1;"
    assert (page.parent / "js" / "a.min-2.js").read_text() == "var a2 = 2;"
    assert localize_scripts(str(page)) == []                         # nothing left to do


def test_bokeh_app_context_show_writes_a_self_contained_page(tmp_path, monkeypatch):
    """The page show() writes names no script outside its directory -- and,
    when Playwright is here, renders in Chromium with nothing failing."""
    opened = []
    monkeypatch.setattr(webbrowser, "open", lambda url, *a, **k: opened.append(url) or True)
    from bokeh.models import Div
    from cubevis.bokeh.models import BokehAppContext
    ctx = BokehAppContext(ui=Div(text="<b>hello from cubevis</b>"), title="local scripts test")
    ctx.show()
    html = opened[0][len("file://"):]
    text = open(html).read()
    assert "file://" not in text, "a script still loads from outside the page's directory"
    assert 'src="js/bokeh.min.js"' in text and 'src="js/cubevisjs.min.js"' in text
    keep = tmp_path / "page"
    shutil.copytree(os.path.dirname(html), keep)          # the context's directory goes with it
    # A Python with Playwright: this one, or the system's python3.
    py = None
    for cand in (sys.executable, shutil.which("python3")):
        if cand and subprocess.run([cand, "-c", "import playwright"],
                                   capture_output=True).returncode == 0:
            py = cand
            break
    if py is None:
        pytest.skip("no Playwright here for the render check")
    code = r'''
import sys
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    b = p.chromium.launch(); pg = b.new_page(); bad = []
    pg.on("requestfailed", lambda r: bad.append(r.url))
    pg.on("pageerror", lambda e: bad.append(str(e)))
    pg.goto("file://" + sys.argv[1])
    pg.wait_for_selector("text=hello from cubevis", timeout=15000)
    print("RENDERED", bad)
    b.close()
'''
    r = subprocess.run([py, "-c", code, str(keep / os.path.basename(html))],
                       capture_output=True, text=True, timeout=120)
    if "Executable doesn't exist" in r.stderr or "playwright install" in r.stderr:
        pytest.skip("Playwright has no Chromium here")
    assert "RENDERED []" in r.stdout, r.stdout + r.stderr
