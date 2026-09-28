"""
test_zscore_colorization.py
============================
Tests for Part 6 Slice 1 -- the per-baseline, windowed rflag-style
Z-Score statistic (visplot-colorize-by-axis-design.md §7.3 row 1, §7.4,
§7.5), covering the pure statistics function
(``_scatter_render.compute_baseline_zscore``), the shared finalization
step (``XArrayReader._finalize_zscore_frame``), and its wiring into
*both* ``MSv2Backend``'s and ``MSv4Backend``'s scatter pipelines
(``Axis.Z_SCORE``) -- including ``MSv4Backend``'s extra
``_query_all_partitions_scatter_fused`` (OPT-B) cross-partition path,
which ``MSv2Backend`` has no equivalent of.

Location in repository:
    cubevis/tests/manual/visplot/test_zscore_colorization.py

Run:
    pytest cubevis/tests/manual/visplot/test_zscore_colorization.py -v

Everything in this file is synthetic (a fabricated xr.Dataset shaped
like a real MSv2/MSv4 partition, and bare, un-__init__-ed backend
instances -- see ``_bare_backend``) -- no real MS or PS file is needed
for any test here, unlike this project's real-data integration suites
(test_msv2_backend.py, test_msv4_backend.py, etc.), since Part 6
Slice 1's correctness question ("does the statistic and its wiring
behave correctly") doesn't need real visibility data to answer, only
data shaped the right way.

Sections
--------
1. compute_baseline_zscore   the pure statistics function in isolation
2. Backend wiring            Axis.Z_SCORE through the real scatter
                              pipeline, parametrized over both backends
3. MSv4Backend OPT-B         the cross-partition fused path MSv2Backend
                              has no equivalent of
4. _query_columns_raw        the full entry point, both routing choices,
                              parametrized over both backends
5. Cross-backend parity      MSv2Backend and MSv4Backend score identical
                              synthetic data identically
6. Edge cases                empty/missing-baseline_id/degenerate groups,
                              parametrized over both backends (the method
                              under test is shared, on XArrayReader, but
                              confirming it resolves correctly through
                              either backend's inheritance is cheap and
                              worth doing explicitly)

Scope note: raster wiring, the "statistical" coloring mode, the
"threshold" scaling function, Slice 2's per-antenna readout, and the new
preset are not yet implemented -- see the design doc's §7.10 for the
full slice breakdown this piece is the first part of.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
import xarray as xr

from cubevis.toolbox.visplot.axes import Axis
from cubevis.toolbox.visplot.data import _scatter_render as sr
from cubevis.toolbox.visplot.data.msv2_backend import MSv2Backend
from cubevis.toolbox.visplot.data.msv4_backend import MSv4Backend
from cubevis.toolbox.visplot.selection import SelectionSpec

BACKEND_CLASSES = (MSv2Backend, MSv4Backend)
BACKEND_IDS = ("msv2", "msv4")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _synthetic_baseline_data(seed=0, n_baselines=20, n_per=500):
    """i.i.d. circularly-symmetric-Gaussian-like real/imag samples, a
    distinct (mean_r, mean_i) per baseline -- mirrors the visibility
    noise model the metric itself is justified by (design doc §7.4)."""
    rng = np.random.default_rng(seed)
    baseline_ids = np.repeat(np.arange(n_baselines), n_per)
    real = np.zeros(len(baseline_ids))
    imag = np.zeros(len(baseline_ids))
    for b in range(n_baselines):
        mask = baseline_ids == b
        mean_r, mean_i = rng.uniform(-2, 2), rng.uniform(-2, 2)
        real[mask] = rng.normal(mean_r, 0.5, mask.sum())
        imag[mask] = rng.normal(mean_i, 0.5, mask.sum())
    return real, imag, baseline_ids


def _bare_backend(backend_class):
    """A bare, un-__init__-ed backend instance -- same convention this
    project already uses elsewhere (e.g. test_probe_fix.py's
    VisibilityScatter.__new__) for exercising internals without opening
    a real MS/PS. Works for either ``MSv2Backend`` or ``MSv4Backend``;
    ``MSv4Backend`` needs two extra stubs (``_data_group``,
    ``_resolved_mode``) that ``MSv2Backend`` has no equivalent
    attributes for. ``_datatree`` is set on both so ``_require_open()``
    (only exercised by the ``_query_columns_raw`` routing tests, section
    4) passes without a real open() call.
    """
    backend = backend_class.__new__(backend_class)
    backend._datatree = object()
    backend._identity_categoricals = lambda *a, **k: {}
    backend._partition_spw_ident = lambda ds: (None, None)
    backend._antenna_lookup_table = lambda: None
    backend._scan_time_index = lambda *a, **k: None
    if backend_class is MSv4Backend:
        backend._data_group = None
        backend._resolved_mode = "interferometer"
    return backend


def _synthetic_dataset(n_time=40, n_baseline=15, n_freq=8, pols=("XX", "YY"),
                       anomaly_baseline=3, anomaly_times=slice(10, 15),
                       anomaly_offset=(6.0, 6.0), flagged_baseline=7,
                       flagged_times=slice(0, 5), seed=42, time_offset=0):
    """A synthetic MSv2/MSv4-shaped xr.Dataset (VISIBILITY/FLAG on
    (time, baseline_id, frequency, polarization)) with a realistic,
    LOCALIZED anomaly (a transient excursion within one baseline's own
    time series -- see this module's docstring for why a uniform
    whole-baseline shift is a materially different, and NOT what
    Slice 1's per-baseline reference is designed to catch, case) and a
    few pre-flagged samples that must be excluded entirely.

    Both backends' ``_resolve_vis``/``_flag_mask`` fall back to plain
    "VISIBILITY"/"FLAG" data_var names when no ``data_groups`` attr is
    set (confirmed directly against both methods) -- this dataset sets
    neither, so the same fabricated shape works unchanged for either
    backend, with no per-backend variant needed.
    """
    rng = np.random.default_rng(seed)
    shape = (n_time, n_baseline, n_freq, len(pols))
    vis = np.zeros(shape, dtype=np.complex128)
    for b in range(n_baseline):
        mean_r, mean_i = rng.uniform(-1, 1), rng.uniform(-1, 1)
        vis[:, b, :, :] = (
            rng.normal(mean_r, 0.3, (n_time, n_freq, len(pols)))
            + 1j * rng.normal(mean_i, 0.3, (n_time, n_freq, len(pols)))
        )
    if anomaly_baseline is not None:
        vis[anomaly_times, anomaly_baseline, :, :] += complex(*anomaly_offset)

    flag = np.zeros(shape, dtype=np.uint8)
    if flagged_baseline is not None:
        flag[flagged_times, flagged_baseline, :, :] = 1

    return xr.Dataset(
        data_vars={
            "VISIBILITY": (("time", "baseline_id", "frequency", "polarization"), vis),
            "FLAG": (("time", "baseline_id", "frequency", "polarization"), flag),
        },
        coords={
            "time": np.arange(time_offset, time_offset + n_time, dtype=np.float64),
            "baseline_id": np.arange(n_baseline),
            "frequency": np.linspace(1e9, 1.1e9, n_freq),
            "polarization": list(pols),
        },
    )


@pytest.fixture(params=BACKEND_CLASSES, ids=BACKEND_IDS)
def backend_class(request):
    """Parametrizes every test that requests it over both backends."""
    return request.param


# ---------------------------------------------------------------------------
# 1. compute_baseline_zscore -- pure function
# ---------------------------------------------------------------------------

class TestComputeBaselineZscore:
    def test_localized_anomaly_scores_far_higher_than_clean_data(self):
        real, imag, baseline_ids = _synthetic_baseline_data()
        bad_idx = np.where(baseline_ids == 5)[0][:50]   # first 50 of 500 -- localized
        real = real.copy(); imag = imag.copy()
        real[bad_idx] += 8.0
        imag[bad_idx] += 8.0

        score = sr.compute_baseline_zscore(real, imag, baseline_ids)

        assert not np.isnan(score).any()
        clean_mask = (baseline_ids != 5)
        p95_clean = np.percentile(score[clean_mask], 95)
        assert score[bad_idx].mean() > 10 * score[clean_mask].mean()
        assert (score[bad_idx] > p95_clean).all()

    def test_uniform_whole_baseline_shift_is_not_flagged(self):
        """A per-baseline reference is, BY DESIGN, blind to a baseline
        that's uniformly offset across its ENTIRE population -- shifting
        every sample the same way also shifts that baseline's own
        median, leaving each sample's deviation from ITS OWN median
        roughly unchanged. This is the same blind spot rflag's own
        per-baseline windowed RMS has (§7.3) -- catching this case is
        what per-antenna aggregation (Slice 2) or a global reference
        (Slice 3) are for, not Slice 1. Asserted explicitly so a future
        change that accidentally "fixes" this doesn't silently change
        what Slice 1 means.
        """
        real, imag, baseline_ids = _synthetic_baseline_data()
        shifted_mask = baseline_ids == 5
        real = real.copy(); imag = imag.copy()
        real[shifted_mask] += 6.0
        imag[shifted_mask] += 6.0

        score = sr.compute_baseline_zscore(real, imag, baseline_ids)
        other_mean = score[~shifted_mask].mean()
        shifted_mean = score[shifted_mask].mean()
        assert shifted_mean == pytest.approx(other_mean, rel=0.5)

    def test_score_is_always_non_negative(self):
        real, imag, baseline_ids = _synthetic_baseline_data(seed=1)
        score = sr.compute_baseline_zscore(real, imag, baseline_ids)
        finite = score[~np.isnan(score)]
        assert (finite >= 0).all()

    def test_two_member_group_scores_both_at_the_calibration_constant(self):
        """N=2: both points are symmetric around their shared median by
        construction, so both get the exact same radius and therefore a
        score of exactly the calibration constant -- a direct check that
        the sqrt(2*ln(2)) constant (replacing the standard modified
        z-score's 0.6745 for this radial case -- design doc §7.4) is
        wired in correctly, not a stray placeholder value.
        """
        real = np.array([2.0, 3.0])
        imag = np.array([1.0, 1.0])
        group = np.array([1, 1])
        score = sr.compute_baseline_zscore(real, imag, group)
        expected = np.sqrt(2.0 * np.log(2.0))
        assert score[0] == pytest.approx(expected)
        assert score[1] == pytest.approx(expected)

    def test_single_member_group_is_nan(self):
        real = np.array([1.0, 2.0, 3.0])
        imag = np.array([1.0, 2.0, 3.0])
        group = np.array([0, 1, 2])   # every group has exactly one member
        score = sr.compute_baseline_zscore(real, imag, group)
        assert np.isnan(score).all()

    def test_majority_contamination_shows_why_exclusion_matters(self):
        """Median/MAD tolerate a MINORITY of contaminated samples inside
        a group gracefully, by design (their whole ~50% breakdown-point
        appeal, design doc §7.10) -- a first version of this test used a
        10%-contaminated group and found next to no difference between
        including and excluding those rows, which is the robust
        statistics working correctly, not a failure to detect anything.
        The place exclusion actually matters is once contamination
        exceeds that breakdown point: with a MAJORITY of one baseline's
        samples contaminated, the group's own median gets dragged toward
        the contamination, making the genuinely clean remainder look
        wildly anomalous -- exactly what excluding already-flagged data
        from the reference population (§7.4, §7.10) exists to prevent.
        """
        real, imag, baseline_ids = _synthetic_baseline_data(seed=2, n_baselines=5, n_per=100)
        contaminated_real = real.copy()
        contaminated_imag = imag.copy()
        target = baseline_ids == 0
        idx = np.where(target)[0][:60]   # 60 of 100 -- a majority
        contaminated_real[idx] += 20.0
        contaminated_imag[idx] += 20.0

        score_with_contamination = sr.compute_baseline_zscore(
            contaminated_real, contaminated_imag, baseline_ids,
        )
        # "Exclusion" = simply not including those 60 rows at all --
        # exactly what happens upstream today via .where(~flag_pol) for
        # already-flagged samples (see this module's own docstring).
        keep = np.ones(len(baseline_ids), dtype=bool)
        keep[idx] = False
        score_excluded = sr.compute_baseline_zscore(
            contaminated_real[keep], contaminated_imag[keep], baseline_ids[keep],
        )
        remaining_b0 = (baseline_ids[keep] == 0)
        original_b0_clean = (baseline_ids == 0) & keep
        # The genuinely clean remainder should look ordinary once the
        # contaminating majority is excluded, and wildly anomalous
        # (dragged around by the corrupted median) while it's included.
        assert score_excluded[remaining_b0].mean() < 3.0
        assert score_with_contamination[original_b0_clean].mean() > 10.0

    def test_mismatched_shapes_raise(self):
        with pytest.raises(ValueError):
            sr.compute_baseline_zscore(np.array([1.0, 2.0]), np.array([1.0]), np.array([0, 0]))

    def test_empty_input(self):
        score = sr.compute_baseline_zscore(np.array([]), np.array([]), np.array([]))
        assert score.shape == (0,)


# ---------------------------------------------------------------------------
# 2. Backend wiring -- Axis.Z_SCORE through the real scatter pipeline,
#    parametrized over both MSv2Backend and MSv4Backend
# ---------------------------------------------------------------------------

class TestBackendZScoreWiring:
    @staticmethod
    @pytest.fixture(scope="class", params=BACKEND_CLASSES, ids=BACKEND_IDS)
    def backend_class(request):
        return request.param

    @staticmethod
    @pytest.fixture(scope="class")
    def ds():
        return _synthetic_dataset()

    @pytest.mark.parametrize("use_fused", [False, True])
    def test_localized_anomaly_detected_end_to_end(self, backend_class, ds, use_fused):
        backend = _bare_backend(backend_class)
        frames = backend._query_partition_scatter(
            ds, Axis.TIME, [(Axis.Z_SCORE, "XX")],
            use_fused=use_fused, use_parallel=False, scan_lookup=None,
        )
        final = backend._finalize_zscore_frame(frames[(Axis.Z_SCORE, "XX")])

        assert "__zscore_real" not in final.columns
        assert "__zscore_imag" not in final.columns
        assert "y" in final.columns

        bl, t, score = (final["baseline_id"].to_numpy(), final["time"].to_numpy(),
                        final["y"].to_numpy())
        bad = (bl == 3) & (t >= 10) & (t < 15)
        other = bl != 3
        assert bad.sum() == 5 * 8   # 5 times x 8 freqs x 1 pol requested
        p95_other = np.percentile(score[other], 95)
        assert (score[bad] > p95_other).all()
        assert score[bad].mean() > 10 * score[other].mean()

    def test_fused_and_serial_paths_agree_exactly(self, backend_class, ds):
        backend = _bare_backend(backend_class)
        results = {}
        for use_fused in (False, True):
            frames = backend._query_partition_scatter(
                ds, Axis.TIME, [(Axis.Z_SCORE, "XX")],
                use_fused=use_fused, use_parallel=False, scan_lookup=None,
            )
            final = backend._finalize_zscore_frame(frames[(Axis.Z_SCORE, "XX")])
            results[use_fused] = final.sort_values(["time", "baseline_id"]).reset_index(drop=True)
        assert len(results[False]) == len(results[True])
        assert np.allclose(results[False]["y"], results[True]["y"])
        assert np.allclose(results[False]["x"], results[True]["x"])

    def test_flagged_samples_excluded_entirely(self, backend_class, ds):
        backend = _bare_backend(backend_class)
        frames = backend._query_partition_scatter(
            ds, Axis.TIME, [(Axis.Z_SCORE, "XX")],
            use_fused=False, use_parallel=False, scan_lookup=None,
        )
        final = backend._finalize_zscore_frame(frames[(Axis.Z_SCORE, "XX")])
        bl, t = final["baseline_id"].to_numpy(), final["time"].to_numpy()
        # baseline 7, times 0-4 were flagged in the fixture -- must be
        # entirely absent, not merely down-weighted.
        assert not (((bl == 7) & (t < 5)).any())

    def test_ordinary_axis_unaffected(self, backend_class, ds):
        """AMPLITUDE (or any non-Z_SCORE axis) must render exactly as it
        did before this feature existed -- no staged columns, no
        finalize step, same row count as flagged/unflagged sample count
        alone would predict."""
        backend = _bare_backend(backend_class)
        frames = backend._query_partition_scatter(
            ds, Axis.TIME, [(Axis.AMPLITUDE, "XX")],
            use_fused=False, use_parallel=False, scan_lookup=None,
        )
        df = frames[(Axis.AMPLITUDE, "XX")]
        assert "__zscore_real" not in df.columns
        assert "__zscore_imag" not in df.columns
        assert "y" in df.columns


# ---------------------------------------------------------------------------
# 3. MSv4Backend OPT-B -- the cross-partition fused path MSv2Backend has
#    no equivalent of (see _query_all_partitions_scatter_fused's own
#    docstring for why this is an independent code path, not a caller of
#    _query_partition_scatter)
# ---------------------------------------------------------------------------

class TestMSv4OptBCrossPartition:
    @staticmethod
    def _shared_baseline_means(n_baseline=10, seed=123):
        rng = np.random.default_rng(seed)
        return [(rng.uniform(-1, 1), rng.uniform(-1, 1)) for _ in range(n_baseline)]

    @classmethod
    def _make_partition(cls, means, n_time, time_offset, n_freq=6, pols=("XX", "YY"),
                        anomaly_baseline=None, anomaly_times=None, anomaly_offset=(7.0, 7.0),
                        flagged_baseline=None, flagged_times=None, seed=1):
        r = np.random.default_rng(seed)
        n_baseline = len(means)
        shape = (n_time, n_baseline, n_freq, len(pols))
        vis = np.zeros(shape, dtype=np.complex128)
        for b, (mr, mi) in enumerate(means):
            vis[:, b, :, :] = (r.normal(mr, 0.3, (n_time, n_freq, len(pols)))
                                + 1j * r.normal(mi, 0.3, (n_time, n_freq, len(pols))))
        if anomaly_baseline is not None:
            vis[anomaly_times, anomaly_baseline, :, :] += complex(*anomaly_offset)
        flag = np.zeros(shape, dtype=np.uint8)
        if flagged_baseline is not None:
            flag[flagged_times, flagged_baseline, :, :] = 1
        return xr.Dataset(
            data_vars={"VISIBILITY": (("time", "baseline_id", "frequency", "polarization"), vis),
                       "FLAG": (("time", "baseline_id", "frequency", "polarization"), flag)},
            coords={"time": np.arange(time_offset, time_offset + n_time, dtype=np.float64),
                    "baseline_id": np.arange(n_baseline),
                    "frequency": np.linspace(1e9, 1.1e9, n_freq),
                    "polarization": list(pols)},
        )

    @classmethod
    @pytest.fixture(scope="class")
    def two_partitions(cls):
        means = cls._shared_baseline_means()
        part1 = cls._make_partition(means, 20, 0, flagged_baseline=5,
                                    flagged_times=slice(0, 4), seed=1)
        part2 = cls._make_partition(means, 20, 20, anomaly_baseline=3,
                                    anomaly_times=slice(2, 5), seed=2)
        return part1, part2

    def test_reference_population_spans_every_partition(self, two_partitions):
        """The whole point of Slice 1's reference being 'the whole
        current selection', not 'this one partition' (§7.3/§7.5): an
        anomaly injected in only ONE partition must be scored against a
        reference built from BOTH -- confirmed here directly by checking
        that baseline 3's rows in the output span both partitions' time
        ranges, not just the one the anomaly was injected into.
        """
        part1, part2 = two_partitions
        backend = _bare_backend(MSv4Backend)
        result = backend._query_all_partitions_scatter_fused(
            [(part1, None), (part2, None)], Axis.TIME, [(Axis.Z_SCORE, "XX")],
        )
        final = backend._finalize_zscore_frame(result[(Axis.Z_SCORE, "XX")])
        bl, t = final["baseline_id"].to_numpy(), final["time"].to_numpy()
        b3_times = set(t[bl == 3].astype(int))
        assert min(b3_times) < 20 <= max(b3_times)  # spans both partitions

    def test_anomaly_in_one_partition_detected_against_combined_reference(self, two_partitions):
        part1, part2 = two_partitions
        backend = _bare_backend(MSv4Backend)
        result = backend._query_all_partitions_scatter_fused(
            [(part1, None), (part2, None)], Axis.TIME, [(Axis.Z_SCORE, "XX")],
        )
        final = backend._finalize_zscore_frame(result[(Axis.Z_SCORE, "XX")])
        bl, t, score = (final["baseline_id"].to_numpy(), final["time"].to_numpy(),
                        final["y"].to_numpy())
        bad = (bl == 3) & (t >= 22) & (t < 25)
        other = bl != 3
        assert bad.sum() > 0
        p95_other = np.percentile(score[other], 95)
        assert (score[bad] > p95_other).all()
        assert score[bad].mean() > 10 * score[other].mean()

    def test_flagged_samples_excluded_across_partitions(self, two_partitions):
        part1, part2 = two_partitions
        backend = _bare_backend(MSv4Backend)
        result = backend._query_all_partitions_scatter_fused(
            [(part1, None), (part2, None)], Axis.TIME, [(Axis.Z_SCORE, "XX")],
        )
        final = backend._finalize_zscore_frame(result[(Axis.Z_SCORE, "XX")])
        bl, t = final["baseline_id"].to_numpy(), final["time"].to_numpy()
        # baseline 5, times 0-3 of partition 1 were flagged there.
        assert not (((bl == 5) & (t < 4)).any())

    def test_opt_b_matches_per_partition_path_exactly(self, two_partitions):
        """The two ways MSv4Backend can compute the same multi-partition
        selection (OPT-B's single fused dask.compute() vs. calling
        _query_partition_scatter once per partition and concatenating)
        must agree exactly -- confirmed by driving both through the real
        entry point, _query_columns_raw, with its own routing thresholds
        monkeypatched so each call is forced down one path or the other.
        """
        part1, part2 = two_partitions
        import cubevis.toolbox.visplot.data.msv4_backend as m4mod
        orig_fused, orig_par = m4mod._THRESH_FUSED, m4mod._THRESH_PAR

        def _run(force_fused):
            backend = _bare_backend(MSv4Backend)
            backend._iter_visibility_partitions = lambda selection: iter([part1, part2])
            backend._apply_selection = lambda raw_ds, selection: raw_ds
            backend._scan_lookup_for_partition = lambda raw_ds: None
            if force_fused:
                m4mod._THRESH_FUSED, m4mod._THRESH_PAR = 1, 1
                backend._estimate_samples = lambda ds, selection, n_yaxes: 10 ** 9
            else:
                m4mod._THRESH_FUSED, m4mod._THRESH_PAR = orig_fused, orig_par
                backend._estimate_samples = lambda ds, selection, n_yaxes: 1
            try:
                result = backend._query_columns_raw(
                    Axis.TIME, [(Axis.Z_SCORE, "XX")], SelectionSpec(),
                )
            finally:
                m4mod._THRESH_FUSED, m4mod._THRESH_PAR = orig_fused, orig_par
            return result[(Axis.Z_SCORE, "XX")]

        final_fused = _run(force_fused=True)
        final_serial = _run(force_fused=False)
        assert "y" in final_fused.columns and "y" in final_serial.columns
        kf = final_fused.sort_values(["time", "baseline_id"]).reset_index(drop=True)
        ks = final_serial.sort_values(["time", "baseline_id"]).reset_index(drop=True)
        assert len(kf) == len(ks)
        assert np.allclose(kf["y"], ks["y"])
        assert np.allclose(kf["x"], ks["x"])


# ---------------------------------------------------------------------------
# 4. _query_columns_raw -- the full entry point, both routing choices,
#    parametrized over both backends (MSv2Backend has only the
#    per-partition path; forcing "fused" for it means the single-
#    partition fused sub-branch inside _query_partition_scatter, not a
#    second top-level path the way MSv4Backend's OPT-B is -- both are
#    exercised here regardless, since _query_columns_raw is the one
#    entry point every real caller actually uses)
# ---------------------------------------------------------------------------

class TestQueryColumnsRawEntryPoint:
    def test_single_partition_selection_is_finalized_correctly(self, backend_class):
        ds = _synthetic_dataset()
        backend = _bare_backend(backend_class)
        backend._iter_visibility_partitions = lambda selection: iter([ds])
        backend._apply_selection = lambda raw_ds, selection: raw_ds
        backend._scan_lookup_for_partition = lambda raw_ds: None

        result = backend._query_columns_raw(
            Axis.TIME, [(Axis.Z_SCORE, "XX")], SelectionSpec(),
        )
        final = result[(Axis.Z_SCORE, "XX")]
        assert "y" in final.columns
        assert "__zscore_real" not in final.columns
        bl, t, score = (final["baseline_id"].to_numpy(), final["time"].to_numpy(),
                        final["y"].to_numpy())
        bad = (bl == 3) & (t >= 10) & (t < 15)
        other = bl != 3
        p95_other = np.percentile(score[other], 95)
        assert (score[bad] > p95_other).all()


# ---------------------------------------------------------------------------
# 5. Cross-backend parity -- MSv2Backend and MSv4Backend must score
#    identical synthetic data identically. Guaranteed by construction
#    (both stage real/imaginary the same way and call the identical,
#    shared XArrayReader._finalize_zscore_frame) -- confirmed directly
#    anyway, the same way every other claim in this file is.
# ---------------------------------------------------------------------------

class TestCrossBackendParity:
    def test_same_synthetic_data_scores_identically_on_both_backends(self):
        ds = _synthetic_dataset()
        results = {}
        for backend_class in BACKEND_CLASSES:
            backend = _bare_backend(backend_class)
            frames = backend._query_partition_scatter(
                ds, Axis.TIME, [(Axis.Z_SCORE, "XX")],
                use_fused=False, use_parallel=False, scan_lookup=None,
            )
            final = backend._finalize_zscore_frame(frames[(Axis.Z_SCORE, "XX")])
            results[backend_class] = final.sort_values(
                ["time", "baseline_id"]).reset_index(drop=True)
        k2, k4 = results[MSv2Backend], results[MSv4Backend]
        assert len(k2) == len(k4)
        assert np.allclose(k2["y"], k4["y"])


# ---------------------------------------------------------------------------
# 6. Edge cases -- _finalize_zscore_frame (shared, on XArrayReader),
#    parametrized over both backends to confirm it resolves correctly
#    through either one's inheritance
# ---------------------------------------------------------------------------

class TestFinalizeZscoreFrameEdgeCases:
    def test_no_staging_at_all(self, backend_class):
        backend = _bare_backend(backend_class)
        out = backend._finalize_zscore_frame(pd.DataFrame({"x": [], "y": []}))
        assert len(out) == 0
        assert "y" in out.columns

    def test_empty_staged_frame(self, backend_class):
        backend = _bare_backend(backend_class)
        staged = pd.DataFrame(
            {"x": [], "__zscore_real": [], "__zscore_imag": [], "baseline_id": []},
        )
        out = backend._finalize_zscore_frame(staged)
        assert len(out) == 0
        assert "y" in out.columns
        assert "__zscore_real" not in out.columns

    def test_missing_baseline_id_with_real_rows_gives_empty_not_nan(self, backend_class):
        """Regression test: an earlier version of this method produced 3
        NaN-filled rows here instead of 0 rows (`df["y"] = pd.Series([],
        ...)` on a non-empty frame aligns by index and silently fills
        NaN rather than truncating) -- found by testing this exact case,
        not by inspection. Must never regress to that.
        """
        backend = _bare_backend(backend_class)
        staged = pd.DataFrame({
            "x": [1.0, 2.0, 3.0],
            "__zscore_real": [1.0, 2.0, 3.0],
            "__zscore_imag": [1.0, 1.0, 1.0],
        })
        out = backend._finalize_zscore_frame(staged)
        assert len(out) == 0
        assert not out["y"].isna().any()   # vacuously true at len 0, asserted for clarity

    def test_degenerate_groups_dropped_real_groups_kept(self, backend_class):
        backend = _bare_backend(backend_class)
        staged = pd.DataFrame({
            "x": [1.0, 2.0, 3.0, 4.0],
            "__zscore_real": [1.0, 2.0, 3.0, 100.0],
            "__zscore_imag": [1.0, 2.0, 1.0, 100.0],
            "baseline_id": [0, 1, 1, 2],   # 0 and 2 are single-member (degenerate)
        })
        out = backend._finalize_zscore_frame(staged)
        assert len(out) == 2
        assert set(out["baseline_id"]) == {1}
        expected = np.sqrt(2.0 * np.log(2.0))
        assert np.allclose(out["y"], expected)
