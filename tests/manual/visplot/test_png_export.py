"""
test_png_export.py
==================
Regression tests for ``png_export`` — the matplotlib compositor.

These run without an MS, without bokeh, and without a display: the
compositor consumes ``RenderedPanel``, which is a plain data structure,
so every case here is built from synthetic specs and fabricated RGBA
arrays.  That is deliberate.  It means a compositor regression is caught
in seconds by a test that cannot be blocked by a missing dataset.

The load-bearing assertion is ``test_axes_bbox_is_exactly_the_agg_size``:
the data area must be the panel image's own pixel dimensions so the
Datashader render is *placed*, never resampled.  Note it asserts on
``ax.get_window_extent()``, not on counted pixels in the output file — a
pixel count comes up one row and one column short because the axes spine
overdraws the boundary, which is a frame drawn over the image and not
data loss.

Test location
-------------
``cubevis/tests/manual/visplot/test_png_export.py``
"""

import numpy as np
import pytest

matplotlib = pytest.importorskip("matplotlib")
matplotlib.use("Agg")
import matplotlib.pyplot as plt                      # noqa: E402

from cubevis.toolbox.visplot.panel_spec import (      # noqa: E402
    ColorBand, PanelSpec, RenderedPanel,
)
from cubevis.toolbox.visplot.colormap_scaling import (  # noqa: E402
    ScalarMapping,
)
from cubevis.toolbox.visplot import png_export as pe  # noqa: E402


PLASMA = ("#0d0887", "#46039f", "#7201a8", "#9c179e", "#bd3786",
          "#d8576b", "#ed7953", "#fb9f3a", "#fdcb26", "#f0f921")

# An MJD-seconds-scale origin, so time-axis regressions show up as the
# raw-epoch-seconds labels they actually produce.
T0, T1 = 4.8e9, 4.8e9 + 1830.0


# ---------------------------------------------------------------------------
# Fixtures / builders
# ---------------------------------------------------------------------------

def make_image(h=64, w=96, marks=True):
    """A ``(H, W) uint32`` RGBA array, bytes R,G,B,A in memory order.

    Solid red with blue first rows and green last rows, so vertical
    orientation is checkable: with ``origin="lower"`` the blue band must
    land at the *bottom* of the axes.

    The bands are three rows deep, not one, because the axes spine
    overdraws the boundary pixel — a single-row marker at the top edge is
    completely covered by the frame and the test can never see it.
    """
    img = np.zeros((h, w), np.uint32)
    v = img.view(np.uint8).reshape(h, w, 4)
    v[..., 3] = 255
    v[..., 0] = 255
    if marks:
        v[:3, :, :3] = [0, 0, 255]
        v[-3:, :, :3] = [0, 255, 0]
    return img


def make_spec(kind="raster", title="Amplitude  [Time vs Channel]  pol=XX",
              x_label="Channel", y_label="Time",
              x_range=(0.0, 48.0), y_range=(T0, T1),
              x_is_time=False, y_is_time=True, bands=None,
              status="ok", note=None, w=96, h=64):
    if bands is None:
        bands = (ColorBand(label="Amplitude", cmap=PLASMA, scaling="eq_hist"),)
    return PanelSpec(
        kind=kind, title=title, x_label=x_label, y_label=y_label,
        x_range=x_range, y_range=y_range,
        x_is_time=x_is_time, y_is_time=y_is_time,
        agg_n_x=w, agg_n_y=h, color_mode="global",
        bands=bands, status=status, note=note,
    )


def make_panel(**kw):
    w = kw.pop("w", 96)
    h = kw.pop("h", 64)
    return RenderedPanel(spec=make_spec(w=w, h=h, **kw), image=make_image(h, w))


def make_scatter_panel(title="Amplitude XX, Amplitude YY  vs  UV Distance",
                       w=96, h=64):
    bands = (
        ColorBand(label="Amplitude XX", cmap=("#08306b", "#9ecae1"),
                  scaling="eq_hist"),
        ColorBand(label="Amplitude YY", cmap=("#67000d", "#fc9272"),
                  scaling="eq_hist"),
    )
    return RenderedPanel(
        spec=make_spec(kind="scatter", title=title,
                       x_label="UV Distance [m]", y_label="Amplitude",
                       x_range=(0.0, 420.0), y_range=(0.0, 3.2),
                       x_is_time=False, y_is_time=False, bands=bands,
                       w=w, h=h),
        image=make_image(h, w),
    )


def make_empty(title="Amp vs UVDist  ant=DA48",
               note="Amplitude XX: query returned 0 rows",
               status="empty", x_label="UV Distance [m]",
               y_label="Amplitude"):
    """An empty panel, with the degenerate (0, 1) ranges _render leaves."""
    return RenderedPanel(spec=make_spec(
        kind="scatter", title=title, x_label=x_label, y_label=y_label,
        x_range=(0.0, 1.0), y_range=(0.0, 1.0),
        x_is_time=False, y_is_time=False, bands=(),
        status=status, note=note))


@pytest.fixture
def spy_axes(monkeypatch):
    """Capture every Axes the compositor creates, in cell order."""
    created = []
    original = plt.Figure.add_axes

    def _spy(self, rect, **kw):
        ax = original(self, rect, **kw)
        created.append(ax)
        return ax

    monkeypatch.setattr(plt.Figure, "add_axes", _spy)
    return created


# ---------------------------------------------------------------------------
# Pixel fidelity
# ---------------------------------------------------------------------------

class TestPixelFidelity:

    @pytest.mark.parametrize("h,w", [(480, 900), (300, 300), (137, 641)])
    @pytest.mark.parametrize("dpi", [72, 100, 200, 300])
    def test_axes_bbox_is_exactly_the_agg_size(self, tmp_path, spy_axes,
                                               h, w, dpi):
        """The data area is the image's own pixel size at every dpi.

        The basis of the whole fidelity claim: matplotlib places the
        Datashader render 1:1 rather than resampling it.  dpi scales the
        chrome (margins are in points) and leaves the data area alone.
        """
        panel = RenderedPanel(spec=make_spec(w=w, h=h), image=make_image(h, w))
        pe.export_png([panel], str(tmp_path / "p.png"), dpi=dpi)
        bb = spy_axes[0].get_window_extent()
        assert bb.width == pytest.approx(w, abs=1e-6)
        assert bb.height == pytest.approx(h, abs=1e-6)

    def test_origin_is_lower(self, tmp_path):
        """Array row 0 lands at the bottom, as Bokeh's image_rgba puts it.

        matplotlib defaults to origin="upper".  Getting this wrong
        mirrors every plot vertically, which on a time axis is very easy
        not to notice.
        """
        from PIL import Image
        out = pe.export_png([make_panel(h=64, w=96)],
                            str(tmp_path / "p.png"), dpi=100)
        a = np.array(Image.open(out).convert("RGB"))
        blue = np.where(((a[:, :, 2] > 200) & (a[:, :, 0] < 80)).any(axis=1))[0]
        green = np.where(((a[:, :, 1] > 200) & (a[:, :, 0] < 80)).any(axis=1))[0]
        assert len(blue) and len(green)
        assert blue.mean() > green.mean(), "image is vertically mirrored"

    def test_no_channel_permutation(self, tmp_path):
        """Red in byte 0 renders red, not blue.

        Datashader emits RGBA in memory order, so RenderedPanel.rgba()
        needs no permutation.  If _img_to_uint32 or rgba() ever
        transposes R and B again, this fails.
        """
        from PIL import Image
        img = np.zeros((32, 32), np.uint32)
        img.view(np.uint8).reshape(32, 32, 4)[...] = [255, 0, 0, 255]
        out = pe.export_png([RenderedPanel(spec=make_spec(w=32, h=32),
                                           image=img)],
                            str(tmp_path / "p.png"), dpi=100)
        a = np.array(Image.open(out).convert("RGB"))
        n_red = int((((a[:, :, 0] > 200) & (a[:, :, 2] < 80))).sum())
        n_blue = int((((a[:, :, 2] > 200) & (a[:, :, 0] < 80))).sum())
        assert n_red > 500 and n_blue == 0


# ---------------------------------------------------------------------------
# Grid layout
# ---------------------------------------------------------------------------

class TestGridLayout:

    @pytest.mark.parametrize("nrows,ncols,n", [
        (1, 1, 1), (1, 2, 2), (2, 1, 2), (2, 2, 4), (2, 3, 6), (3, 3, 9),
    ])
    def test_one_axes_per_grid_position(self, tmp_path, spy_axes,
                                        nrows, ncols, n):
        pe.export_png([make_panel() for _ in range(n)],
                      str(tmp_path / "p.png"), nrows=nrows, ncols=ncols)
        assert len(spy_axes) == nrows * ncols

    def test_short_panel_list_still_fills_the_grid(self, tmp_path, spy_axes):
        """Blank but framed: a 2x2 grid given 3 panels draws 4 cells.

        Reflowing to fill the gap would make cell position meaningless
        across a sequence of exported files, which matters when someone
        is flipping through out_ant00.png ... out_ant42.png.
        """
        pe.export_png([make_panel(), make_panel(), make_panel()],
                      str(tmp_path / "p.png"), nrows=2, ncols=2)
        assert len(spy_axes) == 4

    def test_too_many_panels_raises(self, tmp_path):
        with pytest.raises(ValueError, match="exceeds"):
            pe.export_png([make_panel() for _ in range(5)],
                          str(tmp_path / "p.png"), nrows=2, ncols=2)

    def test_bad_grid_shape_raises(self, tmp_path):
        with pytest.raises(ValueError, match="grid shape"):
            pe.export_png([make_panel()], str(tmp_path / "p.png"), nrows=0)

    def test_row_major_order(self, tmp_path, spy_axes):
        """Cell i goes to (i // ncols, i % ncols)."""
        titles = [f"cell{i}" for i in range(6)]
        pe.export_png([make_panel(title=t) for t in titles],
                      str(tmp_path / "p.png"), nrows=2, ncols=3)
        assert [ax.get_title() for ax in spy_axes] == titles
        # Row 0's cells all sit above row 1's.
        tops = [ax.get_position().y0 for ax in spy_axes]
        assert min(tops[:3]) > max(tops[3:])


# ---------------------------------------------------------------------------
# Empty and failed cells
# ---------------------------------------------------------------------------

class TestEmptyCells:

    def test_empty_cell_keeps_title_and_shows_note(self, tmp_path, spy_axes):
        pe.export_png([make_panel(), make_empty()],
                      str(tmp_path / "p.png"), nrows=1, ncols=2)
        ax = spy_axes[1]
        assert ax.get_title() == "Amp vs UVDist  ant=DA48"
        texts = " ".join(t.get_text() for t in ax.texts)
        assert "query returned 0 rows" in texts.replace("\n", " ")

    def test_error_cell_is_visually_distinct(self, tmp_path, spy_axes):
        """A failed cell must not look like a routine empty one.

        A pipeline emitting 43 PNGs cannot be allowed to swallow an
        exception silently.
        """
        pe.export_png([make_panel(),
                       make_empty(note="shade failed: bad reshape",
                                  status="error")],
                      str(tmp_path / "p.png"), nrows=1, ncols=2,
                      theme="light")     # explicit: the theme now defaults
                                         # to PanelSpec.theme, which the
                                         # fixtures leave at "dark"
        txt = spy_axes[1].texts[0]
        assert "ERROR" in txt.get_text()
        assert txt.get_color() == pe.THEMES["light"].error

    def test_empty_cell_inherits_neighbour_extent(self, tmp_path, spy_axes):
        """Not its own (0, 1): those ranges are the degenerate sentinel.

        They pass every "is this a valid range" test while being pure
        fiction, and 0.0000-1.0000 ticks look like data.
        """
        pe.export_png([make_scatter_panel(), make_empty()],
                      str(tmp_path / "p.png"), nrows=1, ncols=2)
        assert spy_axes[1].get_xlim() == pytest.approx((0.0, 420.0))
        assert spy_axes[1].get_ylim() == pytest.approx((0.0, 3.2))

    def test_inheritance_is_keyed_on_axis_labels(self, tmp_path, spy_axes):
        """An empty scatter must not inherit a raster's channel range.

        Grid mode shares one configuration so this never bites there, but
        a duo export can pair a raster against a scatter, and inheriting
        across them draws axes that are confidently mislabelled.
        """
        pe.export_png([make_panel(), make_scatter_panel(), make_empty(), None],
                      str(tmp_path / "p.png"), nrows=2, ncols=2)
        assert spy_axes[2].get_xlim() == pytest.approx((0.0, 420.0))

    def test_empty_cell_inherits_tick_origin(self, tmp_path, spy_axes):
        """Elapsed-time ticks need the donor's t0, not the empty panel's.

        Formatting an inherited MJD-seconds extent against the empty
        panel's own (0.0, 1.0) origin prints raw epoch seconds
        (4800001750.0000) instead of 29m 10s.
        """
        empty = make_empty(title="spw=3", x_label="Channel", y_label="Time")
        pe.export_png([make_panel(), empty],
                      str(tmp_path / "p.png"), nrows=1, ncols=2)
        fmt = spy_axes[1].yaxis.get_major_formatter()
        assert fmt(T0 + 1750.0, 0) == "29m 10s"

    def test_all_cells_empty_suppresses_ticks(self, tmp_path, spy_axes):
        """With no extent anywhere, draw the frame and no tick values.

        The alternative is 0-1 ticks that look like data.
        """
        pe.export_png([make_empty(), make_empty()],
                      str(tmp_path / "p.png"), nrows=1, ncols=2)
        assert len(spy_axes[0].get_xticks()) == 0
        assert len(spy_axes[0].get_yticks()) == 0

    def test_absent_cell_has_no_title(self, tmp_path, spy_axes):
        """None means "no panel here", distinct from "panel with no data"."""
        pe.export_png([make_panel(), None],
                      str(tmp_path / "p.png"), nrows=1, ncols=2)
        assert spy_axes[1].get_title() == ""
        assert len(spy_axes[1].texts) == 0


# ---------------------------------------------------------------------------
# Chrome
# ---------------------------------------------------------------------------

class TestChrome:

    def test_time_axis_uses_elapsed_formatter(self, tmp_path, spy_axes):
        """Matches the browser's CustomJSTickFormatter, via tick_format."""
        pe.export_png([make_panel()], str(tmp_path / "p.png"))
        fmt = spy_axes[0].yaxis.get_major_formatter()
        assert fmt(T0, 0) == "0.0 s"
        assert fmt(T0 + 125.0, 0) == "2m 05s"

    def test_non_time_axis_trims_trailing_zeros(self, tmp_path, spy_axes):
        """"24", not "24.0000" -- trailing zeros are noise on a channel axis."""
        pe.export_png([make_panel()], str(tmp_path / "p.png"))
        fmt = spy_axes[0].xaxis.get_major_formatter()
        assert fmt(24.0, 0) == "24"
        assert fmt(0.0, 0) == "0"
        assert fmt(1234.5678, 0) == "1234.5678"
        # A non-zero value must never trim all the way to "0".
        assert fmt(1.2e-6, 0) == "0.0000012"

    def test_axis_labels_and_title_come_from_spec(self, tmp_path, spy_axes):
        pe.export_png([make_panel()], str(tmp_path / "p.png"))
        ax = spy_axes[0]
        assert ax.get_xlabel() == "Channel"
        assert ax.get_ylabel() == "Time"
        assert ax.get_title() == "Amplitude  [Time vs Channel]  pol=XX"

    def test_multi_band_panel_gets_a_legend(self, tmp_path, spy_axes):
        pe.export_png([make_scatter_panel()], str(tmp_path / "p.png"))
        leg = spy_axes[0].get_legend()
        assert leg is not None
        assert [t.get_text() for t in leg.get_texts()] == [
            "Amplitude XX", "Amplitude YY"]

    def test_legend_never_overlaps_the_data_area(self, tmp_path, spy_axes):
        """The legend sits above the axes, not inside them.

        matplotlib's default loc="upper right" lands on exactly the
        corner an amplitude-vs-uvdistance scatter tends to occupy, and no
        "find the empty corner" heuristic survives real data.  Space is
        reserved instead.
        """
        pe.export_png([make_scatter_panel()], str(tmp_path / "p.png"))
        ax = spy_axes[0]
        leg = ax.get_legend()
        fig = ax.figure
        fig.canvas.draw()
        lb = leg.get_window_extent()
        ab = ax.get_window_extent()
        assert lb.y0 >= ab.y1 - 1.0, "legend intrudes into the data area"

    def test_single_band_panel_gets_no_legend(self, tmp_path, spy_axes):
        """The title already names the quantity."""
        pe.export_png([make_panel()], str(tmp_path / "p.png"))
        assert spy_axes[0].get_legend() is None

    def test_identical_cells_share_one_figure_legend(self, tmp_path, spy_axes):
        """Grid mode iterates one configuration, so N legends would be
        N-1 too many.  "auto" collapses them to a single figure legend."""
        pe.export_png([make_scatter_panel(f"ant=DA4{i}") for i in range(4)],
                      str(tmp_path / "p.png"), nrows=2, ncols=2)
        assert all(ax.get_legend() is None for ax in spy_axes)
        fig = spy_axes[0].figure
        assert len(fig.legends) == 1
        assert [t.get_text() for t in fig.legends[0].get_texts()] == [
            "Amplitude XX", "Amplitude YY"]

    def test_heterogeneous_cells_keep_per_panel_legends(self, tmp_path,
                                                        spy_axes):
        """A raster paired with a scatter must not share one legend."""
        pe.export_png([make_panel(), make_scatter_panel()],
                      str(tmp_path / "p.png"), nrows=1, ncols=2)
        assert spy_axes[0].figure.legends == []
        assert spy_axes[1].get_legend() is not None

    @pytest.mark.parametrize("mode", ["figure", "panel", "none"])
    def test_legend_mode_can_be_forced(self, tmp_path, spy_axes, mode):
        pe.export_png([make_scatter_panel() for _ in range(2)],
                      str(tmp_path / "p.png"), nrows=1, ncols=2, legend=mode)
        fig = spy_axes[0].figure
        if mode == "figure":
            assert len(fig.legends) == 1
        elif mode == "panel":
            assert spy_axes[0].get_legend() is not None
        else:
            assert not fig.legends
            assert spy_axes[0].get_legend() is None

    def test_hidden_bands_are_omitted_from_the_legend(self, tmp_path, spy_axes):
        """A legend entry for a hidden layer claims something not drawn."""
        bands = (
            ColorBand(label="XX", cmap=("#000", "#fff"), scaling="linear"),
            ColorBand(label="YY", cmap=("#000", "#fff"), scaling="linear",
                      alpha=0.0, visible=False),
            ColorBand(label="XY", cmap=("#000", "#fff"), scaling="linear"),
        )
        panel = RenderedPanel(spec=make_spec(kind="scatter", bands=bands),
                              image=make_image())
        pe.export_png([panel], str(tmp_path / "p.png"))
        leg = spy_axes[0].get_legend()
        assert [t.get_text() for t in leg.get_texts()] == ["XX", "XY"]

    def test_legend_text_is_themed(self, tmp_path, spy_axes):
        """Dark-theme legend text must not stay black on a dark ground."""
        pe.export_png([make_scatter_panel()], str(tmp_path / "p.png"),
                      theme="dark")
        leg = spy_axes[0].get_legend()
        for txt in leg.get_texts():
            assert txt.get_color() == pe.THEMES["dark"].text

    def test_footer_is_written(self, tmp_path):
        import matplotlib.pyplot as _plt
        footer = "sis14_twhya_calibrated_flagged.ps.zarr | Field: all"
        pe.export_png([make_panel()], str(tmp_path / "p.png"), footer=footer)
        # Figure-level text survives on the last figure only if we look
        # before close, so just assert the file grew relative to no footer.
        a = (tmp_path / "p.png").stat().st_size
        pe.export_png([make_panel()], str(tmp_path / "q.png"))
        assert a > (tmp_path / "q.png").stat().st_size

    @pytest.mark.parametrize("theme", ["light", "dark"])
    def test_themes_render(self, tmp_path, spy_axes, theme):
        pe.export_png([make_panel()], str(tmp_path / "p.png"), theme=theme)
        assert spy_axes[0].get_facecolor() is not None

    def test_unknown_theme_raises(self, tmp_path):
        with pytest.raises(ValueError, match="unknown theme"):
            pe.export_png([make_panel()], str(tmp_path / "p.png"),
                          theme="solarized")


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

class TestOutput:

    def test_returns_absolute_path_and_writes_a_png(self, tmp_path):
        out = pe.export_png([make_panel()], str(tmp_path / "sub" / "p.png"))
        import os
        assert os.path.isabs(out)
        assert os.path.exists(out)
        with open(out, "rb") as fh:
            assert fh.read(8) == b"\x89PNG\r\n\x1a\n"

    def test_creates_missing_parent_directories(self, tmp_path):
        out = pe.export_png([make_panel()],
                            str(tmp_path / "a" / "b" / "c" / "p.png"))
        import os
        assert os.path.exists(out)

    def test_figure_grows_with_grid_size(self, tmp_path):
        from PIL import Image
        one = pe.export_png([make_panel()], str(tmp_path / "1.png"))
        two = pe.export_png([make_panel(), make_panel()],
                            str(tmp_path / "2.png"), nrows=1, ncols=2)
        w1, h1 = Image.open(one).size
        w2, h2 = Image.open(two).size
        assert w2 > w1 and h2 == h1

    def test_dpi_grows_the_file_not_the_data_area(self, tmp_path, spy_axes):
        """Margins are in points, so dpi scales chrome only.

        Raising dpi must render text with more pixels and leave the
        Datashader image untouched at 1:1.
        """
        from PIL import Image
        spy_axes.clear()
        a = pe.export_png([make_panel(h=64, w=96)],
                          str(tmp_path / "a.png"), dpi=100)
        bb_a = spy_axes[0].get_window_extent()
        spy_axes.clear()
        b = pe.export_png([make_panel(h=64, w=96)],
                          str(tmp_path / "b.png"), dpi=300)
        bb_b = spy_axes[0].get_window_extent()
        for bb in (bb_a, bb_b):
            assert bb.width == pytest.approx(96, abs=1e-6)
            assert bb.height == pytest.approx(64, abs=1e-6)
        assert Image.open(b).size > Image.open(a).size


# ---------------------------------------------------------------------------
# Colorbars
# ---------------------------------------------------------------------------

def _mapping(seed=0, scale=0.05):
    rng = np.random.default_rng(seed)
    return ScalarMapping.from_values(rng.gamma(2.0, scale, 5000), "eq_hist")


def make_raster_with_bar(title="Amp", mapping=None, w=96, h=64):
    band = ColorBand(label="Amplitude", cmap=PLASMA, scaling="eq_hist",
                     kind="value", mapping=mapping or _mapping())
    return RenderedPanel(spec=make_spec(title=title, bands=(band,), w=w, h=h),
                         image=make_image(h, w))


def make_scatter_with_bars(title="Amp vs UVDist", w=96, h=64):
    bands = tuple(
        ColorBand(label=f"Amplitude {p}", cmap=("#000", "#fff"),
                  scaling="eq_hist", kind="density", peak_density=pd,
                  mapping=_mapping(i))
        for i, (p, pd) in enumerate((("XX", 337.0), ("YY", 291.0)))
    )
    return RenderedPanel(
        spec=make_spec(kind="scatter", title=title, x_label="UV Distance [m]",
                       y_label="Amplitude", x_range=(0.0, 420.0),
                       y_range=(0.0, 3.2), x_is_time=False, y_is_time=False,
                       bands=bands, w=w, h=h),
        image=make_image(h, w))


class TestColorbarResolution:

    def test_identical_mappings_share_one_bar(self, tmp_path):
        """color_mode="global" means one reference distribution, one bar."""
        m = _mapping()
        cells = [make_raster_with_bar(f"spw={i}", m) for i in range(4)]
        assert pe._resolve_colorbar("auto", cells) == "shared"

    def test_divergent_mappings_get_one_bar_each(self, tmp_path):
        """color_mode="local": each panel equalizes to its own data."""
        cells = [make_raster_with_bar(f"spw={i}", _mapping(i, 0.05 * (1 + i)))
                 for i in range(4)]
        assert pe._resolve_colorbar("auto", cells) == "each"

    def test_scatter_defaults_to_none(self):
        """A scatter ramp is points per pixel, and two plotted quantities
        mean two bars per cell.  The legend reports peak density instead."""
        assert pe._resolve_colorbar(
            "auto", [make_scatter_with_bars() for _ in range(4)]) == "none"

    def test_scatter_bars_available_on_request(self):
        assert pe._resolve_colorbar(
            "each", [make_scatter_with_bars()]) == "each"

    def test_scatter_sameness_requires_matching_bin_area(self):
        """_compute_canvas_size is adaptive, so equal counts can mean
        different densities: a shared bar would be a false equivalence."""
        m = _mapping()
        def band():
            return ColorBand(label="XX", cmap=("#000", "#fff"),
                             scaling="eq_hist", kind="density", mapping=m)
        wide = make_spec(kind="scatter", x_range=(0.0, 400.0),
                         bands=(band(),), w=96, h=64)
        narrow = make_spec(kind="scatter", x_range=(0.0, 40.0),
                           bands=(band(),), w=96, h=64)
        assert not pe._mappings_agree([(wide, wide.bands[0]),
                                       (narrow, narrow.bands[0])])
        assert pe._mappings_agree([(wide, wide.bands[0]),
                                   (wide, wide.bands[0])])

    def test_forcing_shared_on_divergent_mappings_falls_back(self, tmp_path):
        """A quietly wrong colorbar in a paper is worse than an
        unexpected layout, so 'shared' is refused, not obeyed."""
        cells = [make_raster_with_bar(f"spw={i}", _mapping(i, 0.05 * (1 + i)))
                 for i in range(2)]
        assert pe._resolve_colorbar("shared", cells) == "each"

    def test_none_suppresses_bars(self, tmp_path, spy_axes):
        pe.export_png([make_raster_with_bar()], str(tmp_path / "p.png"),
                      colorbar="none")
        assert len(spy_axes) == 1          # the cell only, no cax


class TestColorbarDrawing:

    def test_shared_draws_exactly_one_bar(self, tmp_path, spy_axes):
        m = _mapping()
        pe.export_png([make_raster_with_bar(f"spw={i}", m) for i in range(4)],
                      str(tmp_path / "p.png"), nrows=2, ncols=2,
                      colorbar="shared")
        assert len(spy_axes) == 5          # 4 cells + 1 bar

    def test_each_draws_one_bar_per_cell(self, tmp_path, spy_axes):
        cells = [make_raster_with_bar(f"spw={i}", _mapping(i, 0.05 * (1 + i)))
                 for i in range(4)]
        pe.export_png(cells, str(tmp_path / "p.png"), nrows=2, ncols=2,
                      colorbar="each")
        assert len(spy_axes) == 8          # 4 cells + 4 bars

    def test_two_scatter_layers_give_two_bars(self, tmp_path, spy_axes):
        pe.export_png([make_scatter_with_bars()], str(tmp_path / "p.png"),
                      colorbar="each")
        assert len(spy_axes) == 3          # 1 cell + 2 bars

    @pytest.mark.parametrize("side", ["right", "left", "bottom"])
    def test_all_sides_render(self, tmp_path, spy_axes, side):
        pe.export_png([make_raster_with_bar(), make_raster_with_bar()],
                      str(tmp_path / "p.png"), nrows=1, ncols=2,
                      colorbar="each", colorbar_side=side)
        assert len(spy_axes) == 4

    def test_unknown_side_raises(self, tmp_path):
        with pytest.raises(ValueError, match="colorbar_side"):
            pe.export_png([make_raster_with_bar()], str(tmp_path / "p.png"),
                          colorbar_side="middle")

    def test_bars_do_not_shrink_the_data_area(self, tmp_path, spy_axes):
        """Reserved space, not make_axes_locatable: stealing width from
        the parent axes would force a resample of the Datashader image."""
        pe.export_png([make_raster_with_bar(w=96, h=64)],
                      str(tmp_path / "p.png"), colorbar="each")
        bb = spy_axes[0].get_window_extent()
        assert bb.width == pytest.approx(96, abs=1e-6)
        assert bb.height == pytest.approx(64, abs=1e-6)


class TestColorbarLabelling:

    def test_raster_bar_names_the_quantity_and_scaling(self):
        b = ColorBand(label="Amplitude", cmap=PLASMA, scaling="eq_hist")
        assert b.bar_label() == "Amplitude (eq_hist)"

    def test_linear_scaling_is_not_annotated(self):
        b = ColorBand(label="Amplitude", cmap=PLASMA, scaling="linear")
        assert b.bar_label() == "Amplitude"

    def test_scatter_bar_says_density_not_the_layer_label(self):
        """"Amplitude XX" on a density ramp states it is an amplitude
        scale.  That is a real misreading in a published figure."""
        b = ColorBand(label="Amplitude XX", cmap=PLASMA, scaling="eq_hist",
                      kind="density")
        assert b.bar_label() == "Density (eq_hist)"
        assert "Amplitude XX" not in b.bar_label()

    def test_scatter_legend_reports_peak_density(self):
        b = ColorBand(label="Amplitude XX", cmap=PLASMA, scaling="eq_hist",
                      kind="density", peak_density=337.4)
        assert b.legend_label() == "Amplitude XX  (\u2264337 pts/px)"

    def test_raster_legend_label_is_plain(self):
        b = ColorBand(label="Amplitude", cmap=PLASMA, scaling="eq_hist")
        assert b.legend_label() == "Amplitude"

    def test_bar_ticks_land_on_data_values(self, tmp_path, spy_axes):
        """FuncNorm over ScalarMapping is what puts eq_hist ticks at real
        data values, bunched where the data is dense."""
        m = _mapping()
        pe.export_png([make_raster_with_bar("a", m)], str(tmp_path / "p.png"),
                      colorbar="each")
        ticks = spy_axes[1].get_yticks()
        assert len(ticks) >= 3
        assert min(ticks) >= m.vmin - 1e-9
        assert max(ticks) <= m.vmax + 1e-9
        gaps = np.diff(sorted(ticks))
        assert gaps.max() > 3 * gaps.min(), "eq_hist ticks should be uneven"


class TestRealDataRegressions:
    """Cases found by exporting sis14_twhya rather than synthetic ramps."""

    def test_footer_html_is_flattened(self):
        """_status_text() is written for a Bokeh Div, so it arrives as
        markup.  matplotlib draws tags literally."""
        raw = ("<b>sis14_twhya_calibrated_flagged.ps.zarr</b>  |  "
               "Layout: Side by Side<br>Field: all  |  Col: DATA")
        out = pe._plain_text(raw)
        assert "<" not in out and ">" not in out
        assert "sis14_twhya_calibrated_flagged.ps.zarr" in out
        assert "Field: all" in out
        assert pe._plain_text("") == ""
        assert pe._plain_text("a &amp; b") == "a & b"

    def test_auto_gives_no_density_bars_in_a_mixed_duo(self, tmp_path,
                                                       spy_axes):
        """A raster beside a scatter must not drag the scatter's two
        density ramps onto the figure.  Testing "are all bands density?"
        misses this, because a mixed duo has both kinds."""
        pe.export_png([make_raster_with_bar(), make_scatter_with_bars()],
                      str(tmp_path / "p.png"), nrows=1, ncols=2)
        # 2 cells + 1 raster bar, and no scatter bars.
        assert len(spy_axes) == 3

    def test_panel_legend_does_not_collide_with_the_title(self, tmp_path,
                                                          spy_axes):
        """Both wanted the band immediately above the axes."""
        pe.export_png([make_panel(), make_scatter_with_bars()],
                      str(tmp_path / "p.png"), nrows=1, ncols=2)
        ax = spy_axes[1]
        fig = ax.figure
        fig.canvas.draw()
        leg = ax.get_legend()
        title = ax.title
        assert leg is not None
        lb = leg.get_window_extent()
        tb = title.get_window_extent()
        assert tb.y0 >= lb.y1 - 1.0, "cell title overlaps the panel legend"


class TestColorbarAttachment:
    """Found by the first real headless export (2026-08-16)."""

    def test_lone_value_band_attaches_to_its_own_panel(self, tmp_path,
                                                       spy_axes):
        """A raster beside a scatter must not put the raster's bar at the
        figure edge, next to the panel it does not describe.

        _mappings_agree trivially returns True for a single mapping, so
        "auto" resolved to "shared" and the bar landed against the
        scatter.  Resolution now also requires that every drawable panel
        contributes.
        """
        cells = [make_raster_with_bar(), make_scatter_with_bars()]
        assert pe._resolve_colorbar("auto", cells) == "each"
        pe.export_png(cells, str(tmp_path / "p.png"), nrows=1, ncols=2)
        # 2 cells + 1 bar.  Creation order is per-cell: cell 0, then its
        # bar, then cell 1 -- so the bar is spy_axes[1], not [-1].
        assert len(spy_axes) == 3
        raster_ax, bar_ax, scatter_ax = spy_axes
        assert raster_ax.get_title() == "Amp"
        assert scatter_ax.get_title() == "Amp vs UVDist"
        # The bar sits between the raster it describes and the scatter.
        assert (raster_ax.get_position().x1
                <= bar_ax.get_position().x0
                < scatter_ax.get_position().x0)

    def test_homogeneous_grid_still_shares(self, tmp_path):
        """Every panel contributing an identical mapping keeps one bar."""
        m = _mapping()
        cells = [make_raster_with_bar(f"spw={i}", m) for i in range(4)]
        assert pe._resolve_colorbar("auto", cells) == "shared"

    def test_swatch_is_mid_ramp_not_the_extreme(self):
        """Layer cmaps are picked for a dark GUI background, so their top
        end is near-white and vanishes as a swatch on a light export."""
        band = ColorBand(label="XX", scaling="eq_hist",
                         cmap=("#000000", "#7201a8", "#fdfdc0"))
        assert pe._band_swatch(band) == "#7201a8"


class TestThemeDefaulting:
    """The chrome theme follows the theme the pixels were shaded for."""

    def test_defaults_to_the_panels_shading_theme(self, tmp_path, spy_axes):
        """A palette is baked in before export_png sees the image, and
        ramps are conditioned against a specific background — so a fixed
        "light" default drew dark-conditioned pixels on white and lost
        ~2.5x contrast.  That was the default outcome of the obvious
        headless usage, because the caller had to pass the theme twice."""
        from dataclasses import replace
        panel = make_panel()
        dark = RenderedPanel(spec=replace(panel.spec, theme="dark"),
                             image=panel.image)
        pe.export_png([dark], str(tmp_path / "d.png"))
        assert spy_axes[0].get_facecolor()[:3] == pytest.approx(
            tuple(int(pe.THEMES["dark"].axes.lstrip("#")[i:i + 2], 16) / 255
                  for i in (0, 2, 4)), abs=0.01)

    def test_explicit_theme_still_wins(self, tmp_path, spy_axes):
        from dataclasses import replace
        panel = make_panel()
        dark = RenderedPanel(spec=replace(panel.spec, theme="dark"),
                             image=panel.image)
        pe.export_png([dark], str(tmp_path / "l.png"), theme="light")
        assert spy_axes[0].get_facecolor()[:3] == pytest.approx(
            (1.0, 1.0, 1.0), abs=0.01)


class TestSpecThemeIsPopulated:
    """``PanelSpec.theme`` must reflect what the pixels were shaded for.

    It defaults to ``"dark"`` on the dataclass, so a producer that never
    sets it looks correct in every dark-themed test and silently mislabels
    every light one.  That is precisely what happened: VisibilityPlotter
    documented itself as stamping the theme onto each panel and did not,
    so a light-themed headless export drew light-conditioned ramps on a
    dark ground.

    These assert the *contract* rather than a producer, so they hold for
    any future producer too.
    """

    def test_export_follows_a_light_spec(self, tmp_path, spy_axes):
        from dataclasses import replace
        panel = make_panel()
        light = RenderedPanel(spec=replace(panel.spec, theme="light"),
                              image=panel.image)
        pe.export_png([light], str(tmp_path / "p.png"))
        assert spy_axes[0].get_facecolor()[:3] == pytest.approx(
            (1.0, 1.0, 1.0), abs=0.01)

    def test_mixed_specs_take_the_first_drawable(self, tmp_path, spy_axes):
        """Panels in one figure share a background, so the first one wins.

        Mixed themes in a single export are a producer bug -- the
        compositor cannot paint two backgrounds -- but it should behave
        predictably rather than by dict ordering.
        """
        from dataclasses import replace
        a, b = make_panel(), make_panel()
        cells = [RenderedPanel(spec=replace(a.spec, theme="light"),
                               image=a.image),
                 RenderedPanel(spec=replace(b.spec, theme="dark"),
                               image=b.image)]
        pe.export_png(cells, str(tmp_path / "p.png"), nrows=1, ncols=2)
        assert spy_axes[0].get_facecolor()[:3] == pytest.approx(
            (1.0, 1.0, 1.0), abs=0.01)


class TestAxisSIPrefix:
    """The SI prefix lives in the label; ticks are divided to match."""

    def _spec(self, **kw):
        base = dict(kind="raster", title="t", x_label="Frequency",
                    y_label="Time", x_range=(3.7255e11, 3.7276e11),
                    y_range=(T0, T1), x_is_time=False, y_is_time=True,
                    agg_n_x=96, agg_n_y=64, color_mode="global",
                    x_unit="Hz", y_unit="s",
                    bands=(ColorBand(label="Amplitude", cmap=PLASMA,
                                     scaling="eq_hist"),))
        base.update(kw)
        return PanelSpec(**base)

    def test_label_carries_the_prefix(self):
        assert self._spec().axis_label("x") == "Frequency [GHz]"

    def test_ticks_are_divided_to_match(self, tmp_path, spy_axes):
        panel = RenderedPanel(spec=self._spec(), image=make_image(64, 96))
        pe.export_png([panel], str(tmp_path / "p.png"))
        fmt = spy_axes[0].xaxis.get_major_formatter()
        assert fmt(3.72649e11, 0) == "372.649"

    def test_time_axis_takes_no_prefix(self):
        """Elapsed formatting is already human-scaled; dividing MJD
        seconds by 1e9 would be nonsense."""
        spec = self._spec()
        assert spec.axis_scale("y") == (1.0, "s")
        assert spec.axis_label("y") == "Time [s]"

    def test_dimensionless_axis_is_unlabelled(self):
        spec = self._spec(x_label="Amplitude", x_unit="", x_is_time=False)
        assert spec.axis_label("x") == "Amplitude"
        assert spec.axis_scale("x")[0] == 1.0

    def test_compound_unit_is_not_prefixed(self):
        """"km/s" from a value in m/s is right; "kdeg" from degrees is
        not, so compound and angular units are left alone."""
        spec = self._spec(x_label="Velocity", x_unit="m/s",
                          x_range=(0.0, 1.5e4))
        assert spec.axis_label("x") == "Velocity [m/s]"

    def test_scale_uses_the_full_extent_not_the_viewport(self, tmp_path,
                                                         spy_axes):
        """Units must not flip mid-zoom.  A viewport deep inside the band
        still gets GHz, because the scale comes from x_range."""
        panel = RenderedPanel(spec=self._spec(), image=make_image(64, 96),
                              viewport=(3.72600e11, 3.72601e11, T0, T1))
        pe.export_png([panel], str(tmp_path / "p.png"))
        assert spy_axes[0].get_xlabel() == "Frequency [GHz]"
        assert spy_axes[0].xaxis.get_major_formatter()(
            3.726e11, 0) == "372.6"

    def test_existing_unit_in_label_is_not_duplicated(self):
        """_axis_label already emits "UV Distance [m]"; prefixing must
        not produce "UV Distance [m] [m]"."""
        spec = self._spec(x_label="UV Distance [m]", x_unit="m",
                          x_range=(0.0, 420.0), x_is_time=False)
        assert spec.axis_label("x") == "UV Distance [m]"
