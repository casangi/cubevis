"""
test_statistical_ui.py
========================
Tests for the live-GUI path to "statistical" scatter coloring (Part 6,
2026-09).  The backend (query_columns, ScatterLayerSpec, update_colorize)
already supported ``coloring="statistical"``, but nothing in the GUI
could select it: ``colorize_controls()``'s ``mode_group`` had only
Continuous/Categorical and ``buildColorizeArray()`` only ever emitted
``{coloring: 'categorical', ...}`` or ``null``.  This file covers the
pieces that close that gap:

1. ``colorize_controls()`` -- third "Statistical" option, initial state,
   hint visibility (the shipped ``mode_js`` is run under node).
2. ``buildColorizeArray()`` -- the shipped JS is extracted and run under
   node against fake handles.
3. ``_make_scatter_layers`` / the change-detection key pair.
4. The presets' colorize-mode snippets (zscore -> Statistical; the other
   three revert a lingering Statistical, leave Categorical alone).

Location in repository:
    cubevis/tests/manual/visplot/test_statistical_ui.py

Needs ``node`` on PATH (skips the JS tests otherwise).
"""
from __future__ import annotations

import json
import pathlib
import re
import shutil
import subprocess

import pytest

from cubevis.toolbox.visplot.axes import Axis
from cubevis.toolbox.visplot.visibility_scatter import (
    ScatterLayer, VisibilityScatter,
)
from cubevis.toolbox.visplot import visibility_plotter as vp

_SRC = pathlib.Path(vp.__file__).read_text()
_needs_node = pytest.mark.skipif(shutil.which("node") is None,
                                 reason="node not available")


def _node(js: str) -> str:
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True)
    assert out.returncode == 0, f"stdout={out.stdout}\nstderr={out.stderr}"
    return out.stdout.strip()


def _controls(coloring="continuous", **kw):
    """colorize_controls() on a bare scatter -- category enumeration is
    stubbed (it would otherwise query a backend)."""
    lyr = ScatterLayer(y_axis=Axis.AMPLITUDE, polarization="XX",
                       cmap=("#000000", "#ffffff"), coloring=coloring, **kw)
    sc = VisibilityScatter.__new__(VisibilityScatter)
    sc._layers = [lyr]
    sc._colorize_category_values = lambda axis, pol: ["a", "b"]
    return sc.colorize_controls(0)


# ---------------------------------------------------------------------------
# 1. colorize_controls()
# ---------------------------------------------------------------------------

class TestColorizeControlsStatisticalOption:
    def test_mode_group_offers_three_modes_in_order(self):
        _, h = _controls()
        assert list(h["mode_group"].labels) == ["Continuous", "Categorical", "Statistical"]

    @pytest.mark.parametrize("coloring,active", [
        ("continuous", 0),
        ("categorical", 1),
    ])
    def test_initial_active_matches_the_layers_coloring(self, coloring, active):
        kw = {"colorize_axis": Axis.SCAN} if coloring == "categorical" else {}
        _, h = _controls(coloring, **kw)
        assert h["mode_group"].active == active

    def test_statistical_layer_starts_on_statistical(self):
        _, h = _controls("statistical")
        assert h["mode_group"].active == 2

    def test_categorical_only_widgets_are_hidden_for_statistical(self):
        _, h = _controls("statistical")
        assert h["axis_select"].visible is False
        assert h["priority_select"].visible is False
        assert h["display_select"].visible is False
        assert all(not w.visible for _g, w in h["checklists"].values())

    def test_hint_is_visible_only_for_a_statistical_layer(self):
        for coloring, expect in (("continuous", False), ("statistical", True)):
            controls, _ = _controls(coloring)
            hints = [c for c in controls.children
                     if getattr(c, "text", "") and "Z-Score" in c.text]
            assert len(hints) == 1
            assert hints[0].visible is expect

    def test_existing_handles_contract_is_unchanged(self):
        _, h = _controls()
        assert {"mode_group", "axis_select", "priority_select",
                "display_select", "checklists"} <= set(h)

    @_needs_node
    def test_shipped_mode_js_toggles_widgets_for_every_mode(self):
        controls, h = _controls()
        cb = h["mode_group"].js_property_callbacks["change:active"][0]
        hint = next(c for c in controls.children
                    if getattr(c, "text", "") and "Z-Score" in c.text)
        # Map the CustomJS args (Bokeh models) onto plain JS stubs.
        names = list(cb.args)
        assert "statistical_hint" in names
        js = """
        const mk = () => ({visible: null, value: 'SCAN'});
        const axis_select = mk(), priority_select = mk(), display_select = mk();
        const statistical_hint = mk();
        const checklist_by_axis = {SCAN: mk(), FIELD: mk()};
        function run(active) {
            const cb_obj = {active: active};
            (function(){ %s }).call(null);
            return {hint: statistical_hint.visible, axis: axis_select.visible,
                    prio: priority_select.visible, disp: display_select.visible,
                    lists: Object.values(checklist_by_axis).map(w => w.visible)};
        }
        console.log(JSON.stringify([run(0), run(1), run(2)]));
        """ % cb.code
        cont, cat, stat = json.loads(_node(js))
        assert cont == {"hint": False, "axis": False, "prio": False,
                        "disp": False, "lists": [False, False]}
        assert cat["axis"] and cat["prio"] and cat["disp"] and not cat["hint"]
        assert cat["lists"] == [True, False]      # only the selected axis's list
        assert stat["hint"] is True
        assert not (stat["axis"] or stat["prio"] or stat["disp"])
        assert stat["lists"] == [False, False]


# ---------------------------------------------------------------------------
# 2. buildColorizeArray() -- the shipped JS
# ---------------------------------------------------------------------------

def _build_fn_js() -> str:
    start = _SRC.index("function buildColorizeArray(colorize_handles) {")
    end = _SRC.index("function buildPanelPayload(", start)
    return _SRC[start:end]


@_needs_node
class TestBuildColorizeArray:
    def _run(self, active_modes):
        handles = ",".join(
            "{mode_group:{active:%d}, axis_select:{value:'SCAN'},"
            " priority_select:{value:'rarest'}, display_select:{value:'hide'},"
            " checklists:{SCAN:[{tags:['1','2'], active:[0]}, null]}}" % a
            for a in active_modes)
        js = _build_fn_js() + "\nconsole.log(JSON.stringify(buildColorizeArray([%s])));" % handles
        return json.loads(_node(js))

    def test_continuous_is_still_null(self):
        assert self._run([0]) == [None]

    def test_categorical_payload_is_unchanged(self):
        out = self._run([1])[0]
        assert out == {"coloring": "categorical", "colorize_axis": "SCAN",
                       "excluded_categories": ["2"], "category_priority": "rarest",
                       "excluded_display": "hide"}

    def test_statistical_is_just_the_mode(self):
        assert self._run([2]) == [{"coloring": "statistical"}]

    def test_layers_are_independent(self):
        out = self._run([2, 0, 1])
        assert out[0] == {"coloring": "statistical"}
        assert out[1] is None and out[2]["coloring"] == "categorical"


# ---------------------------------------------------------------------------
# 3. Server side: _make_scatter_layers + change-detection keys
# ---------------------------------------------------------------------------

STAT = {"coloring": "statistical"}
CAT = {"coloring": "categorical", "colorize_axis": "SCAN"}


class TestMakeScatterLayersStatistical:
    def test_builds_a_statistical_layer(self):
        (lyr,) = vp._make_scatter_layers(Axis.AMPLITUDE, ["XX"], colorize_overrides=[STAT])
        assert lyr.coloring == "statistical"
        assert lyr.colorize_axis is None and lyr.excluded_categories == ()

    def test_defaults_to_threshold_scaling_at_the_literature_cutoff(self):
        (lyr,) = vp._make_scatter_layers(Axis.AMPLITUDE, ["XX"], colorize_overrides=[STAT])
        assert lyr.scaling == "threshold"
        assert lyr.scaling_vmin == 3.5 == vp._STATISTICAL_THRESHOLD_VMIN

    def test_other_modes_keep_their_default_scaling(self):
        cont, cat = vp._make_scatter_layers(
            Axis.AMPLITUDE, ["XX", "YY"], colorize_overrides=[None, CAT])
        default = ScatterLayer(y_axis=Axis.AMPLITUDE, polarization="XX", cmap=("#0", "#1")).scaling
        assert cont.scaling == cat.scaling == default
        assert cont.scaling_vmin is None and cat.scaling_vmin is None

    def test_scaling_does_not_leak_between_layers(self):
        """A statistical layer's scaling kwargs must not carry over to
        the next layer in the same call."""
        a, b = vp._make_scatter_layers(
            Axis.AMPLITUDE, ["XX", "YY"], colorize_overrides=[STAT, None])
        assert a.scaling == "threshold" and b.scaling != "threshold"
        assert b.scaling_vmin is None

    def test_uses_a_continuous_ramp_not_the_categorical_palette(self):
        (lyr,) = vp._make_scatter_layers(Axis.AMPLITUDE, ["XX"], colorize_overrides=[STAT])
        (cont,) = vp._make_scatter_layers(Axis.AMPLITUDE, ["XX"], colorize_overrides=[None])
        assert tuple(lyr.cmap) == tuple(cont.cmap)


class TestColorizeKeys:
    def test_statistical_override_key(self):
        assert vp._colorize_key_from_override(STAT) == ("statistical", None, (), None, None)

    @pytest.mark.parametrize("o", [None, STAT, CAT])
    def test_layer_and_override_keys_agree(self, o):
        """The invariant the module documents: if these ever disagree, a
        Plot press that changed nothing triggers a full re-render."""
        (lyr,) = vp._make_scatter_layers(Axis.AMPLITUDE, ["XX"], colorize_overrides=[o])
        assert vp._colorize_key_from_layer(lyr) == vp._colorize_key_from_override(o)

    def test_switching_mode_is_a_detected_change(self):
        keys = {vp._colorize_key_from_override(o) for o in (None, STAT, CAT)}
        assert len(keys) == 3

    def test_repeated_statistical_press_is_not_a_change(self):
        (lyr,) = vp._make_scatter_layers(Axis.AMPLITUDE, ["XX"], colorize_overrides=[STAT])
        assert vp._colorize_key_from_layer(lyr) == vp._colorize_key_from_override(STAT)


# ---------------------------------------------------------------------------
# 4. Preset colorize-mode snippets
# ---------------------------------------------------------------------------

def _preset_snippets():
    return re.findall(r'colorize_mode_js = """(.*?)"""', _SRC, flags=re.S)


@_needs_node
class TestPresetColorizeModeSnippets:
    def _apply(self, snippet, modes):
        handles = ",".join("{mode_group:{active:%d}}" % m for m in modes)
        js = ("const panel1_colorize_handles = [%s];\n%s\n"
              "console.log(JSON.stringify(panel1_colorize_handles.map(h => h.mode_group.active)));"
              % (handles, snippet))
        return json.loads(_node(js))

    def test_both_variants_exist(self):
        assert len(_preset_snippets()) == 2

    def test_zscore_sets_every_layer_to_statistical(self):
        zscore, _other = _preset_snippets()
        assert self._apply(zscore, [0, 1, 2]) == [2, 2, 2]

    def test_other_presets_revert_only_a_lingering_statistical(self):
        _zscore, other = _preset_snippets()
        assert self._apply(other, [0, 1, 2]) == [0, 1, 0]

    def test_a_missing_handles_array_never_raises(self):
        for snippet in _preset_snippets():
            _node(snippet)   # panel1_colorize_handles undefined -> caught

    def test_zscore_variant_is_selected_only_for_the_zscore_preset(self):
        assert re.search(
            r'if preset_name == "zscore":\s+colorize_mode_js = """\s*try \{\s*'
            r'panel1_colorize_handles\.forEach\(function\(h\) \{ h\.mode_group\.active = 2;',
            _SRC)

    def test_snippet_runs_before_doplot_is_called(self):
        assert "code=self._do_plot_js + colorize_mode_js + f" in _SRC
