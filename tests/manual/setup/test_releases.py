"""Which published release has this computer's kernel-side code
(``cubevis.setup.releases``): the fingerprint stamped into each release's
description on PyPI (``pdm_build.py``), read newest first.

    pytest tests/manual/setup/test_releases.py

No network: PyPI's JSON is stood in for by a dict.
"""
from __future__ import annotations

from cubevis.setup import releases as R

BASE = "https://pypi.org/pypi/cubevis"


def _index(stamps: dict, extra=()):
    """PyPI's JSON for releases *stamps* (version -> fingerprint or None:
    unstamped, before the hook), plus *extra* versions with no stamp."""
    rel = {v: [{"filename": f"cubevis-{v}.tar.gz", "yanked": False}] for v in list(stamps) + list(extra)}
    pages = {f"{BASE}/json": {"releases": rel}}
    for v, fp in stamps.items():
        desc = "cubevis\n=======\n" + (f"\n{R.MARK} {fp}\n" if fp else "")
        pages[f"{BASE}/{v}/json"] = {"info": {"version": v, "description": desc}}
    seen = []

    def fetch(url, timeout=R.TIMEOUT):
        seen.append(url)
        return pages.get(url)
    return fetch, seen, rel


def test_the_newest_release_with_the_same_code(monkeypatch):
    monkeypatch.delenv("CUBEVIS_SETUP_PYPI", raising=False)
    fetch, seen, _ = _index({"1.0.106": "k2-new", "1.0.105": "k2-here", "1.0.104": "k2-here",
                             "1.0.103": None})
    assert R.release_with_same_code("k2-here", fetch) == ("1.0.105", 2)
    assert f"{BASE}/1.0.104/json" not in seen                     # stopped at the first match


def test_none_matches_and_unstamped_releases_do_not_count(monkeypatch):
    monkeypatch.delenv("CUBEVIS_SETUP_PYPI", raising=False)
    fetch, _seen, _ = _index({"1.0.105": "k2-other", "1.0.104": None, "1.0.103": None})
    assert R.release_with_same_code("k2-here", fetch) == (None, 1)
    fetch, _seen, _ = _index({"1.0.103": None, "1.0.102": None})
    assert R.release_with_same_code("k2-here", fetch) == (None, 0)   # the caller falls back to git


def test_only_final_releases_with_files_that_are_not_yanked():
    idx = {"releases": {"1.0.9": [{"yanked": False}], "1.0.10": [{"yanked": False}],
                        "1.0.11rc1": [{"yanked": False}], "1.0.11.dev3": [{"yanked": False}],
                        "1.0.12": [{"yanked": True}], "1.0.13": []}}
    assert R.newest_releases(idx) == ["1.0.10", "1.0.9"]                # numeric, not text, order
    assert R.newest_releases(idx, limit=1) == ["1.0.10"]


def test_no_network_or_turned_off(monkeypatch):
    monkeypatch.delenv("CUBEVIS_SETUP_PYPI", raising=False)
    assert R.release_with_same_code("k2-here", lambda url, timeout=0: None) == (None, 0)
    monkeypatch.setenv("CUBEVIS_SETUP_PYPI", "off")
    fetch, seen, _ = _index({"1.0.105": "k2-here"})
    assert R.release_with_same_code("k2-here", fetch) == (None, 0) and not seen
    monkeypatch.setenv("CUBEVIS_SETUP_PYPI", "https://mirror.example.org/pypi/cubevis/")
    fetch, seen, _ = _index({"1.0.105": "k2-here"})
    assert R.release_with_same_code("k2-here", fetch) == (None, 0)
    assert seen == ["https://mirror.example.org/pypi/cubevis/json"]


def test_the_stamp_line():
    assert R.published_fingerprint(f"Text.\n\n{R.MARK} k2-abc\n") == "k2-abc"
    assert R.published_fingerprint(f"{R.MARK}\n") is None
    assert R.published_fingerprint(None) is None
