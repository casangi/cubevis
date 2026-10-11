"""Scan / Time range / UV range selection (HRS A1, 2026-10-11).

Until A1 the three sidebar boxes and the ``scan`` / ``timerange`` /
``uvrange`` arguments were stored and ignored.  These tests pin the
CASA-style text parsing (``selection_text``) and the UV-range row
selection shared by both backends (``XArrayReader._apply_uv_range``):
rows outside the range look exactly like rows the data do not have.
"""

from __future__ import annotations

import os

import numpy as np
import pytest

from cubevis.toolbox.visplot.selection import SelectionSpec
from cubevis.toolbox.visplot.selection_text import (
    SelectionTextError, format_timerange, parse_scan_text,
    parse_timerange_text, parse_uvrange_text, uv_range_text,
)

# TW Hya: 2012-11-19 07:37:00 .. 09:11:01.632 UTC
OBS = (1353310620.0, 1353316261.632)
DAY0 = 1353283200.0                     # 2012-11-19 00:00:00 UTC
SCANS = ["4", "7", "10", "12", "14", "33", "34", "36"]


class TestScan:

    @pytest.mark.parametrize("text,want", [
        ("", None),
        ("4", ["4"]),
        ("4,7, 10", ["4", "7", "10"]),
        ("10~14", ["10", "12", "14"]),
        ("14~10", ["10", "12", "14"]),
        (">30", ["33", "34", "36"]),
        ("<7", ["4"]),
        ("!4", ["7", "10", "12", "14", "33", "34", "36"]),
        ("10~36,!34", ["10", "12", "14", "33", "36"]),
    ])
    def test_forms(self, text, want):
        assert parse_scan_text(text, SCANS) == want

    @pytest.mark.parametrize("text", ["99", "abc", "1-3", ">40", "!4,!7,4~7,!4~7"])
    def test_refused(self, text):
        with pytest.raises(SelectionTextError):
            parse_scan_text(text, SCANS)

    def test_message_lists_scans(self):
        with pytest.raises(SelectionTextError, match="scans: 4, 7"):
            parse_scan_text("99", SCANS)


class TestTimeRange:

    def test_empty(self):
        assert parse_timerange_text("  ", OBS) is None

    def test_full_dates(self):
        assert parse_timerange_text(
            "2012/11/19/07:40:00~2012/11/19/08:00:30.5", OBS) == (
            DAY0 + 7 * 3600 + 40 * 60, DAY0 + 8 * 3600 + 30.5)

    def test_dash_dates_and_minutes(self):
        assert parse_timerange_text("2012-11-19/07:40~2012-11-19/08:00", OBS) == (
            DAY0 + 7 * 3600 + 40 * 60, DAY0 + 8 * 3600)

    def test_times_of_day(self):
        assert parse_timerange_text("07:40:00~08:00", OBS) == (
            DAY0 + 7 * 3600 + 40 * 60, DAY0 + 8 * 3600)

    def test_open_ends(self):
        assert parse_timerange_text(">08:00", OBS) == (DAY0 + 8 * 3600, OBS[1])
        assert parse_timerange_text("<08:00", OBS) == (OBS[0], DAY0 + 8 * 3600)

    def test_start_and_length(self):
        assert parse_timerange_text("08:00:00+00:10:00", OBS) == (
            DAY0 + 8 * 3600, DAY0 + 8 * 3600 + 600)
        assert parse_timerange_text("08:00:00+90", OBS) == (
            DAY0 + 8 * 3600, DAY0 + 8 * 3600 + 90)

    def test_single_time_is_one_integration(self):
        t = DAY0 + 8 * 3600
        assert parse_timerange_text("08:00:00", OBS, half_integration=3.0) == (t - 3, t + 3)

    def test_day_number_form(self):
        assert parse_timerange_text("0/07:40:00~0/08:00:00", OBS) == (
            DAY0 + 7 * 3600 + 40 * 60, DAY0 + 8 * 3600)

    def test_time_of_day_after_midnight(self):
        # A session from 22:00 to 02:00 the next day: "01:00" is the
        # second day, and an undated end before its start rolls over.
        obs = (DAY0 + 22 * 3600, DAY0 + 26 * 3600)
        assert parse_timerange_text("01:00~01:30", obs) == (
            DAY0 + 25 * 3600, DAY0 + 25.5 * 3600)
        assert parse_timerange_text("23:00~01:00", obs) == (
            DAY0 + 23 * 3600, DAY0 + 25 * 3600)
        assert parse_timerange_text("1/00:30:00~1/01:00:00", obs) == (
            DAY0 + 24.5 * 3600, DAY0 + 25 * 3600)

    @pytest.mark.parametrize("text", [
        "yesterday", "07:40~", "25:00~26:00", "07:40,08:00",
        "08:00~07:40:00/2012", "2013/01/01/00:00~2013/01/02/00:00",
        "2012/11/19/09:00~2012/11/19/08:00"])
    def test_refused(self, text):
        with pytest.raises(SelectionTextError):
            parse_timerange_text(text, OBS)

    def test_format_round_trip_keeps_both_ends(self):
        tr = (OBS[0], OBS[1])
        back = parse_timerange_text(format_timerange(tr), OBS)
        assert back[0] <= tr[0] and back[1] >= tr[1]
        assert back[1] - tr[1] < 1e-3 and tr[0] - back[0] < 1e-3


class TestUVRange:

    @pytest.mark.parametrize("text,want", [
        ("", None),
        ("100~300m", [[100.0, 300.0, "m"]]),
        ("100~300", [[100.0, 300.0, "m"]]),
        ("0.1~1.5km", [[100.0, 1500.0, "m"]]),
        ("0~50klambda", [[0.0, 50000.0, "lambda"]]),
        ("0~50kl", [[0.0, 50000.0, "lambda"]]),
        ("1~2Mlambda", [[1e6, 2e6, "lambda"]]),
        (">200m", [[200.0, None, "m"]]),
        ("<1.5km", [[None, 1500.0, "m"]]),
        ("<50m, >200m", [[None, 50.0, "m"], [200.0, None, "m"]]),
    ])
    def test_forms(self, text, want):
        assert parse_uvrange_text(text) == want

    @pytest.mark.parametrize("text", ["100", "a~b", "1~2parsec", "0~1km~2",
                                      "10m~20klambda", "300~100m"])
    def test_refused(self, text):
        with pytest.raises(SelectionTextError):
            parse_uvrange_text(text)

    def test_describe(self):
        assert uv_range_text(parse_uvrange_text("<50m,>2klambda")) == "<50m,>2000λ"


# --------------------------------------------------------------------------
# Backends: rows outside the UV range look absent (MSv2 and MSv4 alike)
# --------------------------------------------------------------------------

def _stores():
    out = []
    ms, ps = os.environ.get("MS"), os.environ.get("PS")
    if ms and os.path.isdir(ms):
        out.append(("msv2", ms))
    if ps and os.path.isdir(ps):
        out.append(("msv4", ps))
    return out


def _open(kind, path):
    if kind == "msv2":
        from cubevis.toolbox.visplot.data.msv2_backend import MSv2Backend
        return MSv2Backend(path)
    from cubevis.toolbox.visplot.data.msv4_backend import MSv4Backend
    return MSv4Backend(path)


@pytest.mark.skipif(not _stores(), reason="set MS / PS to the TW Hya data")
class TestBackends:

    @pytest.mark.parametrize("kind,path", _stores())
    def test_rows_outside_look_absent(self, kind, path):
        with _open(kind, path) as b:
            raw = next(iter(b._iter_visibility_partitions(None)))
            base = b._apply_selection(raw, SelectionSpec())
            ds = b._apply_selection(raw, SelectionSpec(uv_range=[[None, 100.0, "m"]]))
            u0 = base.UVW.values
            d0 = np.hypot(u0[..., 0], u0[..., 1])
            out = np.isfinite(d0) & (d0 > 100.0)
            assert out.any() and (np.isfinite(d0) & (d0 <= 100.0)).any()
            assert np.isnan(ds.UVW.values[out]).all()
            assert np.isnan(ds.VISIBILITY.values[out]).all()
            assert ds.FLAG.values[out].all()
            assert np.isnan(ds.EFFECTIVE_INTEGRATION_TIME.values[out]).all()
            keep = np.isfinite(d0) & (d0 <= 100.0)
            np.testing.assert_array_equal(ds.VISIBILITY.values[keep],
                                          base.VISIBILITY.values[keep])
            np.testing.assert_array_equal(ds.FLAG.values[keep], base.FLAG.values[keep])
            # Attributes survive (the window's name is on the frequency
            # coordinate's attrs; losing it broke flag application).
            assert ds.coords["frequency"].attrs == base.coords["frequency"].attrs
            assert ds.attrs == base.attrs

    @pytest.mark.parametrize("kind,path", _stores())
    def test_wavelengths_at_window_centre(self, kind, path):
        with _open(kind, path) as b:
            raw = next(iter(b._iter_visibility_partitions(None)))
            f = raw.coords["frequency"].values
            fc = 0.5 * (f.min() + f.max())
            lam = 100.0 * fc / 299_792_458.0          # 100 m in wavelengths
            a = b._apply_selection(raw, SelectionSpec(uv_range=[[None, 100.0, "m"]]))
            c = b._apply_selection(raw, SelectionSpec(uv_range=[[None, lam, "lambda"]]))
            np.testing.assert_array_equal(np.isfinite(a.UVW.values[..., 0]),
                                          np.isfinite(c.UVW.values[..., 0]))

    def test_msv2_msv4_agree(self):
        stores = _stores()
        if len(stores) < 2:
            pytest.skip("needs both MS and PS")
        sel = SelectionSpec(uv_range=[[50.0, 150.0, "m"]], field_names=["3c279"])
        n = []
        for kind, path in stores:
            with _open(kind, path) as b:
                n.append(sorted(b.baselines_with_rows(sel) or []))
        assert n[0] == n[1] and len(n[0]) > 0

    @pytest.mark.parametrize("kind,path", _stores())
    def test_baseline_stepping_follows_uv_range(self, kind, path):
        with _open(kind, path) as b:
            all_bl = b.baselines_with_rows(SelectionSpec()) or []
            short = b.baselines_with_rows(SelectionSpec(uv_range=[[None, 100.0, "m"]])) or []
            assert 0 < len(short) < len(all_bl)


@pytest.mark.skipif(not (os.environ.get("MS") and os.path.isdir(os.environ["MS"])),
                    reason="set MS to the TW Hya data")
def test_raster_flag_box_stays_inside_uv_range():
    """A raster box under a UV range flags only rows inside the range
    (stored as samples: a coordinate region would reach the others)."""
    from cubevis.toolbox.visplot.data.msv2_backend import MSv2Backend
    from cubevis.toolbox.visplot.flag_model import FlagDelta
    sel = SelectionSpec(field_names=["3c279"], uv_range=[[None, 100.0, "m"]],
                        correlation=["XX"])
    with MSv2Backend(os.environ["MS"]) as b:
        parts = [p for p in b._iter_visibility_partitions(None)
                 if "3c279" in set(np.asarray(p.coords["field_name"].values).astype(str))]
        t = np.concatenate([b._apply_selection(p, sel).time.values for p in parts])
        r = b.evaluate_flag_request({
            "flag": True, "selection": sel, "kind": "raster",
            "x_axis": "TIME", "x0": float(t.min()) - 1, "x1": float(t.max()) + 1,
            "y_axis": "BASELINE", "y0": -1.0, "y1": 1e6, "polarization": "XX"})
        d = FlagDelta.from_dict(r["delta"])
        assert d.is_sample_set
        assert any("UV range" in w for w in r["warnings"])
        b.set_pending_flags([d], 1)
        try:
            n_new = 0
            for p in parts:
                eff = b._flag_mask(p).transpose("time", "baseline_id", "frequency",
                                                "polarization").values.astype(bool)
                f0 = p.FLAG.transpose("time", "baseline_id", "frequency",
                                      "polarization").values.astype(bool)
                new = eff & ~f0
                u = p.UVW.values
                dist = np.hypot(u[..., 0], u[..., 1])
                rows = new.any(axis=(2, 3))
                assert (dist[rows] <= 100.0).all()
                n_new += int(new.sum())
            assert n_new > 0 and n_new == r["counts"]["n_changed"]
        finally:
            b.set_pending_flags([], 0)
