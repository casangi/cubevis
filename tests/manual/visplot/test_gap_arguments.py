"""
test_gap_arguments.py
=====================
Issue #36 part 4: ``time_gaps=`` and ``freq_gaps=`` as arguments.

The flat arguments set every panel's initial value; ``panels=[...]``
sets one panel's; the reproducing call and the plot summary carry them;
the generated casatasks / casashell ``visplot`` layers expose both.

Location in repository:
    cubevis/tests/manual/visplot/test_gap_arguments.py

Run:
    pytest cubevis/tests/manual/visplot/test_gap_arguments.py -v

Data: the simulated MS of ``test_freq_gaps`` (two spectral windows,
1.00 and 2.00 GHz).
"""
from __future__ import annotations

import os
import subprocess
import sys
import warnings

import numpy as np
import pytest

from cubevis_test_paths import ensure_cubevis_importable
ensure_cubevis_importable()

pytest.importorskip("xarray")

from cubevis.toolbox.visplot.axes import Axis                             # noqa: E402

warnings.filterwarnings("ignore")

NCHAN, DF = 16, 10.0e6


def _transform(desc, data):
    data["FLAG"] = (data["FLAG"][0], np.zeros_like(data["FLAG"][1]))
    return data


@pytest.fixture(scope="module")
def sim_ms(tmp_path_factory):
    sim = pytest.importorskip("xarray_ms.testing.simulator")
    path = str(tmp_path_factory.mktemp("gapargs") / "gapargs.ms")
    sim.MSStructureSimulator(
        ntime=8, time_chunks=8, nfield=1, nantenna=4, auto_corrs=False,
        data_description=[(1.00e9 + DF * np.arange(NCHAN), ["XX", "YY"]),
                          (2.00e9 + DF * np.arange(NCHAN), ["XX", "YY"])],
        simulate_data=True, transform_data=_transform).simulate_ms(path)
    return path


def _vp(sim_ms, **kw):
    from cubevis.toolbox.visplot import VisibilityPlotter
    kw.setdefault("layout", "side")
    return VisibilityPlotter(ms=sim_ms, **kw)


def _panels(vp):
    for sl in vp._slots:
        yield sl.raster
        yield sl.scatter


class TestFlatArguments:

    def test_defaults_are_shown(self, sim_ms):
        vp = _vp(sim_ms)
        try:
            for p in _panels(vp):
                assert p.time_gaps == "shown" and p.freq_gaps == "shown"
            w = vp._panel_axis_widgets
            for sl in vp._slots:
                for kind in ("raster", "scatter"):
                    assert w[sl.id][kind]["tgaps_sel"].value == "shown"
                    assert w[sl.id][kind]["fgaps_sel"].value == "shown"
        finally:
            vp.close()

    def test_closed_reaches_every_panel_and_its_controls(self, sim_ms):
        vp = _vp(sim_ms, time_gaps="closed", freq_gaps="closed")
        try:
            for p in _panels(vp):
                assert p.time_gaps == "closed" and p.freq_gaps == "closed"
            w = vp._panel_axis_widgets
            for sl in vp._slots:
                for kind in ("raster", "scatter"):
                    assert w[sl.id][kind]["tgaps_sel"].value == "closed"
                    assert w[sl.id][kind]["fgaps_sel"].value == "closed"
            # the raster (Time x Channel, two windows: drawn in Hz) was
            # drawn with both axes closed
            r = next(sl.raster for sl in vp._slots if sl.kind == "raster")
            assert r._time_axis is not None
            assert r._freq_axis is not None and len(r._freq_axis) == 2
        finally:
            vp.close()

    def test_booleans_are_accepted(self, sim_ms):
        vp = _vp(sim_ms, time_gaps=True, freq_gaps=False)
        try:
            r = vp._slots[0].raster
            assert r.time_gaps == "closed" and r.freq_gaps == "shown"
        finally:
            vp.close()

    @pytest.mark.parametrize("kw, name", [(dict(time_gaps="hidden"), "time_gaps"),
                                          (dict(freq_gaps="remove"), "freq_gaps")])
    def test_refused(self, sim_ms, kw, name):
        with pytest.raises(ValueError, match=name):
            _vp(sim_ms, **kw)


class TestPanelsArgument:

    def test_one_panel(self, sim_ms):
        vp = _vp(sim_ms, panels=[dict(time_gaps="closed"), dict(freq_gaps="closed")])
        try:
            a, b = vp._slots
            assert a.raster.time_gaps == "closed" and a.raster.freq_gaps == "shown"
            assert b.scatter.time_gaps == "shown" and b.scatter.freq_gaps == "closed"
        finally:
            vp.close()

    def test_panel_overrides_flat(self, sim_ms):
        vp = _vp(sim_ms, time_gaps="closed", panels=[None, dict(time_gaps="shown")])
        try:
            a, b = vp._slots
            assert a.raster.time_gaps == "closed"
            assert b.scatter.time_gaps == "shown"
        finally:
            vp.close()

    def test_refused(self, sim_ms):
        with pytest.raises(ValueError, match=r"panels\[1\]\.freq_gaps"):
            _vp(sim_ms, panels=[None, dict(freq_gaps="maybe")])


class TestSummary:

    def test_reproducing_call(self, sim_ms):
        vp = _vp(sim_ms, panels=[dict(time_gaps="closed"), dict(freq_gaps="closed")])
        try:
            call = vp.reproducing_call()
            assert "time_gaps='closed'" in call
            assert "freq_gaps='closed'" in call
        finally:
            vp.close()
        vp = _vp(sim_ms)
        try:
            assert "gaps" not in vp.reproducing_call()      # the default is left out
        finally:
            vp.close()

    def test_call_reproduces(self, sim_ms):
        vp = _vp(sim_ms, panels=[dict(time_gaps="closed"), dict(freq_gaps="closed")])
        try:
            from cubevis.toolbox.visplot.plot_summary import _slot_settings
            settings = [_slot_settings(vp, sl) for sl in vp._slots]
        finally:
            vp.close()
        vp = _vp(sim_ms, panels=settings)
        try:
            a, b = vp._slots
            assert a.raster.time_gaps == "closed" and b.scatter.freq_gaps == "closed"
        finally:
            vp.close()

    def test_summary_rows(self, sim_ms):
        from cubevis.toolbox.visplot.plot_summary import describe
        vp = _vp(sim_ms, time_gaps="closed")
        try:
            for sl in vp._slots:
                d = describe(vp, sl.id)
                rows = dict(r for _title, rs in d["sections"] for r in rs)
                assert rows["Time gaps"] == "closed"
                assert rows["Frequency gaps"] == "shown"
        finally:
            vp.close()


class TestTaskLayers:

    @pytest.mark.parametrize("layer", ["casatasks", "casashell"])
    def test_layers_have_the_arguments(self, layer):
        import cubevis
        path = os.path.join(os.path.dirname(cubevis.__file__), "private", layer, "visplot.py")
        with open(path) as fh:
            src = fh.read()
        # Preferences (issue 6, 2026-11-27): None takes the preference, else
        # "shown" (test_prefs.py checks the resolution)
        assert "time_gaps: Optional[str] = None" in src
        assert "freq_gaps: Optional[str] = None" in src

    def test_layers_in_sync(self):
        root = os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.abspath(__file__)))))
        if not os.path.isdir(os.path.join(root, "scripts", "sync_layers")):
            pytest.skip("not a source checkout")
        pytest.importorskip("jinja2")
        r = subprocess.run([sys.executable, "-m", "scripts.sync_layers", "--check"],
                           cwd=root, capture_output=True, text=True)
        assert r.returncode == 0, r.stdout + r.stderr
