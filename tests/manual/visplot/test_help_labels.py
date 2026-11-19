"""test_help_labels.py -- every documentation entry visplot's ? key opens exists.

visplot's status-area help names its long form by a label
(``help_text``): the **?** key opens ``<docs>/help/<label>.html``, a page
the documentation build writes for each ``vp-`` label
(docs/source/extensions/vp_help.py).  A label used in the code but not
defined in the documentation would send the reader to the help index
instead of the entry, silently.  So:

* every ``vp-`` label in the visplot code -- string literals in
  ``cubevis/toolbox/visplot/*.py``, and the ones made from names (the
  built-in flag filters', ``flag_controls.DOCUMENTED_FILTERS``) -- is
  defined in ``docs/source`` (``(vp-x)=`` in Markdown, ``.. _vp-x:`` in
  reStructuredText);
* no label is defined twice.

Read from the sources, without building the documentation, so it runs
with the quick tests (tests/ci/suites.toml, suite "visplot-help").  Labels
are append-only within a major.minor (devel/docs/visplot): a release's
patch tag replaces its documentation copy, and a label dropped there
breaks ? in the earlier patch releases still installed.

Run:  python -m pytest test_help_labels.py -q
"""
from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

from cubevis_test_paths import ensure_cubevis_importable

ensure_cubevis_importable()

from cubevis.toolbox.visplot import help_text as ht                  # noqa: E402
from cubevis.toolbox.visplot.flag_controls import DOCUMENTED_FILTERS  # noqa: E402

ROOT = Path(__file__).resolve().parents[3]
CODE = ROOT / "cubevis" / "toolbox" / "visplot"
DOCS = ROOT / "docs" / "source"
HELP_PAGE = DOCS / "applications" / "visplot_help.md"

_MD_TARGET = re.compile(r"^\((vp-[a-z0-9-]+)\)=\s*$", re.M)
_RST_TARGET = re.compile(r"^\.\. _(vp-[a-z0-9-]+):\s*$", re.M)
_CODE_LABEL = re.compile(r"""["'](vp-[a-z0-9-]+)["']""")
_FENCE = re.compile(r"^(```|~~~).*?^\1\s*$", re.M | re.S)   # examples, not targets


def doc_labels() -> list:
    out = []
    for p in sorted(DOCS.rglob("*")):
        if p.suffix == ".md":
            out += _MD_TARGET.findall(_FENCE.sub("", p.read_text(encoding="utf-8")))
        elif p.suffix == ".rst":
            out += _RST_TARGET.findall(p.read_text(encoding="utf-8"))
    return out


def code_labels() -> set:
    found = set()
    for p in sorted(CODE.glob("*.py")):
        found |= set(_CODE_LABEL.findall(p.read_text(encoding="utf-8")))
    found |= {ht.LABEL_PREFIX + "flagf-" + n.replace("_", "-") for n in DOCUMENTED_FILTERS}
    found |= set(ht.SELECTION_LABELS.values())
    return found


def doc_section(label: str) -> str:
    """The text of the help page from *label*'s target to the next one:
    for tests that check what the long form says."""
    text = HELP_PAGE.read_text(encoding="utf-8")
    start = text.index(f"({label})=")
    m = _MD_TARGET.search(text, start + 1)
    return text[start:m.start() if m else len(text)]


def test_code_uses_labels():
    # The scan must find the labels it is meant to (a broken regex would
    # pass the next test vacuously).
    labels = code_labels()
    for expected in ("vp-flag-reach", "vp-bcombine", "vp-scan", "vp-flagf-outlier",
                     "vp-flag-export", "vp-preset-antmatrix"):
        assert expected in labels
    assert len(labels) >= 20


def test_every_label_in_the_code_is_documented():
    missing = sorted(code_labels() - set(doc_labels()))
    assert not missing, f"labels used by visplot but not defined in docs/source: {missing}"


def test_no_label_is_defined_twice():
    twice = sorted(k for k, n in Counter(doc_labels()).items() if n > 1)
    assert not twice, twice


def test_help_index_label_exists():
    # help/index.html, which the toolbar's Help button opens, forwards to
    # the page of this label (vp_help.INDEX_LABEL).
    assert "vp-help" in doc_labels()
    assert HELP_PAGE.read_text(encoding="utf-8").startswith("(vp-help)=")


# --------------------------------------------------------------------------
# The site's 404 page: a help address in a copy without it
# --------------------------------------------------------------------------

def _vp_help():
    import importlib.util
    spec = importlib.util.spec_from_file_location("vp_help", DOCS / "extensions" / "vp_help.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _not_found_goes(path: str):
    import json
    import shutil
    import subprocess
    import pytest
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    page = _vp_help()._NOT_FOUND.format(root_js=json.dumps("/cubevis/"), root="/cubevis/")
    script = re.search(r"<script>(.*?)</script>", page, re.S).group(1)
    code = ("let went = null; const location = {pathname: %s, replace: (u) => { went = u; }};\n"
            % json.dumps(path)) + script + "\nconsole.log(JSON.stringify(went));"
    out = subprocess.run([node, "-e", code], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip())


def test_404_sends_a_missing_entry_to_the_latest_copy():
    assert _not_found_goes("/cubevis/v1.1/help/vp-new.html") == "/cubevis/help/vp-new.html"
    assert _not_found_goes("/cubevis/dev/docs-x/help/vp-new.html") == "/cubevis/help/vp-new.html"


def test_404_in_the_latest_copy_goes_to_the_index_not_round_again():
    assert _not_found_goes("/cubevis/help/vp-new.html") == "/cubevis/help/index.html"


def test_404_leaves_other_addresses_alone():
    assert _not_found_goes("/cubevis/v1.1/applications/nothing.html") is None
    assert _not_found_goes("/other/help/vp-x.html") is None
