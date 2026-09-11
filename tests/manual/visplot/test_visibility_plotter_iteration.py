"""
test_visibility_plotter_iteration.py
=====================================
Iteration position and step logic for I-1 (Phase 2.5) duo-mode
"Animate: Field | SPW" + Prev/Next.

Why this module exists
-----------------------
The kickoff for this work (``visplot_duo_iteration_kickoff.md`` §5)
specifically calls for standalone tests, needing no MS, no Bokeh, no
display, of:

* the pure step-computation logic (current index + count -> next/prev,
  wraparound, sentinel skip)
* holding the non-animated axis fixed

The step arithmetic itself lives in ``iteration_step.py`` and is tested in
``test_iteration_step.py`` (including its own JS/node parity harness,
since that logic's shipped copy runs client-side). This module covers the
other half: ``_field_iteration_position`` and ``_spw_iteration_position``
in ``visibility_plotter.py`` -- the *lookups* that translate "what is
currently selected" into a position for the status bar (e.g.
``"Field 3/7: 0637-752"``), correctly skipping the Field ``Select``'s
``("", "All fields")`` sentinel, which is exactly the "skip the sentinel"
requirement the kickoff calls out.

Both functions are pure (metadata in, a tuple or ``None`` out) but live in
a module that imports bokeh/websockets/xarray-ms at module scope, so they
are lifted by AST rather than imported -- see ``test_spw_selection.py``,
which established this pattern for the same module.

Test location
-------------
``cubevis/tests/manual/visplot/test_visibility_plotter_iteration.py``
"""

import ast
import pathlib
import textwrap
import types
from typing import Optional

import pytest


# ---------------------------------------------------------------------------
# Lifting the functions under test
# ---------------------------------------------------------------------------

def _find_visplot() -> pathlib.Path:
    """Locate ``cubevis/toolbox/visplot`` by walking up from this file.

    Searched rather than computed from a fixed ``parents[n]`` index --
    same rationale as ``test_spw_selection.py``'s copy of this helper.
    """
    for base in pathlib.Path(__file__).resolve().parents:
        cand = base / "cubevis" / "toolbox" / "visplot"
        if (cand / "visibility_plotter.py").is_file():
            return cand
    raise RuntimeError(
        "could not locate cubevis/toolbox/visplot above "
        f"{pathlib.Path(__file__).resolve()}"
    )


_PKG = _find_visplot()


def _lift(path, *names, extra=None):
    src = pathlib.Path(path).read_text()
    tree = ast.parse(src)
    ns = {"log": types.SimpleNamespace(warning=lambda *a, **k: None,
                                       debug=lambda *a, **k: None)}
    ns.update(extra or {})
    for name in names:
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == name)
        body = textwrap.dedent(ast.get_source_segment(src, fn))
        # Strip annotations that reference un-imported module symbols.
        body = body.replace("meta: ObservationMetadata", "meta")
        for dec in reversed(fn.decorator_list):
            body = f"@{getattr(dec, 'id', 'staticmethod')}\n" + body
        exec(body, ns)
    return ns


@pytest.fixture(scope="module")
def positions():
    """``_field_iteration_position`` and ``_spw_iteration_position``.

    Lifted together with ``_parse_field_string``, which
    ``_field_iteration_position`` calls -- both land in the same
    namespace, so the global lookup inside the lifted function body
    resolves correctly.
    """
    return _lift(
        _PKG / "visibility_plotter.py",
        "_parse_field_string",
        "_field_iteration_position",
        "_spw_iteration_position",
        extra={"Optional": Optional},
    )


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeField:
    def __init__(self, field_id, name):
        self.field_id = field_id
        self.name = name


class FakeSpw:
    def __init__(self, spw_id, name=""):
        self.spw_id = spw_id
        self.name = name


class FakeMeta:
    def __init__(self, fields=(), spws=()):
        self.fields = list(fields)
        self.spws = list(spws)


# ---------------------------------------------------------------------------
# Field position -- sentinel skip, wraparound is iteration_step's job, this
# is purely "where are we now"
# ---------------------------------------------------------------------------

class TestFieldIterationPosition:

    @pytest.fixture
    def meta(self):
        return FakeMeta(fields=[
            FakeField(0, "0637-752"),
            FakeField(2, "Ceres"),
            FakeField(3, "J0522-364"),
        ])

    def test_sentinel_field_str_has_no_position(self, positions, meta):
        """The "All fields" sentinel (field_str="") is not itself a
        steppable position -- this is the "skip the sentinel"
        requirement the kickoff calls out (§5)."""
        assert positions["_field_iteration_position"]("", meta) is None

    def test_middle_field_reports_its_position(self, positions, meta):
        assert positions["_field_iteration_position"]("Ceres", meta) == (2, 3)

    def test_first_field_reports_position_one(self, positions, meta):
        assert positions["_field_iteration_position"]("0637-752", meta) == (1, 3)

    def test_last_field_reports_position_equal_to_count(self, positions, meta):
        assert positions["_field_iteration_position"]("J0522-364", meta) == (3, 3)

    def test_numeric_field_id_resolves_through_parse_field_string(self, positions, meta):
        """field_str='2' must resolve via the real FIELD_ID (Ceres, the
        §8.4a/§8.25-style bug this guards against), not position 2."""
        assert positions["_field_iteration_position"]("2", meta) == (2, 3)

    def test_unresolvable_field_str_has_no_position(self, positions, meta):
        assert positions["_field_iteration_position"]("nosuchfield", meta) is None

    def test_no_fields_at_all(self, positions):
        assert positions["_field_iteration_position"]("", FakeMeta()) is None
        assert positions["_field_iteration_position"]("X", FakeMeta()) is None


# ---------------------------------------------------------------------------
# SPW position -- exactly one selected, else None (multi-select is a real
# state, not a sentinel)
# ---------------------------------------------------------------------------

class TestSpwIterationPosition:

    @pytest.fixture
    def meta(self):
        return FakeMeta(spws=[
            FakeSpw(0, "SPW0"),
            FakeSpw(1, "SPW1"),
            FakeSpw(17, "WVR"),
        ])

    def test_no_selection_has_no_position(self, positions, meta):
        assert positions["_spw_iteration_position"]([], meta) is None
        assert positions["_spw_iteration_position"](None, meta) is None

    def test_single_selection_reports_position(self, positions, meta):
        assert positions["_spw_iteration_position"]([1], meta) == (2, 3)

    def test_last_window_reports_position_equal_to_count(self, positions, meta):
        assert positions["_spw_iteration_position"]([17], meta) == (3, 3)

    def test_multi_selection_is_not_a_position(self, positions, meta):
        """A manual multi-window pick is a normal, common state -- not
        the same thing as an unresolved sentinel -- and correctly
        reports no position (the pre-existing "SPW: <str>" display
        covers it, unchanged)."""
        assert positions["_spw_iteration_position"]([0, 1], meta) is None

    def test_unmatched_id_has_no_position(self, positions, meta):
        assert positions["_spw_iteration_position"]([99], meta) is None

    def test_name_identity_works_the_same_as_numeric_id(self, positions):
        """SPW identity may be a bare name rather than an int (xarray-ms
        stores) -- position lookup must not assume numeric identities."""
        meta = FakeMeta(spws=[
            FakeSpw("ALMA_RB_07#BB_2#SW-01#FULL_RES"),
            FakeSpw("WVR#NOMINAL"),
        ])
        assert positions["_spw_iteration_position"](
            ["WVR#NOMINAL"], meta) == (2, 2)

    def test_no_spws_at_all(self, positions):
        assert positions["_spw_iteration_position"]([0], FakeMeta()) is None
