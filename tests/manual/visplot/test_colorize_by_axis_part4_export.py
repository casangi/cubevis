"""
test_colorize_by_axis_part4_export.py -- Unit tests for Part 4's
export-path additions: ColorBand's categorical fields (panel_spec.py)
and png_export.py's categorical legend path.

Location in repository:
    cubevis/tests/manual/visplot/test_colorize_by_axis_part4_export.py

Tests against:
    cubevis/cubevis/toolbox/visplot/panel_spec.py
        (ColorBand.kind == "categorical", .categories,
        .category_colors, .category_members)
    cubevis/cubevis/toolbox/visplot/png_export.py
        (_categorical_bands, _wants_legend, _legend_handle_count,
        _legend_ncol, _legend_rows, _legend_handles, _band_key,
        and export_png end-to-end with a categorical band)

Companion documents:
    visplot-colorize-by-axis-design.md
    visplot-colorize-by-axis-handoff-part4.md
    test_colorize_by_axis_render.py (Part 3's equivalent suite, for
    the widget-side/backend pieces this file does not cover -- see
    "Scope" below)

Run from the cubevis repository root (so the package is importable):

    pytest cubevis/tests/manual/visplot/test_colorize_by_axis_part4_export.py -v

Scope
-----
This suite covers only the two modules above -- the parts of Part 4
with no dependency on datashader/xarray-ms/arcae/Bokeh's live-comm
machinery, and the ones actually exercised (and, in two cases, found
wrong and fixed) while implementing this against real inputs rather
than by inspection alone:

    - ``_legend_ncol`` originally sized columns from ``len(bands)``
      (band count) instead of the handle count -- a single 20-category
      band produced ``ncol=1`` (one absurdly wide row) instead of the
      intended wrap into several rows of ``cap`` columns.
    - ``_legend_handles``'/``_legend_handle_count``'s "does the plain
      band get an identifying swatch" condition originally required
      ``len(plain) >= 2`` -- so one continuous layer overlaid with one
      categorical layer (2 visible bands total, only 1 of them
      "plain") silently dropped the continuous layer's own identifying
      swatch. Fixed to gate on total visible-band count instead.

``VisibilityScatter``'s new widget-side pieces (``ScatterLayer.coloring``
/``colorize_axis``, ``update_colorize``, ``colorize_controls``,
``_legend_html``, ``_handle_colorize``) and the sidebar wiring in
``visibility_plotter.py`` are NOT covered here -- they pull in
datashader, xarray, Bokeh's comm/CustomJS machinery, and the rest of
the real backend stack, none of which are available outside the real
environment. Those were reviewed by hand against the same contracts
this file checks mechanically, but have not been executed. Treat them
as reviewed-but-unverified until exercised for real -- ideally by
extending ``test_colorize_by_axis_render.py`` (which already has the
real-MS fixture machinery this suite deliberately avoids needing) with
a widget-level pass alongside its existing backend-level one.
"""
from __future__ import annotations

import math
import os
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Import strategy -- mirrors test_colorize_by_axis_render.py's fallback
# path (local copies, dependency order, sys.modules patching for the
# relative imports each module expects), but only for the two modules
# this suite needs: panel_spec.py has no third-party dependency beyond
# numpy, and png_export.py needs only numpy + matplotlib -- neither
# pulls in datashader/xarray-ms/arcae/Bokeh the way the widget-side
# modules do, which is what makes this suite runnable standalone.
# ---------------------------------------------------------------------------

try:
    from cubevis.toolbox.visplot.panel_spec import ColorBand, PanelSpec, RenderedPanel
    from cubevis.toolbox.visplot import png_export as pe
except ImportError:
    import importlib.util
    import types

    _HERE = Path(__file__).resolve().parent

    def _load(name: str, filename: str, pkg: str):
        spec = importlib.util.spec_from_file_location(f"{pkg}.{name}", _HERE / filename)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[f"{pkg}.{name}"] = mod
        spec.loader.exec_module(mod)
        return mod

    _pkg = "cubevis_visplot_stub"
    sys.modules[_pkg] = types.ModuleType(_pkg)
    _panel_spec_mod = _load("panel_spec", "panel_spec.py", _pkg)
    _load("tick_format", "tick_format.py", _pkg)
    _pe_mod = _load("png_export", "png_export.py", _pkg)

    ColorBand     = _panel_spec_mod.ColorBand
    PanelSpec     = _panel_spec_mod.PanelSpec
    RenderedPanel = _panel_spec_mod.RenderedPanel
    pe            = _pe_mod

import numpy as np


def _band(label="Amplitude XX", kind="density", categories=None,
          category_colors=None, category_members=None, visible=True,
          cmap=("#111111", "#222222")):
    """Small ColorBand builder -- most tests only care about a few fields."""
    return ColorBand(
        label=label, cmap=cmap, scaling="linear", visible=visible,
        kind=kind, categories=categories,
        category_colors=category_colors, category_members=category_members,
    )


def _categorical_band(label="Amplitude XX", n=20, prefix="DA"):
    cats = tuple(f"{prefix}{i:02d}" for i in range(1, n + 1))
    colors = {c: f"#{(i * 37) % 256:02x}{(i * 91) % 256:02x}{(i * 53) % 256:02x}"
              for i, c in enumerate(cats)}
    members = {c: (c,) for c in cats}
    return _band(label=label, kind="categorical", categories=cats,
                 category_colors=colors, category_members=members)


def _spec(bands, title="t"):
    return PanelSpec(
        kind="scatter", title=title, x_label="x", y_label="y",
        x_range=(0.0, 1.0), y_range=(0.0, 1.0),
        x_is_time=False, y_is_time=False,
        agg_n_x=10, agg_n_y=10, color_mode="global", bands=tuple(bands),
    )


# ---------------------------------------------------------------------------
# 1. ColorBand -- categorical fields
# ---------------------------------------------------------------------------

class TestColorBandCategoricalFields:

    def test_categorical_band_constructs_and_defaults(self):
        b = _categorical_band(n=3)
        assert b.kind == "categorical"
        assert len(b.categories) == 3
        assert b.mapping is None
        assert b.peak_density is None

    def test_pre_part4_band_unaffected(self):
        """A band built exactly as before Part 4 gets the old defaults:
        kind='value', and every new field is None."""
        b = ColorBand(label="Amplitude XX", cmap=("#111", "#222"), scaling="linear")
        assert b.kind == "value"
        assert b.categories is None
        assert b.category_colors is None
        assert b.category_members is None


# ---------------------------------------------------------------------------
# 2. png_export -- legend gating (_wants_legend, _categorical_bands)
# ---------------------------------------------------------------------------

class TestWantsLegend:

    def test_single_categorical_band_wants_a_legend(self):
        """The pre-Part-4 rule ('a single band needs no legend, the
        title already names it') does not apply to a categorical band
        -- its per-category color mapping is new information no title
        can carry, so it must get a legend even alone."""
        spec = _spec([_categorical_band()])
        assert pe._wants_legend(spec) is True

    def test_single_plain_band_wants_no_legend(self):
        """Unchanged pre-Part-4 behaviour."""
        spec = _spec([_band(kind="density")])
        assert pe._wants_legend(spec) is False

    def test_two_plain_bands_want_a_legend(self):
        spec = _spec([_band(label="A"), _band(label="B")])
        assert pe._wants_legend(spec) is True

    def test_categorical_band_with_no_categories_yet_wants_no_legend(self):
        """A categorical layer that hasn't rendered (or skipped) has
        categories=None -- nothing to draw a legend from yet."""
        spec = _spec([_band(kind="categorical", categories=None)])
        assert pe._wants_legend(spec) is False


# ---------------------------------------------------------------------------
# 3. png_export -- handle counting and column/row layout
# ---------------------------------------------------------------------------

class TestLegendLayout:

    @pytest.mark.parametrize("n,expected_ncol,expected_rows", [
        (1, 1, 1),
        (5, 5, 1),
        (6, 6, 1),
        (7, 6, 2),
        (12, 6, 2),
        (20, 6, 4),   # CATEGORY_CAP's real worst case
    ])
    def test_ncol_and_rows_wrap_correctly(self, n, expected_ncol, expected_rows):
        spec = _spec([_categorical_band(n=n)])
        assert pe._legend_handle_count(spec.bands) == n
        assert pe._legend_ncol(spec.bands) == expected_ncol
        assert pe._legend_rows(spec.bands) == expected_rows

    def test_plain_bands_are_always_one_row(self):
        """A plain per-layer legend never wraps -- there are rarely
        more than 2-3 layers, and pre-Part-4 behaviour was always
        ncol=len(handles) (one row)."""
        spec = _spec([_band(label=f"L{i}") for i in range(4)])
        assert pe._legend_ncol(spec.bands) == 4
        assert pe._legend_rows(spec.bands) == 1

    def test_single_categorical_band_ncol_bug_regression(self):
        """Regression guard for a real bug found while building this:
        _legend_ncol originally sized columns from len(bands) (band
        count), so one categorical band with 20 categories -> ncol=1
        (band count), not the intended cap of 6. This must stay a
        handle-count computation, not a band-count one."""
        spec = _spec([_categorical_band(n=20)])
        assert pe._legend_ncol(spec.bands) != 1
        assert pe._legend_ncol(spec.bands) == 6


# ---------------------------------------------------------------------------
# 4. png_export -- _legend_handles content
# ---------------------------------------------------------------------------

class TestLegendHandles:

    def test_lone_categorical_band_unprefixed_labels(self):
        spec = _spec([_categorical_band(n=3)])
        handles = pe._legend_handles(spec.bands, pe.THEMES["dark"])
        labels = [h.get_label() for h in handles]
        assert labels == ["DA01", "DA02", "DA03"]

    def test_lone_plain_band_produces_no_handles(self):
        spec = _spec([_band()])
        assert pe._legend_handles(spec.bands, pe.THEMES["dark"]) == []

    def test_two_plain_bands_produce_layer_identifying_handles(self):
        spec = _spec([_band(label="Amp XX"), _band(label="Amp YY")])
        handles = pe._legend_handles(spec.bands, pe.THEMES["dark"])
        labels = {h.get_label() for h in handles}
        assert labels == {"Amp XX", "Amp YY"}

    def test_mixed_plain_and_categorical_disambiguates_and_identifies_both(self):
        """Regression guard for the second real bug found while
        building this: the plain band's identifying swatch must still
        appear when it's outnumbered by a categorical band's many
        per-category swatches -- 'plain bands need >=2 of themselves'
        was the wrong test; 'total visible bands >=2' is the right one.
        """
        plain = _band(label="Amp XX")
        cat = _categorical_band(label="Amp YY (colorized)", n=3)
        spec = _spec([plain, cat])
        handles = pe._legend_handles(spec.bands, pe.THEMES["dark"])
        labels = [h.get_label() for h in handles]
        assert "Amp XX" in labels
        assert sum(lbl.startswith("Amp YY (colorized): ") for lbl in labels) == 3
        assert len(handles) == 4

    def test_hidden_categorical_band_produces_no_handles(self):
        spec = _spec([_categorical_band(n=3, label="hidden")])
        hidden_band = pe._categorical_bands(spec.bands)[0]
        from dataclasses import replace
        spec2 = _spec([replace(hidden_band, visible=False)])
        assert pe._legend_handles(spec2.bands, pe.THEMES["dark"]) == []


# ---------------------------------------------------------------------------
# 5. png_export -- _band_key (figure vs. panel legend resolution)
# ---------------------------------------------------------------------------

class TestBandKey:

    def test_plain_band_key_shape_unchanged(self):
        spec = _spec([_band()])
        key = pe._band_key(spec)
        assert key == (("Amplitude XX", ("#111111", "#222222"), None),)

    def test_two_categorical_bands_with_different_categories_differ(self):
        """Grid/iteration mode: two cells colorizing the same axis can
        still show genuinely different real categories (different
        scan subsets, say). Must NOT be judged 'the same band set' --
        that would share one figure legend that is only correct for
        one of the two cells."""
        spec_a = _spec([_categorical_band(n=2, prefix="DA")], title="a")
        spec_b = _spec([_categorical_band(n=2, prefix="EA")], title="b")
        assert pe._band_key(spec_a) != pe._band_key(spec_b)

    def test_two_categorical_bands_with_same_categories_match(self):
        spec_a = _spec([_categorical_band(n=2)], title="a")
        spec_b = _spec([_categorical_band(n=2)], title="b")
        assert pe._band_key(spec_a) == pe._band_key(spec_b)


# ---------------------------------------------------------------------------
# 6. export_png -- end to end
# ---------------------------------------------------------------------------

class TestExportPngEndToEnd:
    """Full export_png() calls -- catches layout-math bugs unit tests
    on the smaller helpers above could still miss (e.g. reserved-space
    sizing not actually matching what matplotlib needs)."""

    def _panel(self, spec):
        img = np.full((200, 300), 0xFF000000, dtype=np.uint32)
        return RenderedPanel(spec=spec, image=img, viewport=None)

    def test_single_categorical_layer_exports_without_error(self, tmp_path):
        spec = _spec([_categorical_band(n=15)], title="Amplitude vs Time")
        spec = PanelSpec(**{**spec.__dict__, "theme": "dark", "status": "ok"})
        out = pe.export_png([self._panel(spec)], str(tmp_path / "out.png"))
        assert os.path.exists(out)
        assert os.path.getsize(out) > 0

    def test_mixed_plain_and_categorical_exports_without_error(self, tmp_path):
        plain = _band(label="Amp XX")
        cat = _categorical_band(label="Amp YY", n=8)
        spec = _spec([plain, cat], title="Mixed")
        spec = PanelSpec(**{**spec.__dict__, "theme": "dark", "status": "ok"})
        out = pe.export_png([self._panel(spec)], str(tmp_path / "out2.png"))
        assert os.path.exists(out)

    def test_two_plain_layers_unchanged_from_pre_part4(self, tmp_path):
        """Explicit regression check that the ordinary multi-layer
        (no colorize-by-axis involved at all) export path still works
        exactly as before -- Part 4 must be additive, not disruptive,
        for every pre-existing use of this module."""
        band_a = _band(label="Amp XX", cmap=("#ff0000", "#ffaa00"))
        band_b = _band(label="Amp YY", cmap=("#0000ff", "#00aaff"))
        spec = _spec([band_a, band_b], title="Two plain layers")
        spec = PanelSpec(**{**spec.__dict__, "theme": "dark", "status": "ok"})
        out = pe.export_png([self._panel(spec)], str(tmp_path / "out3.png"))
        assert os.path.exists(out)
