"""
test_prefs.py
=============
visplot preferences (``cubevis/toolbox/visplot/prefs.py``; issue 6 of
``devel/notes/drs/review/issue35_spinoffs.md``, design in
``notebook_state_design.md``, 2026-11-27).

1. Layers       built-in < user file < override (prefs= / CUBEVIS_PREFS),
                an override partial; prefs=False / CUBEVIS_PREFS=none; a dict
2. Bad input    invalid values, unknown keys, bad JSON, a newer schema: all
                reported and ignored, never raised
3. Saving       save_prefs merges into the file, only the groups given, checks
                everything first, writes nothing on an error
4. Vocabulary   prefs.py's copies of the plotter's choices and defaults pinned
                to their sources
5. Signature    every preference-backed VisibilityPlotter argument defaults
                to None (the layers pass every argument; AST, no import), and
                the generated layers agree
6. Constructor  with an MS (``MS=``, skipped without): a preference reaches
                the plotter; an explicit argument wins

Tests 1-5 need neither Bokeh nor an MS.
"""
from __future__ import annotations

import ast
import json
import os
from pathlib import Path

import pytest

from cubevis.toolbox.visplot import prefs as P

ROOT = Path(__file__).resolve().parents[3]
PLOTTER = ROOT / "cubevis" / "toolbox" / "visplot" / "visibility_plotter.py"
MS = os.environ.get("MS", "")


def _write(path: Path, data) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data if isinstance(data, str) else json.dumps(data))
    return path


@pytest.fixture
def user(tmp_path):
    return tmp_path / "config" / "cubevis" / "prefs.json"


def load(user, prefs=None, env=None):
    return P.load_prefs(prefs, env=env or {}, user_path=user, quiet=True)


# --------------------------------------------------------------------------- #
# 1. Layers
# --------------------------------------------------------------------------- #

class TestLayers:

    def test_no_file_is_builtin(self, user):
        r = load(user)
        assert r.values == P.builtin_defaults()
        assert r.sources == [] and r.problems == []
        assert r.save_path == user                 # "Save" would create it

    def test_user_file(self, user):
        _write(user, {"app": {"theme": "light"}, "panel": {"time_labels": "elapsed"}})
        r = load(user)
        assert r.get("app", "theme") == "light"
        assert r.get("panel", "time_labels") == "elapsed"
        assert r.get("app", "layout") == "side"    # not set: built-in
        assert r.sources == [str(user)]

    def test_override_is_partial(self, user, tmp_path):
        _write(user, {"app": {"theme": "light", "layout": "over"}})
        o = _write(tmp_path / "project.json", {"app": {"layout": "one"}})
        r = load(user, prefs=str(o))
        assert r.get("app", "layout") == "one"     # the override
        assert r.get("app", "theme") == "light"    # the user file, still
        assert r.sources == [str(user), str(o)]
        assert r.save_path == o                    # saving writes the file in use

    def test_env_override(self, user, tmp_path):
        o = _write(tmp_path / "e.json", {"panel": {"time_gaps": "closed"}})
        r = load(user, env={P.ENV: str(o)})
        assert r.get("panel", "time_gaps") == "closed" and r.save_path == o

    def test_argument_beats_env(self, user, tmp_path):
        a = _write(tmp_path / "a.json", {"app": {"theme": "light"}})
        e = _write(tmp_path / "e.json", {"app": {"theme": "dark", "layout": "over"}})
        r = load(user, prefs=str(a), env={P.ENV: str(e)})
        assert r.get("app", "theme") == "light"
        assert r.get("app", "layout") == "side"    # e.json not read

    def test_missing_override_is_where_save_goes(self, user, tmp_path):
        r = load(user, prefs=str(tmp_path / "new.json"))
        assert r.problems == [] and r.save_path == tmp_path / "new.json"

    @pytest.mark.parametrize("off", [False, "none", "NONE", "off"])
    def test_builtin_only(self, user, off):
        _write(user, {"app": {"theme": "light"}})
        r = load(user, prefs=off) if off is False else load(user, env={P.ENV: off})
        assert r.values == P.builtin_defaults()
        assert r.sources == [] and r.save_path is None

    def test_dict(self, user):
        _write(user, {"app": {"theme": "light"}})
        r = load(user, prefs={"panel": {"flag_display": "color"}})
        assert r.get("panel", "flag_display") == "color"
        assert r.get("app", "theme") == "light"
        assert r.sources == [str(user), "prefs= argument"]
        assert r.save_path is None                 # nothing to save to

    def test_null_means_not_set(self, user, tmp_path):
        _write(user, {"app": {"theme": "light"}, "panel": {"raster_cmap": "viridis"}})
        o = _write(tmp_path / "o.json", {"app": {"theme": None}, "panel": {"raster_cmap": None}})
        r = load(user, prefs=str(o))
        assert r.get("app", "theme") == "light"    # null: the layer below
        # a colormap's built-in is null (follow the theme), so an override
        # can put it back to that
        assert r.get("panel", "raster_cmap") is None

    def test_resolve(self, user):
        _write(user, {"app": {"layout": "over"}})
        r = load(user)
        assert r.resolve("app", "layout", None) == "over"
        assert r.resolve("app", "layout", "one") == "one"   # explicit wins
        assert r.resolve("app", "pin_layout", False) is False

    def test_xdg(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        assert P.user_prefs_path() == tmp_path / "cubevis" / "prefs.json"
        monkeypatch.delenv("XDG_CONFIG_HOME")
        assert P.user_prefs_path() == Path.home() / ".config" / "cubevis" / "prefs.json"

    def test_describe(self, user):
        assert load(user).describe() == "preferences: built-in defaults"
        _write(user, {"app": {"theme": "light"}})
        assert str(user) in load(user).describe()

    def test_layer_defaults(self, monkeypatch, tmp_path):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        monkeypatch.delenv(P.ENV, raising=False)
        _write(tmp_path / "cubevis" / "prefs.json", {"app": {"layout": "over"}})
        d = P.layer_defaults()
        assert d["layout"] == "over" and d["theme"] == "dark"
        assert "gear_pin" not in d and "sidebar_open" not in d   # GUI only
        assert set(d) == {k for _, k in P.ARGUMENT_KEYS}


# --------------------------------------------------------------------------- #
# 2. Bad input: reported, ignored, never raised
# --------------------------------------------------------------------------- #

class TestBadInput:

    @pytest.mark.parametrize("group,key,value", [
        ("app", "theme", "purple"), ("app", "layout", 3), ("app", "pin_layout", "yes"),
        ("app", "gridrows", 0), ("app", "gridrows", 9), ("app", "gridcols", True),
        ("app", "gridcolor", "row"), ("panel", "time_labels", "local"),
        ("panel", "time_gaps", "open"), ("panel", "raster_cmap", "no-such-map"),
        ("panel", "flag_color", "#12345"), ("panel", "flag_color", "rgb(1,2,3)"),
        ("panel", "flag_display", "show"), ("panel", "gear_pin", 1),
    ])
    def test_invalid_value(self, user, group, key, value):
        _write(user, {group: {key: value, }, "app": {"compact_toolbar": False}} if group != "app"
               else {"app": {key: value, "compact_toolbar": False}})
        r = load(user)
        assert r.get(group, key) == P.PREFS[group][key].default
        assert r.get("app", "compact_toolbar") is False      # the rest still applies
        assert len(r.problems) == 1 and f"{group}.{key}" in r.problems[0]

    def test_unknown_key_and_group(self, user):
        _write(user, {"app": {"colour": "red"}, "panle": {}, "panel": {"time_labels": "elapsed"}})
        r = load(user)
        assert r.get("panel", "time_labels") == "elapsed"
        assert len(r.problems) == 2

    def test_bad_json(self, user):
        _write(user, "{not json")
        r = load(user)
        assert r.values == P.builtin_defaults() and "not valid JSON" in r.problems[0]
        assert r.sources == []

    def test_not_an_object(self, user):
        _write(user, "[1, 2]")
        assert "not a JSON object" in load(user).problems[0]

    def test_newer_schema(self, user):
        _write(user, {"schema": P.SCHEMA + 1, "app": {"theme": "light"}})
        r = load(user)
        assert r.get("app", "theme") == "dark" and "newer" in r.problems[0]

    def test_other_format(self, user):
        _write(user, {"format": "something.else", "app": {"theme": "light"}})
        assert load(user).get("app", "theme") == "dark"

    def test_problems_are_logged(self, user, caplog):
        _write(user, {"app": {"theme": "purple"}})
        with caplog.at_level("WARNING", logger="cubevis.toolbox.visplot.prefs"):
            P.load_prefs(None, env={}, user_path=user)
        assert any("app.theme" in r.message for r in caplog.records)


# --------------------------------------------------------------------------- #
# 3. Saving
# --------------------------------------------------------------------------- #

class TestSave:

    def test_creates_file_and_dirs(self, tmp_path):
        f = tmp_path / "a" / "b" / "prefs.json"
        out = P.save_prefs(f, {"app": {"theme": "light"}})
        assert json.loads(f.read_text()) == out
        assert out == {"format": P.FORMAT, "schema": P.SCHEMA, "app": {"theme": "light"}}

    def test_merges_and_keeps_other_group(self, tmp_path):
        f = _write(tmp_path / "p.json", {"app": {"theme": "light", "layout": "over"},
                                          "panel": {"time_labels": "elapsed"}})
        P.save_prefs(f, {"app": {"layout": "one"}, "panel": {"gear_pin": True}},
                     groups=("panel",))
        data = json.loads(f.read_text())
        # only the panel group was saved: the app group is as it was
        assert data["app"] == {"theme": "light", "layout": "over"}
        assert data["panel"] == {"time_labels": "elapsed", "gear_pin": True}

    def test_round_trip(self, user):
        P.save_prefs(user, {"app": {"gridrows": 4}, "panel": {"scatter_cmap": None}})
        r = load(user)
        assert r.get("app", "gridrows") == 4 and r.get("panel", "scatter_cmap") is None

    def test_invalid_writes_nothing(self, tmp_path):
        f = _write(tmp_path / "p.json", {"app": {"theme": "light"}})
        before = f.read_text()
        for bad in ({"app": {"theme": "purple"}}, {"app": {"nope": 1}}, {"other": {}}):
            with pytest.raises(P.PrefError):
                P.save_prefs(f, bad)
            assert f.read_text() == before
        assert [p.name for p in tmp_path.iterdir()] == ["p.json"]   # no temp file left

    def test_unreadable_is_replaced(self, tmp_path):
        f = _write(tmp_path / "p.json", "{broken")
        assert P.save_prefs(f, {"app": {"theme": "light"}})["app"] == {"theme": "light"}


# --------------------------------------------------------------------------- #
# 4. Vocabulary pinned to its sources
# --------------------------------------------------------------------------- #

class TestVocabulary:

    def test_grid(self):
        from cubevis.toolbox.visplot.vis_grid_source import COLOR_SCOPES
        from cubevis.toolbox.gridplot.grid_view import MAX_POOL
        assert P.GRID_COLOR_SCOPES == tuple(COLOR_SCOPES)
        assert P.GRID_MAX == tuple(MAX_POOL)

    def test_flags(self):
        from cubevis.toolbox.visplot import flag_controls as fc
        assert P.FLAG_DISPLAY_MODES == tuple(fc.DISPLAY_MODES)
        assert P.PREFS["panel"]["flag_color"].default == fc.DEFAULT_PENDING_COLOR
        assert P.PREFS["panel"]["flag_flagged_color"].default == fc.DEFAULT_FLAGGED_COLOR

    def test_gaps(self):
        from cubevis.toolbox.gridplot.raster_grid import normalize_time_gaps
        assert tuple(normalize_time_gaps(g) for g in P.GAPS) == P.GAPS

    def test_every_default_passes_its_check(self):
        for g, keys in P.PREFS.items():
            for k, spec in keys.items():
                assert spec.check(spec.default) == spec.default, f"{g}.{k}"

    def test_palettes(self):
        from cubevis.toolbox.gridplot import palettes
        for name in palettes.raster_names():
            assert P.PREFS["panel"]["raster_cmap"].check(name) == name
        for name in palettes.scatter_names():
            assert P.PREFS["panel"]["scatter_cmap"].check(name) == name


# --------------------------------------------------------------------------- #
# 5. Signature: preference-backed arguments default to None (AST)
# --------------------------------------------------------------------------- #

def _init_defaults(path: Path, cls: str = "VisibilityPlotter") -> dict:
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == cls:
            for f in node.body:
                if isinstance(f, ast.FunctionDef) and f.name == "__init__":
                    a = f.args
                    return {arg.arg: ast.literal_eval(d) if isinstance(d, ast.Constant) else ast.unparse(d)
                            for arg, d in zip(a.kwonlyargs, a.kw_defaults) if d is not None}
    raise AssertionError(f"{cls}.__init__ not found in {path}")


class TestSignature:

    def test_preference_arguments_default_to_none(self):
        defaults = _init_defaults(PLOTTER)
        for _g, key in P.ARGUMENT_KEYS:
            assert key in defaults, key
            assert defaults[key] is None, f"{key} defaults to {defaults[key]!r}, not None"
        assert defaults["prefs"] is None

    def test_builtin_defaults_documented(self):
        # each argument's documented built-in is the one prefs.py uses
        text = PLOTTER.read_text()
        assert 'Default ``"side"``' in text and P.PREFS["app"]["layout"].default == "side"
        assert "(1-8, default 3)" in text and P.PREFS["app"]["gridrows"].default == 3

    def test_layers_pass_none(self):
        # the generated layers (sync_layers) default these to None too, so a
        # task call that leaves them alone gets the preference
        layer = ROOT / "cubevis" / "private" / "casatasks" / "visplot.py"
        text = layer.read_text()
        import re
        for _g, key in P.ARGUMENT_KEYS:
            assert re.search(rf"^        {key}: [^=]+= None,$", text, re.M), key
        shell = (ROOT / "cubevis" / "private" / "casashell" / "visplot.py").read_text()
        assert "from cubevis.toolbox.visplot.prefs import layer_defaults" in shell


# --------------------------------------------------------------------------- #
# 6. The constructor (needs an MS)
# --------------------------------------------------------------------------- #

@pytest.fixture
def ms():
    if not os.path.isdir(MS):
        pytest.skip("no MS (MS=)")
    return MS


class TestConstructor:

    def test_preferences_reach_the_plotter(self, ms):
        from cubevis.toolbox.visplot import VisibilityPlotter
        prefs = {"app": {"theme": "light", "layout": "over", "gridrows": 2,
                         "pin_layout": True, "gridcolor": "grid", "compact_toolbar": False},
                 "panel": {"time_labels": "elapsed", "time_gaps": "closed",
                           "flag_display": "color", "flag_color": "#00ff00",
                           "raster_cmap": P.PREFS["panel"]["raster_cmap"].check(
                               __import__("cubevis.toolbox.gridplot.palettes",
                                          fromlist=["x"]).raster_names()[-1])}}
        with VisibilityPlotter(ms=ms, headless=True, prefs=prefs) as vp:
            assert vp._theme == "light"
            assert vp._layout == "over"
            assert vp._grid_shape == (2, 3)
            assert vp._pin_layout is True
            assert vp._grid_color == "grid"
            assert vp._compact_toolbar is False
            assert vp._time_labels == "elapsed"
            assert vp._time_gaps == "closed"
            assert vp._flags.display == "color" and vp._flags.color == "#00ff00"
            assert vp._raster_cmap_name == prefs["panel"]["raster_cmap"]
            assert vp._raster_cmap_user_set          # kept across theme changes
            assert not vp._scatter_cmap_user_set     # follows the theme

    def test_explicit_argument_wins(self, ms):
        from cubevis.toolbox.visplot import VisibilityPlotter
        prefs = {"app": {"theme": "light", "layout": "over"}, "panel": {"time_labels": "elapsed"}}
        with VisibilityPlotter(ms=ms, headless=True, prefs=prefs,
                               theme="dark", layout="side", time_labels="utc") as vp:
            assert (vp._theme, vp._layout, vp._time_labels) == ("dark", "side", "utc")

    def test_builtin_only(self, ms):
        from cubevis.toolbox.visplot import VisibilityPlotter
        with VisibilityPlotter(ms=ms, headless=True, prefs=False) as vp:
            assert (vp._theme, vp._layout, vp._time_labels) == ("dark", "side", "utc")
            assert vp._prefs.sources == []

    def test_gui_only_preferences(self, ms):
        # gear_pin and sidebar_open act on the GUI as it is built
        from bokeh.models import CheckboxGroup
        from cubevis.toolbox.visplot import VisibilityPlotter
        prefs = {"app": {"sidebar_open": False}, "panel": {"gear_pin": True}}
        vp = VisibilityPlotter(ms=ms, prefs=prefs)
        try:
            vp._build_layout()
            assert vp._sidebar_col.visible is False
            assert vp._sidebar_toggle_btn.label == "⟩"
            pins = list(vp._panel_pin.values())
            assert pins and all(isinstance(p, CheckboxGroup) and p.active == [0] for p in pins)
        finally:
            vp.close()
        vp = VisibilityPlotter(ms=ms, prefs=False)
        try:
            vp._build_layout()
            assert vp._sidebar_col.visible is not False
            assert all(p.active == [] for p in vp._panel_pin.values())
        finally:
            vp.close()
