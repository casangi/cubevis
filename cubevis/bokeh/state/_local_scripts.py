"""Make a saved page's local scripts loadable by every browser.

cubevis saves its page to a temporary directory and opens it as a
``file://`` URL; its scripts (casalib, Bokeh's, cubevisjs) are ``file://``
URLs into the installed packages (``resolve_js_library_paths``).  Chrome and
Firefox load those; **Safari does not**: a local page may only read files in
its own directory and below, so every script fails ("An error occurred
trying to load the resource") and the window stays blank, with no message
(Darrell, 2026-10-27, Safari on macOS).

``localize_scripts`` copies each ``file://`` script the page names into a
``js/`` directory beside the page and points the page at it with a relative
path -- the same files, loaded from where every browser allows.  Scripts
from the network (a CDN) are left as they are.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
from urllib.parse import unquote, urlparse

logger = logging.getLogger(__name__)

_SRC_RE = re.compile(r'(<script\b[^>]*\bsrc=)(["\'])(file://[^"\']+)\2', re.IGNORECASE)


def localize_scripts(html_path: str, subdir: str = "js") -> list:
    """Rewrite *html_path* so its ``file://`` scripts load from *subdir*
    beside it (copied there).  Returns the scripts copied.  A script that
    cannot be found or copied is left as it was (and logged)."""
    with open(html_path, encoding="utf-8") as f:
        text = f.read()
    here = os.path.dirname(os.path.abspath(html_path))
    dest_dir = os.path.join(here, subdir)
    copied, names = [], {}

    def repl(m):
        url = m.group(3)
        src = unquote(urlparse(url).path)
        if not os.path.isfile(src):
            logger.warning("page script %s does not exist; left as it is", src)
            return m.group(0)
        name = names.get(src)
        if name is None:
            base = os.path.basename(src)
            name, i = base, 2
            while name in names.values():            # two files of one name
                root, ext = os.path.splitext(base)
                name, i = f"{root}-{i}{ext}", i + 1
            try:
                os.makedirs(dest_dir, exist_ok=True)
                shutil.copyfile(src, os.path.join(dest_dir, name))
            except OSError as exc:
                logger.warning("cannot copy page script %s (%s); left as it is", src, exc)
                return m.group(0)
            names[src] = name
            copied.append(src)
        return f"{m.group(1)}{m.group(2)}{subdir}/{name}{m.group(2)}"

    new = _SRC_RE.sub(repl, text)
    if new != text:
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(new)
    return copied
