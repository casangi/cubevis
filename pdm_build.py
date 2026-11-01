"""pdm-backend build hook: stamp the package's description with the
kernel-side fingerprint of the code being built.

``python -m cubevis.setup`` decides whether a release on PyPI can serve this
computer's kernels by comparing kernel-side fingerprints
(``cubevis/_fingerprint.py``; remote session design 7c).  A release's own
fingerprint has to come from what was *published* -- not from its git tag,
since the build adds files a checkout does not have (``__version__.py``) --
so every build records it in the package's long description (the README
shown on PyPI), as a reStructuredText comment, invisible on the page::

    .. cubevis-kernel-fingerprint: k2-67f9938a2b6ba9ce4a63

Setup reads it from PyPI's JSON API (``info.description`` of
``https://pypi.org/pypi/cubevis/<version>/json``).  The README file itself is
not changed: the stamped text goes only into the build's metadata.  Every
build path runs this hook (CI's ``pdm build``, a local build, a wheel built
from the sdist, an editable install).
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

MARK = ".. cubevis-kernel-fingerprint:"


def kernel_fingerprint(root: Path) -> str:
    """The fingerprint of the ``cubevis`` package under *root*, by that
    tree's own ``_fingerprint.py`` (standard library only: nothing of
    cubevis's dependencies is needed in the build environment)."""
    spec = importlib.util.spec_from_file_location(
        "_cubevis_build_fingerprint", root / "cubevis" / "_fingerprint.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.fingerprint(root=str(root))


def stamped(text: str, fp: str) -> str:
    """*text* with exactly one fingerprint line, at the end."""
    lines = [l for l in text.rstrip("\n").split("\n") if not l.startswith(MARK)]
    return "\n".join(lines).rstrip("\n") + f"\n\n{MARK} {fp}\n"


def pdm_build_initialize(context) -> None:
    root = Path(context.root)
    meta = context.config.metadata
    readme = meta.get("readme")
    if isinstance(readme, str):
        readme = {"file": readme,
                  "content-type": "text/x-rst" if readme.endswith(".rst") else "text/markdown"}
    if not isinstance(readme, dict):
        raise RuntimeError("pdm_build.py: [project] readme is missing")
    if readme.get("file"):
        text = (root / readme["file"]).read_text(encoding="utf-8")
    elif "text" in readme:
        text = readme["text"]   # a wheel built from the sdist: its metadata, stamped again
    else:
        raise RuntimeError("pdm_build.py: [project] readme has neither file nor text")
    meta["readme"] = {"text": stamped(text, kernel_fingerprint(root)),
                      "content-type": readme.get("content-type", "text/x-rst")}
