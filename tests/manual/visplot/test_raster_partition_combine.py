"""
test_raster_partition_combine.py
================================
Raster cells that several partitions cover (2026-10-11, before HRS H7).

Until now the first partition's value won wherever two partitions
covered the same raster cell: Baseline x Time on a multi-SPW MS showed
the first spectral window only.  Cells are now combined across
partitions, weighted by the samples each reduced there
(``_raster_merge.merge_raster_partitions``, ``combine_rule``).

1. The merge itself, on hand-made partitions
2. Both backends, Baseline x Time on a simulated MS with two spectral
   windows (8 and 16 channels, the first 4 of each flagged), against
   numpy: Real (exact), vector Amplitude and Phase (each SPW averaged,
   then combined by weight), Flag fraction, Maximum
"""
from __future__ import annotations

import numpy as np
import pytest
import xarray as xr

from cubevis.toolbox.visplot.axes import Axis
from cubevis.toolbox.visplot.data._raster_merge import combine_rule, merge_raster_partitions
from cubevis.toolbox.visplot.selection import SelectionSpec

NTIME, NANT, NFLAG = 4, 3, 4
NCHANS = (8, 16)


def _da(vals, ys=(0, 1), xs=(0, 1)):
    return xr.DataArray(np.asarray(vals, float), dims=("y", "x"),
                        coords={"y": list(ys), "x": list(xs)})


class TestMerge:

    def test_rules(self):
        a, b = _da([[1, 2], [3, np.nan]]), _da([[3, 4], [5, 6]])
        wa, wb = _da([[1, 1], [3, 0]]), _da([[1, 3], [1, 1]])
        assert merge_raster_partitions([a, b], "y", "x").values.tolist() == [[1, 2], [3, 6]]
        got = merge_raster_partitions([a, b], "y", "x", "wmean", [wa, lambda: wb])
        assert got.values.tolist() == [[2, 3.5], [3.5, 6]]
        assert merge_raster_partitions([a, b], "y", "x", "max").values.tolist() == [[3, 4], [5, 6]]
        assert merge_raster_partitions([a, b], "y", "x", "median").values.tolist() == [[2, 3], [4, 6]]
        p1, p2 = _da([[170.0]], (0,), (0,)), _da([[-170.0]], (0,), (0,))
        assert abs(abs(merge_raster_partitions([p1, p2], "y", "x", "circmean").item()) - 180) < 1e-9

    def test_tiling_partitions_need_no_weights(self):
        calls = []
        a, b = _da([[1, 2]], (0,), (0, 1)), _da([[3, 4]], (1,), (0, 1))
        got = merge_raster_partitions([a, b], "y", "x", "wmean",
                                      [lambda: calls.append(1), lambda: calls.append(1)])
        assert got.values.tolist() == [[1, 2], [3, 4]] and calls == []

    def test_weights_follow_a_decimated_partition(self):
        a = _da([[1.0]], (0,), (1,))                     # kept cell (0, 1) only
        wa = _da([[9, 1]], (0,), (0, 1))                 # weights on the full grid
        b = _da([[3.0]], (0,), (1,))
        got = merge_raster_partitions([a, b], "y", "x", "wmean", [wa, None])
        assert got.item() == pytest.approx((1 * 1 + 3 * 1) / 2)

    def test_combine_rule(self):
        assert combine_rule(Axis.Z_SCORE) == "max"
        assert combine_rule(Axis.PHASE, "max") == "circmean"
        assert combine_rule(Axis.AMPLITUDE, "max") == "max"
        assert combine_rule(Axis.AMPLITUDE, "median") == "median"
        assert combine_rule(Axis.AMPLITUDE, "coherent") == "wmean"
        for q in (Axis.REAL, Axis.IMAGINARY, Axis.FLAG, Axis.PHASE_RMS, Axis.COHERENCE):
            assert combine_rule(q, "max") == "wmean"


# ---------------------------------------------------------------------------
# 2. Backends
# ---------------------------------------------------------------------------

def _transform(desc, data):
    dims, vis = data["DATA"]
    a1 = np.asarray(data["ANTENNA1"][1]).astype(float)
    a2 = np.asarray(data["ANTENNA2"][1]).astype(float)
    nch = vis.shape[1]
    ch = np.arange(nch)[None, :, None]
    spw = 0 if nch == NCHANS[0] else 1
    amp = (10 + a1 + 2 * a2)[:, None, None] + 0.5 * ch + 3 * spw
    ph = (0.3 * a1 - 0.2 * a2)[:, None, None] + 0.1 * ch + 0.7 * spw
    v = np.broadcast_to(amp * np.exp(1j * ph), vis.shape).astype(np.complex64)
    data["DATA"] = (dims, v)
    fd, _ = data["FLAG"]
    f = np.zeros(vis.shape, bool)
    f[:, :NFLAG, :] = True
    data["FLAG"] = (fd, f)
    return data


@pytest.fixture(scope="module")
def sim_ms(tmp_path_factory):
    sim = pytest.importorskip("xarray_ms.testing.simulator")
    path = str(tmp_path_factory.mktemp("rpc") / "a.ms")
    sim.MSStructureSimulator(
        ntime=NTIME, time_chunks=NTIME, dump_rate=8.0, time_start=5.0e9,
        nantenna=NANT, auto_corrs=False,
        data_description=[(n, ["XX", "YY"]) for n in NCHANS],
        simulate_data=True, transform_data=_transform).simulate_ms(path)
    return path


@pytest.fixture(scope="module")
def sim_ps(sim_ms, tmp_path_factory):
    out = str(tmp_path_factory.mktemp("rpcps") / "a.ps.zarr")
    xr.open_datatree(sim_ms, engine="xarray-ms:msv2").to_zarr(out, mode="w", compute=True)
    return out


@pytest.fixture(params=["msv2", "msv4"])
def backend(request, sim_ms, sim_ps):
    if request.param == "msv2":
        from cubevis.toolbox.visplot.data.msv2_backend import MSv2Backend
        b = MSv2Backend(sim_ms)
    else:
        from cubevis.toolbox.visplot.data.msv4_backend import MSv4Backend
        b = MSv4Backend(sim_ps)
    b.open()
    yield b
    b.close()


def _per_spw(a1, a2):
    """[(complex samples of the unflagged channels), ...] per SPW for
    baseline (a1, a2), XX (correlation 0), as _transform makes them."""
    out = []
    for spw, nch in enumerate(NCHANS):
        ch = np.arange(NFLAG, nch)
        amp = (10 + a1 + 2 * a2) + 0.5 * ch + 3 * spw
        ph = (0.3 * a1 - 0.2 * a2) + 0.1 * ch + 0.7 * spw
        out.append(amp * np.exp(1j * ph))
    return out


def _raster(b, q, combine="mean"):
    sel = SelectionSpec(correlation=["XX"], averaging="vector", baseline_combine=combine)
    agg, _, _, _ = b.query_raster(Axis.BASELINE, Axis.TIME, q, sel, polarization="XX")
    return agg


def _pairs():
    return [(i, j) for i in range(NANT) for j in range(i + 1, NANT)]


class TestBaselineByTimeOverTwoSpws:

    def test_shape(self, backend):
        agg = _raster(backend, Axis.REAL)
        assert agg.shape == (len(_pairs()), NTIME)
        assert np.isfinite(agg.values).all()

    def test_real_is_the_mean_over_both_spws(self, backend):
        agg = _raster(backend, Axis.REAL)
        for row, (a1, a2) in enumerate(_pairs()):
            want = np.concatenate(_per_spw(a1, a2)).real.mean()
            assert agg.values[row] == pytest.approx(want, rel=1e-5)

    def test_vector_amplitude_and_phase(self, backend):
        amp = _raster(backend, Axis.AMPLITUDE)
        ph = _raster(backend, Axis.PHASE)
        for row, (a1, a2) in enumerate(_pairs()):
            means = [s.mean() for s in _per_spw(a1, a2)]
            w = np.array([n - NFLAG for n in NCHANS], float)
            want_amp = (w * np.abs(means)).sum() / w.sum()
            want_ph = np.degrees(np.angle((w * np.exp(1j * np.angle(means))).sum()))
            assert amp.values[row] == pytest.approx(want_amp, rel=1e-5)
            assert ph.values[row] == pytest.approx(want_ph, abs=1e-3)
            # the old first-SPW-only answer differs
            assert abs(amp.values[row, 0] - abs(means[0])) > 1e-3

    def test_maximum(self, backend):
        agg = _raster(backend, Axis.AMPLITUDE, combine="max")
        for row, (a1, a2) in enumerate(_pairs()):
            want = max(abs(s.mean()) for s in _per_spw(a1, a2))
            assert agg.values[row] == pytest.approx(want, rel=1e-5)

    def test_flag_fraction(self, backend):
        agg = _raster(backend, Axis.FLAG)
        assert agg.values == pytest.approx(2 * NFLAG / sum(NCHANS))
