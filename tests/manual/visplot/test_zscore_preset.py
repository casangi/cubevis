"""
test_zscore_preset.py
=======================
Tests for the ``"zscore"`` preset (Part 6, 2026-09;
visplot-colorize-by-axis-design.md's own "bad antenna" workflow): a
Baseline vs Time raster colored by Z-Score (spot an outlier baseline at
a glance) alongside a plain Amplitude vs Time scatter (confirm what's
there), the same axis shape as the existing ``"vplot"`` preset with the
raster's quantity swapped from Amplitude to Z_SCORE.

Deliberately does NOT also color the scatter panel by Z-Score
("statistical" mode) -- there is currently no client-side UI path to
select that coloring mode at all (``buildColorizeArray()`` only ever
emits ``{coloring: 'categorical', ...}`` or ``null``, confirmed directly
against the shipped JS, not assumed). That's a distinct, larger, later
enhancement (a new ``mode_group`` option), not something this preset
alone should scope-creep into building.

Threshold scaling (added after a live screenshot showed the raster's
default eq_hist scaling made a real outlier hard to distinguish from
ordinary baselines): NOT covered in this file. It lives in
``VisibilityRaster.update_axes()`` itself (set alongside ``self._quantity``
in the same call, whenever the new quantity is ``Z_SCORE``), not in
anything preset-specific here -- see ``test_zscore_threshold_scaling.py``
for that fix's own tests, including the real, live-observed bug an
earlier version of this fix (sending a separate comm message from the
preset's own JS) caused and that module's own account of why that
approach was abandoned.

Why this module exists
-----------------------
Same lifting approach as ``test_layout_kind_shortcut.py`` (see that
file's own rationale) -- ``visibility_plotter.py`` pulls in bokeh/
websockets/xarray-ms at module scope, so pure data (``_PRESETS``) and
snippets (the constructor-time preset resolution in ``_resolve_config``)
are lifted and evaluated directly rather than constructing a live
plotter.

Location in repository:
    cubevis/tests/manual/visplot/test_zscore_preset.py
"""
from __future__ import annotations

import ast
import pathlib
import re
import textwrap
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

from cubevis.toolbox.visplot.axes import Axis  # noqa: E402


# ---------------------------------------------------------------------------
# 1. _PRESETS["zscore"] -- pure data, lifted and evaluated directly
# ---------------------------------------------------------------------------

def _lift_presets_dict():
    start = _SRC.index("_PRESETS = {")
    end = _SRC.index("\n}\n", start) + 3
    ns = {"Axis": Axis}
    exec(_SRC[start:end], ns)
    return ns["_PRESETS"]


@pytest.fixture(scope="module")
def presets():
    return _lift_presets_dict()


class TestPresetsDict:
    def test_zscore_preset_exists_with_the_expected_shape(self, presets):
        assert presets["zscore"] == (
            Axis.BASELINE, Axis.TIME, Axis.Z_SCORE,
            Axis.TIME, Axis.AMPLITUDE,
            "side",
        )

    def test_zscore_matches_vplots_axis_shape_except_the_raster_quantity(self, presets):
        """The whole point: same combination as vplot (an already-
        established, familiar layout), just the raster's own quantity
        swapped to Z_SCORE."""
        vplot = presets["vplot"]
        zscore = presets["zscore"]
        assert zscore[0] == vplot[0]   # raster_y
        assert zscore[1] == vplot[1]   # raster_x
        assert zscore[2] == Axis.Z_SCORE and vplot[2] == Axis.AMPLITUDE
        assert zscore[3] == vplot[3]   # scatter_x
        assert zscore[4] == vplot[4]   # scatter_y
        assert zscore[5] == vplot[5]   # layout

    def test_existing_presets_unaffected(self, presets):
        assert presets["vplot"] == (
            Axis.BASELINE, Axis.TIME, Axis.AMPLITUDE,
            Axis.TIME, Axis.AMPLITUDE, "side",
        )
        assert presets["radplot"] == (
            Axis.BASELINE, Axis.TIME, Axis.AMPLITUDE,
            Axis.UVDIST, Axis.AMPLITUDE, "side",
        )
        assert presets["waterfall"] == (
            Axis.TIME, Axis.CHANNEL, Axis.AMPLITUDE,
            Axis.TIME, Axis.AMPLITUDE, "over",
        )


# ---------------------------------------------------------------------------
# 2. The constructor-time preset resolution in _resolve_config
# ---------------------------------------------------------------------------

def _lift_preset_resolution_expr():
    """Pulls the ``if self._preset and self._preset in _PRESETS: ...``
    block out of ``VisibilityPlotter._resolve_config`` -- same AST-
    lifting approach as ``test_layout_kind_shortcut.py``'s own
    ``_lift_slot_kind_exprs``.
    """
    tree = ast.parse(_SRC)
    cls = next(n for n in ast.walk(tree)
              if isinstance(n, ast.ClassDef) and n.name == "VisibilityPlotter")
    fn = next(n for n in ast.walk(cls)
             if isinstance(n, ast.FunctionDef) and n.name == "_resolve_config")
    for node in ast.walk(fn):
        if (isinstance(node, ast.If)
                and isinstance(node.test, ast.BoolOp)
                and any(isinstance(v, ast.Compare) and
                       any(isinstance(c, ast.Name) and c.id == "_PRESETS"
                           for c in ast.walk(v))
                       for v in node.test.values)):
            return textwrap.dedent(ast.get_source_segment(_SRC, node))
    raise RuntimeError("could not find the preset-resolution if-block in _resolve_config")


@pytest.fixture(scope="module")
def resolve_preset():
    block_src = _lift_preset_resolution_expr()
    presets = _lift_presets_dict()

    def _run(preset_name):
        fake_self = types.SimpleNamespace(_preset=preset_name, _layout="one")
        ns = {"self": fake_self, "_PRESETS": presets}
        exec(block_src, ns)
        return {
            "raster_y": ns.get("_resolved_raster_y"),
            "raster_x": ns.get("_resolved_raster_x"),
            "raster_qty": ns.get("_resolved_raster_qty"),
            "scatter_x": ns.get("_resolved_scatter_x"),
            "scatter_y": ns.get("_resolved_scatter_y"),
            "layout": fake_self._layout,
        }
    return _run


class TestConstructorTimePresetResolution:
    def test_zscore_preset_resolves_all_six_values(self, resolve_preset):
        resolved = resolve_preset("zscore")
        assert resolved == {
            "raster_y": Axis.BASELINE,
            "raster_x": Axis.TIME,
            "raster_qty": Axis.Z_SCORE,
            "scatter_x": Axis.TIME,
            "scatter_y": Axis.AMPLITUDE,
            "layout": "side",
        }

    def test_unknown_preset_name_resolves_nothing(self, resolve_preset):
        """self._preset not in _PRESETS -- the if-block's own guard --
        must not raise, just leave the resolved_* names unset."""
        resolved = resolve_preset("not_a_real_preset")
        assert resolved["layout"] == "one"   # untouched, still the fake default
        assert resolved["raster_y"] is None

    def test_none_preset_resolves_nothing(self, resolve_preset):
        resolved = resolve_preset(None)
        assert resolved["layout"] == "one"


# ---------------------------------------------------------------------------
# 3. The live preset button's generated JS actually sets Z_SCORE
# ---------------------------------------------------------------------------

class TestPresetJsSubstitution:
    """_preset_js is a closure inside _build_toolbar (needs layout_rbg,
    _pos0_slot, etc. from that method's own locals), so it can't be
    called directly without a live plotter -- confirmed instead by
    reconstructing the exact f-string substitution
    _preset_js("zscore") performs, using the real _PRESETS entry, and
    checking the result names Z_SCORE where a raster-quantity assignment
    is written.
    """

    def test_the_shipped_template_assigns_rq_sel_from_the_preset_tuple(self, presets):
        template_src = _SRC[_SRC.index("def _preset_js("):_SRC.index("self._preset_js_objects = [")]
        assert "panel0_rq_sel.value = '{rq.name}'" in template_src, (
            "the preset-button template no longer assigns the raster "
            "quantity select from the preset tuple's rq element -- this "
            "test's own substitution check below would be meaningless "
            "if that assignment moved or was renamed"
        )
        ry, rx, rq, sx, sy, pl = presets["zscore"]
        assert rq.name == "Z_SCORE"
        rendered = f"panel0_rq_sel.value = '{rq.name}';"
        assert rendered == "panel0_rq_sel.value = 'Z_SCORE';"

    def test_raster_quantity_dropdown_actually_offers_z_score(self):
        """The assignment above is meaningless if the dropdown itself
        has no such option -- confirmed directly against
        _RASTER_QTY_OPTIONS, not assumed from the raster backend work
        alone."""
        start = _SRC.index("_RASTER_QTY_OPTIONS = [")
        end = _SRC.index("]", start) + 1
        ns = {}
        exec(f"_RASTER_QTY_OPTIONS = {_SRC[start + len('_RASTER_QTY_OPTIONS = '):end]}", ns)
        values = [v for v, _label in ns["_RASTER_QTY_OPTIONS"]]
        assert "Z_SCORE" in values


# ---------------------------------------------------------------------------
# 4. The button and its wiring exist (structural, source-level)
# ---------------------------------------------------------------------------

class TestZscoreButtonWiring:
    def test_button_is_constructed(self):
        assert re.search(r'zscore_btn\s*=\s*Button\(', _SRC)

    def test_button_is_wired_to_a_preset_js_object(self):
        assert "zscore_btn.js_on_click(self._preset_js_objects[3])" in _SRC

    def test_preset_js_objects_list_includes_zscore_in_position_3(self):
        start = _SRC.index("self._preset_js_objects = [")
        end = _SRC.index("]", start) + 1
        block = _SRC[start:end]
        entries = [l.strip().rstrip(",") for l in block.splitlines()[1:-1]]
        assert entries[3] == '_preset_js("zscore")', entries

    def test_button_appears_in_the_toolbar_row_with_a_tooltip(self):
        assert re.search(r"Tip\(zscore_btn,\s*tooltip=", _SRC)

