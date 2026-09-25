"""
test_frame_cache.py -- Tests for Part 6: the backend frame cache that makes
pan / zoom / recolor / export re-use the rows already read instead of reading
the MS again.

Location in repository:
    cubevis/tests/manual/visplot/test_frame_cache.py

Tests against:
    data/reader.py            _FrameCache, _selection_fingerprint, _frame_extent,
                              _default_frame_cache_bytes, XArrayReader
                              (_query_columns_cached, set_frame_cache_limit_mb,
                              frame_cache_stats, _clear_frame_cache)
    data/msv2_backend.py      query_columns (uses the cache)
    selection.py              SelectionSpec.cache_generation
    visibility_plotter.py     Reload -> cache_generation bump + re-render

Run from the cubevis repository root:

    MS=sis14_twhya_calibrated_flagged.ms \\
        pytest cubevis/tests/manual/visplot/test_frame_cache.py -v

Design being tested (see the Part 6 notes): one cache entry per (x axis, y axis,
polarization, selection fingerprint); an entry is valid only for the
``cache_generation`` it was read under (Reload bumps it); a byte-budgeted LRU;
returned frames are shallow copies; the cache lives on the backend, so it works
identically for a local backend and a remote worker.
"""
from __future__ import annotations

import asyncio
import os
import threading
import time

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("datashader")

from cubevis.toolbox.visplot import palettes
from cubevis.toolbox.visplot.axes import Axis
from cubevis.toolbox.visplot.data import reader as rd
from cubevis.toolbox.visplot.data.reader import ScatterLayerSpec
from cubevis.toolbox.visplot.selection import SelectionSpec

CMAP = tuple(palettes.scatter_cmaps(theme="dark")[0])
CAT = tuple(palettes.categorical_cmap(theme="dark"))


def _ms_path() -> str:
    path = os.environ.get("MS", "sis14_twhya_calibrated_flagged.ms")
    if not os.path.isdir(path):
        pytest.skip(f"Test MS not found at {path!r}; set MS=")
    return path


@pytest.fixture(scope="module")
def _backend():
    from cubevis.toolbox.visplot.data.msv2_backend import MSv2Backend
    b = MSv2Backend(_ms_path())
    b.open()
    yield b
    b.close()


@pytest.fixture()
def be(_backend, monkeypatch):
    """The shared backend with a clean cache, a generous budget, and a spy on
    the real (uncached) read so tests can count actual disk reads."""
    monkeypatch.setattr(rd, "_GLOBAL_FRAME_CACHE", None)   # a fresh process-wide cache
    _backend.set_frame_cache_limit_mb(512)
    reads = []
    real = _backend._query_columns_raw

    def spy(xaxis, yaxes, selection):
        reads.append(list(yaxes))
        return real(xaxis, yaxes, selection)

    monkeypatch.setattr(_backend, "_query_columns_raw", spy)
    _backend.reads = reads
    yield _backend
    _backend._clear_frame_cache()


def _sel(**kw):
    kw.setdefault("scan", ["12", "14"])
    kw.setdefault("channel_range", (0, 8))
    return SelectionSpec(**kw)


def _cont(pol="XX", axis=Axis.AMPLITUDE):
    return ScatterLayerSpec(y_axis=axis, polarization=pol, cmap=CMAP)


def _cat(axis=Axis.SCAN, pol="XX"):
    return ScatterLayerSpec(y_axis=Axis.AMPLITUDE, polarization=pol, cmap=CAT,
                            coloring="categorical", colorize_axis=axis)


def _q(be, layers, sel=None, xaxis=Axis.UVDIST, **kw):
    return be.query_columns(xaxis, list(layers), sel or _sel(), width=300, height=200, **kw)


# ---------------------------------------------------------------------------
# 1. Hits
# ---------------------------------------------------------------------------

class TestHits:
    def test_the_first_call_reads_and_a_pan_or_zoom_does_not(self, be):
        r = _q(be, [_cont()])
        assert len(be.reads) == 1
        fx, fy = r.x_range, r.y_range
        _q(be, [_cont()], x_range=(fx[0], fx[0] + (fx[1] - fx[0]) / 2), y_range=fy)
        _q(be, [_cont()], x_range=fx, y_range=(fy[0], fy[0] + (fy[1] - fy[0]) / 3))
        assert len(be.reads) == 1

    def test_recoloring_the_same_selection_does_not_read_again(self, be):
        _q(be, [_cont()])
        _q(be, [_cat(Axis.SCAN)])
        _q(be, [_cat(Axis.ANTENNA1)])
        assert len(be.reads) == 1

    def test_the_cached_result_is_identical_to_an_uncached_one(self, be):
        layers = [_cont("XX"), _cat(Axis.SCAN, "YY")]
        _q(be, layers)                                     # populate
        cached = _q(be, layers, x_range=(20.0, 200.0), y_range=(0.0, 30.0))
        be.set_frame_cache_limit_mb(0)                     # the old behaviour
        fresh = _q(be, layers, x_range=(20.0, 200.0), y_range=(0.0, 30.0))
        assert cached.x_range == fresh.x_range and cached.y_range == fresh.y_range
        assert (cached.canvas_width, cached.canvas_height) == (fresh.canvas_width, fresh.canvas_height)
        for a, b in zip(cached.layers, fresh.layers):
            assert np.array_equal(a.image, b.image)
            assert a.n_in_view == b.n_in_view and a.categories == b.categories
            assert np.array_equal(a.id_grid_value, b.id_grid_value, equal_nan=True)

    def test_a_new_layer_reads_only_what_is_missing(self, be):
        _q(be, [_cont("XX")])
        be.reads.clear()
        _q(be, [_cont("XX"), _cont("YY")])
        assert be.reads == [[(Axis.AMPLITUDE, "YY")]]

    def test_dropping_a_layer_is_still_a_hit(self, be):
        _q(be, [_cont("XX"), _cont("YY")])
        be.reads.clear()
        _q(be, [_cont("YY")])
        assert be.reads == []

    def test_stats_count_hits_and_misses(self, be):
        _q(be, [_cont()])
        _q(be, [_cont()])
        s = be.frame_cache_stats()
        assert s["entries"] == 1 and s["misses"] == 1 and s["hits"] == 1 and s["bytes"] > 0

    def test_selection_lists_and_tuples_are_the_same_key(self, be):
        _q(be, [_cont()], _sel(scan=["12", "14"]))
        _q(be, [_cont()], _sel(scan=("12", "14")))
        assert len(be.reads) == 1


# ---------------------------------------------------------------------------
# 2. Misses: anything that changes the rows read must re-read
# ---------------------------------------------------------------------------

class TestMisses:
    def test_a_different_selection(self, be):
        _q(be, [_cont()], _sel(scan=["12"]))
        _q(be, [_cont()], _sel(scan=["14"]))
        _q(be, [_cont()], _sel(scan=["12"], channel_range=(0, 4)))
        assert len(be.reads) == 3

    def test_a_different_x_axis(self, be):
        _q(be, [_cont()], xaxis=Axis.UVDIST)
        _q(be, [_cont()], xaxis=Axis.TIME)
        assert len(be.reads) == 2

    def test_a_different_y_quantity(self, be):
        _q(be, [_cont(axis=Axis.AMPLITUDE)])
        _q(be, [_cont(axis=Axis.PHASE)])
        assert len(be.reads) == 2

    def test_a_different_polarization(self, be):
        _q(be, [_cont("XX")])
        _q(be, [_cont("YY")])
        assert len(be.reads) == 2

    def test_a_bumped_generation_is_a_miss_and_replaces_the_stale_entry(self, be):
        _q(be, [_cont()], _sel(cache_generation=0))
        _q(be, [_cont()], _sel(cache_generation=1))
        assert len(be.reads) == 2
        assert be.frame_cache_stats()["entries"] == 1          # replaced, not accumulated
        _q(be, [_cont()], _sel(cache_generation=1))
        assert len(be.reads) == 2                              # and the new one is a hit

    def test_the_old_generation_does_not_come_back(self, be):
        _q(be, [_cont()], _sel(cache_generation=0))
        _q(be, [_cont()], _sel(cache_generation=1))
        _q(be, [_cont()], _sel(cache_generation=0))
        assert len(be.reads) == 3


# ---------------------------------------------------------------------------
# 3. Budget and eviction
# ---------------------------------------------------------------------------

class TestBudget:
    def test_zero_disables_the_cache_entirely(self, be):
        be.set_frame_cache_limit_mb(0)
        _q(be, [_cont()])
        _q(be, [_cont()])
        assert len(be.reads) == 2 and be.frame_cache_stats()["entries"] == 0

    def test_a_frame_bigger_than_the_whole_budget_is_not_stored(self, be):
        be.set_frame_cache_limit_mb(0.001)
        _q(be, [_cont()])
        _q(be, [_cont()])
        assert len(be.reads) == 2 and be.frame_cache_stats()["entries"] == 0

    def test_lru_evicts_the_least_recently_used_entry(self, be):
        # Three equal-sized selections (4 channels each), so the budget arithmetic
        # holds on any MS -- varying the scan would give an empty frame on a
        # one-scan MS.
        A, B, C = (_sel(channel_range=(0, 4)), _sel(channel_range=(4, 8)),
                   _sel(channel_range=(8, 12)))
        _q(be, [_cont()], A)
        one = be.frame_cache_stats()["bytes"]
        assert one > 0
        be.set_frame_cache_limit_mb(one * 2.6 / 2 ** 20)       # room for two, not three
        _q(be, [_cont()], B)                                   # cache: A, B
        _q(be, [_cont()], A)                                   # touch A -> B is now oldest
        _q(be, [_cont()], C)                                   # C evicts B
        be.reads.clear()
        _q(be, [_cont()], A)                                   # A survived
        assert be.reads == []
        _q(be, [_cont()], B)                                   # B was evicted
        assert len(be.reads) == 1
        assert be.frame_cache_stats()["evictions"] >= 1

    def test_the_budget_is_never_exceeded(self, be):
        _q(be, [_cont()], _sel(channel_range=(0, 4)))
        one = be.frame_cache_stats()["bytes"]
        assert one > 0
        be.set_frame_cache_limit_mb(one * 1.5 / 2 ** 20)
        for cr in ((0, 4), (4, 8), (0, 8), (0, 4)):
            _q(be, [_cont()], _sel(channel_range=cr))
            s = be.frame_cache_stats()
            assert s["bytes"] <= s["max_bytes"]

    def test_lowering_the_limit_evicts_down_to_it(self, be):
        _q(be, [_cont("XX"), _cont("YY")])
        assert be.frame_cache_stats()["entries"] == 2
        be.set_frame_cache_limit_mb(be.frame_cache_stats()["bytes"] * 0.6 / 2 ** 20)
        s = be.frame_cache_stats()
        assert s["entries"] == 1 and s["bytes"] <= s["max_bytes"]

    def test_close_releases_the_frames(self, be):
        _q(be, [_cont()])
        assert be.frame_cache_stats()["entries"] == 1
        be._clear_lookup_caches()                              # what close() calls
        assert be.frame_cache_stats()["entries"] == 0 and be.frame_cache_stats()["bytes"] == 0


class TestBudgetDefaults:
    def test_the_environment_variable_wins(self, monkeypatch):
        monkeypatch.setenv(rd.FRAME_CACHE_ENV, "123")
        assert rd._default_frame_cache_bytes() == 123 * 2 ** 20

    def test_zero_in_the_environment_disables(self, monkeypatch):
        monkeypatch.setenv(rd.FRAME_CACHE_ENV, "0")
        assert rd._default_frame_cache_bytes() == 0

    def test_a_garbage_environment_value_falls_back_to_the_default(self, monkeypatch):
        monkeypatch.setenv(rd.FRAME_CACHE_ENV, "lots")
        monkeypatch.setattr(rd, "_physical_memory_bytes", lambda: 16 * 2 ** 30)
        assert rd._default_frame_cache_bytes() == int(0.10 * 16 * 2 ** 30)

    @pytest.mark.parametrize("phys_gib,expected_bytes", [
        (1, 256 * 2 ** 20),                       # clamped up to the floor
        (16, int(0.10 * 16 * 2 ** 30)),           # a tenth of memory
        (512, 4 * 2 ** 30),                       # clamped down to the ceiling
    ])
    def test_default_is_a_tenth_of_memory_clamped(self, monkeypatch, phys_gib, expected_bytes):
        monkeypatch.delenv(rd.FRAME_CACHE_ENV, raising=False)
        monkeypatch.setattr(rd, "_physical_memory_bytes", lambda: phys_gib * 2 ** 30)
        assert rd._default_frame_cache_bytes() == expected_bytes

    def test_the_default_leaves_room_for_a_reads_transient_peak(self, monkeypatch):
        """A read peaks at ~4x its final frame; the cache must not be so big
        that cache + one such read exceeds memory on any machine size."""
        monkeypatch.delenv(rd.FRAME_CACHE_ENV, raising=False)
        for gib in (2, 4, 8, 16, 64, 256):
            monkeypatch.setattr(rd, "_physical_memory_bytes", lambda g=gib: g * 2 ** 30)
            b = rd._default_frame_cache_bytes()
            assert b + 4 * b <= 0.75 * gib * 2 ** 30 or b == 256 * 2 ** 20

    def test_unknown_memory_gets_a_conservative_default(self, monkeypatch):
        monkeypatch.delenv(rd.FRAME_CACHE_ENV, raising=False)
        monkeypatch.setattr(rd, "_physical_memory_bytes", lambda: None)
        assert rd._default_frame_cache_bytes() == 512 * 2 ** 20


# ---------------------------------------------------------------------------
# 4. Safety
# ---------------------------------------------------------------------------

class TestSafety:
    def test_returned_frames_are_copies_so_a_caller_cannot_alter_the_cache(self, be):
        key = [(Axis.AMPLITUDE, "XX")]
        first = be._query_columns_cached(Axis.UVDIST, key, _sel())[key[0]]
        first["scribble"] = 1
        first.loc[first.index[:5], "y"] = -1.0
        again = be._query_columns_cached(Axis.UVDIST, key, _sel())[key[0]]
        assert "scribble" not in again.columns
        assert (again["y"].iloc[:5] != -1.0).all()

    def test_rendering_never_writes_into_a_cached_frame(self, be):
        key = [(Axis.AMPLITUDE, "XX")]
        frame = be._query_columns_cached(Axis.UVDIST, key, _sel())[key[0]]
        cols = [c for c in ("x", "y", "time", "baseline_id", "frequency") if c in frame.columns]
        before = pd.util.hash_pandas_object(frame[cols], index=False).sum()
        for layer in (_cont(), _cat(Axis.SCAN), _cat(Axis.BASELINE), _cat(Axis.ANTENNA1)):
            _q(be, [layer])
        again = be._query_columns_cached(Axis.UVDIST, key, _sel())[key[0]]
        assert pd.util.hash_pandas_object(again[cols], index=False).sum() == before

    def test_concurrent_requests_for_one_key_read_once(self, be):
        results, errors = [], []

        def worker():
            try:
                results.append(_q(be, [_cont()]))
            except Exception as exc:                          # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        assert not errors and len(results) == 4
        assert len(be.reads) == 1

    def test_an_unhashable_selection_just_skips_the_cache(self, be):
        class Unhashable:
            __hash__ = None

        sel = _sel()
        sel.scan = ["12", Unhashable()]
        assert rd._selection_fingerprint(sel) is None
        frames = be._query_columns_cached(Axis.UVDIST, [(Axis.AMPLITUDE, "XX")], _sel())
        assert len(frames[(Axis.AMPLITUDE, "XX")]) > 0        # and the normal path is unaffected

    def test_the_extent_is_memoized_and_correct(self, be):
        key = (Axis.AMPLITUDE, "XX")
        frame = be._query_columns_cached(Axis.UVDIST, [key], _sel())[key]
        assert frame.attrs["extent"] == (float(frame.x.min()), float(frame.x.max()),
                                         float(frame.y.min()), float(frame.y.max()))
        assert rd._frame_extent(frame) == frame.attrs["extent"]

    def test_extent_of_an_empty_or_missing_frame_is_none(self):
        assert rd._frame_extent(None) is None
        assert rd._frame_extent(pd.DataFrame({"x": [], "y": []})) is None

    def test_a_fresh_read_gets_a_fresh_extent(self, be):
        key = (Axis.AMPLITUDE, "XX")
        a = be._query_columns_cached(Axis.UVDIST, [key], _sel(cache_generation=0))[key]
        b = be._query_columns_cached(Axis.UVDIST, [key], _sel(cache_generation=1))[key]
        assert a.attrs["extent"] == b.attrs["extent"]          # same data, recomputed not reused
        assert len(be.reads) == 2


class TestSharedBudget:
    """The cache is process-wide: one budget however many backends exist (three
    plotters in one Jupyter kernel must not claim three quarters of RAM), and
    each backend's entries are its own."""

    @pytest.fixture()
    def be2(self, be, monkeypatch):
        from cubevis.toolbox.visplot.data.msv2_backend import MSv2Backend
        b = MSv2Backend(_ms_path())
        b.open()
        reads = []
        real = b._query_columns_raw
        monkeypatch.setattr(b, "_query_columns_raw", lambda x, y, s: (reads.append(list(y)), real(x, y, s))[1])
        b.reads = reads
        yield b
        b.close()

    def test_two_backends_share_one_budget(self, be, be2):
        _q(be, [_cont()])
        one = be.frame_cache_stats()["bytes"]
        be.set_frame_cache_limit_mb(one * 1.6 / 2 ** 20)        # room for one and a half
        _q(be2, [_cont()])
        s = be.frame_cache_stats()
        assert s["bytes"] <= s["max_bytes"] and s["entries"] == 1     # the older one was evicted

    def test_a_backend_is_never_served_another_backends_frames(self, be, be2):
        _q(be, [_cont()])
        _q(be2, [_cont()])                                       # same selection, other backend
        assert len(be.reads) == 1 and len(be2.reads) == 1        # both really read
        assert be.frame_cache_stats()["entries"] == 2

    def test_tokens_are_stable_and_distinct(self, be, be2):
        assert be._frame_token() == be._frame_token()
        assert be._frame_token() != be2._frame_token()

    def test_stats_report_this_backends_share(self, be, be2):
        _q(be, [_cont("XX"), _cont("YY")])
        _q(be2, [_cont("XX")])
        assert be.frame_cache_stats()["backend_entries"] == 2
        assert be2.frame_cache_stats()["backend_entries"] == 1
        assert be.frame_cache_stats()["entries"] == 3

    def test_closing_one_backend_drops_only_its_frames(self, be, be2):
        _q(be, [_cont()])
        _q(be2, [_cont()])
        be2._clear_lookup_caches()                               # what close() calls
        assert be2.frame_cache_stats()["backend_entries"] == 0
        assert be.frame_cache_stats()["backend_entries"] == 1
        be.reads.clear()
        _q(be, [_cont()])
        assert be.reads == []                                    # the other's cache is intact

    def test_a_collected_backend_releases_its_frames_even_if_never_closed(self, be):
        import gc
        from cubevis.toolbox.visplot.data.msv2_backend import MSv2Backend
        b = MSv2Backend(_ms_path())
        b.open()
        _q(b, [_cont()])
        before = be.frame_cache_stats()["entries"]
        assert before == 1
        del b
        gc.collect()
        assert be.frame_cache_stats()["entries"] == 0

    def test_the_limit_is_shared_too(self, be, be2):
        be.set_frame_cache_limit_mb(0)
        assert be2.frame_cache_stats()["max_bytes"] == 0
        _q(be2, [_cont()])
        assert len(be2.reads) == 1 and be2.frame_cache_stats()["entries"] == 0


class TestFrameCacheUnit:
    """The LRU on its own, with synthetic frames."""

    def _frame(self, n=1000):
        return pd.DataFrame({"x": np.zeros(n), "y": np.zeros(n)})

    def test_get_put_and_lru_order(self):
        nb = rd._frame_nbytes(self._frame())
        c = rd._FrameCache(int(nb * 2.5))
        c.put("a", 0, self._frame()); c.put("b", 0, self._frame())
        assert c.get("a", 0) is not None                      # a is now most recent
        c.put("c", 0, self._frame())                          # evicts b
        assert c.get("b", 0) is None and c.get("a", 0) is not None and c.get("c", 0) is not None

    def test_a_stale_generation_is_dropped_on_lookup(self):
        c = rd._FrameCache(10 ** 9)
        c.put("k", 0, self._frame())
        assert c.get("k", 1) is None and c.stats()["entries"] == 0

    def test_putting_the_same_key_twice_does_not_double_count_bytes(self):
        c = rd._FrameCache(10 ** 9)
        c.put("k", 0, self._frame()); c.put("k", 0, self._frame())
        assert c.stats()["entries"] == 1 and c.bytes == rd._frame_nbytes(self._frame())

    def test_an_oversize_frame_is_refused_and_evicts_nothing(self):
        c = rd._FrameCache(rd._frame_nbytes(self._frame(10)) * 3)
        c.put("small", 0, self._frame(10))
        assert c.put("huge", 0, self._frame(100000)) is False
        assert c.get("small", 0) is not None

    def test_clear(self):
        c = rd._FrameCache(10 ** 9)
        c.put("k", 0, self._frame()); c.clear()
        assert c.stats()["entries"] == 0 and c.bytes == 0

    def test_drop_token_removes_exactly_that_tokens_entries_and_their_bytes(self):
        c = rd._FrameCache(10 ** 9)
        for tok in ("a", "b"):
            for i in range(3):
                c.put((tok, i), 0, self._frame())
        one = rd._frame_nbytes(self._frame())
        assert c.drop_token("a") == 3
        assert c.stats()["entries"] == 3 and c.bytes == 3 * one
        assert c.count_token("a") == 0 and c.count_token("b") == 3
        assert c.drop_token("nobody") == 0


# ---------------------------------------------------------------------------
# 5. SelectionSpec.cache_generation
# ---------------------------------------------------------------------------

class TestCacheGenerationField:
    def test_defaults_to_zero(self):
        assert SelectionSpec().cache_generation == 0

    def test_copy_preserves_it(self):
        assert SelectionSpec(scan=["1"], cache_generation=7).copy().cache_generation == 7

    def test_it_is_not_a_constraint(self):
        assert SelectionSpec(cache_generation=3).is_empty()

    def test_it_is_ignored_by_the_fingerprint(self):
        assert (rd._selection_fingerprint(SelectionSpec(scan=["1"], cache_generation=0))
                == rd._selection_fingerprint(SelectionSpec(scan=["1"], cache_generation=9)))

    def test_it_makes_two_selections_unequal(self):
        assert SelectionSpec(cache_generation=0) != SelectionSpec(cache_generation=1)

    @pytest.mark.parametrize("a,b", [
        (dict(scan=["1"]), dict(scan=["2"])),
        (dict(spw=[0]), dict(spw=[1])),
        (dict(field_names=["a"]), dict(field_names=["b"])),
        (dict(channel_range=(0, 4)), dict(channel_range=(0, 5))),
        (dict(time_range=(0.0, 1.0)), dict(time_range=(0.0, 2.0))),
        (dict(data_column="DATA"), dict(data_column="CORRECTED")),
        (dict(correlation=["XX"]), dict(correlation=["YY"])),
    ])
    def test_every_row_deciding_field_changes_the_fingerprint(self, a, b):
        assert rd._selection_fingerprint(SelectionSpec(**a)) != rd._selection_fingerprint(SelectionSpec(**b))


# ---------------------------------------------------------------------------
# 6. End to end: Reload through the real plotter
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def plotter():
    import warnings
    warnings.filterwarnings("ignore")
    from cubevis.toolbox.visplot import VisibilityPlotter
    return VisibilityPlotter(ms=_ms_path(), backend="auto", field="",
                             correlation="XX,YY", layout="side")


def _run(coro):
    return asyncio.run(coro)


def _msg(plotter, colorize=None, reload=False):
    W = plotter._panel_axis_widgets
    return {
        "field": "", "correlation": "XX,YY", "datacolumn": "data", "reload": reload,
        "panels": {
            "A": {"kind": "raster", "y": W["A"]["raster"]["y_sel"].value,
                  "x": W["A"]["raster"]["x_sel"].value, "qty": W["A"]["raster"]["q_sel"].value},
            "B": {"kind": "scatter", "x": W["B"]["scatter"]["x_sel"].value,
                  "y": W["B"]["scatter"]["y_sel"].value,
                  "colorize": colorize if colorize is not None else [None, None]},
        },
    }


class TestReloadThroughThePlotter:
    @pytest.fixture()
    def backend(self, plotter, monkeypatch):
        b = getattr(plotter._reader, "_backend", None)
        if b is None or not hasattr(b, "_query_columns_raw"):
            pytest.skip("needs an in-process backend")
        reads = []
        real = b._query_columns_raw
        monkeypatch.setattr(b, "_query_columns_raw",
                            lambda x, y, s: (reads.append(list(y)), real(x, y, s))[1])
        b.reads = reads
        return b

    def test_the_selection_carries_the_generation(self, plotter):
        assert plotter._build_selection().cache_generation == plotter._cache_generation

    def test_a_recolor_press_reuses_the_cached_frames(self, plotter, backend):
        _run(plotter._handle_plot(_msg(plotter)))              # warm
        backend.reads.clear()
        cat = {"coloring": "categorical", "colorize_axis": "SCAN", "excluded_categories": []}
        _run(plotter._handle_plot(_msg(plotter, [cat, None])))
        assert backend.reads == []                             # recolored without a disk read

    def test_reload_bumps_the_generation_and_reads_fresh(self, plotter, backend):
        _run(plotter._handle_plot(_msg(plotter)))
        g0 = plotter._cache_generation
        backend.reads.clear()
        _run(plotter._handle_plot(_msg(plotter, reload=True)))
        assert plotter._cache_generation == g0 + 1
        assert plotter._build_selection().cache_generation == g0 + 1
        assert backend.reads, "Reload must actually re-read the data"

    def test_a_press_after_reload_is_cached_again(self, plotter, backend):
        _run(plotter._handle_plot(_msg(plotter, reload=True)))
        backend.reads.clear()
        cat = {"coloring": "categorical", "colorize_axis": "SCAN", "excluded_categories": []}
        _run(plotter._handle_plot(_msg(plotter, [cat, None])))
        assert backend.reads == []

    def test_reload_twice_bumps_twice(self, plotter):
        g = plotter._cache_generation
        _run(plotter._handle_plot(_msg(plotter, reload=True)))
        _run(plotter._handle_plot(_msg(plotter, reload=True)))
        assert plotter._cache_generation == g + 2
