"""``cubevis.docs()``: the documentation for the installed cubevis.

The documentation is published in several copies (docs/publish_site.py):
the latest at https://casangi.github.io/cubevis/, and one per release
under ``v<major>.<minor>/``, all listed in ``versions.json`` at the top of
the site.  ``docs()`` picks the copy for this cubevis' major.minor version
when one is published, and the latest otherwise (a development version, a
release made before versioned documentation, or no network).

Standard library only, and not imported by anything that runs in a remote
kernel (see cubevis/_fingerprint.py), so it does not change the kernel
fingerprint.
"""
from __future__ import annotations

import json
import re
import sys
import urllib.request
import webbrowser
from typing import Optional

SITE = "https://casangi.github.io/cubevis/"
TIMEOUT = 3.0            # seconds to wait for versions.json

# docs('visplot') and friends: short names for the pages people look for.
PAGES = {
    "install":  "applications/installing.html",
    "visplot":  "applications/visplot_starting.html",
    "iclean":   "applications/iclean_starting.html",
    "remote":   "applications/remote_kernels.html",
}


class DocsURL(str):
    """A URL that a notebook shows as a link (it is still a ``str``)."""

    def _repr_html_(self) -> str:
        from html import escape
        url = escape(self, quote=True)
        return f'<a href="{url}" target="_blank" rel="noopener">{url}</a>'


def _version() -> Optional[str]:
    try:
        from .__version__ import __version__      # written by the build
        return __version__
    except ImportError:
        pass
    try:
        from importlib.metadata import version
        return version("cubevis")
    except Exception:                             # not installed: a checkout
        return None


def _release(version: Optional[str]) -> Optional[str]:
    """'1.2.3' -> '1.2'; None for a development version ('1.2.4.dev3+g...')."""
    m = re.match(r"^(\d+)\.(\d+)(?:\.\d+)?$", version or "")
    return f"{m.group(1)}.{m.group(2)}" if m else None


def _published(path: str) -> bool:
    """Whether versions.json lists a copy at *path* ('v1.2/')."""
    try:
        with urllib.request.urlopen(SITE + "versions.json", timeout=TIMEOUT) as r:
            return any(e.get("path") == path for e in json.load(r))
    except Exception:
        return False


def _in_notebook() -> bool:
    """True in a Jupyter kernel (local or remote), where a browser started by
    Python would open on the kernel's computer, not in front of the user."""
    ipython = sys.modules.get("IPython")
    try:
        shell = ipython.get_ipython() if ipython else None
    except Exception:
        return False
    return shell is not None and type(shell).__name__ == "ZMQInteractiveShell"


def url(page: Optional[str] = None) -> DocsURL:
    """The documentation URL for this cubevis (see ``docs``), without opening it."""
    release = _release(_version())
    base = SITE
    if release and _published(f"v{release}/"):
        base = f"{SITE}v{release}/"
    if page:
        page = PAGES.get(page, page).lstrip("/")
        if not page.endswith((".html", "/")):
            page += ".html"
    return DocsURL(base + (page or ""))


def docs(page: Optional[str] = None, launch: Optional[bool] = None) -> DocsURL:
    """Open the cubevis documentation for this version of cubevis.

    Parameters
    ----------
    page : str, optional
        A page to open instead of the front page: ``'install'``,
        ``'visplot'``, ``'iclean'`` or ``'remote'``, or a path in the
        documentation such as ``'applications/visplot'``.
    launch : bool, optional
        Whether to open a web browser (the one ``$BROWSER`` names, else the
        system's).  By default a browser is opened except in a Jupyter
        notebook, where the returned URL is shown as a link instead: there
        Python runs in the kernel, which may be on another computer.

    Returns
    -------
    str
        The URL: the documentation for this cubevis' major.minor release
        when it is published, otherwise the latest documentation.
    """
    link = url(page)
    if launch is None:
        launch = not _in_notebook()
    if launch and not webbrowser.open(link):
        print(f"cubevis documentation: {link}")
    return link
