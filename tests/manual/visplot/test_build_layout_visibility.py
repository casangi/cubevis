"""
test_build_layout_visibility.py
================================
Regression test for the "layout='one' shows a blank plot area" bug.

Why this module exists
-----------------------
``VisibilityPlotter._build_layout`` calls ``_build_plot_area()``, which
already computes the *correct* initial visibility for ``side_container``
(``self._layout in ("one", "side")``, matching ``layout_js``'s runtime
rule "covers both 'one' and 'side'"). ``_build_layout`` then immediately
overwrote that with ``self._layout == "side"`` only -- dropping the
``"one"`` case. With ``layout="one"``, that left *both*
``side_container`` and ``over_container`` invisible at page load: no
container in the document was ever shown, so the plot area rendered
empty even though the panels themselves were built and populated
correctly.

This is purely a container-visibility computation, independent of any
Bokeh figure, MS backend, or datashader pipeline, so it is checked here
by lifting just the two assignment expressions out of ``_build_layout``
by AST and evaluating them against a stub ``self`` -- no MS, no Bokeh,
no display, following the same lifting approach as
``test_spw_selection.py`` / ``test_visibility_plotter_iteration.py``
for this module's heavyweight (bokeh/websockets/xarray-ms) imports.

Test location
-------------
``cubevis/tests/manual/visplot/test_build_layout_visibility.py``
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


def _lift_initial_visibility_exprs():
    """Pull the ``side_container.visible`` / ``over_container.visible``
    assignment expressions out of ``VisibilityPlotter._build_layout``.

    Returns a dict mapping container name -> compiled expression, each
    evaluable given a namespace with a ``self`` object exposing
    ``_layout``.
    """
    src = pathlib.Path(_PKG / "visibility_plotter.py").read_text()
    tree = ast.parse(src)

    cls = next(n for n in ast.walk(tree)
               if isinstance(n, ast.ClassDef) and n.name == "VisibilityPlotter")
    fn = next(n for n in ast.walk(cls)
              if isinstance(n, ast.FunctionDef) and n.name == "_build_layout")

    exprs = {}
    for node in ast.walk(fn):
        if not isinstance(node, ast.Assign):
            continue
        target = node.targets[0]
        if (isinstance(target, ast.Attribute) and target.attr == "visible"
                and isinstance(target.value, ast.Name)
                and target.value.id in ("side_container", "over_container")):
            exprs[target.value.id] = compile(
                ast.Expression(node.value), "<lifted>", "eval")

    assert set(exprs) == {"side_container", "over_container"}, (
        "expected exactly one initial-visibility assignment for each of "
        f"side_container/over_container in _build_layout, found {set(exprs)}"
    )
    return exprs


@pytest.fixture(scope="module")
def visibility_exprs():
    return _lift_initial_visibility_exprs()


def _eval(expr, layout):
    fake_self = types.SimpleNamespace(_layout=layout)
    return eval(expr, {}, {"self": fake_self})


class TestInitialContainerVisibility:
    """For every valid ``layout`` value, exactly one container must be
    visible at page load -- an all-hidden state is the blank-plot bug."""

    @pytest.mark.parametrize("layout", ["one", "side", "over"])
    def test_exactly_one_container_visible(self, visibility_exprs, layout):
        side_visible = _eval(visibility_exprs["side_container"], layout)
        over_visible = _eval(visibility_exprs["over_container"], layout)
        assert side_visible or over_visible, (
            f"layout={layout!r} leaves both containers hidden -- "
            "this is the blank-plot-area bug"
        )
        assert not (side_visible and over_visible)

    def test_one_reuses_side_container(self, visibility_exprs):
        """'one' has no dedicated container (see _build_plot_area's own
        comment): it must render inside side_container, matching
        layout_js's runtime rule ``side_container.visible = !over``."""
        assert _eval(visibility_exprs["side_container"], "one") is True
        assert _eval(visibility_exprs["over_container"], "one") is False

    def test_side_shows_side_container_only(self, visibility_exprs):
        assert _eval(visibility_exprs["side_container"], "side") is True
        assert _eval(visibility_exprs["over_container"], "side") is False

    def test_over_shows_over_container_only(self, visibility_exprs):
        assert _eval(visibility_exprs["side_container"], "over") is False
        assert _eval(visibility_exprs["over_container"], "over") is True
