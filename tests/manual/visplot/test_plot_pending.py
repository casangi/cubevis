"""
test_plot_pending.py
====================
Plot ▶ turns orange when a control Plot reads has changed since the last
successful Plot, and green when it answers (2026-10-18).

Structure only (which controls carry the callback; the reset in doPlot's
reply).  The colour change itself was checked live with
devel/tools/visplot_headless (see hrs_h8_consolidation_prep.md section 24).
"""
from __future__ import annotations

import os
import warnings

import pytest

warnings.filterwarnings("ignore")

MS = os.environ.get("MS", "")


def _plotter(tmp_path_factory):
    from cubevis.toolbox.visplot import VisibilityPlotter
    if os.path.isdir(MS):
        return VisibilityPlotter(ms=MS, field="3c279")
    sim = pytest.importorskip("xarray_ms.testing.simulator")
    path = str(tmp_path_factory.mktemp("pp") / "sim.ms")
    sim.MSStructureSimulator(ntime=4, nantenna=4, data_description=[(8, ["XX", "YY"])],
                             simulate_data=True).simulate_ms(path)
    return VisibilityPlotter(ms=path)


@pytest.fixture(scope="module")
def vp(tmp_path_factory):
    p = _plotter(tmp_path_factory)
    p._build_layout()
    yield p
    p.close()


def _marked(vp):
    m = vp._plot_pending_js
    out = set()
    for name, w in vp._plot_js_args.items():
        cbs = getattr(w, "js_property_callbacks", {}) or {}
        if any(m in v for v in cbs.values()):
            out.add(name)
        sel = getattr(w, "selected", None)
        if sel is not None and any(m in v for v in sel.js_property_callbacks.values()):
            out.add(name)
    return out


def test_data_and_panel_controls_mark_pending(vp):
    got = _marked(vp)
    for name in ("field_sel", "scan_inp", "time_inp", "uv_inp", "ant_src", "bl_src",
                 "ant_mode", "spw_src", "corr_cbg", "autocorr_rbg", "col_sel",
                 "panel0_kind_switch", "panel0_ry_sel", "panel0_rq_sel", "panel0_rk_sel",
                 "panel1_sx_sel", "panel1_sy_sel", "panel1_sat_sel"):
        assert name in got, name


def test_controls_that_act_at_once_do_not(vp):
    got = _marked(vp)
    for name in ("layout_rbg", "panel_a_pin", "panel_b_pin"):
        assert name not in got, name
    assert not [n for n in got if "img" in n]


def test_reply_resets_and_hint_says_so(vp):
    assert "window.__cvPlotPending(false)" in vp._do_plot_js
    assert "Orange" in vp._hint_tb_plot.text
