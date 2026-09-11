"""
test_layout_kind_shortcut.py
=============================
``layout="raster"``/``"scatter"`` shortcut and ``kind=`` constructor
parameter (design decision settled 2026-08-31).

Why this module exists
-----------------------
Two independent things need checking, and both are amenable to the
lifting approach already established in this test suite (see
``test_spw_selection.py`` for the rationale -- ``visibility_plotter.py``
pulls in bokeh/websockets/xarray-ms at module scope):

1. ``_normalize_layout_kind`` -- the pure function that resolves the
   ``layout="raster"``/``"scatter"`` shortcut into ``(layout, kind)``.
   Lifted and unit-tested directly, same as ``_resolve_axis_arg``'s
   sibling functions elsewhere in this module.

2. The wiring in ``_build_panels`` that turns ``self._kind`` into
   ``_slot_a_kind``/``_slot_b_kind``. This is *not* a standalone
   function -- it's two assignment statements inside a much larger
   method that also opens comm channels, builds Bokeh models, etc. --
   so it is checked the same way ``test_build_layout_visibility.py``
   checks ``_build_layout``'s container-visibility assignments: lift
   just those two expressions by AST and evaluate them against a stub
   ``self._kind``, rather than instantiating the whole plotter.

Test location
-------------
``cubevis/tests/manual/visplot/test_layout_kind_shortcut.py``
"""

import ast
import pathlib
import types

import pytest


def _find_visplot() -> pathlib.Path:
    for base in pathlib.Path(__file__).resolve().parents:
        cand = base / "cubevis" / "toolbox" / "visplot"
        if (cand / "visibility_plotter.py").is_file():
            return cand
    raise RuntimeError(
        "could not locate cubevis/toolbox/visplot above "
        f"{pathlib.Path(__file__).resolve()}"
    )


_PKG = _find_visplot()
_SRC = (_PKG / "visibility_plotter.py").read_text()


# ---------------------------------------------------------------------------
# Part 1: _normalize_layout_kind -- pure function, lifted directly
# ---------------------------------------------------------------------------

def _lift_function(name):
    tree = ast.parse(_SRC)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == name)
    import textwrap
    body = textwrap.dedent(ast.get_source_segment(_SRC, fn))
    ns = {}
    exec(body, ns)
    return ns[name]


@pytest.fixture(scope="module")
def normalize():
    return _lift_function("_normalize_layout_kind")


class TestShortcutResolution:

    def test_raster_shortcut_resolves_to_one_and_raster(self, normalize):
        assert normalize("raster", None) == ("one", "raster")

    def test_scatter_shortcut_resolves_to_one_and_scatter(self, normalize):
        assert normalize("scatter", None) == ("one", "scatter")

    def test_shortcut_is_case_insensitive(self, normalize):
        assert normalize("Raster", None) == ("one", "raster")
        assert normalize("SCATTER", None) == ("one", "scatter")

    def test_matching_explicit_kind_is_not_a_conflict(self, normalize):
        """layout="scatter", kind="scatter" is redundant, not invalid."""
        assert normalize("scatter", "scatter") == ("one", "scatter")
        assert normalize("scatter", "Scatter") == ("one", "scatter")

    def test_conflicting_explicit_kind_raises(self, normalize):
        with pytest.raises(ValueError, match="conflicting"):
            normalize("scatter", "raster")
        with pytest.raises(ValueError, match="conflicting"):
            normalize("raster", "scatter")


class TestOrdinaryLayoutValues:

    @pytest.mark.parametrize("layout", ["one", "side", "over"])
    def test_ordinary_layout_passes_through_unchanged(self, normalize, layout):
        result_layout, _ = normalize(layout, None)
        assert result_layout == layout

    def test_ordinary_layout_is_case_insensitive(self, normalize):
        assert normalize("Side", None)[0] == "side"
        assert normalize("OVER", None)[0] == "over"

    def test_kind_defaults_to_raster_when_omitted(self, normalize):
        """Preserves today's exact default appearance for every
        ordinary layout value, not just "one"."""
        for layout in ("one", "side", "over"):
            assert normalize(layout, None) == (layout, "raster")

    def test_explicit_kind_honored_with_ordinary_layout(self, normalize):
        assert normalize("side", "scatter") == ("side", "scatter")
        assert normalize("over", "raster") == ("over", "raster")

    def test_explicit_kind_is_case_insensitive(self, normalize):
        assert normalize("one", "Scatter") == ("one", "scatter")


# ---------------------------------------------------------------------------
# Part 2: _build_panels' _slot_a_kind/_slot_b_kind wiring
# ---------------------------------------------------------------------------

def _lift_slot_kind_exprs():
    """Pull the ``_slot_a_kind``/``_slot_b_kind`` assignment expressions
    out of ``VisibilityPlotter._build_panels``.

    Returns compiled expressions evaluable given a namespace with
    ``self._kind`` set.
    """
    tree = ast.parse(_SRC)
    cls = next(n for n in ast.walk(tree)
               if isinstance(n, ast.ClassDef) and n.name == "VisibilityPlotter")
    fn = next(n for n in ast.walk(cls)
              if isinstance(n, ast.FunctionDef) and n.name == "_build_panels")

    exprs = {}
    for node in ast.walk(fn):
        if not isinstance(node, ast.Assign):
            continue
        target = node.targets[0]
        if isinstance(target, ast.Name) and target.id in ("_slot_a_kind", "_slot_b_kind"):
            exprs[target.id] = compile(ast.Expression(node.value), "<lifted>", "eval")

    assert set(exprs) == {"_slot_a_kind", "_slot_b_kind"}, (
        "expected exactly one assignment each to _slot_a_kind/_slot_b_kind "
        f"in _build_panels, found {set(exprs)}"
    )
    return exprs


@pytest.fixture(scope="module")
def slot_kind_exprs():
    return _lift_slot_kind_exprs()


def _eval_slot_kinds(exprs, kind):
    fake_self = types.SimpleNamespace(_kind=kind)
    ns = {"self": fake_self}
    return (eval(exprs["_slot_a_kind"], {}, ns),
            eval(exprs["_slot_b_kind"], {}, ns))


class TestSlotKindWiring:

    def test_raster_kind_gives_raster_a_scatter_b(self, slot_kind_exprs):
        assert _eval_slot_kinds(slot_kind_exprs, "raster") == ("raster", "scatter")

    def test_scatter_kind_gives_scatter_a_raster_b(self, slot_kind_exprs):
        """kind="scatter" starts the *scatter* panel in the primary
        position; slot B still takes the complementary kind, so duo
        mode's one-raster-one-scatter pairing survives."""
        assert _eval_slot_kinds(slot_kind_exprs, "scatter") == ("scatter", "raster")

    @pytest.mark.parametrize("kind", ["raster", "scatter"])
    def test_slots_are_always_complementary(self, slot_kind_exprs, kind):
        a, b = _eval_slot_kinds(slot_kind_exprs, kind)
        assert {a, b} == {"raster", "scatter"}
