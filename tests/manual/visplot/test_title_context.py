"""test_title_context.py -- the panel titles say what the selection is narrowed to.

Pre-preview feedback (#24): the value being stepped through (an SPW, a
field, an antenna, a baseline) should be in the plot title.  The PNG
export has the titles but not the status line, so PNGs saved while
stepping could not be told apart.  debug-0003: a default title ends with
"  —  <field>, SPW <n>, <antenna or baseline>" for each of those the
selection holds exactly one of, when the data has more than one
(``VisibilityPlotter._title_context``, ``VisibilityPlot._titled``).  A
caller's own title is left as it is, and grid cells (labelled by their
own value) get nothing.

Run:  python -m pytest test_title_context.py -q
"""
from __future__ import annotations

import pytest

from cubevis_test_paths import ensure_cubevis_importable

ensure_cubevis_importable()

from cubevis.toolbox.visplot.visibility_plot import wrap_title  # noqa: E402


@pytest.fixture(scope="module")
def sim_ms(tmp_path_factory):
    sim = pytest.importorskip("xarray_ms.testing.simulator")
    path = str(tmp_path_factory.mktemp("ttl") / "s.ms")
    sim.MSStructureSimulator(
        ntime=8, nantenna=4, auto_corrs=False,
        data_description=[(8, ["XX", "YY"])],
        simulate_data=True).simulate_ms(path)
    return path


def _plotter(sim_ms, **kw):
    from cubevis.toolbox.visplot import VisibilityPlotter
    return VisibilityPlotter(ms=sim_ms, layout="side", correlation="XX,YY", **kw)


def _titles(vp):
    out = []
    for s in vp._slots:
        for p in (getattr(s, "raster", None), getattr(s, "scatter", None)):
            if p is not None:
                out.append(p._effective_title())
    return out


def test_nothing_narrowed_no_suffix(sim_ms):
    vp = _plotter(sim_ms)
    try:
        assert vp._title_context() == ""
        assert all("—" not in t for t in _titles(vp))
    finally:
        vp.close()


def test_one_antenna(sim_ms):
    vp = _plotter(sim_ms, antenna="ANTENNA-2")
    try:
        assert vp._title_context() == "antenna ANTENNA-2"
        for t in _titles(vp):
            assert t.replace("\n", " ").endswith("\u2014  antenna ANTENNA-2"), t
    finally:
        vp.close()


def test_one_spw_of_several(sim_ms, monkeypatch):
    # The simulator's SPWs carry no usable identity, so the SPW branch is
    # driven through the same helpers the status line uses.
    from types import SimpleNamespace
    from cubevis.toolbox.visplot import visibility_plotter as vpm
    vp = _plotter(sim_ms)
    try:
        monkeypatch.setattr(vp, "_spw_ids", [7], raising=False)
        monkeypatch.setattr(vp, "_meta", SimpleNamespace(
            spws=[SimpleNamespace(spw_id=3, name="none"), SimpleNamespace(spw_id=7, name="none")],
            fields=vp._meta.fields, antennas=vp._meta.antennas,
            baselines=getattr(vp._meta, "baselines", None)))
        monkeypatch.setattr(vpm, "_field_iteration_position", lambda f, m: None)
        monkeypatch.setattr(vp, "_antenna_baseline_selection", lambda: (None, None))
        assert vp._title_context() == "SPW 7"
        vp._meta.spws[1].name = "EVLA_X#A0C0#3"
        assert vp._title_context() == "SPW EVLA_X#A0C0#3"
        monkeypatch.setattr(vp, "_spw_ids", [3, 7], raising=False)
        assert vp._title_context() == ""
    finally:
        vp.close()


def test_one_baseline(sim_ms):
    vp = _plotter(sim_ms, antenna="ANTENNA-0&ANTENNA-3")
    try:
        assert vp._title_context() == "ANTENNA-0&ANTENNA-3"
    finally:
        vp.close()


def test_a_callers_title_is_left_alone(sim_ms):
    vp = _plotter(sim_ms, antenna="ANTENNA-1")
    try:
        r = next(s.raster for s in vp._slots if getattr(s, "raster", None))
        r._title = "My title"
        assert r._effective_title() == "My title"
    finally:
        vp.close()


def test_the_dash_starts_the_second_line():
    t = wrap_title("Amplitude (mean of baselines)  [Time vs Channel]  pol=LL  —  "
                   "1642+689, SPW 1, DA41&DV01", limit=60)
    first, second = t.split("\n")
    assert not first.rstrip().endswith("—")
