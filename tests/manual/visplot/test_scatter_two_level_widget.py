"""
test_scatter_two_level_widget.py
=================================
Widget-level tests for the scatter two-level (Level-1/Level-2) rendering
feature introduced by the scatter two-level rendering handoff notes
(2026-09).

Location in repository:
    cubevis/tests/manual/visplot/test_scatter_two_level_widget.py

Tests against:
    cubevis/cubevis/toolbox/visplot/visibility_scatter.py
        (VisibilityScatter.__init__'s ref_scale parameter,
        _is_remote_backend, _resolve_ref_scale, set_ref_scale,
        _can_resample_locally, _resample_and_composite,
        _do_viewport_rerender, _render_all_layers's ref_scale wiring)

Builds a bare, un-``__init__``-ed ``VisibilityScatter`` via
``VisibilityScatter.__new__`` (matching the existing
test_probe_fix.py/test_scatter_helpers.py convention for exercising
scatter internals without real Bokeh app / Comm / backend machinery),
and populates only the instance attributes each test needs -- most of
them a real ``ScatterLayerReference`` built by
``_scatter_render.build_layer_reference`` against synthetic data, so
these tests exercise the actual reference-consuming code paths, not a
mocked stand-in for them.

Run:
    pytest cubevis/tests/manual/visplot/test_scatter_two_level_widget.py -v

Sections
--------
1. REF_SCALE resolution      _resolve_ref_scale, _is_remote_backend,
                              set_ref_scale
2. _can_resample_locally     the Level-1/Level-2 gate, color_mode="local"
3. _resample_and_composite   per-layer resample + composite, hidden layers
4. _do_viewport_rerender     end-to-end branch selection
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("datashader")
pytest.importorskip("scipy")
pytest.importorskip("bokeh")

from cubevis.toolbox.visplot.axes import Axis
from cubevis.toolbox.visplot.data import _scatter_render as sr
from cubevis.toolbox.visplot.data.reader import ScatterLayerSpec, ScatterLayerReference
from cubevis.toolbox.visplot.remote_reduction_context import RemoteReductionContext
from cubevis.toolbox.visplot.visibility_scatter import (
    VisibilityScatter, ScatterLayer,
    _REF_SCALE_LOCAL_DEFAULT, _REF_SCALE_REMOTE_DEFAULT,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _bare() -> VisibilityScatter:
    """A bare, un-``__init__``-ed VisibilityScatter -- see this module's
    own docstring."""
    return VisibilityScatter.__new__(VisibilityScatter)


class _FakeRemoteBackend(RemoteReductionContext):
    """A RemoteReductionContext by type only -- isinstance checks are all
    _is_remote_backend needs; real construction wants a live kernel."""
    def __init__(self):
        pass


CANVAS_W, CANVAS_H = 200, 150
REF_SCALE = 2.0
REF_W, REF_H = int(round(CANVAS_W * REF_SCALE)), int(round(CANVAS_H * REF_SCALE))


def _synthetic_df(seed=5, n=100_000):
    rng = np.random.default_rng(seed)
    return pd.DataFrame({
        "x": rng.uniform(0, 1000, n),
        "y": np.abs(rng.normal(5, 3, n)),
        "time": rng.uniform(0, 1000, n),
        "baseline_id": rng.integers(0, 40, n),
        "frequency": rng.uniform(1e9, 2e9, n),
    })


def _make_layer_and_reference(df, alpha=1.0):
    layer = ScatterLayer(y_axis=Axis.AMPLITUDE, polarization="XX",
                         cmap=["#000000", "#ffffff"], scaling="eq_hist",
                         alpha=alpha)
    spec = ScatterLayerSpec(y_axis=Axis.AMPLITUDE, polarization="XX",
                            cmap=("#000000", "#ffffff"), scaling="eq_hist",
                            alpha=alpha)
    full_x = (0.0, 1000.0)
    full_y = (float(df.y.min()), float(df.y.max()))
    rendered = sr.render_layer(df, spec, *full_x, *full_y, CANVAS_W, CANVAS_H,
                               "global", full_y)
    ref = sr.build_layer_reference(
        df, spec, *full_x, *full_y, REF_W, REF_H, CANVAS_W, CANVAS_H, "global",
    )
    return layer, rendered, ref, full_x, full_y


def _wired_vs(df=None, alpha=1.0, color_mode="global"):
    """A bare VisibilityScatter with one real layer + reference wired up,
    ready for _can_resample_locally/_resample_and_composite/
    _do_viewport_rerender."""
    df = df if df is not None else _synthetic_df()
    layer, rendered, ref, full_x, full_y = _make_layer_and_reference(df, alpha)
    vs = _bare()
    vs._color_mode = color_mode
    vs._layers = [layer]
    vs._layer_reference = [ref]
    vs._layer_categories = [None]
    vs._layer_images = [rendered.image]
    vs._layer_n_in_view = [rendered.n_in_view]
    vs._layer_skip_reason = [rendered.skip_reason]
    vs._layer_id_grid = [None]
    vs._canvas_width, vs._canvas_height = CANVAS_W, CANVAS_H
    vs._width, vs._height = CANVAS_W, CANVAS_H
    vs._probe_grid_max_cells = 3072
    vs._current_viewport = None
    vs._image_source = None
    return vs, full_x, full_y


# ---------------------------------------------------------------------------
# 1. REF_SCALE resolution
# ---------------------------------------------------------------------------

class TestRefScaleResolution:
    def test_explicit_value_wins_over_default(self):
        vs = _bare()
        assert vs._resolve_ref_scale(3.5, object()) == 3.5

    def test_local_backend_gets_local_default(self):
        vs = _bare()
        assert vs._resolve_ref_scale(None, object()) == _REF_SCALE_LOCAL_DEFAULT

    def test_remote_backend_gets_remote_default(self):
        vs = _bare()
        remote = _FakeRemoteBackend()
        assert vs._resolve_ref_scale(None, remote) == _REF_SCALE_REMOTE_DEFAULT

    def test_is_remote_backend_detects_remote_reduction_context(self):
        vs = _bare()
        assert vs._is_remote_backend(_FakeRemoteBackend()) is True
        assert vs._is_remote_backend(object()) is False
        assert vs._is_remote_backend(None) is False

    def test_set_ref_scale_rejects_non_positive(self):
        vs = _bare()
        vs._ref_scale = 2.0
        vs._layer_images = [None]
        with pytest.raises(ValueError):
            vs.set_ref_scale(0.0)
        with pytest.raises(ValueError):
            vs.set_ref_scale(-1.0)
        assert vs._ref_scale == 2.0  # unchanged after a rejected call

    def test_set_ref_scale_noop_before_first_render(self):
        """No layer has ever been rendered (_layer_images all None) --
        set_ref_scale should update the value but not attempt a
        _rerender() that has nothing to query yet."""
        vs = _bare()
        vs._ref_scale = 2.0
        vs._layer_images = [None, None]
        calls = []
        vs._rerender = lambda *a, **k: calls.append((a, k))
        vs.set_ref_scale(5.0)
        assert vs._ref_scale == 5.0
        assert calls == []

    def test_set_ref_scale_rerenders_after_first_render(self):
        vs = _bare()
        vs._ref_scale = 2.0
        vs._layer_images = [np.zeros((10, 10), dtype=np.uint32)]
        calls = []
        vs._rerender = lambda *a, **k: calls.append((a, k))
        vs.set_ref_scale(5.0)
        assert vs._ref_scale == 5.0
        assert len(calls) == 1


# ---------------------------------------------------------------------------
# 2. _can_resample_locally
# ---------------------------------------------------------------------------

class TestCanResampleLocally:
    def test_true_for_zoom_within_ref_scale(self):
        vs, full_x, full_y = _wired_vs()
        assert vs._can_resample_locally(200.0, 800.0, *full_y) is True

    def test_false_for_zoom_beyond_ref_scale(self):
        vs, full_x, full_y = _wired_vs()
        y_mid = (full_y[0] + full_y[1]) / 2
        span = (full_y[1] - full_y[0]) * 0.02
        assert vs._can_resample_locally(
            400.0, 460.0, y_mid - span, y_mid + span) is False

    def test_false_when_color_mode_is_local(self):
        """color_mode="local" always takes Level-2 -- see
        _can_resample_locally's docstring for why (the local-mode
        reference-population approximation is not shipped)."""
        vs, full_x, full_y = _wired_vs(color_mode="local")
        assert vs._can_resample_locally(200.0, 800.0, *full_y) is False

    def test_false_when_no_layer_has_a_reference(self):
        vs, full_x, full_y = _wired_vs()
        vs._layer_reference = [None]
        assert vs._can_resample_locally(200.0, 800.0, *full_y) is False

    def test_uses_first_available_reference_when_some_layers_lack_one(self):
        """A layer can be skip_reason'd (no reference) while another
        isn't -- the gate must not bail out on index 0 specifically."""
        vs, full_x, full_y = _wired_vs()
        ref = vs._layer_reference[0]
        vs._layers = vs._layers * 2
        vs._layer_reference = [None, ref]
        assert vs._can_resample_locally(200.0, 800.0, *full_y) is True


# ---------------------------------------------------------------------------
# 3. _resample_and_composite
# ---------------------------------------------------------------------------

class TestResampleAndComposite:
    def test_updates_image_and_n_in_view(self):
        vs, full_x, full_y = _wired_vs()
        original_image = vs._layer_images[0]
        img = vs._resample_and_composite(200.0, 800.0, *full_y)
        assert img.shape == (CANVAS_H, CANVAS_W)
        assert img.dtype == np.uint32
        assert vs._layer_images[0] is not original_image
        assert vs._layer_n_in_view[0] > 0
        assert vs._layer_id_grid[0] is not None

    def test_hidden_layer_still_resampled_but_composites_transparent(self):
        """set_alpha()'s free fast path can only un-hide whatever image
        is already cached for the CURRENT viewport -- a hidden layer's
        cached image must stay current across every pan/zoom, exactly
        like _scatter_render.render_layer's own Level-2 behavior."""
        df = _synthetic_df()
        vs, full_x, full_y = _wired_vs(df=df, alpha=0.0)
        img = vs._resample_and_composite(200.0, 800.0, *full_y)
        assert vs._layer_images[0] is not None
        assert np.count_nonzero(img) == 0  # alpha=0 -> fully transparent composite

    def test_layer_with_no_reference_is_left_untouched(self):
        vs, full_x, full_y = _wired_vs()
        vs._layers = vs._layers * 2
        vs._layer_reference = [vs._layer_reference[0], None]
        vs._layer_categories = [None, None]
        vs._layer_images = [vs._layer_images[0], None]
        vs._layer_n_in_view = [vs._layer_n_in_view[0], 0]
        vs._layer_skip_reason = [vs._layer_skip_reason[0], "not queried"]
        vs._layer_id_grid = [None, None]
        vs._resample_and_composite(200.0, 800.0, *full_y)
        assert vs._layer_images[1] is None  # untouched, no reference to resample


# ---------------------------------------------------------------------------
# 4. _do_viewport_rerender
# ---------------------------------------------------------------------------

class TestDoViewportRerender:
    def test_level1_taken_for_easy_zoom_no_backend_call(self):
        vs, full_x, full_y = _wired_vs()
        calls = []
        vs._rerender = lambda *a, **k: calls.append((a, k)) or np.zeros(
            (CANVAS_H, CANVAS_W), dtype=np.uint32)
        result = vs._do_viewport_rerender(200.0, 800.0, *full_y)
        assert calls == []
        assert vs._current_viewport == (200.0, 800.0, full_y[0], full_y[1])
        assert set(result.keys()) == {"image", "x0", "x1", "y0", "y1"}
        assert result["image"].shape == (CANVAS_H, CANVAS_W)
        assert vs._image_source is not None

    def test_level2_taken_for_tight_zoom_calls_rerender(self):
        vs, full_x, full_y = _wired_vs()
        calls = []
        vs._rerender = lambda *a, **k: calls.append((a, k)) or np.zeros(
            (CANVAS_H, CANVAS_W), dtype=np.uint32)
        y_mid = (full_y[0] + full_y[1]) / 2
        span = (full_y[1] - full_y[0]) * 0.02
        result = vs._do_viewport_rerender(400.0, 460.0, y_mid - span, y_mid + span)
        assert len(calls) == 1
        (args, kwargs) = calls[0]
        assert kwargs["x_range"] == (400.0, 460.0)
        assert kwargs["y_range"] == (y_mid - span, y_mid + span)

    def test_normalises_reversed_bounds(self):
        """Bokeh box-zoom can produce start > end on either axis."""
        vs, full_x, full_y = _wired_vs()
        vs._rerender = lambda *a, **k: np.zeros((CANVAS_H, CANVAS_W), dtype=np.uint32)
        result = vs._do_viewport_rerender(800.0, 200.0, full_y[1], full_y[0])
        assert result["x0"] == 200.0 and result["x1"] == 800.0
        assert result["y0"] == full_y[0] and result["y1"] == full_y[1]

    def test_local_color_mode_always_calls_rerender(self):
        vs, full_x, full_y = _wired_vs(color_mode="local")
        calls = []
        vs._rerender = lambda *a, **k: calls.append((a, k)) or np.zeros(
            (CANVAS_H, CANVAS_W), dtype=np.uint32)
        vs._do_viewport_rerender(200.0, 800.0, *full_y)
        assert len(calls) == 1
