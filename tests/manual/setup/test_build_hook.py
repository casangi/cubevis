"""The build hook that stamps the package description with the kernel-side
fingerprint (``pdm_build.py`` at the repository root; pdm-backend runs it).

    pytest tests/manual/setup/test_build_hook.py

A real build (``python -m build``) was compared by hand with pdm-pep517's
(2026-11-01, notes section 48): the same files and metadata, the
description stamped once.  Here: the hook's own logic, without a build.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

from cubevis import _fingerprint as F

ROOT = Path(__file__).resolve().parents[3]


def _hook():
    spec = importlib.util.spec_from_file_location("pdm_build_under_test", ROOT / "pdm_build.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Ctx:
    def __init__(self, root, readme):
        self.root = Path(root)

        class Config:
            metadata = {"name": "cubevis", "readme": readme}
        self.config = Config()


def test_the_description_is_stamped_with_the_code_being_built():
    h = _hook()
    c = _Ctx(ROOT, "README.rst")
    h.pdm_build_initialize(c)
    r = c.config.metadata["readme"]
    assert r["content-type"] == "text/x-rst"
    readme = (ROOT / "README.rst").read_text()
    assert r["text"].startswith(readme.rstrip("\n"))
    assert r["text"].endswith(f"\n\n{h.MARK} {F.fingerprint(root=str(ROOT))}\n")
    assert r["text"].count(h.MARK) == 1


def test_a_wheel_from_the_sdist_is_stamped_again_not_twice(tmp_path):
    """The sdist's pyproject carries the stamped text; the wheel built from it
    stamps that text with its own tree's fingerprint, once."""
    h = _hook()
    c = _Ctx(ROOT, {"text": "Doc.\n\n.. cubevis-kernel-fingerprint: k2-stale\n",
                    "content-type": "text/x-rst"})
    h.pdm_build_initialize(c)
    t = c.config.metadata["readme"]["text"]
    assert "k2-stale" not in t and t.count(h.MARK) == 1 and t.startswith("Doc.\n")


def test_the_stamp_is_what_setup_reads(tmp_path):
    from cubevis.setup import releases
    h = _hook()
    assert h.MARK == releases.MARK
    text = h.stamped("Some *rst*.\n", "k2-abc")
    assert releases.published_fingerprint(text) == "k2-abc"
    assert releases.published_fingerprint("no stamp here") is None


def test_the_backend_is_pdm_backend():
    """pdm-pep517 has no hooks: the stamp needs pdm-backend."""
    text = (ROOT / "pyproject.toml").read_text()
    assert 'build-backend = "pdm.backend"' in text and 'requires = ["pdm-backend' in text
