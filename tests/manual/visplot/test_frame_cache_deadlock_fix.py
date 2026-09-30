"""
test_frame_cache_deadlock_fix.py
==================================
Tests for a real, confirmed deadlock in the shared frame cache
(reader.py), found via a full-suite hang's actual py-spy stack trace
(2026-09) -- not by inspection, not hypothetical. Also covers a real
regression an EARLIER version of this fix introduced, found the same
way: via a subsequent full-suite run that no longer hung, but now had
one new, genuine failure.

The cycle: ``XArrayReader._query_columns_cached`` holds the cache's
process-wide ``RLock`` across the entire, potentially slow, multi-
threaded ``_query_columns_raw`` build call (a real dask compute) --
deliberately: that's what makes concurrent requests for the same
missing key coalesce into a single real read rather than each caller
redundantly repeating the same expensive query (see
``TestSafety.test_concurrent_requests_for_one_key_read_once`` in
``test_frame_cache.py`` -- an existing, pre-2026-09 test this file's
own fix must not break). A ``weakref.finalize`` callback
(``_drop_backend_frames``, registered on every backend to drop its
cache entries when garbage-collected without an explicit ``close()``)
needs that SAME lock via ``drop_token``. If that callback fires --
which can happen at essentially any allocation point, including inside
a dask worker thread's own internal graph traversal -- while some
OTHER, unrelated ``_query_columns_cached`` call holds the lock across
its own build, the two deadlock: the worker thread blocks on the lock
forever, while the compute that thread is part of never finishes, so
the lock is never released.

The fix that shipped: ONLY ``_drop_backend_frames``, which now calls a
new, non-blocking ``try_drop_token`` instead of the blocking
``drop_token``. If the lock is contended when the finalizer fires, it
simply leaves that backend's entries for the normal LRU/budget eviction
to reclaim later, rather than making a finalizer -- which should never
block, on principle -- wait on a lock some unrelated thread might hold
for an unbounded time. Explicit, user-initiated ``close()``
(``_clear_frame_cache`` -> ``drop_token``) is unchanged and still
blocking, since waiting briefly there is expected and the drop should
stay guaranteed, not best-effort.

An EARLIER version of this fix instead split ``_query_columns_cached``'s
own critical section, releasing the lock during the build call. That
also fixed the deadlock (confirmed: a subsequent full-suite run no
longer hung) but broke the coalescing guarantee above, since every
concurrent caller now passed through the "is this key already cached"
check before any of them had finished building it. Found immediately
via that same full-suite run's one new failure, not by re-reading the
change. Reverted in favor of the finalizer-only fix, which resolves the
deadlock without touching this method's own locking at all.

Location in repository:
    cubevis/tests/manual/visplot/test_frame_cache_deadlock_fix.py

Run:
    pytest cubevis/tests/manual/visplot/test_frame_cache_deadlock_fix.py -v

All synthetic and backend-independent (the frame cache itself is pure
Python) except sections 3-4, which need a bare MSv2Backend instance but
no real MS.
"""
from __future__ import annotations

import threading
import time

import pandas as pd
import pytest

from cubevis.toolbox.visplot.axes import Axis
from cubevis.toolbox.visplot.data.msv2_backend import MSv2Backend
from cubevis.toolbox.visplot.data.reader import _FrameCache, _drop_backend_frames
from cubevis.toolbox.visplot.selection import SelectionSpec


# ---------------------------------------------------------------------------
# 1. The finalizer path (_drop_backend_frames / try_drop_token) never blocks
# ---------------------------------------------------------------------------

class TestFinalizerNeverBlocks:
    def test_drop_backend_frames_does_not_block_while_the_lock_is_held(self, monkeypatch):
        import cubevis.toolbox.visplot.data.reader as reader_mod
        cache = _FrameCache(max_bytes=10_000_000)
        monkeypatch.setattr(reader_mod, "_GLOBAL_FRAME_CACHE", cache)

        results = {}
        def holder():
            with cache.lock:
                time.sleep(1.5)

        def finalizer_call():
            t0 = time.perf_counter()
            _drop_backend_frames(("other_backend_token",))
            results["duration"] = time.perf_counter() - t0

        t1 = threading.Thread(target=holder)
        t2 = threading.Thread(target=finalizer_call)
        t1.start()
        time.sleep(0.2)
        t2.start()
        t2.join(timeout=5.0)
        t1.join(timeout=5.0)

        assert not t2.is_alive(), "the finalizer call is still blocked"
        assert results["duration"] < 0.5, (
            f"took {results['duration']}s -- should return almost "
            "instantly rather than waiting for a lock held elsewhere"
        )

    def test_the_old_blocking_path_really_did_block(self):
        """Confirms the test above is a meaningful regression guard, not
        vacuously passing: the SAME scenario through the old,
        still-available blocking drop_token() really does block for the
        full duration the lock is held."""
        cache = _FrameCache(max_bytes=10_000_000)
        results = {}

        def holder():
            with cache.lock:
                time.sleep(1.5)

        def old_blocking_call():
            t0 = time.perf_counter()
            cache.drop_token(("other_backend_token",))
            results["duration"] = time.perf_counter() - t0

        t1 = threading.Thread(target=holder)
        t2 = threading.Thread(target=old_blocking_call)
        t1.start()
        time.sleep(0.2)
        t2.start()
        t2.join(timeout=5.0)
        t1.join(timeout=5.0)

        assert results["duration"] > 1.0

    def test_try_drop_token_still_drops_entries_when_uncontended(self):
        """The non-blocking path must still do real work in the ordinary
        (no contention) case -- this isn't a no-op, just non-blocking."""
        cache = _FrameCache(max_bytes=10_000_000)
        cache.put(("tok_a", "x"), 0, pd.DataFrame({"a": [1]}))
        cache.put(("tok_b", "x"), 0, pd.DataFrame({"a": [1]}))
        dropped = cache.try_drop_token("tok_a")
        assert dropped == 1
        assert cache.count_token("tok_a") == 0
        assert cache.count_token("tok_b") == 1


# ---------------------------------------------------------------------------
# 2. Explicit close() is unweakened -- still blocking, still guaranteed
# ---------------------------------------------------------------------------

class TestExplicitCloseStillGuaranteed:
    def test_drop_token_still_blocks_for_an_explicit_caller(self):
        """drop_token() itself (as opposed to try_drop_token) must still
        wait for the lock -- an explicit, user-initiated close() should
        never silently skip dropping frames just because the lock
        happened to be contended at that moment."""
        cache = _FrameCache(max_bytes=10_000_000)
        results = {}

        def holder():
            with cache.lock:
                time.sleep(1.0)

        def explicit_close_call():
            t0 = time.perf_counter()
            cache.drop_token(("tok",))
            results["duration"] = time.perf_counter() - t0

        t1 = threading.Thread(target=holder)
        t2 = threading.Thread(target=explicit_close_call)
        t1.start()
        time.sleep(0.2)
        t2.start()
        t2.join(timeout=5.0)
        t1.join(timeout=5.0)

        assert results["duration"] > 0.5, (
            "drop_token() must still wait for the lock -- weakening this "
            "would make explicit close() unreliable"
        )


# ---------------------------------------------------------------------------
# 3. The deadlock itself: fixed by the finalizer alone, lock-holding intact
# ---------------------------------------------------------------------------

class TestDeadlockFixedWithoutChangingQueryColumnsCachedLocking:
    def test_finalizer_does_not_block_a_concurrent_slow_build(self):
        """Directly reproduces the original hang's shape:
        _query_columns_cached holding cache.lock across a slow build
        (exactly as it still does, deliberately, for coalescing -- see
        section 4) while _drop_backend_frames fires concurrently for an
        unrelated token. Must complete promptly on both sides; neither
        thread may still be alive after the join timeout.
        """
        backend = MSv2Backend.__new__(MSv2Backend)
        backend._datatree = object()
        cache = _FrameCache(max_bytes=10_000_000)
        backend._frame_cache_obj = lambda: cache
        backend._frame_token = lambda: ("test_token",)

        build_started = threading.Event()

        def slow_raw(xaxis, yaxes, selection):
            build_started.set()
            time.sleep(1.0)
            return {k: pd.DataFrame({"x": [1.0], "y": [2.0]}) for k in yaxes}

        backend._query_columns_raw = slow_raw

        results = {}
        def query_thread():
            backend._query_columns_cached(Axis.TIME, [(Axis.AMPLITUDE, "XX")], SelectionSpec())

        def finalizer_thread():
            build_started.wait(timeout=5.0)
            t0 = time.perf_counter()
            import cubevis.toolbox.visplot.data.reader as reader_mod
            reader_mod._GLOBAL_FRAME_CACHE = cache
            _drop_backend_frames(("other_backend_token",))
            results["finalizer_duration"] = time.perf_counter() - t0

        t1 = threading.Thread(target=query_thread)
        t2 = threading.Thread(target=finalizer_thread)
        t1.start()
        t2.start()
        t1.join(timeout=5.0)
        t2.join(timeout=5.0)

        assert not t1.is_alive() and not t2.is_alive(), "one of the threads is still hung"
        assert results["finalizer_duration"] < 0.5


# ---------------------------------------------------------------------------
# 4. Coalescing is preserved: the lock IS still held across the build
# ---------------------------------------------------------------------------

class TestCoalescingPreserved:
    def test_concurrent_requests_for_one_key_trigger_exactly_one_real_read(self):
        """Mirrors test_frame_cache.py's own
        TestSafety.test_concurrent_requests_for_one_key_read_once against
        a bare backend -- this is the exact guarantee an earlier version
        of the deadlock fix broke (see this file's own module docstring).
        """
        backend = MSv2Backend.__new__(MSv2Backend)
        backend._datatree = object()
        cache = _FrameCache(max_bytes=10_000_000)
        backend._frame_cache_obj = lambda: cache
        backend._frame_token = lambda: ("test_token",)
        # These stub frames carry no sample identity, so they exercise the
        # per-flag-state ("legacy") cache path.  Say so up front: otherwise
        # the FlagDB v2 raw-frame path (2026-09-30) reads once, finds no
        # identity columns, and falls back -- a second read that is an
        # artefact of the stub, not of coalescing.  The raw path's own
        # coalescing is tested below.
        backend._cv_raw_unsupported = True

        reads = []
        def counting_raw(xaxis, yaxes, selection):
            reads.append(list(yaxes))
            time.sleep(0.2)   # widens the race window a concurrency bug would need
            return {k: pd.DataFrame({"x": [1.0], "y": [2.0]}) for k in yaxes}
        backend._query_columns_raw = counting_raw

        results, errors = [], []
        def worker():
            try:
                results.append(backend._query_columns_cached(
                    Axis.TIME, [(Axis.AMPLITUDE, "XX")], SelectionSpec(),
                ))
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        [t.start() for t in threads]
        [t.join(timeout=5.0) for t in threads]

        assert not errors and len(results) == 4
        assert len(reads) == 1, (
            f"expected exactly 1 real read for 4 concurrent identical "
            f"requests, got {len(reads)} -- coalescing is broken"
        )

    def test_raw_frame_path_coalesces_concurrent_reads(self):
        """FlagDB v2 raw frames (every valid sample + identity + disk flag):
        4 concurrent identical requests -> exactly one real read."""
        backend = MSv2Backend.__new__(MSv2Backend)
        backend._datatree = object()
        cache = _FrameCache(max_bytes=10_000_000)
        backend._frame_cache_obj = lambda: cache
        backend._frame_token = lambda: ("test_token",)
        backend._pending_deltas = lambda: ()

        reads = []
        def counting_raw(xaxis, yaxes, selection):
            reads.append(list(yaxes))
            time.sleep(0.2)
            return {k: pd.DataFrame({"x": [1.0, 2.0], "y": [2.0, 3.0],
                                     "time": [0.0, 1.0], "baseline_id": [0, 0],
                                     "frequency": [1e9, 1e9],
                                     "__disk_flag": [False, True], "__spw": [0, 0],
                                     "__chan": [0, 0]}) for k in yaxes}
        backend._query_columns_raw = counting_raw

        results, errors = [], []
        def worker():
            try:
                results.append(backend._query_columns_cached(
                    Axis.TIME, [(Axis.AMPLITUDE, "XX")], SelectionSpec(),
                ))
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        [t.start() for t in threads]
        [t.join(timeout=5.0) for t in threads]
        assert not errors and len(results) == 4
        assert len(reads) == 1, f"raw-frame path: {len(reads)} reads for 4 requests"
        for r in results:            # the on-disk flagged row is not drawn
            assert len(r[(Axis.AMPLITUDE, "XX")]) == 1

    def test_the_query_still_returns_correct_data(self):
        """The caching contract itself, unaffected by any of the above."""
        backend = MSv2Backend.__new__(MSv2Backend)
        backend._datatree = object()
        cache = _FrameCache(max_bytes=10_000_000)
        backend._frame_cache_obj = lambda: cache
        backend._frame_token = lambda: ("test_token",)
        backend._query_columns_raw = lambda xaxis, yaxes, selection: {
            k: pd.DataFrame({"x": [1.0, 2.0], "y": [3.0, 4.0]}) for k in yaxes
        }
        result = backend._query_columns_cached(
            Axis.TIME, [(Axis.AMPLITUDE, "XX")], SelectionSpec(),
        )
        assert (Axis.AMPLITUDE, "XX") in result
        assert list(result[(Axis.AMPLITUDE, "XX")]["x"]) == [1.0, 2.0]

        calls = []
        real = backend._query_columns_raw
        backend._query_columns_raw = lambda xaxis, yaxes, selection: (
            calls.append(list(yaxes)), real(xaxis, yaxes, selection),
        )[1]
        backend._query_columns_cached(Axis.TIME, [(Axis.AMPLITUDE, "XX")], SelectionSpec())
        assert calls == [], "a cached key should not trigger another raw query"
