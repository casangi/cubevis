"""visplot's Grid layout (gridplot G2) without a browser: the grid source, the
headless page export, the GUI's grid models and the Plot handler's Grid path.

    pytest -q tests/manual/gridplot/test_vis_grid.py

Data: the quick tests' synthetic MSv2 (``tests/ci/make_tiny_ms.py``: 5
antennas, 10 baselines, 2 SPWs of 8 channels), written once per session.
The browser half is ``visplot_grid_check.py``.
"""
from __future__ import annotations

import asyncio
import os
import sys
import warnings

import numpy as np
import pytest

warnings.filterwarnings("ignore")
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
pytest.importorskip("xradio")
pytest.importorskip("arcae")


@pytest.fixture(scope="session")
def ms(tmp_path_factory):
    sys.path.insert(0, os.path.join(ROOT, "tests", "ci"))
    import make_tiny_ms
    return make_tiny_ms.make(str(tmp_path_factory.mktemp("grid") / "tiny.ms"))


@pytest.fixture(scope="session")
def plotter(ms):
    from cubevis.toolbox.visplot.visibility_plotter import VisibilityPlotter
    p = VisibilityPlotter(ms=ms, headless=True, layout="one", preset="waterfall")
    yield p
    p.close()


# --- arguments ---------------------------------------------------------------

def test_exprange():
    from cubevis.toolbox.visplot.visibility_plotter import parse_exprange
    assert parse_exprange(None) == "current"
    assert parse_exprange("current") == "current"
    assert parse_exprange("ALL") == "all"
    assert parse_exprange("2") == [2]
    assert parse_exprange("1-3, 5") == [1, 2, 3, 5]
    for bad in ("x", "0", "3-1", ","):
        with pytest.raises(ValueError):
            parse_exprange(bad)


def test_iterate_by_names():
    from cubevis.toolbox.visplot.vis_grid_source import normalize_iterate_by
    assert normalize_iterate_by("Baselines") == "baseline"
    assert normalize_iterate_by("ant") == "antenna"
    assert normalize_iterate_by("corr") == "correlation"
    with pytest.raises(ValueError):
        normalize_iterate_by("time")


def test_constructor_checks(ms):
    from cubevis.toolbox.visplot.visibility_plotter import VisibilityPlotter
    for kw in (dict(gridrows=0), dict(gridcols=9), dict(gridrows=2.5),
               dict(iterate_by="nope"), dict(exprange="x")):
        with pytest.raises(ValueError):
            VisibilityPlotter(ms=ms, headless=True, layout="grid", **kw)


# --- the source ----------------------------------------------------------------

@pytest.mark.parametrize("by,n", [("baseline", 10), ("antenna", 5), ("spw", 2),
                                  ("field", 1), ("correlation", None)])
def test_iteration_values(plotter, by, n):
    from cubevis.toolbox.visplot.vis_grid_source import VisGridSource
    src = VisGridSource(plotter).configure(iterate_by=by)
    labels = src.labels()
    if n is not None:
        assert len(labels) == n, labels
    assert len(set(labels)) == len(labels)
    t = src.drill_target(0)
    assert len(t) == 1 and next(iter(t.values()))


def test_cells_drawn_at_a_common_extent(plotter):
    from cubevis.toolbox.visplot.vis_grid_source import VisGridSource
    src = VisGridSource(plotter).configure(iterate_by="baseline")
    ext = src.extent()
    spec = plotter._pos0._panel_spec()
    assert ext == (*map(float, spec.x_range), *map(float, spec.y_range))
    cells = src.render([0, 1, 2], ext, (160, 100))
    assert [c.spec.title for c in cells] == src.labels()[:3]
    assert all(c.spec.status == "ok" and c.image.shape == (100, 160) for c in cells)
    assert all(c.image.dtype == np.uint32 for c in cells)
    # a zoom re-shades what the cell holds
    x0, x1, y0, y1 = ext
    z = src.render([1], (x0, (x0 + x1) / 2, y0, y1), (160, 100))[0]
    assert z.spec.status == "ok" and z.viewport[1] == (x0 + x1) / 2
    names = src.axis_names()
    assert ("Channel" in names[0] or "Frequency" in names[0]) and "Time" in names[1]
    assert src.probe(1, (x0 + x1) / 2, (y0 + y1) / 2).startswith(f"<b>{src.labels()[1]}</b>")
    out = src.render([999], ext, (160, 100))[0]
    assert out.image is None and out.spec.note


def _ranges(src, idx, colour, page=None):
    """Each drawn cell's (vmin, vmax) as its colour bar has it."""
    from cubevis.toolbox.visplot.visibility_raster import VisibilityRaster
    src.render(idx, src.extent(), (120, 90), colour=colour, page_indices=page)
    out = []
    for i in idx:
        c = src._cells[i]
        spec = c._panel_spec()
        m = VisibilityRaster._bands_with_mappings(c, spec)[0].mapping
        out.append((round(m.vmin, 6), round(m.vmax, 6)))
    return out


def test_selection_narrows_the_cells(plotter):
    """An antenna or baseline selection (the steppers' too) narrows a grid
    by baseline to those baselines (gridplot, 2026-11-12)."""
    from cubevis.toolbox.visplot.vis_grid_source import VisGridSource
    names = [a.name for a in plotter._meta.antennas]
    sel = plotter._selection.copy()
    sel.antenna_names = [names[2]]
    src = VisGridSource(plotter).configure(iterate_by="baseline", selection=sel)
    labels = src.labels()
    assert len(labels) == len(names) - 1
    assert all(names[2] in lab.split("-") for lab in labels)
    sel = plotter._selection.copy()
    sel.baselines = [[names[0], names[1]], [names[1], names[2]]]
    src.configure(iterate_by="baseline", selection=sel)
    assert src.labels() == [f"{names[0]}-{names[1]}", f"{names[1]}-{names[2]}"]
    src.configure(iterate_by="baseline", selection=plotter._selection)
    assert len(src.labels()) == len(names) * (len(names) - 1) // 2


def test_colour_scopes(plotter):
    """per cell: each its own range; this page: one over the page; whole
    grid: one over every cell, after the background pass (gridplot, 11-11)."""
    from cubevis.toolbox.visplot.vis_grid_source import VisGridSource
    src = VisGridSource(plotter).configure(iterate_by="baseline")
    n = len(src.labels())
    page = list(range(4))
    own = _ranges(src, page, "cell")
    assert len(set(own)) > 1, own                       # the baselines differ
    shared = _ranges(src, page, "page")
    assert len(set(shared)) == 1
    lo, hi = shared[0]
    assert lo == min(r[0] for r in own) and hi == max(r[1] for r in own)
    # a cell drawn again alone (a zoom) keeps its page's range
    assert _ranges(src, [1], "page", page) == [shared[0]]
    st = src.colour_state("grid")
    assert st["pending"] and f"/{n} cells" in st["note"]
    assert src.wait_for_grid_range(timeout=120)
    assert not src.colour_state("grid")["pending"]
    whole = _ranges(src, page, "grid")
    assert len(set(whole)) == 1
    allc = _ranges(src, list(range(n)), "cell")
    assert whole[0] == (min(r[0] for r in allc), max(r[1] for r in allc))
    # a new configuration forgets the ranges
    src.configure(iterate_by="baseline")
    assert src.colour_state("grid")["pending"]
    with pytest.raises(ValueError):
        src.render([0], None, (120, 90), colour="everything")


def test_colour_eq_hist_shares_the_curve(plotter):
    """eq_hist on a page: one curve (the pooled values'), not one per cell."""
    from cubevis.toolbox.visplot.vis_grid_source import VisGridSource
    old = plotter._pos0._scaling
    plotter._pos0._scaling = "eq_hist"
    try:
        src = VisGridSource(plotter).configure(iterate_by="baseline")
        src.render([0, 1], src.extent(), (120, 90), colour="page")
        a, b = src._cells[0], src._cells[1]
        assert a._shared_reference is b._shared_reference is not None
        ma = a._bands_with_mappings(a._panel_spec())[0].mapping
        mb = b._bands_with_mappings(b._panel_spec())[0].mapping
        assert np.allclose(ma._x, mb._x) and np.allclose(ma._u, mb._u)
    finally:
        plotter._pos0._scaling = old


def test_gridcolour_argument(ms):
    from cubevis.toolbox.visplot.visibility_plotter import VisibilityPlotter
    with pytest.raises(ValueError):
        VisibilityPlotter(ms=ms, headless=True, gridcolour="all")


def test_theme_recolours_kept_cells(plotter):
    from cubevis.toolbox.visplot.vis_grid_source import VisGridSource
    from cubevis.toolbox.gridplot import palettes
    src = VisGridSource(plotter).configure(iterate_by="spw")
    before = src.render([0], src.extent(), (120, 90))[0].image.copy()
    src.set_theme("light", palettes.raster_cmap("viridis", "light"))
    after = src.render([0], src.extent(), (120, 90))[0].image
    assert not np.array_equal(before, after)


def test_scatter_template_refused(ms):
    from cubevis.toolbox.visplot.visibility_plotter import VisibilityPlotter
    from cubevis.toolbox.visplot.vis_grid_source import GridTemplateError, VisGridSource
    with VisibilityPlotter(ms=ms, headless=True, layout="scatter") as p:
        with pytest.raises(GridTemplateError):
            VisGridSource(p).configure()


# --- plotfile= -----------------------------------------------------------------

def test_plotfile_pages(ms, tmp_path):
    from cubevis.toolbox.visplot.visibility_plotter import VisibilityPlotter
    out = str(tmp_path / "wf.png")
    with VisibilityPlotter(ms=ms, layout="grid", preset="waterfall", gridrows=2, gridcols=2,
                           plotfile=out) as p:
        assert p() == out and os.path.getsize(out) > 5000          # current: page 1
        paths = p(exprange="all")
        assert [os.path.basename(x) for x in paths] == [f"wf_page{i:02d}.png" for i in (1, 2, 3)]
        one = p(exprange="2", plotfile=str(tmp_path / "p{page}_{value}.png"))
        assert os.path.basename(one).startswith("p02_")
        with pytest.raises(ValueError):
            p(exprange="7")


def test_plotfile_iterate_by_antenna(ms, tmp_path):
    from cubevis.toolbox.visplot.visibility_plotter import VisibilityPlotter
    out = str(tmp_path / "ant.png")
    with VisibilityPlotter(ms=ms, layout="grid", preset="vplot", iterate_by="antenna",
                           gridrows=1, gridcols=5, plotfile=out) as p:
        assert p() == out
        assert "Grid by antenna" in p._status_text()


# --- the GUI's models and the Plot handler --------------------------------------

def test_gui_models(ms):
    from cubevis.toolbox.visplot.visibility_plotter import VisibilityPlotter
    with VisibilityPlotter(ms=ms, preset="waterfall", layout="grid", gridrows=2,
                           gridcols=3) as p:
        assert p._grid_on and p._layout_rbg.active == 3
        assert p._layout_rbg.labels[-1] == "Grid"
        assert p._grid_area.visible and not p._plot_area.visible
        assert list(p._grid_area.children) == [p._grid_container]
        assert p._grid_view.key and len(p._grid_source.labels()) == 10
        assert {d for _, _, d in p._app_context.init_scripts} >= {"grid view", "grid layout"}
    with VisibilityPlotter(ms=ms, preset="waterfall") as p:
        # not shown yet: the grid is attached when Grid is first chosen
        assert not p._grid_on and not p._grid_area.visible
        assert list(p._grid_area.children) == []


def test_plot_in_grid(ms):
    from cubevis.toolbox.visplot.visibility_plotter import VisibilityPlotter
    with VisibilityPlotter(ms=ms, preset="waterfall") as p:
        resp = asyncio.run(p._handle_plot({"layout": "grid", "iterate_by": "spw"}))
        assert resp["status"] == "ok", resp
        assert resp["grid"] == {"n_cells": 2, "iterate_by": "spw"}
        assert p._grid_on and p._layout == "over"          # the panels' layout kept
        assert "Grid by spw" in resp["status_text"]
        d = p._grid_drill(1, p._grid_source.labels()[1])
        assert d["select"] == {"spw": p._grid_source.values()[1]["spw"][0]}
        page = p._grid_view.page({"page": 0, "rows": 2, "cols": 2, "w": 140, "h": 100})
        assert page["n_cells"] == 2 and all("image" in c and "axes" in c for c in page["cells"])
        resp = asyncio.run(p._handle_plot({"layout": "one"}))
        assert resp["status"] == "ok" and not p._grid_on


def test_reproducing_call(ms):
    from cubevis.toolbox.visplot.visibility_plotter import VisibilityPlotter
    with VisibilityPlotter(ms=ms, preset="waterfall") as p:
        asyncio.run(p._handle_plot({"layout": "grid", "iterate_by": "antenna",
                                    "grid_shape": [2, 4]}))
        call = p.reproducing_call()
        assert "layout='grid'" in call or 'layout="grid"' in call, call
        assert "antenna" in call and "gridrows=2" in call and "gridcols=4" in call
