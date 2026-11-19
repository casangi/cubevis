"""Stable addresses for visplot's in-window help (2026-11-19).

Pressing **?** in the visplot window opens the documentation at the entry
for what the status area is showing (``cubevis/toolbox/visplot/
help_text.py``).  The code names an entry by a label, a MyST target such
as ``(vp-flag-reach)=`` in ``applications/visplot_help.md``, and must not
need to know which page that label is on or what its heading's anchor
is: pages get reorganised.  So for every label starting with ``vp-`` this
extension writes a small page

    help/<label>.html   ->   ../<page>.html#<anchor>

that forwards to it, and ``help/index.html``, which forwards to the page
of the ``vp-help`` label.  The forward is relative, so it works in every
published copy (the latest at the top of the site, ``v<X.Y>/``,
``dev/<branch>/``) and in a local build opened from disk.

It also writes ``404.html``, which GitHub Pages serves for any address
that does not exist -- only the one at the top of the site, i.e. the
latest copy's.  For an address ``<copy>/help/<label>.html`` it forwards
to the latest copy's ``help/<label>.html``: a release made before the
label existed, or a development install whose version has no copy of its
own, still lands on the entry.  A label that even the latest copy lacks
goes to the latest ``help/index.html``.  ``vp_help_site_root`` is the
site's path on its host (``/cubevis/`` on casangi.github.io).

The labels the code uses are checked against the documentation sources
by ``tests/manual/visplot/test_help_labels.py``, without building.
"""
from __future__ import annotations

import html
import os

PREFIX = "vp-"
INDEX_LABEL = "vp-help"
HELP_DIR = "help"

_STUB = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>{title}</title>
<meta name="robots" content="noindex">
<meta http-equiv="refresh" content="0; url={url}">
<script>location.replace({url_js} + location.search);</script>
</head><body><p><a href="{url}">{title}</a></p></body></html>
"""

_NOT_FOUND = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Page not found</title>
<meta name="robots" content="noindex">
<script>
(function () {{
  var root = {root_js};
  var p = location.pathname;
  if (p.indexOf(root) !== 0) return;
  var m = /^(.*?)help\\/([^\\/]+)\\.html$/.exec(p.slice(root.length));
  if (!m) return;
  // A copy without this help entry: try the latest copy's; the latest
  // copy without it: its help index.
  location.replace(root + "help/" + (m[1] ? m[2] : "index") + ".html");
}})();
</script>
</head><body>
<h1>Page not found</h1>
<p>The <a href="{root}">cubevis documentation</a> has no page at this address.</p>
</body></html>
"""


def _write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _stub(url: str, title: str) -> str:
    import json
    return _STUB.format(url=html.escape(url, quote=True), url_js=json.dumps(url),
                        title=html.escape(title))


def help_targets(app) -> dict:
    """``{label: (docname, anchor, title)}`` for every ``vp-`` label.

    A label may be put anywhere in the documentation, on any page: before
    a heading, or before a paragraph, list or note inside a section.
    Sphinx lists only the first kind in ``labels`` (with the heading as
    title); ``anonlabels`` has both, so it is the one read here."""
    std = app.env.domaindata.get("std", {})
    titled = std.get("labels", {})
    out = {}
    for name, (doc, anchor) in std.get("anonlabels", {}).items():
        if name.startswith(PREFIX) and doc:
            title = titled.get(name, (None, None, None))[2]
            out[name] = (doc, anchor, title)
    return out


def write_help_pages(app, exception) -> None:
    if exception is not None or app.builder.format != "html":
        return
    out = app.builder.outdir
    targets = help_targets(app)
    for name, (doc, anchor, title) in targets.items():
        url = "../" + app.builder.get_target_uri(doc) + (f"#{anchor}" if anchor else "")
        _write(os.path.join(out, HELP_DIR, f"{name}.html"), _stub(url, title or name))
    if INDEX_LABEL in targets:
        doc, _anchor, title = targets[INDEX_LABEL]
        _write(os.path.join(out, HELP_DIR, "index.html"),
               _stub("../" + app.builder.get_target_uri(doc), title or "visplot help"))
    import json
    root = app.config.vp_help_site_root
    _write(os.path.join(out, "404.html"),
           _NOT_FOUND.format(root_js=json.dumps(root), root=html.escape(root, quote=True)))


def setup(app):
    app.add_config_value("vp_help_site_root", "/cubevis/", "html")
    app.connect("build-finished", write_help_pages)
    return {"parallel_read_safe": True, "parallel_write_safe": True}
