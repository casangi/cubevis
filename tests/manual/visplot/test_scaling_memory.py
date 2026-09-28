"""
test_scaling_memory.py
========================
Tests for the pure pieces of ``scaling_memory.py``: ``ScalingSettings``,
``ScalingMemory`` and the per-quantity first-visit defaults (Part 6 follow-up,
2026-09).  The behavior on a live raster panel is in
``test_zscore_threshold_scaling.py``.

Location: cubevis/tests/manual/visplot/test_scaling_memory.py
"""
from __future__ import annotations

import json

import pytest

from cubevis.toolbox.visplot.axes import Axis
from cubevis.toolbox.visplot.scaling_memory import (
    DEFAULT_ALPHA, DEFAULT_GAMMA, DEFAULT_SCALING, ScalingMemory, ScalingSettings,
    default_scaling_settings,
)


class TestScalingSettings:
    def test_defaults(self):
        s = ScalingSettings()
        assert (s.scaling, s.alpha, s.gamma, s.vmin, s.vmax, s.auto_cutoff) == \
            (DEFAULT_SCALING, DEFAULT_ALPHA, DEFAULT_GAMMA, None, None, False)

    def test_is_immutable(self):
        with pytest.raises(Exception):
            ScalingSettings().scaling = "log"

    def test_round_trip(self):
        s = ScalingSettings("threshold", 3.0, 1.5, 4.9, 30.0, True)
        assert ScalingSettings.from_dict(s.to_dict()) == s

    def test_to_dict_is_json_serializable(self):
        d = ScalingSettings("log", vmin=1.0).to_dict()
        assert json.loads(json.dumps(d)) == d

    def test_from_dict_fills_missing_fields_with_defaults(self):
        s = ScalingSettings.from_dict({"scaling": "sqrt"})
        assert s == ScalingSettings(scaling="sqrt")

    def test_from_dict_coerces_numbers(self):
        s = ScalingSettings.from_dict({"scaling": "log", "vmin": "2", "vmax": 9, "alpha": "7"})
        assert (s.vmin, s.vmax, s.alpha) == (2.0, 9.0, 7.0)

    def test_from_dict_rejects_an_unknown_scaling(self):
        with pytest.raises(ValueError, match="unknown scaling"):
            ScalingSettings.from_dict({"scaling": "from_a_newer_build"})


class TestDefaults:
    def test_z_score_is_threshold_at_the_literature_cutoff_and_automatic(self):
        s = default_scaling_settings(Axis.Z_SCORE)
        assert (s.scaling, s.vmin, s.vmax, s.auto_cutoff) == ("threshold", 3.5, None, True)

    @pytest.mark.parametrize("q", [Axis.AMPLITUDE, Axis.PHASE, Axis.REAL,
                                   Axis.IMAGINARY, Axis.FLAG])
    def test_every_other_quantity_gets_the_class_default(self, q):
        """Deliberate: changing how Phase/Flag look is a visual decision, not
        a side effect. _QUANTITY_DEFAULTS is where to do it later."""
        assert default_scaling_settings(q) == ScalingSettings()

    def test_the_panels_own_alpha_and_gamma_are_kept(self):
        s = default_scaling_settings(Axis.Z_SCORE, alpha=42.0, gamma=2.0)
        assert (s.alpha, s.gamma) == (42.0, 2.0)
        s = default_scaling_settings(Axis.PHASE, alpha=42.0, gamma=2.0)
        assert (s.alpha, s.gamma) == (42.0, 2.0)


class TestScalingMemory:
    def test_remember_and_recall(self):
        m = ScalingMemory()
        s = ScalingSettings("log", vmin=1.0)
        m.remember("AMPLITUDE", s)
        assert m.recall("AMPLITUDE") == s and m.recall("PHASE") is None
        assert m.names() == ["AMPLITUDE"] and len(m) == 1

    def test_remember_overwrites(self):
        m = ScalingMemory()
        m.remember("A", ScalingSettings("log"))
        m.remember("A", ScalingSettings("sqrt"))
        assert m.recall("A").scaling == "sqrt" and len(m) == 1

    def test_forget_one_and_all(self):
        m = ScalingMemory()
        m.remember("A", ScalingSettings())
        m.remember("B", ScalingSettings())
        m.forget("A")
        assert m.names() == ["B"]
        m.forget()
        assert len(m) == 0

    def test_round_trip_through_json(self):
        m = ScalingMemory()
        m.remember("AMPLITUDE", ScalingSettings("log", vmin=1.0, vmax=9.0))
        m.remember("Z_SCORE", ScalingSettings("threshold", vmin=4.9, auto_cutoff=True))
        again = ScalingMemory.from_dict(json.loads(json.dumps(m.to_dict())))
        assert again.to_dict() == m.to_dict()

    def test_from_dict_drops_unusable_entries_and_keeps_the_rest(self):
        m = ScalingMemory.from_dict({
            "AMPLITUDE": {"scaling": "no_such_scaling"},
            "PHASE": {"scaling": "log"},
            "BROKEN": "not a dict",
            "ALSO": None,
        })
        assert m.names() == ["PHASE"]

    def test_from_dict_none_and_empty(self):
        assert len(ScalingMemory.from_dict(None)) == 0
        assert len(ScalingMemory.from_dict({})) == 0

    def test_a_copy_via_dict_is_independent(self):
        m = ScalingMemory()
        m.remember("A", ScalingSettings("log"))
        c = ScalingMemory.from_dict(m.to_dict())
        c.remember("A", ScalingSettings("sqrt"))
        assert m.recall("A").scaling == "log"
