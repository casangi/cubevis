"""Which published cubevis release has this computer's kernel-side code
(remote session design 7c; notes section 48).

When cubevis here is not a release -- a checkout, past its tag or with local
changes -- the kernels can still use a release from PyPI, as long as that
release's kernel-side code is the same (``cubevis._fingerprint``).  Two ways
to know a release's fingerprint, in order:

1. **What was published**: every build since the switch to pdm-backend
   stamps the package description (the README on PyPI) with the fingerprint
   of the code it packaged (``pdm_build.py``)::

       .. cubevis-kernel-fingerprint: k2-67f9938a2b6ba9ce4a63

   PyPI's JSON API returns each release's description
   (``https://pypi.org/pypi/cubevis/<version>/json``): the newest releases
   are read, newest first, until one matches.  Nothing is downloaded but
   that JSON; no network, or no answer: skipped.
2. **The checkout's own release tag** (``sync.release_with_same_code``):
   the nearest tag's code from git, fingerprinted here -- for releases from
   before the stamp, and without network.

``CUBEVIS_SETUP_PYPI`` names another index's JSON base (the tests'; also a
mirror), ``CUBEVIS_SETUP_PYPI=off`` skips the lookup.
"""
from __future__ import annotations

import json
import os
import re
from typing import Callable, Optional

PYPI = "https://pypi.org/pypi/cubevis"
MARK = ".. cubevis-kernel-fingerprint:"     # pdm_build.MARK
NEWEST = 12                                 # releases read at most
TIMEOUT = 10.0
_RELEASE = re.compile(r"^\d+(\.\d+)*$")


def published_fingerprint(description: Optional[str]) -> Optional[str]:
    """The fingerprint a release's description was stamped with, or None."""
    for line in (description or "").splitlines():
        if line.startswith(MARK):
            fp = line[len(MARK):].strip()
            return fp or None
    return None


def _base() -> Optional[str]:
    b = os.environ.get("CUBEVIS_SETUP_PYPI", PYPI).strip()
    return None if b.lower() in ("off", "no", "0", "") else b.rstrip("/")


def fetch_json(url: str, timeout: float = TIMEOUT) -> Optional[dict]:
    """GET *url* as JSON (``urllib``: the proxy variables apply); None on
    any failure."""
    import urllib.request
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json",
                                                   "User-Agent": "cubevis-setup"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def _key(v: str) -> tuple:
    return tuple(int(x) for x in v.split("."))


def newest_releases(index: dict, limit: int = NEWEST) -> list:
    """Final releases (no dev, pre or local versions) with files that are not
    all yanked, newest first, from the project's JSON."""
    out = []
    for v, files in (index.get("releases") or {}).items():
        if _RELEASE.match(v) and files and not all(f.get("yanked") for f in files):
            out.append(v)
    return sorted(out, key=_key, reverse=True)[:limit]


def release_with_same_code(here: str, fetch: Optional[Callable] = None,
                           limit: int = NEWEST) -> tuple:
    """``(version, checked)``: the newest published release stamped with
    fingerprint *here* (None when none of the *limit* newest is), and how
    many stamped releases were looked at (0: PyPI could not be asked, or no
    release is stamped yet -- the caller falls back to git)."""
    base = _base()
    if not base or not here:
        return None, 0
    fetch = fetch or fetch_json
    index = fetch(f"{base}/json")
    if not index:
        return None, 0
    checked = 0
    for v in newest_releases(index, limit):
        info = fetch(f"{base}/{v}/json")
        fp = published_fingerprint(((info or {}).get("info") or {}).get("description"))
        if fp is None:
            continue
        checked += 1
        if fp == here:
            return v, checked
    return None, checked
