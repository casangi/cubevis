"""
test_frame_columns.py -- Tests for Part 6b: every per-row IDENTITY column of a
frame (scan, field, baseline, both antennas, spw, polarization) is a
small-integer ``pandas.Categorical``, not a per-row string.

Location in repository:
    cubevis/tests/manual/visplot/test_frame_columns.py

Tests against:
    data/reader.py          XArrayReader._identity_categoricals, _scan_categories,
                            _spw_categories, _antenna_code_tables,
                            _PartitionScanLookup.scan_codes
    data/msv2_backend.py    _query_columns_raw / _query_partition_scatter
    data/msv4_backend.py    (same code path; MSv4 tests need the .ps.zarr)
    data/_scatter_render.py render_layer (must be indifferent to the representation)

Run from the cubevis repository root:

    MS=sis14_twhya_calibrated_flagged.ms \\
        pytest cubevis/tests/manual/visplot/test_frame_columns.py -v

Why: scan / antenna1 / antenna2 were per-row ``object`` columns (~0.17 s each per
4M rows to build, 8 B/row) and spw / polarization per-row pandas ``str`` columns
(38 and 10 B/row) -- ~80 of a frame's 111 B/row and ~0.65 s of a 1.7 s read, paid on
every read whether or not any layer colored by them, and held by every cached frame.
Ground truth here is casacore reading the MS directly, not the code under test.
"""
from __future__ import annotations

import os

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("datashader")

from cubevis.toolbox.visplot import palettes
from cubevis.toolbox.visplot.axes import Axis
from cubevis.toolbox.visplot.data import _scatter_render as sr
from cubevis.toolbox.visplot.data.reader import (
    COLORIZE_AXIS_COLUMNS, ScatterLayerSpec, colorizable_axes,
)
from cubevis.toolbox.visplot.selection import SelectionSpec

IDENTITY = ("scan_name", "field_name", "baseline_name", "baseline_antenna1_name",
            "baseline_antenna2_name", "spw", "polarization")
CAT = tuple(palettes.categorical_cmap(theme="dark"))


def _ms_path() -> str:
    path = os.environ.get("MS", "sis14_twhya_calibrated_flagged.ms")
    if not os.path.isdir(path):
        pytest.skip(f"Test MS not found at {path!r}; set MS=")
    return path


@pytest.fixture(scope="module")
def backend():
    from cubevis.toolbox.visplot.data.msv2_backend import MSv2Backend
    b = MSv2Backend(_ms_path())
    b.open()
    b.set_frame_cache_limit_mb(0)             # these tests exercise the read itself
    yield b
    b.close()


SEL = SelectionSpec(scan=["12", "14"], channel_range=(0, 4))


@pytest.fixture(scope="module")
def frames(backend):
    keys = [(Axis.AMPLITUDE, "XX"), (Axis.AMPLITUDE, "YY")]
    return backend._query_columns_raw(Axis.UVDIST, keys, SEL)


@pytest.fixture(scope="module")
def frame(frames):
    return frames[(Axis.AMPLITUDE, "XX")]


@pytest.fixture(scope="module")
def truth():
    casacore = pytest.importorskip("casacore.tables")
    ms = _ms_path()
    t = casacore.table(ms, ack=False)
    return dict(scan=t.getcol("SCAN_NUMBER"), field=t.getcol("FIELD_ID"),
                a1=t.getcol("ANTENNA1"), a2=t.getcol("ANTENNA2"),
                ant=list(casacore.table(ms + "/ANTENNA", ack=False).getcol("NAME")),
                fld=list(casacore.table(ms + "/FIELD", ack=False).getcol("NAME")))


class TestEveryIdentityColumnIsCategorical:
    @pytest.mark.parametrize("col", IDENTITY)
    def test_dtype(self, frame, col):
        assert isinstance(frame[col].dtype, pd.CategoricalDtype), (col, frame[col].dtype)

    @pytest.mark.parametrize("col", IDENTITY)
    def test_no_row_is_missing_an_identity(self, frame, col):
        assert not frame[col].isna().any()

    def test_no_object_or_string_column_remains(self, frame):
        assert not [c for c in frame.columns if frame[c].dtype == object or str(frame[c].dtype) == "str"]

    def test_bookkeeping_column_does_not_leak(self, frame):
        assert "__scan_time_idx" not in frame.columns

    def test_a_frame_is_small(self, frame):
        """Regression guard against reintroducing per-row strings: the frame was
        111 B/row; the five numeric columns alone are 36."""
        per_row = frame.memory_usage(index=False, deep=False).sum() / len(frame)
        assert per_row < 50, per_row


class TestValuesAreRight:
    """Against casacore, independent of the code under test."""

    def test_the_scans_are_the_selected_ones(self, frame):
        assert set(frame["scan_name"].astype(str)) <= {"12", "14"} and len(set(frame["scan_name"])) >= 1

    def test_antenna_names_per_scan_match_the_ms(self, frame, truth):
        for scan in set(frame["scan_name"].astype(str)):
            rows = truth["scan"] == int(scan)
            want1 = {truth["ant"][i] for i in truth["a1"][rows]}
            want2 = {truth["ant"][i] for i in truth["a2"][rows]}
            sub = frame[frame["scan_name"].astype(str) == scan]
            assert set(sub["baseline_antenna1_name"].astype(str)) == want1, scan
            assert set(sub["baseline_antenna2_name"].astype(str)) == want2, scan

    def test_baseline_label_is_row_wise_the_two_antennas(self, frame):
        want = (frame["baseline_antenna1_name"].astype(str) + "&"
                + frame["baseline_antenna2_name"].astype(str))
        assert (frame["baseline_name"].astype(str) == want).all()

    def test_field_per_scan_matches_the_ms(self, frame, truth):
        for scan in set(frame["scan_name"].astype(str)):
            want = {truth["fld"][i] for i in truth["field"][truth["scan"] == int(scan)]}
            got = set(frame.loc[frame["scan_name"].astype(str) == scan, "field_name"].astype(str))
            assert got == want, scan

    @pytest.mark.parametrize("pol", ["XX", "YY"])
    def test_polarization_is_the_frames_own_key(self, frames, pol):
        f = frames[(Axis.AMPLITUDE, pol)]
        assert set(f["polarization"].astype(str)) == {pol}

    def test_spw_is_the_partitions_identity(self, backend, frame):
        idents = set()
        for raw in backend._iter_visibility_partitions(SEL):
            ident, _kind = backend._partition_spw_ident(raw)
            idents.add(str(ident))
        assert set(frame["spw"].astype(str)) <= idents and len(set(frame["spw"])) >= 1

    def test_the_two_polarization_frames_agree_on_everything_but_the_polarization(self, frames):
        xx, yy = frames[(Axis.AMPLITUDE, "XX")], frames[(Axis.AMPLITUDE, "YY")]
        for col in ("scan_name", "field_name", "baseline_name", "baseline_antenna1_name",
                    "baseline_antenna2_name", "spw"):
            assert xx[col].cat.categories.equals(yy[col].cat.categories), col


class TestSharedCategories:
    """MS-wide category lists are what let per-partition frames concatenate
    without falling back to object."""

    def test_the_lists_are_sorted_unique_and_stable(self, backend):
        for get in (backend._scan_categories, backend._spw_categories, backend._field_categories):
            cats = get()
            assert cats is get()                                   # cached
            assert list(cats) == sorted(set(cats))

    def test_antenna_code_tables_invert_the_lookup(self, backend):
        names, c1, c2 = backend._antenna_code_tables()
        a1, a2 = backend._antenna_lookup_table()
        for bid in range(len(a1)):
            if a1[bid] != "":
                assert names[c1[bid]] == a1[bid] and names[c2[bid]] == a2[bid]
            else:
                assert c1[bid] == -1 and c2[bid] == -1

    def test_a_multi_partition_selection_stays_categorical(self, backend):
        """The whole point of MS-wide lists: pd.concat across partitions."""
        n_parts = sum(1 for _ in backend._iter_visibility_partitions(SelectionSpec()))
        df = backend._query_columns_raw(
            Axis.UVDIST, [(Axis.AMPLITUDE, "XX")], SelectionSpec(channel_range=(0, 2))
        )[(Axis.AMPLITUDE, "XX")]
        for col in IDENTITY:
            assert isinstance(df[col].dtype, pd.CategoricalDtype), (col, n_parts)
        assert n_parts >= 1


class TestRenderingIsIndifferentToTheRepresentation:
    """render_layer must give the SAME result for the categorical frame and for
    the equivalent all-strings frame the code used to build."""

    @staticmethod
    def _as_strings(df):
        out = df.copy()
        for col in IDENTITY:
            out[col] = pd.Series(df[col].astype(str).to_numpy(), dtype=object, index=df.index)
        return out

    @pytest.mark.parametrize("axis", [a for a in colorizable_axes()])
    def test_identical_render_for_every_colorizable_axis(self, frame, axis):
        old = self._as_strings(frame)
        x0, x1, y0, y1 = (float(frame.x.min()), float(frame.x.max()),
                          float(frame.y.min()), float(frame.y.max()))
        spec = ScatterLayerSpec(y_axis=Axis.AMPLITUDE, polarization="XX", cmap=CAT,
                                coloring="categorical", colorize_axis=axis)
        a = sr.render_layer(frame, spec, x0, x1, y0, y1, 200, 150, "global", (y0, y1))
        b = sr.render_layer(old, spec, x0, x1, y0, y1, 200, 150, "global", (y0, y1))
        assert a.skip_reason == b.skip_reason
        assert a.categories == b.categories and a.category_members == b.category_members
        assert a.category_colors == b.category_colors
        assert np.array_equal(a.image, b.image)


class TestIdentityCategoricalsUnit:
    """The builder on its own: edge cases the real MS does not exercise."""

    def test_an_out_of_range_baseline_id_becomes_missing_not_wrong(self, backend):
        out = backend._identity_categoricals(None, None, np.array([0, 10 ** 6, -5]))
        assert out["baseline_name"].isna().tolist() == [False, True, True]
        assert out["baseline_antenna1_name"].isna().tolist() == [False, True, True]

    def test_an_empty_frame_is_fine(self, backend):
        out = backend._identity_categoricals(None, None, np.array([], dtype=np.int64),
                                             spw_ident="x", pol="XX", n=0)
        assert all(len(c) == 0 for c in out.values())

    @pytest.mark.parametrize("where", ["before", "after"])
    def test_an_unknown_spw_identity_is_omitted_rather_than_mislabelled(self, backend, where):
        """searchsorted returns an INSERTION point.  A bogus value that sorts
        after every real one lands past the end (caught by a bounds check
        alone); one that sorts BEFORE a real one lands on it and would silently
        take that window's label -- the case that needs the exact-match check.
        (This MS has one SPW, so "after" alone would not exercise it.)"""
        cats = backend._spw_categories()
        bogus = "\x00" if where == "before" else str(cats[-1]) + "_not_a_real_window"
        out = backend._identity_categoricals(None, None, None, spw_ident=bogus, n=5)
        assert "spw" not in out

    def test_a_known_spw_identity_int_or_str_maps_to_its_string(self, backend):
        cats = backend._spw_categories()
        out = backend._identity_categoricals(None, None, None, spw_ident=cats[0], n=3)
        assert out["spw"].astype(str).tolist() == [str(cats[0])] * 3

    def test_polarization_needs_a_row_count(self, backend):
        assert "polarization" not in backend._identity_categoricals(None, None, None, pol="XX")
        out = backend._identity_categoricals(None, None, None, pol="XX", n=4)
        assert out["polarization"].astype(str).tolist() == ["XX"] * 4

    def test_no_lookup_means_no_scan_or_field_column(self, backend):
        out = backend._identity_categoricals(None, np.array([0, 1]), None)
        assert "scan_name" not in out and "field_name" not in out
