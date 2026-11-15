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


def _ranges(src, idx, color, page=None):
    """Each drawn cell's (vmin, vmax) as its color bar has it."""
    from cubevis.toolbox.visplot.visibility_raster import VisibilityRaster
    src.render(idx, src.extent(), (120, 90), color=color, page_indices=page)
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


@pytest.mark.parametrize("rows, cols, n, want", [
    (3, 3, 1, (1, 1)), (3, 3, 2, (1, 2)), (3, 3, 4, (2, 2)), (3, 3, 5, (2, 3)),
    (3, 3, 9, (3, 3)), (3, 3, 10, (3, 3)), (4, 4, 7, (2, 4)), (2, 5, 3, (1, 3)),
    (3, 3, 0, (3, 3))])
def test_fit_grid_shape(rows, cols, n, want):
    """Rows x cols is a page's most: all the cells on one page shrink it
    (gridplot, 2026-11-12)."""
    from cubevis.toolbox.visplot.visibility_plotter import fit_grid_shape
    assert fit_grid_shape(rows, cols, n) == want


def test_color_scopes(plotter):
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
    st = src.color_state("grid")
    assert st["pending"] and f"/{n} cells" in st["note"]
    assert src.wait_for_grid_range(timeout=120)
    assert not src.color_state("grid")["pending"]
    whole = _ranges(src, page, "grid")
    assert len(set(whole)) == 1
    allc = _ranges(src, list(range(n)), "cell")
    assert whole[0] == (min(r[0] for r in allc), max(r[1] for r in allc))
    # a new configuration forgets the ranges
    src.configure(iterate_by="baseline")
    assert src.color_state("grid")["pending"]
    with pytest.raises(ValueError):
        src.render([0], None, (120, 90), color="everything")


def test_color_eq_hist_shares_the_curve(plotter):
    """eq_hist on a page: one curve (the pooled values'), not one per cell."""
    from cubevis.toolbox.visplot.vis_grid_source import VisGridSource
    old = plotter._pos0._scaling
    plotter._pos0._scaling = "eq_hist"
    try:
        src = VisGridSource(plotter).configure(iterate_by="baseline")
        src.render([0, 1], src.extent(), (120, 90), color="page")
        a, b = src._cells[0], src._cells[1]
        assert a._shared_reference is b._shared_reference is not None
        ma = a._bands_with_mappings(a._panel_spec())[0].mapping
        mb = b._bands_with_mappings(b._panel_spec())[0].mapping
        assert np.allclose(ma._x, mb._x) and np.allclose(ma._u, mb._u)
    finally:
        plotter._pos0._scaling = old


def test_gridcolor_argument(ms):
    from cubevis.toolbox.visplot.visibility_plotter import VisibilityPlotter
    with pytest.raises(ValueError):
        VisibilityPlotter(ms=ms, headless=True, gridcolor="all")


def test_theme_recolors_kept_cells(plotter):
    from cubevis.toolbox.visplot.vis_grid_source import VisGridSource
    from cubevis.toolbox.gridplot import palettes
    src = VisGridSource(plotter).configure(iterate_by="spw")
    before = src.render([0], src.extent(), (120, 90))[0].image.copy()
    src.set_theme("light", palettes.raster_cmap("viridis", "light"))
    after = src.render([0], src.extent(), (120, 90))[0].image
    assert not np.array_equal(before, after)


def test_scatter_template(ms):
    """Scatter grids (G3 item 3): a scatter template draws scatter cells,
    each its own density scale whatever the color scope."""
    from cubevis.toolbox.visplot.visibility_plotter import VisibilityPlotter
    from cubevis.toolbox.visplot.vis_grid_source import VisGridSource
    with VisibilityPlotter(ms=ms, headless=True, layout="scatter") as p:
        src = VisGridSource(p).configure()
        assert src._kind == "scatter" and len(src.labels()) > 1
        r = src.render([0, 1], src.extent(), (160, 120), color="page")
        assert [x.spec.kind for x in r] == ["scatter", "scatter"]
        assert all(x.spec.status == "ok" and x.image is not None for x in r)
        assert src.color_state("page")["note"].startswith("scatter")
        assert src._shared_reference("grid", [0, 1]) is None


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


# --- the grid's own panel (gridplot G3) -------------------------------------------

def test_grid_panel_argument(ms):
    from cubevis.toolbox.visplot.visibility_plotter import VisibilityPlotter
    from cubevis.toolbox.visplot.axes import Axis
    for bad in ({"raster_qty": "NOPE"}, {"color": "red"}, "PHASE",
                {"raster_y": "TIME", "raster_x": "TIME"}):
        with pytest.raises(ValueError):
            VisibilityPlotter(ms=ms, headless=True, grid_panel=bad)
    with VisibilityPlotter(ms=ms, headless=True, preset="waterfall") as p:
        g, a = p._grid_slot, p._slots[0]
        assert g.raster is not a.raster and g.raster._headless
        assert (g.raster._y_dim, g.raster._x_dim, g.raster._quantity) == \
               (a.raster._y_dim, a.raster._x_dim, a.raster._quantity)      # a copy of A
    with VisibilityPlotter(ms=ms, headless=True, preset="waterfall",
                           grid_panel=dict(raster_x="BASELINE", raster_qty="PHASE")) as p:
        r = p._grid_slot.raster
        assert (r._y_dim, r._x_dim, r._quantity) == (Axis.TIME, Axis.BASELINE, Axis.PHASE)
        assert p._slots[0].raster._quantity == Axis.AMPLITUDE            # A as the preset says
        # the grid's panel is never drawn: only its cells are
        assert r._agg is None


def _counting(p, monkeypatch):
    """Panels A and B (both kinds) that draw from now on (``_render``)."""
    from cubevis.toolbox.visplot.visibility_raster import VisibilityRaster
    from cubevis.toolbox.visplot.visibility_scatter import VisibilityScatter
    calls, mine = [], {id(x) for x in p._all_panels}
    for cls in (VisibilityRaster, VisibilityScatter):
        orig = cls._render
        def wrap(self, *a, _o=orig, **k):
            if id(self) in mine:
                calls.append(self)
            return _o(self, *a, **k)
        monkeypatch.setattr(cls, "_render", wrap)
    return calls


def test_grid_panel_is_its_own(ms, monkeypatch):
    from cubevis.toolbox.visplot.visibility_plotter import VisibilityPlotter
    from cubevis.toolbox.visplot.axes import Axis
    with VisibilityPlotter(ms=ms, preset="waterfall") as p:
        calls = _counting(p, monkeypatch)
        # the first Plot in Grid: the grid's panel becomes a copy of Panel A
        resp = asyncio.run(p._handle_plot({"layout": "grid", "grid_slot": "A",
                                           "field": "", "panels": {}}))
        assert resp["status"] == "ok", resp
        assert resp["grid_template"]["rq"] == "AMPLITUDE" and resp["panels_stale"]
        assert resp["panels"] == {} and calls == []                    # A and B not drawn
        # the grid's tab, edited and plotted: only the grid's panel changes
        g = {"kind": "raster",
             "raster": {"y": "TIME", "x": "CHANNEL", "qty": "PHASE", "averaging": "scalar"},
             "color": {"scaling": "auto", "vmin": "", "vmax": ""}}
        resp = asyncio.run(p._handle_plot({"layout": "grid", "panels": {"G": g}}))
        assert resp["status"] == "ok", resp
        assert "grid_template" not in resp and calls == []
        r = p._grid_slot.raster
        assert r._quantity == Axis.PHASE and r._averaging == "scalar"
        assert (r._scaling, r._scaling_vmin, r._scaling_vmax) == ("linear", -180.0, 180.0)
        assert p._slots[0].raster._quantity == Axis.AMPLITUDE
        assert p._grid_source._template is r
        # back to the panels: they are drawn for the grid's selection
        resp = asyncio.run(p._handle_plot({"layout": "one", "panels": {
            "A": {"kind": "raster", "y": "TIME", "x": "CHANNEL", "qty": "AMPLITUDE"}}}))
        assert resp["status"] == "ok" and not p._grid_on


def test_grid_panel_color_and_settings(ms):
    from cubevis.toolbox.visplot.visibility_plotter import VisibilityPlotter
    with VisibilityPlotter(ms=ms, preset="waterfall",
                           grid_panel=dict(raster_qty="PHASE")) as p:
        assert not p._grid_tmpl_pending                     # grid_panel= given: no copy
        assert p.grid_panel_settings()["raster_qty"] == "PHASE"
        assert "scaling" not in p.grid_panel_settings()     # the quantity's own
        g = {"kind": "raster", "raster": {"y": "TIME", "x": "CHANNEL", "qty": "AMPLITUDE"},
             "color": {"scaling": "linear", "vmin": "0.5", "vmax": ""}}
        resp = asyncio.run(p._handle_plot({"layout": "grid", "panels": {"G": g}}))
        assert resp["status"] == "ok", resp
        r = p._grid_slot.raster
        assert (r._scaling, r._scaling_vmin, r._scaling_vmax) == ("linear", 0.5, None)
        s = p.grid_panel_settings()
        assert s["scaling"] == "linear" and s["vmin"] == 0.5 and "vmax" not in s
        # every cell is drawn with it
        cell = p._grid_source._cell(0)
        assert (cell._scaling, cell._scaling_vmin) == ("linear", 0.5)
        bad = dict(g, color={"vmin": "low"})
        resp = asyncio.run(p._handle_plot({"layout": "grid", "panels": {"G": bad}}))
        assert resp["status"] == "error" and resp["failed_slot"] == "G"
        sc = {"kind": "scatter", "scatter": {"x": "TIME", "y": "AMPLITUDE"}}
        resp = asyncio.run(p._handle_plot({"layout": "grid", "panels": {"G": sc}}))
        assert resp["status"] == "ok" and p._grid_source._kind == "scatter"


def test_drill_down_copies_the_grid_panel(ms):
    from cubevis.toolbox.visplot.visibility_plotter import VisibilityPlotter
    from cubevis.toolbox.visplot.axes import Axis
    with VisibilityPlotter(ms=ms, preset="waterfall",
                           grid_panel=dict(raster_y="TIME", raster_x="BASELINE",
                                           raster_qty="PHASE")) as p:
        assert asyncio.run(p._handle_plot({"layout": "grid", "iterate_by": "spw"}))["status"] == "ok"
        d = p._grid_drill(0, p._grid_source.labels()[0])
        assert (d["panel"]["rq"], d["panel"]["rx"]) == ("PHASE", "BASELINE")
        assert d["color"]["scaling"] == "linear" and d["color"]["vmin"] == -180.0
        # the browser sets Panel A's tab from d["panel"] and sends the color
        resp = asyncio.run(p._handle_plot({"layout": "one", "spw_ids": [d["select"]["spw"]],
            "panels": {"A": {"kind": "raster", "y": d["panel"]["ry"], "x": d["panel"]["rx"],
                             "qty": d["panel"]["rq"],
                             "color": dict(d["color"], scaling="sqrt")}}}))
        assert resp["status"] == "ok", resp
        a = p._slots[0].raster
        assert (a._quantity, a._x_dim, a._scaling, a._scaling_vmin) == \
               (Axis.PHASE, Axis.BASELINE, "sqrt", -180.0)


def test_plotfile_grid_panel(ms, tmp_path):
    from cubevis.toolbox.visplot.visibility_plotter import VisibilityPlotter
    out = str(tmp_path / "gp.png")
    with VisibilityPlotter(ms=ms, layout="grid", preset="waterfall", iterate_by="spw",
                           grid_panel=dict(raster_qty="PHASE"), plotfile=out) as p:
        a = p._slots[0].raster
        assert a._agg is None                         # Panel A is not drawn for the grid
        assert p() == out and os.path.getsize(out) > 5000
        assert p._grid_source._cell(0)._quantity.name == "PHASE"
        call = p.reproducing_call()
        assert "grid_panel=dict(" in call and "raster_qty='PHASE'" in call, call
        # the panels are still there to export: brought up to date when shown
        one = p(layout="one", plotfile=str(tmp_path / "one.png"))
        assert os.path.getsize(one) > 5000 and a._agg is not None


# --- read once, split (gridplot G3 item 2) ---------------------------------------

def _grid_images(ms, monkeypatch, held, preset, by, flags=False, **kw):
    """Every cell of a headless grid, drawn with the rows held in memory
    (*held*) or read cell by cell; also what the source says it held."""
    from cubevis.toolbox.visplot.visibility_plotter import VisibilityPlotter
    from cubevis.toolbox.visplot.flag_model import FlagDelta
    monkeypatch.setenv("CUBEVIS_VISPLOT_GRID_PRELOAD", "1" if held else "0")
    with VisibilityPlotter(ms=ms, headless=True, layout="grid", preset=preset,
                           iterate_by=by, **kw) as p:
        if flags:                          # a pending flag over part of the time
            t0, t1 = p._meta.time_range
            p._reader.set_pending_flags([FlagDelta(time_range=(t0, t0 + (t1 - t0) / 3))], 1)
            p._selection.pending_version = 1
        src = p._grid()
        n = len(src.labels())
        imgs = [r.image for r in src.render(list(range(n)), src.extent(), (160, 120),
                                            color="cell")]
        return imgs, dict(src.preload_info)


@pytest.mark.parametrize("preset, by, flags, kw", [
    ("waterfall", "baseline", False, {}),
    ("waterfall", "antenna", True, {}),
    ("vplot", "antenna", False, {}),
    ("vplot", "spw", True, {}),
    ("zscore", "baseline", False, {}),
    ("waterfall", "correlation", False, {}),
    ("waterfall", "scan", False, {"autocorr": "include"}),
    ("waterfall", "baseline", True, {"uvrange": "0~2000m"}),
    ("vplot", "baseline", False, {"grid_panel": {"raster_qty": "PHASE",
                                                  "baseline_combine": "median"}}),
])
def test_rows_held_draw_the_same(ms, monkeypatch, preset, by, flags, kw):
    """The cells drawn from the rows held in memory are pixel for pixel the
    cells read one by one -- whatever the quantity, iteration, pending flags
    and selection."""
    read, info0 = _grid_images(ms, monkeypatch, False, preset, by, flags, **kw)
    held, info = _grid_images(ms, monkeypatch, True, preset, by, flags, **kw)
    assert info0["key"] is None and info["key"], info
    assert len(read) == len(held) > 1
    for i, (a, b) in enumerate(zip(read, held)):
        assert a is not None and np.array_equal(a, b), f"cell {i} differs"


def test_rows_held_only_when_asked(ms):
    """The reader serves the held rows only to a selection with the key,
    within the held rows, at the same cache generation."""
    from dataclasses import replace
    from cubevis.toolbox.visplot.visibility_plotter import VisibilityPlotter
    with VisibilityPlotter(ms=ms, headless=True, preset="waterfall") as p:
        be = p._reader._backend
        base = replace(p._selection, antenna_names=[p._meta.antennas[0].name])
        key = be.preload(base)["key"]
        assert key and be._preloaded_partitions(replace(base, preload=key))
        a0 = p._meta.antennas[0].name
        mine = [b.pair for b in p._meta.baselines if a0 in b.pair][:1]
        other = [b.pair for b in p._meta.baselines if a0 not in b.pair][:1]
        assert be._preloaded_partitions(replace(base, preload=key, antenna_names=None,
                                                baselines=[tuple(x) for x in mine]))
        assert be._preloaded_partitions(replace(base, preload=key, antenna_names=None,
                                                baselines=[tuple(x) for x in other])) is None
        assert be._preloaded_partitions(base) is None                          # no key
        assert be._preloaded_partitions(replace(base, preload="other")) is None
        assert be._preloaded_partitions(replace(base, preload=key,
                                                cache_generation=7)) is None  # Reload
        be._clear_frame_cache()                                                # a commit
        assert be._preloaded_partitions(replace(base, preload=key)) is None
        tiny = be.preload(p._selection, budget_mb=0.001)                       # too big
        assert tiny["key"] is None and "MiB" in tiny["reason"]


@pytest.mark.parametrize("narrow, flags, kw", [
    ("baselines", False, {}),
    ("antenna_names", True, {}),
    ("baselines", False, {"scatter_avg_time": "scan"}),
])
def test_scatter_cells_cut_from_the_union(ms, narrow, flags, kw):
    """A scatter cell's samples cut from the union's frames (read once) draw
    as the cell read by itself does -- scatter grids (item 3) will use it."""
    from dataclasses import replace
    from cubevis.toolbox.visplot.visibility_plotter import VisibilityPlotter
    from cubevis.toolbox.visplot.refresh import RefreshLevel
    from cubevis.toolbox.visplot.flag_model import FlagDelta
    with VisibilityPlotter(ms=ms, headless=True, layout="one", kind="scatter",
                           scatter_x="TIME", scatter_y="AMPLITUDE", **kw) as p:
        if flags:
            t0, t1 = p._meta.time_range
            p._reader.set_pending_flags([FlagDelta(time_range=(t0, t0 + (t1 - t0) / 3))], 1)
            p._selection.pending_version = 1
        sc = p._slots[0].scatter
        cells = ([{"baselines": [tuple(b.pair)]} for b in p._meta.baselines[:4]]
                 if narrow == "baselines" else
                 [{"antenna_names": [a.name]} for a in p._meta.antennas[:3]])
        key = p._reader.preload(p._selection)["key"]
        assert key
        for cell in cells:
            imgs = []
            for k in (None, key):
                sc._selection = replace(p._selection, preload=k, **cell)
                sc.apply_refresh(RefreshLevel.QUERY)
                imgs.append(sc.render_result(None).image.copy())
            assert np.array_equal(*imgs), cell


# --- scatter grids (gridplot G3 item 3) -------------------------------------------

@pytest.mark.parametrize("by, flags, panel", [
    ("baseline", False, {"scatter_x": "TIME", "scatter_y": "AMPLITUDE"}),
    ("antenna", True, {"scatter_x": "TIME", "scatter_y": "PHASE"}),
    ("correlation", False, {"scatter_x": "FREQUENCY", "scatter_y": "AMPLITUDE",
                            "scatter_avg_time": "scan"}),
    ("spw", True, {"scatter_x": "TIME", "scatter_y": "AMPLITUDE",
                   "scatter_avg_chan": "all"}),
])
def test_scatter_grid_held_draws_the_same(ms, monkeypatch, by, flags, panel):
    """A scatter grid's cells, from the rows held in memory and cut from the
    union's frames, are pixel for pixel the cells read one by one."""
    gp = dict(kind="scatter", **panel)
    read, _ = _grid_images(ms, monkeypatch, False, None, by, flags, grid_panel=gp)
    held, info = _grid_images(ms, monkeypatch, True, None, by, flags, grid_panel=gp)
    assert info["key"] and len(read) == len(held) > 1
    for i, (a, b) in enumerate(zip(read, held)):
        assert a is not None and np.array_equal(a, b), f"cell {i} differs"


def test_scatter_grid_probe_and_drill(ms):
    """The probe reads a scatter cell anywhere in the grid's extent (not
    only its own data range), and a tap sets the panel up as a scatter."""
    from cubevis.toolbox.visplot.visibility_plotter import VisibilityPlotter
    with VisibilityPlotter(ms=ms, preset="waterfall",
                           grid_panel=dict(kind="scatter", scatter_x="TIME",
                                           scatter_y="AMPLITUDE")) as p:
        assert asyncio.run(p._handle_plot({"layout": "grid"}))["status"] == "ok"
        src = p._grid_source
        ext = src.extent()
        src.render([0], ext, (200, 150))
        cell = src._cells[0]
        assert cell._current_viewport == tuple(ext)
        x = (ext[0] + ext[1]) / 2
        texts = [src.probe(0, x, ext[2] + f * (ext[3] - ext[2])) for f in (0.02, 0.5, 0.98)]
        assert all("out of range" not in t for t in texts), texts
        d = p._grid_drill(0, src.labels()[0])
        assert d["panel"]["kind"] == "scatter" and d["panel"]["sy"] == "AMPLITUDE"


def test_timeseries_preset_in_grid(ms):
    """A scatter-pair preset pressed in Grid makes the grid's panel a
    scatter (the browser sends it as the grid tab): the grid draws it."""
    from cubevis.toolbox.visplot.visibility_plotter import VisibilityPlotter
    with VisibilityPlotter(ms=ms, preset="waterfall") as p:
        # entering Grid draws once (the copy of Panel A); the preset follows
        assert asyncio.run(p._handle_plot({"layout": "grid"}))["status"] == "ok"
        g = {"kind": "scatter", "scatter": {"x": "TIME", "y": "AMPLITUDE",
                                            "avg_time": "off", "avg_chan": "all"}}
        resp = asyncio.run(p._handle_plot({"layout": "grid", "iterate_by": "baseline",
                                           "panels": {"G": g}}))
        assert resp["status"] == "ok", resp
        assert p.grid_panel_settings()["kind"] == "scatter"
        assert p.grid_panel_settings()["scatter_avg_chan"] == "all"
        cells = p._grid_source.render([0, 1, 2], p._grid_source.extent(), (160, 120))
        assert all(c.spec.kind == "scatter" and c.spec.status == "ok" for c in cells)


def test_plotfile_scatter_grid(ms, tmp_path):
    from cubevis.toolbox.visplot.visibility_plotter import VisibilityPlotter
    out = str(tmp_path / "sg.png")
    with VisibilityPlotter(ms=ms, layout="grid", iterate_by="antenna", plotfile=out,
                           grid_panel=dict(kind="scatter", scatter_x="TIME",
                                           scatter_y="AMPLITUDE")) as p:
        assert p() == out and os.path.getsize(out) > 5000
        assert "kind='scatter'" in p.reproducing_call()


def test_scatter_split_stops_when_the_union_does_not_fit(ms):
    """When the union's frames take more than half the frame cache, the
    cells stop being cut from them (they would push each other out and be
    read again per cell) and read their own rows: still the same pixels."""
    from dataclasses import replace
    from cubevis.toolbox.visplot.visibility_plotter import VisibilityPlotter
    from cubevis.toolbox.visplot.refresh import RefreshLevel
    with VisibilityPlotter(ms=ms, headless=True, layout="one", kind="scatter",
                           scatter_x="TIME", scatter_y="AMPLITUDE") as p:
        be = p._reader._backend
        key = be.preload(p._selection, budget_mb=4096)["key"]
        was = be._frame_cache_obj().max_bytes / (1 << 20)
        be.set_frame_cache_limit_mb(0.05)            # far below the union's frames
        try:
            sc = p._slots[0].scatter
            for b in p._meta.baselines[:3]:
                imgs = []
                for k in (None, key):
                    sc._selection = replace(p._selection, preload=k, baselines=[tuple(b.pair)])
                    sc.apply_refresh(RefreshLevel.QUERY)
                    imgs.append(sc.render_result(None).image.copy())
                assert np.array_equal(*imgs)
            assert be._cv_preload["no_split"] is True
        finally:                                     # the limit is process-wide
            be.set_frame_cache_limit_mb(was)


# --- compound iteration and the axes override (gridplot G3 item 4) ---------------

def test_compound_iterate_by_names():
    from cubevis.toolbox.visplot.vis_grid_source import iterate_axes, normalize_iterate_by
    assert normalize_iterate_by("antenna,spw") == "antenna,spw"
    assert normalize_iterate_by("Antennas then SPWs") == "antenna,spw"
    assert normalize_iterate_by(("field", "scan")) == "field,scan"
    assert iterate_axes("spw > corr") == ("spw", "correlation")
    assert iterate_axes(None) == ("baseline",)
    for bad in ("spw,spw", "baseline,antenna", "antenna,baseline", "spw,field,scan",
                "antenna,time"):
        with pytest.raises(ValueError):
            normalize_iterate_by(bad)


def test_compound_values_and_labels(plotter):
    from cubevis.toolbox.visplot.vis_grid_source import VisGridSource
    one = VisGridSource(plotter).configure(iterate_by="antenna")
    src = VisGridSource(plotter).configure(iterate_by="antenna,spw")
    spws = VisGridSource(plotter).configure(iterate_by="spw")
    ants, n = one.labels(), len(one.labels())
    s0, s1 = spws.labels()
    sid1 = spws.values()[1]["spw"][0]
    assert len(src.labels()) == 2 * n
    # the outer axis's values in turn, each with the inner axis's
    assert src.labels()[:2] == [f"{ants[0]} · {s0}", f"{ants[0]} · {s1}"]
    assert src.labels()[2].startswith(ants[1])
    v = src.values()[1]
    assert v["spw"] == [sid1] and v["baselines"] == one.values()[0]["baselines"]
    # a drill-down selects both
    assert src.drill_target(1) == {"antenna": ants[0], "spw": sid1}
    # spw then baseline: each SPW's baselines with rows
    sb = VisGridSource(plotter).configure(iterate_by="spw,baseline")
    assert len(sb.labels()) == 20 and sb.labels()[0].startswith(f"{s0} · ")
    t = sb.drill_target(10)
    assert t["spw"] == sid1 and len(t["baselines"]) == 1


def test_compound_cells_draw_as_the_simple_grid(plotter):
    """A cell of "antenna, then SPW" is the cell of "antenna" with its SPW
    picked: the same pixels as that antenna's cell in a grid by antenna
    over that SPW alone."""
    from cubevis.toolbox.visplot.vis_grid_source import VisGridSource
    src = VisGridSource(plotter).configure(iterate_by="antenna,spw")
    sel = plotter._selection.copy()
    sel.spw = [VisGridSource(plotter).configure(iterate_by="spw").values()[1]["spw"][0]]
    alone = VisGridSource(plotter).configure(iterate_by="antenna", selection=sel)
    ext = src.extent()
    a = src.render([1], ext, (160, 100))[0]
    b = alone.render([0], ext, (160, 100))[0]
    assert a.spec.status == "ok" and np.array_equal(a.image, b.image)


def test_compound_plot_and_plotfile(ms, tmp_path):
    from cubevis.toolbox.visplot.visibility_plotter import VisibilityPlotter
    out = str(tmp_path / "c.png")
    with VisibilityPlotter(ms=ms, layout="grid", preset="vplot", iterate_by="antenna, then spw",
                           gridrows=2, gridcols=5, exprange="all", plotfile=out) as p:
        assert p._iterate_by == "antenna,spw"
        assert p() == out and os.path.getsize(out) > 5000
        assert "Grid by antenna, then spw" in p._status_text()
        assert "iterate_by='antenna,spw'" in p.reproducing_call()
    with VisibilityPlotter(ms=ms, headless=True, layout="grid", preset="waterfall") as p:
        resp = asyncio.run(p._handle_plot({"layout": "grid", "iterate_by": "spw,correlation"}))
        assert resp["status"] == "ok" and resp["grid"]["iterate_by"] == "spw,correlation"
        from cubevis.toolbox.visplot.vis_grid_source import VisGridSource
        n = [len(VisGridSource(p).configure(iterate_by=b).labels()) for b in ("spw", "correlation")]
        assert resp["grid"]["n_cells"] == n[0] * n[1] >= 4
        bad = asyncio.run(p._handle_plot({"layout": "grid", "iterate_by": "baseline,antenna"}))
        assert bad["status"] == "error"


def test_compound_gui_select(ms):
    from cubevis.toolbox.visplot.visibility_plotter import VisibilityPlotter
    with VisibilityPlotter(ms=ms, layout="grid", iterate_by="field,scan") as p:
        p._build_layout() if not hasattr(p, "_grid_then_sel") else None
        assert (p._grid_iter_sel.value, p._grid_then_sel.value) == ("field", "scan")
        assert p._grid_then_sel.options[0] == ("", "—")


def test_scatter_compound_grid(ms, monkeypatch):
    held, info = _grid_images(ms, monkeypatch, True, "vplot", "spw,correlation",
                              grid_panel=dict(kind="scatter"))
    read, _ = _grid_images(ms, monkeypatch, False, "vplot", "spw,correlation",
                           grid_panel=dict(kind="scatter"))
    assert len(held) == len(read) >= 2 and info["key"]
    assert all(a is not None and np.array_equal(a, b) for a, b in zip(held, read))


def test_axes_override_hook(plotter):
    from cubevis.toolbox.visplot.vis_grid_source import VisGridSource
    from cubevis.toolbox.visplot.axes import Axis
    src = VisGridSource(plotter)
    src.set_axes_override(lambda i, label, value: {"quantity": "PHASE"} if i == 1 else None)
    src.configure(iterate_by="baseline")
    assert not src.own_scale(0) and src.own_scale(1)
    ext = src.extent()
    c0, c1 = src.render([0, 1], ext, (160, 100), color="page")
    assert c1.own_scale and not c0.own_scale
    assert src._cell(1)._quantity == Axis.PHASE and src._cell(0)._quantity != Axis.PHASE
    # drawn at its own extent, not the grid's
    assert c0.viewport == ext and c1.viewport is None
    # by label; an override equal to the template's is none
    src.set_axes_override({src.labels()[2]: {"quantity": src._template._quantity.name}})
    src.configure()
    assert not any(src.own_scale(i) for i in range(len(src.labels())))
    src.set_axes_override(lambda i, label, value: {"layers": 1})
    with pytest.raises(ValueError):
        src.configure()
    src.set_axes_override(None)
    src.configure()


def test_axes_override_in_the_view(ms, tmp_path):
    from cubevis.toolbox.visplot.visibility_plotter import VisibilityPlotter
    with VisibilityPlotter(ms=ms, layout="grid", preset="waterfall", iterate_by="spw") as p:
        src = p._grid_source
        plain = p._grid().extent()
        src.set_axes_override(lambda i, label, value: {"x_dim": "BASELINE"} if i == 0 else None)
        p._grid()
        # the cell with its own axes does not widen the others' extent
        assert src.extent() == plain
        view = p._grid_view
        resp = view.page({"page": 0, "rows": 1, "cols": 2})
        assert resp["cells"][0].get("own") and not resp["cells"][1].get("own")
        saved = view.save({"page": 0, "rows": 1, "cols": 2}, path=str(tmp_path / "o.png"))
        assert os.path.getsize(saved["path"]) > 5000
    with VisibilityPlotter(ms=ms, headless=True, layout="grid", preset="waterfall",
                           iterate_by="spw") as p:
        p._grid().set_axes_override(lambda i, label, value: {"x_dim": "BASELINE"} if i else None)
        out = p(plotfile=str(tmp_path / "pf.png"))
        assert os.path.getsize(out) > 5000 and p._grid_source.own_scale(1)
