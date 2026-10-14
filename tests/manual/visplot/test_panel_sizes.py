"""``row_height`` / ``column_width`` (2026-10-13).

They replace ``plot_width`` / ``plot_height``, which applied only at
startup and were reset by the first layout click.  Every layout is a grid
(One 1 x 1, Side by Side 1 x 2, Over / Under 2 x 1); a panel's plot is its
row's height by its column's width.  The parsing and the sizes per layout
are checked without data; the plotter checks need ``MS`` (TW Hya).
"""
from __future__ import annotations

import os
import uuid

import pytest

from cubevis.toolbox.visplot import panel_sizes as ps

MS = os.environ.get("MS")
needs_ms = pytest.mark.skipif(not (MS and os.path.isdir(MS)),
                              reason="set MS to the TW Hya data")


# ---------------------------------------------------------------------- #
# Parsing and resolution                                                   #
# ---------------------------------------------------------------------- #

@pytest.mark.parametrize("value, expected", [
    (None, None), ("", None), ("default", None),
    ("fit", "fit"), ("FIT", "fit"),
    (300, 300), (300.4, 300), ("300", 300), ("300px", 300),
    ([250, 400], [250, 400]), ((250, 400), [250, 400]), ([300], 300),
    ("[250,400]", [250, 400]), ("250, 400", [250, 400]),
])
def test_parse(value, expected):
    assert ps.parse_size("row_height", value) == expected


@pytest.mark.parametrize("value", ["tall", 50, 5000, True, [1, 2, 3], [250, "x"]])
def test_parse_rejects(value):
    with pytest.raises(ps.SizeError):
        ps.parse_size("row_height", value)


def test_row_heights_default_by_layout():
    assert ps.row_heights(None, "one") == [550]
    assert ps.row_heights(None, "side") == [550]
    assert ps.row_heights(None, "over") == [280, 280]
    assert ps.row_heights(300, "over") == [300, 300]
    assert ps.row_heights([250, 400], "over") == [250, 400]
    assert ps.row_heights([250, 400], "side") == [250]      # one row
    assert ps.row_heights("fit", "over") == ["fit", "fit"]


def test_column_widths_follow_the_window_except_in_a_file():
    assert ps.column_widths(None, "side") == [None, None]
    assert ps.column_widths("fit", "over") == [None]
    assert ps.column_widths(None, "side", for_file=True) == [500, 500]
    assert ps.column_widths(None, "over", for_file=True) == [1020]
    assert ps.column_widths([600, 400], "side") == [600, 400]


def test_cell_sizes_reading_order():
    assert ps.cell_sizes(None, None, "over") == [(1020, 280), (1020, 280)]
    assert ps.cell_sizes([200, 400], None, "over") == [(1020, 200), (1020, 400)]
    assert ps.cell_sizes(300, [600, 400], "side") == [(600, 300), (400, 300)]
    # "fit" has no window in a file: the layout's default height
    assert ps.cell_sizes("fit", None, "one") == [(1020, 550)]


def test_size_text_and_value():
    assert ps.size_text(None) == "default"
    assert ps.size_text("fit") == "fit"
    assert ps.size_text([250, 400]) == "250 px, 400 px"
    assert ps.size_value([250, 400]) == [250, 400]


def test_png_rows_of_different_heights():
    """export_png lays out rows of different heights; each data area is
    exactly its row's height."""
    import numpy as np
    from PIL import Image
    from cubevis.toolbox.visplot.png_export import export_png
    import tempfile
    d = tempfile.mkdtemp()
    a = export_png([None, None], os.path.join(d, "a.png"), nrows=2, ncols=1,
                   cell_size=(400, 200))
    b = export_png([None, None], os.path.join(d, "b.png"), nrows=2, ncols=1,
                   cell_size=(400, 200), row_heights=[200, 350])
    ha, hb = Image.open(a).size[1], Image.open(b).size[1]
    assert hb - ha == 150
    assert np.asarray(Image.open(b)).shape[1] == Image.open(a).size[0]


# ---------------------------------------------------------------------- #
# The plotter                                                              #
# ---------------------------------------------------------------------- #

def _plotter(**kw):
    from cubevis.toolbox.visplot.visibility_plotter import VisibilityPlotter
    kw.setdefault("field", "3c279")
    kw.setdefault("headless", True)
    return VisibilityPlotter(ms=MS, **kw)


@needs_ms
def test_old_names_are_gone():
    with pytest.raises(TypeError):
        _plotter(plot_height=300)


@needs_ms
def test_bad_size_fails_at_construction():
    with pytest.raises(ValueError, match="row_height"):
        _plotter(row_height="tall")


@needs_ms
def test_each_panel_drawn_at_its_cell():
    p = _plotter(layout="over", row_height=[200, 400])
    try:
        assert [(s.raster._width, s.raster._height) for s in p._slots] == \
            [(1020, 200), (1020, 400)]
    finally:
        p.close()


@needs_ms
def test_plotfile_uses_row_height_and_column_width(tmp_path):
    from PIL import Image
    p = _plotter(layout="side")
    q = _plotter(layout="side", row_height=300, column_width=[600, 400])
    try:
        a = Image.open(p(plotfile=str(tmp_path / "a.png"), theme="light")).size
        b = Image.open(q(plotfile=str(tmp_path / "b.png"), theme="light")).size
        # default 500 + 500 wide, 550 high; now 600 + 400 wide, 300 high
        assert b[0] == a[0]
        assert a[1] - b[1] == 250
    finally:
        p.close()
        q.close()


@needs_ms
def test_reproducing_call_and_summary_carry_sizes():
    p = _plotter(layout="over", row_height=[250, 400])
    try:
        call = p.reproducing_call()
        assert "row_height=[250, 400]," in call and "column_width" not in call
        html = p.describe("A", None, True)
        assert "Row height" in html and "250 px, 400 px" in html
    finally:
        p.close()
    q = _plotter(layout="over")
    try:
        assert "row_height" not in q.reproducing_call()      # the default
    finally:
        q.close()


@needs_ms
def test_browser_sizes_are_adopted():
    """Plot, Describe this plot and Export send the sizes as the browser
    holds them (the Row height menu changes them there)."""
    p = _plotter(layout="over")
    try:
        p._adopt_sizes({"sizes": {"row_height": "fit", "column_width": None}})
        assert p._row_height == "fit"
        p._adopt_sizes({"sizes": {"row_height": [300, 200]}})
        assert p._row_height == [300, 200]
        p._adopt_sizes({"sizes": {"row_height": "nonsense"}})   # ignored
        assert p._row_height == [300, 200]
        assert "row_height=[300, 200]" in p.reproducing_call()
    finally:
        p.close()


@needs_ms
def test_gui_startup_sizes_and_menu():
    from cubevis import exe
    from cubevis.toolbox.visplot.visibility_plotter import VisibilityPlotter
    p = VisibilityPlotter(ms=MS, field="3c279", layout="over", row_height=[250, 400])
    try:
        p(exe.Context(exe.Mode.SYNC), uuid.uuid4())
        pos = [p._slots[i] for i in p._slot_display_order]
        assert [(s.raster.figure.height, s.scatter.figure.height) for s in pos] == \
            [(250, 250), (400, 400)]
        assert p._row_height_sel.value == "rows:250,400"
        assert ("rows:250,400", "Height: 250 / 400 px") in p._row_height_sel.options
        assert '"row_height": [250, 400]' in p._size_source.data["spec"][0]
        # The layout buttons and presets keep the chosen sizes.
        assert "window.__cvApplySizes()" in p._layout_js.code
        assert all("window.__cvApplySizes()" in js.code for js in p._preset_js_objects)
        scripts = [s[0].code for s in p._app_context.init_scripts]
        assert any("window.__cvApplySizes = apply" in c for c in scripts)
    finally:
        p.close()


@needs_ms
def test_gui_default_over_is_280():
    from cubevis import exe
    from cubevis.toolbox.visplot.visibility_plotter import VisibilityPlotter
    p = VisibilityPlotter(ms=MS, field="3c279", layout="over")
    try:
        p(exe.Context(exe.Mode.SYNC), uuid.uuid4())
        assert {s.raster.figure.height for s in p._slots} == {280}
        assert p._row_height_sel.value == "default"
    finally:
        p.close()


# ---------------------------------------------------------------------- #
# Plots wider than the window (2026-10-14)                                 #
# ---------------------------------------------------------------------- #

def test_more_to_the_right_cue():
    """The plot area scrolls sideways when the plots are wider than it
    (column_width); a chip says so, as "more below" does for height."""
    from cubevis.toolbox.visplot.visibility_plotter import _MORE_BELOW_JS
    assert "more to the right" in _MORE_BELOW_JS
    assert "area.scrollWidth - area.clientWidth - area.scrollLeft" in _MORE_BELOW_JS


@needs_ms
def test_nothing_is_wider_than_its_place():
    """No stray horizontal scrolling: the readout / colour-bar strips are
    their panel's width less their margins, and the toolbar's last button
    stays inside the window (checked in a browser: page and plot area
    exactly the window's width at 900-1400 px)."""
    from cubevis import exe
    from cubevis.toolbox.visplot.visibility_plotter import VisibilityPlotter
    p = VisibilityPlotter(ms=MS, field="3c279", layout="side")
    try:
        p(exe.Context(exe.Mode.SYNC), uuid.uuid4())
        for s in p._slots:
            for d in (s.raster._info_div, s.scatter._colorbar_content):
                assert d.styles["width"] == "calc(100% - 10px)"
        from bokeh.models import Row
        toolbar = next(m for m in p._app_context.ui.references()
                       if isinstance(m, Row) and (m.styles or {}).get("flex-wrap") == "wrap")
        assert toolbar.styles.get("padding-right")
    finally:
        p.close()
