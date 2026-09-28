"""
test_view_state.py
====================
Tests for ``view_state.py``: the generic save/restore registry (Part 6
follow-up, 2026-09).  Pure Python, no Bokeh, no backends.

Location: cubevis/tests/manual/visplot/test_view_state.py
"""
from __future__ import annotations

import json

import pytest

from cubevis.toolbox.visplot.view_state import (
    ApplyReport, CallableUnit, FORMAT, SCHEMA, StateRegistry,
)


class Box:
    """A trivial piece of 'GUI state' for tests."""
    def __init__(self, value=None):
        self.value = value


def unit(box, key, *, version=1, order=100, scopes=(), migrate=None, fail_on_apply=False):
    def apply(state):
        if fail_on_apply:
            raise RuntimeError("boom")
        box.value = state["value"]
    return CallableUnit(key, lambda: {"value": box.value}, apply,
                        version=version, order=order, scopes=scopes, migrate=migrate)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

class TestRegistration:
    def test_register_and_lookup(self):
        reg = StateRegistry()
        u = reg.register(unit(Box(1), "a.b"))
        assert reg.get("a.b") is u and "a.b" in reg and len(reg) == 1

    def test_duplicate_key_is_an_error(self):
        reg = StateRegistry()
        reg.register(unit(Box(1), "a"))
        with pytest.raises(ValueError, match="already registered"):
            reg.register(unit(Box(2), "a"))

    def test_replace_swaps_the_unit(self):
        reg = StateRegistry()
        reg.register(unit(Box(1), "a"))
        new = reg.register(unit(Box(2), "a"), replace=True)
        assert reg.get("a") is new

    @pytest.mark.parametrize("bad", ["", None, 5])
    def test_a_key_is_required(self, bad):
        with pytest.raises(ValueError):
            StateRegistry().register(unit(Box(), bad))

    def test_unregister(self):
        reg = StateRegistry()
        reg.register(unit(Box(), "a"))
        assert reg.unregister("a") is True and reg.unregister("a") is False

    def test_unregister_prefix_removes_a_whole_subtree_only(self):
        reg = StateRegistry()
        for k in ("panel.A.raster.scaling", "panel.A.raster.axes", "panel.AB.x", "panel.B.y"):
            reg.register(unit(Box(), k))
        assert reg.unregister_prefix("panel.A") == 2
        assert reg.keys() == ["panel.AB.x", "panel.B.y"]      # "panel.AB" is not under "panel.A"


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------

class TestCapture:
    def test_envelope_shape(self):
        reg = StateRegistry()
        reg.register(unit(Box(7), "a", version=3))
        env = reg.capture()
        assert env == {"format": FORMAT, "schema": SCHEMA,
                       "units": {"a": {"version": 3, "state": {"value": 7}}}}

    def test_empty_registry_captures_an_empty_envelope(self):
        assert StateRegistry().capture()["units"] == {}

    def test_select_by_explicit_keys(self):
        reg = StateRegistry()
        for k in "abc":
            reg.register(unit(Box(k), k))
        assert set(reg.capture(["a", "c"])["units"]) == {"a", "c"}

    def test_select_by_prefix(self):
        reg = StateRegistry()
        for k in ("panel.A.x", "panel.A.y", "panel.B.x", "global.z"):
            reg.register(unit(Box(), k))
        assert set(reg.capture(prefix="panel.A")["units"]) == {"panel.A.x", "panel.A.y"}

    def test_select_by_scope_and_exclude_scope(self):
        reg = StateRegistry()
        reg.register(unit(Box(), "disp", scopes={"display"}))
        reg.register(unit(Box(), "flags", scopes={"data"}))
        reg.register(unit(Box(), "both", scopes={"display", "data"}))
        assert set(reg.capture(scopes={"display"})["units"]) == {"disp", "both"}
        assert set(reg.capture(exclude_scopes={"data"})["units"]) == {"disp"}   # "without data"

    def test_selectors_combine_with_and(self):
        reg = StateRegistry()
        reg.register(unit(Box(), "panel.A.d", scopes={"display"}))
        reg.register(unit(Box(), "panel.A.f", scopes={"data"}))
        reg.register(unit(Box(), "panel.B.d", scopes={"display"}))
        env = reg.capture(prefix="panel.A", scopes={"display"})
        assert set(env["units"]) == {"panel.A.d"}

    def test_keys_are_listed_in_apply_order(self):
        reg = StateRegistry()
        reg.register(unit(Box(), "z", order=10))
        reg.register(unit(Box(), "a", order=50))
        reg.register(unit(Box(), "m", order=10))
        assert reg.keys() == ["m", "z", "a"]           # by order, then key

    def test_non_json_state_fails_at_capture_naming_the_unit(self):
        reg = StateRegistry()
        reg.register(CallableUnit("bad", lambda: {"x": object()}, lambda s: None))
        with pytest.raises(TypeError, match="'bad'"):
            reg.capture()

    def test_a_unit_that_raises_on_capture_propagates(self):
        def boom():
            raise RuntimeError("capture bug")
        reg = StateRegistry()
        reg.register(CallableUnit("x", boom, lambda s: None))
        with pytest.raises(RuntimeError, match="capture bug"):
            reg.capture()


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------

class TestApply:
    def test_round_trip(self):
        a, b = Box(1), Box("x")
        reg = StateRegistry()
        reg.register(unit(a, "a"))
        reg.register(unit(b, "b"))
        env = reg.capture()
        a.value, b.value = None, None
        report = reg.apply(env)
        assert (a.value, b.value) == (1, "x") and report.ok
        assert sorted(report.applied) == ["a", "b"]

    def test_applies_in_order(self):
        seen = []
        reg = StateRegistry()
        for key, order in (("late", 90), ("early", 10), ("mid", 50)):
            reg.register(CallableUnit(key, lambda: {"value": 0},
                                      lambda s, k=key: seen.append(k), order=order))
        reg.apply(reg.capture())
        assert seen == ["early", "mid", "late"]

    def test_one_failing_unit_does_not_block_the_rest(self):
        a, c = Box(1), Box(3)
        reg = StateRegistry()
        reg.register(unit(a, "a", order=10))
        reg.register(unit(Box(2), "b", order=20, fail_on_apply=True))
        reg.register(unit(c, "c", order=30))
        env = reg.capture()
        a.value = c.value = None
        report = reg.apply(env)
        assert (a.value, c.value) == (1, 3)
        assert not report.ok and [k for k, _ in report.failed] == ["b"]
        assert isinstance(report.failed[0][1], RuntimeError)
        assert report.applied == ["a", "c"]

    def test_keyboard_interrupt_is_not_swallowed(self):
        def interrupt(s):
            raise KeyboardInterrupt
        reg = StateRegistry()
        reg.register(CallableUnit("k", lambda: {"v": 1}, interrupt))
        with pytest.raises(KeyboardInterrupt):
            reg.apply(reg.capture())

    def test_unknown_keys_in_the_envelope_are_skipped_not_errors(self):
        reg = StateRegistry()
        reg.register(unit(Box(1), "a"))
        env = reg.capture()
        env["units"]["panel.Z.gone"] = {"version": 1, "state": {"value": 9}}
        report = reg.apply(env)
        assert report.ok and ("panel.Z.gone", "no such unit registered") in report.skipped

    def test_a_registered_unit_missing_from_the_envelope_is_left_alone(self):
        a, b = Box(1), Box(2)
        reg = StateRegistry()
        reg.register(unit(a, "a"))
        reg.register(unit(b, "b"))
        env = reg.capture(["a"])
        a.value, b.value = None, "keep"
        report = reg.apply(env)
        assert a.value == 1 and b.value == "keep" and report.applied == ["a"]

    def test_apply_can_be_limited_by_prefix_or_scope(self):
        a, b, f = Box(1), Box(2), Box(3)
        reg = StateRegistry()
        reg.register(unit(a, "panel.A.d", scopes={"display"}))
        reg.register(unit(b, "panel.B.d", scopes={"display"}))
        reg.register(unit(f, "panel.A.f", scopes={"data"}))
        env = reg.capture()
        a.value = b.value = f.value = None
        reg.apply(env, prefix="panel.A", exclude_scopes={"data"})
        assert (a.value, b.value, f.value) == (1, None, None)

    def test_a_malformed_entry_is_skipped(self):
        reg = StateRegistry()
        reg.register(unit(Box(1), "a"))
        env = reg.capture()
        env["units"]["a"] = {"version": "x", "state": "nope"}
        report = reg.apply(env)
        assert report.ok and report.skipped == [("a", "malformed entry")]

    def test_summary(self):
        assert ApplyReport(applied=["a"], skipped=[("b", "r")]).summary() == \
            "1 applied, 1 skipped, 0 failed"


# ---------------------------------------------------------------------------
# Versioning
# ---------------------------------------------------------------------------

class TestVersioning:
    def test_state_from_a_newer_version_is_skipped(self):
        b = Box("keep")
        reg = StateRegistry()
        reg.register(unit(b, "a", version=1))
        env = reg.capture()
        env["units"]["a"]["version"] = 2
        report = reg.apply(env)
        assert b.value == "keep" and report.ok
        assert "newer version" in report.skipped[0][1]

    def test_older_state_without_migration_is_skipped(self):
        b = Box("keep")
        reg = StateRegistry()
        reg.register(unit(b, "a", version=2))
        env = {"format": FORMAT, "schema": SCHEMA,
               "units": {"a": {"version": 1, "state": {"value": 5}}}}
        report = reg.apply(env)
        assert b.value == "keep" and "no migration" in report.skipped[0][1]

    def test_older_state_is_migrated_then_applied(self):
        b = Box(None)
        reg = StateRegistry()
        reg.register(unit(b, "a", version=2,
                          migrate=lambda v, s: {"value": s["old_name"] * 10}))
        env = {"format": FORMAT, "schema": SCHEMA,
               "units": {"a": {"version": 1, "state": {"old_name": 4}}}}
        report = reg.apply(env)
        assert b.value == 40 and report.applied == ["a"]

    def test_a_failing_migration_is_reported_as_a_failure(self):
        def bad(v, s):
            raise KeyError("missing")
        reg = StateRegistry()
        reg.register(unit(Box(), "a", version=2, migrate=bad))
        env = {"format": FORMAT, "schema": SCHEMA,
               "units": {"a": {"version": 1, "state": {}}}}
        report = reg.apply(env)             # reported, never raised
        assert not report.ok and report.failed[0][0] == "a"
        assert isinstance(report.failed[0][1], KeyError)


# ---------------------------------------------------------------------------
# JSON and envelope validation
# ---------------------------------------------------------------------------

class TestJson:
    def test_dumps_loads_round_trip(self):
        reg = StateRegistry()
        reg.register(unit(Box({"nested": [1, 2, None]}), "a"))
        env = reg.capture()
        assert StateRegistry.loads(StateRegistry.dumps(env)) == env

    def test_output_is_stable(self):
        reg = StateRegistry()
        reg.register(unit(Box(1), "b"))
        reg.register(unit(Box(2), "a"))
        text = StateRegistry.dumps(reg.capture())
        assert text == StateRegistry.dumps(json.loads(text))

    @pytest.mark.parametrize("bad", [
        {}, {"format": "other", "schema": 1, "units": {}},
        {"format": FORMAT, "schema": 1}, [], "x", None])
    def test_a_foreign_envelope_is_rejected(self, bad):
        with pytest.raises(ValueError, match="not a cubevis"):
            StateRegistry().apply(bad)

    def test_an_unsupported_schema_is_rejected(self):
        with pytest.raises(ValueError, match="schema"):
            StateRegistry().apply({"format": FORMAT, "schema": 99, "units": {}})

    def test_loads_validates(self):
        with pytest.raises(ValueError):
            StateRegistry.loads('{"hello": 1}')
