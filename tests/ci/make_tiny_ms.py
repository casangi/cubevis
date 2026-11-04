"""A tiny, deterministic MSv2 for the quick tests (``tests/ci``): 5
antennas, 2 spectral windows of 8 channels, 2 polarizations, 2 scans of
100 times each -- about 3 MB, made in a few seconds.

xradio's test generator (``xradio.testing``) writes the tables; its rows
each get a time of their own, its UVWs are tiny and many of its data are
zero, and all its channels have one frequency.  So afterwards the rows are regrouped into
integrations (one time per 10 baselines), and given plausible UVWs and
data with structure (a per-baseline amplitude, a spectral ripple, a slow
phase drift, some noise), a few flags and channel frequencies -- enough for a raster and a
scatter to show something, and for local and remote reads to be compared.

    python tests/ci/make_tiny_ms.py OUT.ms
"""
from __future__ import annotations

import copy
import sys
import warnings

import numpy as np

warnings.filterwarnings("ignore")

NTIMES = 200            # per spectral window
NANT = 5
NCHAN = 8
NSPW = 2
INTERVAL = 10.0


def make(path: str) -> str:
    from xradio.testing.measurement_set.msv2_io import gen_test_ms, default_ms_descr
    from casacore.tables import table

    nbl = NANT * (NANT - 1) // 2
    d = copy.deepcopy(default_ms_descr)
    d["nrows_per_ddi"] = NTIMES * nbl
    d["nchans"] = NCHAN
    d["ANTENNA"] = {str(i): i for i in range(NANT)}
    d["SPECTRAL_WINDOW"] = {str(i): i for i in range(NSPW)}
    gen_test_ms(path, descr=d, vlbi_tables=False)

    rng = np.random.default_rng(20261104)
    pos = rng.normal(0.0, 150.0, size=(NANT, 3))           # metres, an array ~300 m across
    with table(path, readonly=False, ack=False) as t:
        n = t.nrows()
        a1 = t.getcol("ANTENNA1")
        a2 = t.getcol("ANTENNA2")
        ddi = t.getcol("DATA_DESC_ID")
        t0 = float(t.getcol("TIME", 0, 1)[0])
        shape = t.getcol("DATA", 0, 1).shape[1:]           # (nchan, npol)
        time = np.empty(n)
        for dd in np.unique(ddi):
            rows = np.flatnonzero(ddi == dd)
            time[rows] = t0 + (np.arange(rows.size) // nbl) * INTERVAL
        k = (time - t0) / INTERVAL                         # integration index
        scan = np.where(k < NTIMES // 2, 1, 2).astype(np.int32)
        # Earth rotation, crudely: the baseline turns slowly in the uv plane.
        ang = 2 * np.pi * k / (4 * NTIMES)
        b = pos[a2] - pos[a1]
        uvw = np.stack([b[:, 0] * np.cos(ang) - b[:, 1] * np.sin(ang),
                        b[:, 0] * np.sin(ang) + b[:, 1] * np.cos(ang),
                        b[:, 2]], axis=1)
        bl = (a1 * NANT + a2).astype(float)
        chan = np.arange(shape[0])[None, :, None]
        amp = (2.0 + bl[:, None, None] / 10.0) * (1.0 + 0.3 * np.sin(chan + ddi[:, None, None]))
        phase = 0.02 * k[:, None, None] * (1 + a1[:, None, None]) + 0.1 * chan
        noise = rng.normal(0.0, 0.2, size=(n,) + shape) + 1j * rng.normal(0.0, 0.2, size=(n,) + shape)
        data = (amp * np.exp(1j * phase) + noise).astype(np.complex64)
        flag = np.zeros((n,) + shape, dtype=bool)
        flag[(k.astype(int) % 37 == 5) & (a1 == 0)] = True     # a few flagged integrations
        t.putcol("TIME", time)
        t.putcol("TIME_CENTROID", time)
        t.putcol("INTERVAL", np.full(n, INTERVAL))
        t.putcol("EXPOSURE", np.full(n, INTERVAL))
        t.putcol("SCAN_NUMBER", scan)
        t.putcol("UVW", uvw)
        t.putcol("DATA", data)
        t.putcol("FLAG", flag)
        t.putcol("FLAG_ROW", flag.all(axis=(1, 2)))
    # The generator gives every channel the same frequency (10 Hz), which
    # collapses a channel axis to one point: 100 GHz and up, 2 MHz channels,
    # the windows side by side (a gap between them would draw as nothing).
    with table(f"{path}/SPECTRAL_WINDOW", readonly=False, ack=False) as sw:
        nspw = sw.nrows()
        width = 2.0e6
        freq = np.array([100.0e9 + width * (s * NCHAN + np.arange(NCHAN)) for s in range(nspw)])
        sw.putcol("CHAN_FREQ", freq)
        sw.putcol("CHAN_WIDTH", np.full((nspw, NCHAN), width))
        sw.putcol("EFFECTIVE_BW", np.full((nspw, NCHAN), width))
        sw.putcol("RESOLUTION", np.full((nspw, NCHAN), width))
        sw.putcol("REF_FREQUENCY", freq[:, 0])
        sw.putcol("TOTAL_BANDWIDTH", np.full(nspw, width * NCHAN))
    return path


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    print(make(sys.argv[1]))
