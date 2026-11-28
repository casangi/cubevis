"""User preferences for visplot: how the tool looks and behaves, never what
data it shows or how they are computed.

Design: ``devel/notes/drs/review/notebook_state_design.md`` (section
"Preferences") and spin-off issue 6 of ``issue35_spinoffs.md``
(2026-11-25 .. 2026-11-27, with Darrell).

Two groups:

``app``
    The visplot application: theme, initial layout, the grid's shape,
    toolbar, whether the sidebar starts open.
``panel``
    Defaults for every plot panel (Panels A and B, and a panel shown on
    its own once that exists): time labels, gaps, colormaps, flag display,
    whether the gear tab's Pin starts ticked.  Raster-only and
    scatter-only settings are named by kind (``raster_cmap``), not kept
    in groups of their own: a panel can switch kind, and most settings
    mean the same for both.

Layers, lowest first: built-in defaults < the user file
(:func:`user_prefs_path`, ``~/.config/cubevis/prefs.json``) < an override
file (``prefs=<path>`` or ``$CUBEVIS_PREFS``) < explicit arguments.  An
override is partial: only the keys it sets, so the user file still
supplies the rest.  ``prefs=False`` (or ``CUBEVIS_PREFS=none``) uses the
built-in defaults only -- for tests and reproducible runs.  A file is
never looked for anywhere else (no hidden functionality).

The file::

    {"format": "cubevis.visplot.prefs", "schema": 1,
     "app":   {"theme": "light", "layout": "over"},
     "panel": {"time_labels": "elapsed"}}

A value that is not valid, an unknown key, or a file that cannot be read
is reported (:attr:`Prefs.problems`, logged as a warning) and ignored: a
preference file must never stop visplot from starting.  An explicit
argument is validated by the plotter as before and does raise.

A value of ``None`` for a key means "not set here": the layer below
supplies it.  For ``raster_cmap`` / ``scatter_cmap`` the built-in default
is ``None``: the colormap follows the theme (see ``_raster_cmap_user_set``
in ``visibility_plotter.py``); a saved colormap is a choice the user made
and is kept across theme changes, as an explicit argument is.

Standard library only (palette names are checked with a lazy import), so
the rules here are tested without Bokeh or an MS.
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Union

log = logging.getLogger(__name__)

FORMAT = "cubevis.visplot.prefs"
SCHEMA = 1
ENV = "CUBEVIS_PREFS"
#: ``$CUBEVIS_PREFS`` / ``prefs=`` value that means "built-in defaults only"
NONE_VALUES = ("none", "off", "false", "0")

GROUPS = ("app", "panel")

# Vocabularies.  Copies of the plotter's own (which live in modules that
# import Bokeh / numpy); tests/manual/visplot/test_prefs.py pins each to
# its source.
THEMES = ("dark", "light")
LAYOUTS = ("one", "side", "over", "grid", "raster", "scatter")
GRID_COLOR_SCOPES = ("cell", "page", "grid")      # vis_grid_source.COLOR_SCOPES
GRID_MAX = (8, 8)                                 # gridplot.grid_view.MAX_POOL
TIME_LABELS = ("utc", "elapsed")
GAPS = ("shown", "closed")                        # raster_grid.normalize_time_gaps
FLAG_DISPLAY_MODES = ("hide", "color")            # flag_controls.DISPLAY_MODES

_HEX = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
_NAME = re.compile(r"^[a-zA-Z]+$")


class PrefError(ValueError):
    """A preference value that is not valid (the message says why)."""


# ---- validators: return the value to use, or raise PrefError ---------- #

def _choice(options):
    def check(v):
        if isinstance(v, str) and v in options:
            return v
        raise PrefError(f"must be one of {', '.join(options)}")
    return check


def _bool(v):
    if isinstance(v, bool):
        return v
    raise PrefError("must be true or false")


def _grid(i):
    def check(v):
        if isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= GRID_MAX[i]:
            return v
        raise PrefError(f"must be an integer 1..{GRID_MAX[i]}")
    return check


def _color(v):
    # A hex color or a CSS color name (what Bokeh accepts as a color string)
    if isinstance(v, str) and (_HEX.match(v) or _NAME.match(v)):
        return v
    raise PrefError("must be a color: #rrggbb, #rgb or a CSS color name")


def _cmap(role):
    def check(v):
        if v is None:
            return None
        if not isinstance(v, str):
            raise PrefError("must be a colormap name or null")
        try:
            from ..gridplot import palettes
            names = palettes.raster_names() if role == "raster" else palettes.scatter_names()
        except Exception:                       # pragma: no cover - palettes is stdlib-only
            return v
        if v not in names:
            raise PrefError(f"must be one of {', '.join(names)} (or null: follow the theme)")
        return v
    return check


@dataclass(frozen=True)
class Pref:
    """One preference: its built-in default and its check."""
    default: Any
    check: Callable[[Any], Any]
    doc: str = ""


#: The known preferences.  The key is the ``VisibilityPlotter`` argument it
#: supplies, except ``sidebar_open`` and ``gear_pin``, which exist only in
#: the GUI.
PREFS: Dict[str, Dict[str, Pref]] = {
    "app": {
        "theme":           Pref("dark", _choice(THEMES), "dark or light chrome and palettes"),
        "layout":          Pref("side", _choice(LAYOUTS), "the layout at startup"),
        "pin_layout":      Pref(False, _bool, "presets keep the layout"),
        "gridrows":        Pref(3, _grid(0), "Grid layout: rows per page"),
        "gridcols":        Pref(3, _grid(1), "Grid layout: columns per page"),
        "gridcolor":       Pref("page", _choice(GRID_COLOR_SCOPES), "Grid layout: color scaling scope"),
        "compact_toolbar": Pref(True, _bool, "the compact toolbar"),
        "sidebar_open":    Pref(True, _bool, "the sidebar is open at startup"),
    },
    "panel": {
        "time_labels":        Pref("utc", _choice(TIME_LABELS), "time axes: UTC or elapsed"),
        "time_gaps":          Pref("shown", _choice(GAPS), "gaps on a Time axis"),
        "freq_gaps":          Pref("shown", _choice(GAPS), "gaps on a frequency axis"),
        "raster_cmap":        Pref(None, _cmap("raster"), "raster colormap (null: follow the theme)"),
        "scatter_cmap":       Pref(None, _cmap("scatter"), "scatter colormap (null: follow the theme)"),
        "flag_display":       Pref("hide", _choice(FLAG_DISPLAY_MODES), "pending flags: hidden or colored"),
        "flag_color":         Pref("#ff00ff", _color, "color of pending flags"),
        "flag_show_flagged":  Pref(False, _bool, "show data already flagged"),
        "flag_flagged_color": Pref("#7f849c", _color, "color of data already flagged"),
        "gear_pin":           Pref(False, _bool, "the gear tab's Pin starts ticked"),
    },
}

#: Keys that are ``VisibilityPlotter`` arguments (``group``, ``key``).
ARGUMENT_KEYS = tuple((g, k) for g in GROUPS for k in PREFS[g]
                      if k not in ("sidebar_open", "gear_pin"))


def builtin_defaults() -> Dict[str, Dict[str, Any]]:
    """The built-in defaults, as a preferences dict."""
    return {g: {k: p.default for k, p in PREFS[g].items()} for g in GROUPS}


def user_prefs_path() -> Path:
    """``$XDG_CONFIG_HOME/cubevis/prefs.json``, by default
    ``~/.config/cubevis/prefs.json``.  Read (and written by "Save as
    default") where the GUI is built: a remote kernel does not supply
    preferences of its own."""
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return Path(base) / "cubevis" / "prefs.json"


PrefsArg = Union[None, bool, str, os.PathLike, Mapping[str, Any]]


@dataclass
class Prefs:
    """Preferences as resolved for one plotter.

    ``values``: every key of every group (built-in defaults where no file
    set one).  ``sources``: the files (or ``"prefs= argument"``) that
    supplied values, lowest first.  ``save_path``: the file "Save as
    default" writes -- the override file when there is one, else the user
    file; None with ``prefs=False`` or a dict.  ``problems``: what was
    ignored, and why."""
    values: Dict[str, Dict[str, Any]] = field(default_factory=builtin_defaults)
    sources: List[str] = field(default_factory=list)
    save_path: Optional[Path] = None
    problems: List[str] = field(default_factory=list)

    def get(self, group: str, key: str) -> Any:
        return self.values[group][key]

    def resolve(self, group: str, key: str, explicit: Any) -> Any:
        """*explicit* (an argument) if it is not None, else the preference."""
        return explicit if explicit is not None else self.values[group][key]

    def describe(self) -> str:
        """One line for the log / status area: which files were applied."""
        if not self.sources:
            return "preferences: built-in defaults"
        return "preferences: " + ", ".join(self.sources)


def _merge(prefs: Prefs, data: Any, source: str) -> bool:
    """Merge one layer (*data*, as read from *source*) into *prefs*.
    Returns whether it supplied any value."""
    if not isinstance(data, Mapping):
        prefs.problems.append(f"{source}: not a JSON object; ignored")
        return False
    fmt = data.get("format", FORMAT)
    if fmt != FORMAT:
        prefs.problems.append(f"{source}: format {fmt!r} is not {FORMAT!r}; ignored")
        return False
    schema = data.get("schema", SCHEMA)
    if not isinstance(schema, int) or schema > SCHEMA:
        prefs.problems.append(f"{source}: schema {schema!r} is newer than this cubevis "
                              f"({SCHEMA}); ignored")
        return False
    supplied = False
    for name in data:
        if name in ("format", "schema"):
            continue
        if name not in GROUPS:
            prefs.problems.append(f"{source}: unknown group {name!r} ignored")
            continue
        group = data[name]
        if not isinstance(group, Mapping):
            prefs.problems.append(f"{source}: {name!r} is not a JSON object; ignored")
            continue
        for key, value in group.items():
            spec = PREFS[name].get(key)
            if spec is None:
                prefs.problems.append(f"{source}: unknown preference {name}.{key} ignored")
                continue
            if value is None and spec.default is not None:
                continue                    # not set here
            try:
                prefs.values[name][key] = spec.check(value)
                supplied = True
            except PrefError as exc:
                prefs.problems.append(f"{source}: {name}.{key} = {value!r} ignored: {exc}")
    return supplied


def _read(path: Path, prefs: Prefs) -> Optional[Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        prefs.problems.append(f"{path}: cannot be read ({exc}); ignored")
        return None
    try:
        return json.loads(text)
    except ValueError as exc:
        prefs.problems.append(f"{path}: not valid JSON ({exc}); ignored")
        return None


def layer_defaults(prefs: PrefsArg = None) -> Dict[str, Any]:
    """What a preference-backed argument left at None comes to, by argument
    name: for the task layers' ``inp()``, which shows that value for an
    argument left at None (``prefs_module`` in
    ``scripts/sync_layers/project_layers.yaml``); None is still what the
    layer passes and saves in ``visplot.last``, so ``tget()`` keeps
    following the preferences.
    Quiet: problems are logged when the plotter is made, not on every
    ``inp()``."""
    resolved = load_prefs(prefs, quiet=True)
    return {k: resolved.values[g][k] for g, k in ARGUMENT_KEYS}


def load_prefs(prefs: PrefsArg = None, *, env: Optional[Mapping[str, str]] = None,
               user_path: Optional[Path] = None, quiet: bool = False) -> Prefs:
    """Resolve the preference layers.

    *prefs*: the ``prefs=`` argument -- None (the user file, then
    ``$CUBEVIS_PREFS`` if set), a path (the user file, then that file), a
    dict (the user file, then the dict; nothing to save to), or False
    (built-in defaults only).  *env* and *user_path* are for tests;
    *quiet*: do not log (``layer_defaults``).
    """
    env = os.environ if env is None else env
    user_path = user_prefs_path() if user_path is None else Path(user_path)
    result = Prefs()

    override: PrefsArg = prefs
    if override is None:
        override = env.get(ENV) or None
    if override is False or (isinstance(override, str)
                             and override.strip().lower() in NONE_VALUES):
        if not quiet:
            _report(result)
        return result

    data = _read(user_path, result)
    if data is not None and _merge(result, data, str(user_path)):
        result.sources.append(str(user_path))
    result.save_path = user_path

    if isinstance(override, Mapping):
        if _merge(result, override, "prefs= argument"):
            result.sources.append("prefs= argument")
        result.save_path = None
    elif override is not None and override is not True:
        path = Path(os.path.expanduser(os.fspath(override)))
        data = _read(path, result)
        if data is not None and _merge(result, data, str(path)):
            result.sources.append(str(path))
        result.save_path = path               # saving writes the file in use
    if not quiet:
        _report(result)
    return result


def _report(prefs: Prefs) -> None:
    for problem in prefs.problems:
        log.warning("visplot %s", problem)
    log.info("visplot %s", prefs.describe())


def save_prefs(path: Union[str, os.PathLike], values: Mapping[str, Mapping[str, Any]],
               *, groups: Optional[tuple] = None) -> Dict[str, Any]:
    """Write *values* (``{"app": {...}, "panel": {...}}``) into the file at
    *path*, merged into what it already holds: only the *groups* given
    (default: those in *values*) are changed, and within them only the keys
    given, so an override file stays partial and "Save" from a panel on its
    own leaves the app group as it was.  Every value is checked first;
    nothing is written if one is not valid (:class:`PrefError`).  The write
    is atomic (a temporary file, then a rename).  Returns what the file
    holds now.
    """
    path = Path(os.path.expanduser(os.fspath(path)))
    groups = tuple(values) if groups is None else tuple(groups)
    checked: Dict[str, Dict[str, Any]] = {}
    for g in groups:
        if g not in GROUPS:
            raise PrefError(f"unknown preference group {g!r}")
        for key, value in (values.get(g) or {}).items():
            spec = PREFS[g].get(key)
            if spec is None:
                raise PrefError(f"unknown preference {g}.{key}")
            try:
                checked.setdefault(g, {})[key] = spec.check(value)
            except PrefError as exc:
                raise PrefError(f"{g}.{key} = {value!r}: {exc}") from None

    current: Dict[str, Any] = {}
    try:
        current = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(current, dict):
            current = {}
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as exc:
        # An unreadable file is replaced rather than merged into; say so.
        log.warning("visplot preferences: %s could not be read (%s); rewriting it", path, exc)
        current = {}
    out: Dict[str, Any] = {"format": FORMAT, "schema": SCHEMA}
    for g in GROUPS:
        group = dict(current.get(g) or {}) if isinstance(current.get(g), dict) else {}
        group.update(checked.get(g, {}))
        if group:
            out[g] = group
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".prefs-", suffix=".json", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(out, fh, indent=2, sort_keys=False)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return out
