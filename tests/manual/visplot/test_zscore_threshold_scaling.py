"""
test_zscore_threshold_scaling.py
==================================
Raster scaling behavior when the quantity changes (Part 6, 2026-09), now
implemented as PER-QUANTITY scaling memory (``scaling_memory.py``) instead of
the earlier Z-Score-only special case.

History that shaped these tests (each was a real, live-observed bug):
1. The eq_hist default made the Z-Score raster unreadable -> switching TO Z-Score
   applies threshold scaling (its first-visit default).
2. A separate comm message for that raced update_axes and hit
   ``AttributeError: 'NoneType' object has no attribute 'label'`` -> the settings
   are switched inside ``update_axes`` itself, in the same call that sets the
   quantity.
3. Leaving Z-Score left Amplitude threshold-scaled (solid yellow raster) -> each
   quantity keeps its OWN settings.
4. The first fix for (3) keyed on "the previous quantity was Z_SCORE" and never
   fired in the GUI, because ``VisibilityPlotter._handle_plot`` resets
   ``panel._quantity = None`` before calling ``update_axes``; its unit tests set
   the quantity directly and passed anyway -> switching now keys on
   ``_scaling_owner`` (which quantity the live settings belong to), and the
   ``TestRealPlotterCallPattern`` class drives ``update_axes`` the way the
   plotter does.

Location: cubevis/tests/manual/visplot/test_zscore_threshold_scaling.py
All synthetic (a bare VisibilityRaster with rendering stubbed out).
"""
from __future__ import annotations

import json

import pytest

from cubevis.toolbox.visplot.axes import Axis
from cubevis.toolbox.visplot.scaling_memory import (
    RasterScalingUnit, ScalingMemory,
)
from cubevis.toolbox.visplot.view_state import StateRegistry
from cubevis.toolbox.visplot.visibility_raster import VisibilityRaster


def _bare_raster(initial_quantity=Axis.AMPLITUDE, agg_is_set=True,
                 initial_scaling="eq_hist", initial_vmin=None, initial_vmax=None):
    """A VisibilityRaster with only what the scaling logic touches.  Mimics
    the constructor by calling ``_init_scaling_state()``."""
    r = VisibilityRaster.__new__(VisibilityRaster)
    r._quantity = initial_quantity
    r._polarization = "XX"
    r._y_dim, r._x_dim, r._title = Axis.BASELINE, Axis.TIME, None
    r._scaling = initial_scaling
    r._scaling_alpha, r._scaling_gamma = 10.0, 1.0
    r._scaling_vmin, r._scaling_vmax = initial_vmin, initial_vmax
    r._zscore_vmin_auto = False
    r._scaling_memory = ScalingMemory()
    r._scaling_owner = None
    r._agg = object() if agg_is_set else None
    r._selection = object()
    r._render = lambda sel: None
    r._notify_axes_changed = lambda: None
    r._reshade_calls = 0

    def _reshade():
        r._reshade_calls += 1
    r._reshade_image = _reshade
    r._init_scaling_state()
    return r


def _plotter_style_update(r, **kw):
    """Drive update_axes exactly as VisibilityPlotter._handle_plot() does for a
    raster whose axes changed: first RESET _y_dim/_x_dim/_quantity to None (a
    force-a-change trick), then call update_axes()."""
    r._y_dim = r._x_dim = r._quantity = None
    r.update_axes(**kw)


def _live(r):
    return (r._scaling, r._scaling_vmin, r._scaling_vmax)


# ---------------------------------------------------------------------------
# Switching TO Z-Score
# ---------------------------------------------------------------------------

class TestSwitchingToZScore:
    def test_first_visit_gets_threshold_at_the_literature_cutoff(self):
        r = _bare_raster(Axis.AMPLITUDE)
        r.update_axes(quantity=Axis.Z_SCORE)
        assert r._quantity == Axis.Z_SCORE
        assert (r._scaling, r._scaling_vmin) == ("threshold", 3.5)
        assert r._zscore_vmin_auto is True

    def test_live_bug_scenario_quantity_none_agg_set_does_not_raise(self):
        r = _bare_raster(Axis.AMPLITUDE, agg_is_set=True)
        _plotter_style_update(r, quantity=Axis.Z_SCORE)
        assert r._quantity == Axis.Z_SCORE and r._scaling == "threshold"

    def test_quantity_label_is_never_none_after_the_switch(self):
        """The exact attribute the live traceback died on."""
        r = _bare_raster(Axis.AMPLITUDE)
        _plotter_style_update(r, quantity=Axis.Z_SCORE)
        assert r._quantity.label == "Z-Score"

    def test_applies_however_z_score_was_reached(self):
        r = _bare_raster(Axis.PHASE)
        r.update_axes(quantity=Axis.Z_SCORE)     # hand-picked from the dropdown
        assert r._scaling == "threshold" and r._scaling_vmin == 3.5


# ---------------------------------------------------------------------------
# The general rule: per-quantity memory
# ---------------------------------------------------------------------------

class TestPerQuantityMemory:
    def test_returning_to_a_quantity_restores_its_settings(self):
        r = _bare_raster(Axis.AMPLITUDE, initial_scaling="log",
                         initial_vmin=2.0, initial_vmax=9.0)
        r.update_axes(quantity=Axis.PHASE)
        r.update_axes(quantity=Axis.AMPLITUDE)
        assert _live(r) == ("log", 2.0, 9.0)

    def test_a_new_quantity_does_not_inherit_the_old_ones_range(self):
        """The latent bug this generalizes away: an Amplitude range (say
        8-30) carried into Phase, whose values are degrees."""
        r = _bare_raster(Axis.AMPLITUDE, initial_scaling="eq_hist",
                         initial_vmin=8.1, initial_vmax=30.5)
        r.update_axes(quantity=Axis.PHASE)
        # Phase's own first-visit default (2026-10-07: linear over the
        # full circle, for the cyclic colormap) -- not Amplitude's range.
        assert _live(r) == ("linear", -180.0, 180.0)
        r.update_axes(quantity=Axis.REAL)
        assert _live(r) == ("eq_hist", None, None)

    def test_alpha_and_gamma_are_remembered_too(self):
        r = _bare_raster(Axis.AMPLITUDE, initial_scaling="gamma")
        r._scaling_alpha, r._scaling_gamma = 33.0, 2.2
        r.update_axes(quantity=Axis.PHASE)
        assert (r._scaling_alpha, r._scaling_gamma) == (10.0, 1.0)   # defaults
        r.update_axes(quantity=Axis.AMPLITUDE)
        assert (r._scaling_alpha, r._scaling_gamma) == (33.0, 2.2)

    def test_each_quantity_is_independent(self):
        r = _bare_raster(Axis.AMPLITUDE, initial_scaling="log")
        r.update_axes(quantity=Axis.PHASE)
        r._scaling = "sqrt"
        r.update_axes(quantity=Axis.REAL)
        r._scaling = "square"
        for q, expect in ((Axis.AMPLITUDE, "log"), (Axis.PHASE, "sqrt"),
                          (Axis.REAL, "square")):
            r.update_axes(quantity=q)
            assert r._scaling == expect

    def test_z_score_keeps_what_the_user_tuned_on_it(self):
        r = _bare_raster(Axis.AMPLITUDE)
        r.update_axes(quantity=Axis.Z_SCORE)
        r._scaling_vmin, r._zscore_vmin_auto = 6.0, False      # user drags the cutoff
        r.update_axes(quantity=Axis.AMPLITUDE)
        assert r._scaling == "eq_hist" and r._scaling_vmin is None
        r.update_axes(quantity=Axis.Z_SCORE)
        assert (r._scaling, r._scaling_vmin) == ("threshold", 6.0)
        assert r._zscore_vmin_auto is False                     # still the user's

    def test_the_automatic_cutoff_flag_is_remembered(self):
        r = _bare_raster(Axis.AMPLITUDE)
        r.update_axes(quantity=Axis.Z_SCORE)
        assert r._zscore_vmin_auto is True
        r.update_axes(quantity=Axis.AMPLITUDE)
        assert r._zscore_vmin_auto is False
        r.update_axes(quantity=Axis.Z_SCORE)
        assert r._zscore_vmin_auto is True

    def test_same_quantity_is_a_no_op(self):
        r = _bare_raster(Axis.AMPLITUDE, initial_scaling="log", initial_vmin=2.0)
        r.update_axes(quantity=Axis.AMPLITUDE)
        assert _live(r) == ("log", 2.0, None)

    def test_no_quantity_change_leaves_scaling_untouched(self):
        r = _bare_raster(Axis.Z_SCORE)
        r._scaling_vmin = 4.9
        r.update_axes(polarization="YY")
        assert r._scaling == "threshold" and r._scaling_vmin == 4.9


# ---------------------------------------------------------------------------
# The plotter's real call pattern (quantity reset to None first)
# ---------------------------------------------------------------------------

class TestRealPlotterCallPattern:
    def test_z_score_then_waterfall_leaves_amplitude_with_normal_scaling(self):
        """The exact live sequence: default raster -> Z-Score preset ->
        Waterfall preset. Must not leave Amplitude threshold-scaled."""
        r = _bare_raster(Axis.AMPLITUDE)
        _plotter_style_update(r, y_dim=Axis.BASELINE, x_dim=Axis.TIME,
                              quantity=Axis.Z_SCORE)
        assert r._scaling == "threshold"
        _plotter_style_update(r, y_dim=Axis.TIME, x_dim=Axis.CHANNEL,
                              quantity=Axis.AMPLITUDE)
        assert _live(r) == ("eq_hist", None, None) and r._zscore_vmin_auto is False

    def test_pressing_z_score_twice_keeps_the_original_amplitude_settings(self):
        r = _bare_raster(Axis.AMPLITUDE, initial_scaling="log", initial_vmin=2.0)
        _plotter_style_update(r, quantity=Axis.Z_SCORE)
        _plotter_style_update(r, quantity=Axis.Z_SCORE)      # again
        _plotter_style_update(r, quantity=Axis.AMPLITUDE)
        assert _live(r) == ("log", 2.0, None)

    def test_z_score_to_z_score_via_the_plotter_keeps_the_live_settings(self):
        r = _bare_raster(Axis.AMPLITUDE)
        _plotter_style_update(r, quantity=Axis.Z_SCORE)
        r._scaling_vmin = 4.9
        _plotter_style_update(r, quantity=Axis.Z_SCORE)
        assert r._scaling == "threshold" and r._scaling_vmin == 4.9

    def test_other_quantity_round_trip_via_the_plotter(self):
        r = _bare_raster(Axis.AMPLITUDE, initial_scaling="sqrt", initial_vmin=1.0)
        _plotter_style_update(r, quantity=Axis.REAL)
        assert _live(r) == ("eq_hist", None, None)
        _plotter_style_update(r, quantity=Axis.AMPLITUDE)
        assert _live(r) == ("sqrt", 1.0, None)

    def test_the_plotter_still_uses_the_reset_to_none_pattern(self):
        """This class assumes _handle_plot resets _quantity before
        update_axes; if that ever changes, revisit it rather than letting the
        tests drift from reality."""
        import pathlib
        from cubevis.toolbox.visplot import visibility_plotter as vp
        assert "panel._quantity = None" in pathlib.Path(vp.__file__).read_text()


# ---------------------------------------------------------------------------
# A panel constructed directly as Z_SCORE (e.g. preset="zscore")
# ---------------------------------------------------------------------------

class TestPanelBornAsZScore:
    def test_gets_threshold_scaling_like_a_later_switch_would(self):
        r = _bare_raster(Axis.Z_SCORE)
        assert (r._scaling, r._scaling_vmin) == ("threshold", 3.5)
        assert r._zscore_vmin_auto is True and r._scaling_owner == Axis.Z_SCORE

    def test_leaving_it_gives_the_class_default(self):
        r = _bare_raster(Axis.Z_SCORE)
        _plotter_style_update(r, quantity=Axis.AMPLITUDE)
        assert _live(r) == ("eq_hist", None, None)

    def test_an_explicit_scaling_argument_is_respected(self):
        r = _bare_raster(Axis.Z_SCORE, initial_scaling="log")
        assert (r._scaling, r._scaling_vmin) == ("log", None)
        assert r._zscore_vmin_auto is False

    def test_a_non_z_score_panel_is_untouched(self):
        r = _bare_raster(Axis.AMPLITUDE)
        assert _live(r) == ("eq_hist", None, None) and r._scaling_owner == Axis.AMPLITUDE


# ---------------------------------------------------------------------------
# Capture / apply on the raster
# ---------------------------------------------------------------------------

class TestCaptureApply:
    def _tuned(self):
        r = _bare_raster(Axis.AMPLITUDE, initial_scaling="log",
                         initial_vmin=2.0, initial_vmax=9.0)
        r.update_axes(quantity=Axis.Z_SCORE)
        r._scaling_vmin, r._zscore_vmin_auto = 6.0, False
        return r

    def test_capture_is_json_serializable(self):
        state = self._tuned().capture_scaling_state()
        assert json.loads(json.dumps(state)) == state

    def test_capture_includes_the_live_settings_of_the_current_owner(self):
        state = self._tuned().capture_scaling_state()
        assert state["owner"] == "Z_SCORE"
        assert state["by_quantity"]["Z_SCORE"]["vmin"] == 6.0          # live
        assert state["by_quantity"]["AMPLITUDE"]["scaling"] == "log"   # remembered

    def test_capture_does_not_mutate_the_panel(self):
        r = self._tuned()
        before = (_live(r), r._scaling_memory.to_dict(), r._scaling_owner)
        r.capture_scaling_state()
        assert (_live(r), r._scaling_memory.to_dict(), r._scaling_owner) == before

    def test_round_trip_to_a_fresh_panel_on_the_same_quantity(self):
        state = self._tuned().capture_scaling_state()
        fresh = _bare_raster(Axis.Z_SCORE)
        fresh.apply_scaling_state(state)
        assert (fresh._scaling, fresh._scaling_vmin) == ("threshold", 6.0)
        assert fresh._zscore_vmin_auto is False
        fresh.update_axes(quantity=Axis.AMPLITUDE)                     # memory came along
        assert _live(fresh) == ("log", 2.0, 9.0)

    def test_apply_loads_the_settings_of_the_CURRENT_quantity_not_the_saved_owner(self):
        state = self._tuned().capture_scaling_state()                  # saved owner: Z_SCORE
        other = _bare_raster(Axis.AMPLITUDE)
        other.apply_scaling_state(state)
        assert _live(other) == ("log", 2.0, 9.0)                       # Amplitude's entry

    def test_apply_with_nothing_saved_for_the_current_quantity_keeps_live(self):
        other = _bare_raster(Axis.PHASE, initial_scaling="sqrt")
        other.apply_scaling_state(self._tuned().capture_scaling_state())
        assert other._scaling == "sqrt"

    def test_apply_re_shades(self):
        r = _bare_raster(Axis.AMPLITUDE)
        r.apply_scaling_state({"by_quantity": {}})
        assert r._reshade_calls == 1

    def test_apply_tolerates_unusable_entries(self):
        r = _bare_raster(Axis.AMPLITUDE)
        r.apply_scaling_state({"by_quantity": {
            "AMPLITUDE": {"scaling": "from_a_newer_build"},
            "PHASE": {"scaling": "log", "vmin": 1.0}}})
        assert r._scaling == "eq_hist"                                 # bad entry dropped
        r.update_axes(quantity=Axis.PHASE)
        assert (r._scaling, r._scaling_vmin) == ("log", 1.0)

    def test_apply_empty_state_is_harmless(self):
        r = _bare_raster(Axis.AMPLITUDE, initial_scaling="log")
        r.apply_scaling_state({})
        assert r._scaling == "log"


# ---------------------------------------------------------------------------
# As a registered unit
# ---------------------------------------------------------------------------

class TestRegisteredUnit:
    def _two_panels(self):
        a = _bare_raster(Axis.AMPLITUDE, initial_scaling="log")
        b = _bare_raster(Axis.PHASE, initial_scaling="sqrt")
        reg = StateRegistry()
        reg.register(RasterScalingUnit(a, "panel.A.raster.scaling"))
        reg.register(RasterScalingUnit(b, "panel.B.raster.scaling"))
        return a, b, reg

    def test_round_trip_through_a_registry_with_two_panels(self):
        a, b, reg = self._two_panels()
        env = reg.capture()
        a._scaling, b._scaling = "eq_hist", "eq_hist"
        report = reg.apply(env)
        assert report.ok and len(report.applied) == 2
        assert a._scaling == "log" and b._scaling == "sqrt"

    def test_one_panel_can_be_restored_without_the_other(self):
        a, b, reg = self._two_panels()
        env = reg.capture()
        a._scaling = b._scaling = "eq_hist"
        reg.apply(env, prefix="panel.A")
        assert a._scaling == "log" and b._scaling == "eq_hist"

    def test_declares_the_display_scope(self):
        a, _b, reg = self._two_panels()
        assert reg.keys(scopes={"display"}) == ["panel.A.raster.scaling",
                                                "panel.B.raster.scaling"]
        assert reg.capture(exclude_scopes={"display"})["units"] == {}

    def test_the_plotter_registers_one_unit_per_slot(self):
        """Source-level check (a live plotter needs a backend): the plotter
        must register a RasterScalingUnit per slot under
        panel.<id>.raster.scaling."""
        import pathlib
        from cubevis.toolbox.visplot import visibility_plotter as vp
        src = pathlib.Path(vp.__file__).read_text()
        assert "self._view_state = StateRegistry()" in src
        assert "RasterScalingUnit(" in src and 'f"panel.{_slot.id}.raster.scaling"' in src
