"""
test_spw_selection.py
=====================
Spectral-window identity, selection and display.

Why this module exists
----------------------
This logic has failed **silently twice**, and both times a plot appeared to
confirm it working:

* §8.4a — ``SelectionSpec.spw`` was populated by the constructor, the parser
  and the Plot button, and read by **no backend**. ``spw='0'`` plotted every
  window with no error.
* §8.25 — once the backends did read it, they compared parsed *integers*
  against identities that xarray-ms reports as *names*, so nothing ever
  matched and ``_spw_selected`` fell through to keeping every partition.

Neither was detectable by looking at a plot, and neither would have survived a
test. The single-window test dataset cannot exercise selection at all — with
one window, selecting it changes nothing and deselecting it is the empty case —
so a fabricated multi-window metadata is the *only* way to check this short of
finding another MS.

Everything here is a pure function over metadata: no MS, no backend, no
datashader.

Test location
-------------
``cubevis/tests/manual/visplot/test_spw_selection.py``
"""

import ast
import pathlib
import textwrap
import types

import pytest


# ---------------------------------------------------------------------------
# Lifting the functions under test
# ---------------------------------------------------------------------------
#
# ``_parse_spw_string`` and ``_partition_spw_ident`` live in modules that pull
# in bokeh, xarray-ms and datashader at import time.  They are pure, so they
# are lifted by AST rather than importing their modules -- which keeps this
# file runnable in a bare environment and keeps a failure here pointing at the
# logic rather than at an import chain.

def _find_visplot() -> pathlib.Path:
    """Locate ``cubevis/toolbox/visplot`` by walking up from this file.

    Searched rather than computed from a fixed ``parents[n]`` index: the
    test tree's depth relative to the package is not a fact this file
    should encode, and a wrong index produces a wall of
    ``FileNotFoundError`` that says nothing about what is actually wrong.
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
def parse_spw():
    ns = _lift(_PKG / "visibility_plotter.py", "_parse_spw_string")
    return ns["_parse_spw_string"]


@pytest.fixture(scope="module", params=["msv2_backend.py", "msv4_backend.py"])
def ident(request):
    """``_partition_spw_ident`` from each backend.

    Parameterised over both because the same fix has landed in one and not the
    other three times (§8.7 arity, §8.11 DDID qualifier, §8.27 kind
    resolution).
    """
    ns = _lift(_PKG / "data" / request.param, "_partition_spw_ident")
    return ns["_partition_spw_ident"].__func__ \
        if isinstance(ns["_partition_spw_ident"], staticmethod) \
        else ns["_partition_spw_ident"]


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

NAME = "ALMA_RB_07#BB_2#SW-01#FULL_RES"
WVR = "WVR#NOMINAL"


class _Coord:
    def __init__(self, **attrs):
        self.attrs = attrs


class FakeDS:
    """Minimal stand-in for a partition Dataset."""

    def __init__(self, attrs=None, spw_name=None, channel_width=None):
        self.attrs = attrs or {}
        self.coords = {}
        if spw_name is not None or channel_width is not None:
            ca = {}
            if spw_name is not None:
                ca["spectral_window_name"] = spw_name
            if channel_width is not None:
                ca["channel_width"] = {"attrs": {"units": "Hz"},
                                       "data": channel_width}
            self.coords["frequency"] = _Coord(**ca)


class FakeSpw:
    def __init__(self, spw_id, name=""):
        self.spw_id = spw_id
        self.name = name


class FakeMeta:
    def __init__(self, *spws):
        self.spws = list(spws)


# ---------------------------------------------------------------------------
# Identity resolution
# ---------------------------------------------------------------------------

class TestPartitionIdentity:

    def test_numeric_id_preferred(self, ident):
        assert ident(FakeDS(attrs={"spectral_window_id": 5})) == (5, "spw")

    def test_ddid_is_reported_as_ddid(self, ident):
        """DATA_DESC_ID indexes DATA_DESCRIPTION -- a (spw, polarization
        setup) pair -- so DDID 3 can be SPW 5.  Reporting it as "spw 3"
        would name something ``spw='3'`` does not select (§8.11)."""
        assert ident(FakeDS(attrs={"DATA_DESC_ID": 3})) == (3, "ddid")

    def test_name_from_frequency_attrs(self, ident):
        """What xarray-ms 0.5.6 actually provides: no id in ``ds.attrs``
        at all, only a name on the frequency coordinate (§8.25)."""
        assert ident(FakeDS(spw_name=NAME)) == (NAME, "name")

    def test_id_wins_over_name(self, ident):
        got = ident(FakeDS(attrs={"spectral_window_id": 5}, spw_name=NAME))
        assert got == (5, "spw")

    def test_nothing_declared(self, ident):
        assert ident(FakeDS()) == (None, "none")

    def test_empty_name_is_not_an_identity(self, ident):
        """An empty string must not become an identity: it would group
        every undeclared partition under one bogus key."""
        assert ident(FakeDS(spw_name="")) == (None, "none")


# ---------------------------------------------------------------------------
# Selection matching
# ---------------------------------------------------------------------------

def _spw_selected(ident_fn, ds, spw):
    """Reimplementation of the backends' matching rule, for clarity.

    Kept in the test rather than lifted because the backend method is an
    instance method entangled with logging state; the rule itself is two
    lines and asserting it here documents the contract.
    """
    if spw is None:
        return True
    i, _kind = ident_fn(ds)
    if i is None:
        return True
    wanted = set(spw)
    return i in wanted or str(i) in {str(w) for w in wanted}


class TestSelectionMatching:

    def test_unconstrained_keeps_everything(self, ident):
        assert _spw_selected(ident, FakeDS(spw_name=NAME), None)

    def test_name_selects_name(self, ident):
        """The §8.25 defect: names could never match parsed integers, so
        filtering was a silent no-op on every xarray-ms store."""
        assert _spw_selected(ident, FakeDS(spw_name=NAME), [NAME])
        assert not _spw_selected(ident, FakeDS(spw_name=NAME), [WVR])

    def test_id_selects_id(self, ident):
        ds = FakeDS(attrs={"spectral_window_id": 5})
        assert _spw_selected(ident, ds, [5])
        assert not _spw_selected(ident, ds, [0, 1])

    def test_stringified_fallback(self, ident):
        """A caller that resolved against different metadata may supply
        "5" where the store reports 5.  Keeping the partition is the
        right failure direction: showing more than asked is visible,
        showing less looks like a correct plot of a smaller dataset."""
        assert _spw_selected(ident, FakeDS(attrs={"spectral_window_id": 5}),
                             ["5"])

    def test_undeclared_partition_is_kept(self, ident):
        """Refusing to plot because a store omits an optional attribute
        is a worse failure than plotting more than asked for."""
        assert _spw_selected(ident, FakeDS(), [NAME])


# ---------------------------------------------------------------------------
# String parsing
# ---------------------------------------------------------------------------

class TestParseSpwString:

    @pytest.fixture
    def named(self):
        return FakeMeta(FakeSpw(NAME, NAME), FakeSpw(WVR, WVR))

    @pytest.fixture
    def numeric(self):
        return FakeMeta(FakeSpw(0, "SPW0"), FakeSpw(1, "SPW1"),
                        FakeSpw(17, "WVR"))

    def test_empty_selects_all(self, parse_spw, named):
        assert parse_spw("", named) == [NAME, WVR]

    def test_exact_name(self, parse_spw, named):
        assert parse_spw(NAME, named) == [NAME]

    def test_substring_of_name(self, parse_spw, named):
        """So a user need not retype the full ASDM name."""
        assert parse_spw("SW-01", named) == [NAME]
        assert parse_spw("wvr", named) == [WVR]      # case-insensitive

    def test_numeric_token_does_not_substring_match(self, parse_spw, named):
        """"0" occurs inside "ALMA_RB_07#..." -- a bare digit matching a
        name by coincidence would silently preempt positional matching and
        pick a window the user did not mean, with no warning, because the
        match "succeeded"."""
        assert parse_spw("1", named) == [WVR]        # position 1, not a substring

    def test_numeric_id_matches_id_not_position(self, parse_spw, numeric):
        """With real ids present, "17" is the window *called* 17 -- not
        the eighteenth, which does not exist."""
        assert parse_spw("17", numeric) == [17]

    def test_multiple_tokens(self, parse_spw, named):
        assert parse_spw("SW-01,wvr", named) == [NAME, WVR]

    def test_duplicates_collapse(self, parse_spw, named):
        assert parse_spw("SW-01,SW-01", named) == [NAME]

    def test_unmatched_token_falls_back_to_all(self, parse_spw, named):
        """Logged and skipped.  Showing everything is the visible failure;
        showing nothing would render an empty plot that looks like a
        legitimate empty selection."""
        assert parse_spw("nosuchwindow", named) == [NAME, WVR]

    def test_whitespace_tolerated(self, parse_spw, named):
        assert parse_spw("  SW-01 , wvr  ", named) == [NAME, WVR]


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------

class TestSpwInfoLabel:
    """``SpwInfo.label()`` is what a selection control renders."""

    @staticmethod
    @pytest.fixture(scope="class")
    def SpwInfo():
        src = (_PKG / "reduction_context.py").read_text()
        tree = ast.parse(src)
        cls = next(n for n in ast.walk(tree)
                   if isinstance(n, ast.ClassDef) and n.name == "SpwInfo")
        ns = {}
        exec("from dataclasses import dataclass\nfrom typing import Optional\n"
             "@dataclass(frozen=True)\n" + ast.get_source_segment(src, cls), ns)
        return ns["SpwInfo"]

    def test_full_row(self, SpwInfo):
        s = SpwInfo(spw_id=NAME, centre_freq_hz=372.64997e9,
                    bandwidth_hz=234.375e6, n_channels=384,
                    polarizations=("XX", "YY"), name=NAME)
        assert s.label() == f"{NAME}   372.53-372.77 GHz   384 ch"

    def test_single_channel_window_is_not_degenerate(self, SpwInfo):
        """Bandwidth spans channel *edges*, so a WVR window reads as a
        real span rather than collapsing -- which is what makes it
        recognisable in the list."""
        s = SpwInfo(spw_id=17, centre_freq_hz=7.55e9, bandwidth_hz=1.5e9,
                    n_channels=1, polarizations=("XX",), name="WVR#NOMINAL")
        assert s.label() == "WVR#NOMINAL   6.80-8.30 GHz   1 ch"

    def test_falls_back_to_bare_identity(self, SpwInfo):
        """A backend that supplies no detail still produces a usable row
        rather than an empty one."""
        s = SpwInfo(spw_id=3, centre_freq_hz=0.0, bandwidth_hz=0.0,
                    n_channels=0, polarizations=())
        assert s.label() == "3"
