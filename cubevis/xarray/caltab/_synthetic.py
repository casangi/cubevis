"""A calibration table from numbers: the synthetic source.

``synthetic_caltab("Fringe", nant=27)`` is a tree of the same shape as one
read from a CASA table -- it goes through the same builder -- with values
that follow a stated rule (``synthetic_value``), so a test or a plot can
say what it should see.  It is for tests, for demonstrations, and for
the cases no table at hand covers: a Tsys with channels, one receptor, a
reference antenna that changes, antennas with times of their own, 66
antennas.

``_write.write_caltab`` makes a CASA table of one, with arcae alone.

No Bokeh, no CASA, no arcae.
"""
from __future__ import annotations

from typing import Iterable, Optional, Sequence, Union

import numpy as np

from .. import _arcae_util as au
from . import _schema as S
from ._build import RawCalTable, SpwCells, build_tree

#: short name -> CASA subType, for the types the schema knows
SUBTYPE_OF = {ct.caltype: subtype for subtype, ct in S.CAL_TYPES.items()}

DEFAULT_START = 1763222400.0        # 2025-11-15 16:00:00 UTC, Unix seconds
BANDWIDTH_HZ = 64e6                 # of each spectral window
FIRST_FREQUENCY_HZ = 1.4e9          # window s starts 128 MHz x s above it


def synthetic_value(caltype: str, name: str, antenna, receptor, tau, x, reference=0):
    """The value the synthetic source gives a parameter (before noise).

    Parameters
    ----------
    caltype
        The short type name (``"G"``, ``"B"``, ``"Fringe"``, ...).
    name
        The parameter, as in ``CALPARAM_<name>`` (``"GAIN"``, ``"DELAY"``, ...);
        for a type the schema does not know, the parameter's number.
    antenna, receptor
        Numbers, from 0, along ``antenna_name`` and ``receptor_label``.
    tau
        Where in the table's time span the solution is: 0 at the first
        time, 1 at the last (0 when there is one time).
    x
        Where in the window the channel is: -1 to 1 (0 for one channel).
    reference
        The reference antenna's number.  Phases, fringe delays and rates
        are relative to it, so the reference antenna's are exactly 0.

    The rules (a = antenna, r = receptor, d = a - reference):

    ========  ===========  ==================================================
    G, T      GAIN         (1 + 0.02 a + 0.01 r) exp(i 0.3 d (tau - 0.5))
    Accor     GAIN         1 + 0.02 a + 0.01 r   (real)
    B         GAIN         (1 + 0.05 cos(pi x (1 + a / 8))) exp(i 0.2 x d)
    K         DELAY        a + 0.1 r             ns
    Tsys      TSYS         40 + 2 a + 5 r + 3 sin(2 pi tau) + 2 x^2    K
    Fringe    PHASE        0.2 d (tau - 0.5)     rad
    Fringe    DELAY        d (1 + 0.1 r)         ns
    Fringe    RATE         1e-13 d
    Fringe    DISP_DELAY   0
    other     number p     100 p + a + 0.1 r
    ========  ===========  ==================================================
    """
    a, r, tau, x = (np.asarray(v, dtype=np.float64) for v in (antenna, receptor, tau, x))
    d = a - np.asarray(reference, dtype=np.float64)
    zero = np.zeros(np.broadcast(a, r, tau, x, d).shape)
    key = (caltype, name)
    if key in (("G", "GAIN"), ("T", "GAIN")):
        return (1 + 0.02 * a + 0.01 * r) * np.exp(1j * 0.3 * d * (tau - 0.5)) + zero
    if key == ("Accor", "GAIN"):
        return (1 + 0.02 * a + 0.01 * r) + 0j + zero
    if key == ("B", "GAIN"):
        return (1 + 0.05 * np.cos(np.pi * x * (1 + a / 8))) * np.exp(1j * 0.2 * x * d) + zero
    if key == ("K", "DELAY"):
        return a + 0.1 * r + zero
    if key == ("Tsys", "TSYS"):
        return 40 + 2 * a + 5 * r + 3 * np.sin(2 * np.pi * tau) + 2 * x ** 2 + zero
    if key == ("Fringe", "PHASE"):
        return 0.2 * d * (tau - 0.5) + zero
    if key == ("Fringe", "DELAY"):
        return d * (1 + 0.1 * r) + zero
    if key == ("Fringe", "RATE"):
        return 1e-13 * d + zero
    if key == ("Fringe", "DISP_DELAY"):
        return zero
    return 100.0 * float(name) + a + 0.1 * r + zero


def synthetic_raw(caltype: str = "G", *, nant: int = 10, nspw: int = 2, ntime: int = 12,
                  nchan: Union[int, Sequence[int], None] = None, nreceptor: Optional[int] = None, npar: int = 4,
                  antenna_names: Optional[Sequence[str]] = None,
                  receptor_labels: Optional[Sequence[str]] = None,
                  field_names: Sequence[str] = ("3C286", "J1331+3030"), nscan: int = 2,
                  reference_antenna: Union[int, str, None] = 0,
                  flag_fraction: float = 0.0, missing_fraction: float = 0.0, noise: float = 0.0,
                  outliers: int = 0, own_times: bool = True, start: float = DEFAULT_START,
                  interval: float = 60.0, seed: int = 0) -> RawCalTable:
    """The rows of a synthetic cal table.  See ``synthetic_caltab``."""
    ct = S.CAL_TYPES.get(SUBTYPE_OF.get(caltype, caltype))
    subtype = SUBTYPE_OF.get(caltype, caltype)
    short = ct.caltype if ct else caltype
    if nchan is None:
        nchan = 32 if short == "B" else 1
    nchans = [int(nchan)] * nspw if np.isscalar(nchan) else [int(n) for n in nchan]
    if len(nchans) != nspw:
        raise ValueError(f"nchan has {len(nchans)} values for {nspw} spectral windows")
    if ct is not None:
        if nreceptor is None:
            nreceptor = 1 if ct.one_for_all else 2
        names = [p.name for p in ct.params]
        npar = nreceptor * len(names)
        complex_ = ct.partype == "Complex"
    else:
        nreceptor, names, complex_ = 1, list(range(npar)), False
    if antenna_names is None:
        antenna_names = [f"A{i:02d}" for i in range(nant)]
    antenna_names = [str(n) for n in antenna_names]
    nant = len(antenna_names)
    rng = np.random.default_rng(seed)

    # the reference antenna of each time: one for the table, none, or a change half way
    if reference_antenna is None or short in ("K", "Tsys", "Accor") or ct is None:
        ref_of_time = np.full(ntime, -1)                              # these have none (ANTENNA2 -1)
    elif reference_antenna == "changing":
        ref_of_time = np.where(np.arange(ntime) < (ntime + 1) // 2, 0, min(1, nant - 1))
    else:
        ref = (antenna_names.index(reference_antenna) if isinstance(reference_antenna, str)
               else int(reference_antenna))
        ref_of_time = np.full(ntime, ref)

    tau = np.linspace(0.0, 1.0, ntime) if ntime > 1 else np.zeros(1)
    scan_of_time = 1 + (np.arange(ntime) * nscan) // max(ntime, 1)
    field_of_time = (scan_of_time - 1) % len(field_names)
    A, R = np.arange(nant), np.arange(nreceptor)
    dtype = np.complex64 if complex_ else np.float32

    main = {c: [] for c in ("TIME", "FIELD_ID", "SPECTRAL_WINDOW_ID", "ANTENNA1", "ANTENNA2",
                            "INTERVAL", "SCAN_NUMBER", "OBSERVATION_ID")}
    cells: dict[int, SpwCells] = {}
    nrow = 0
    for s in range(nspw):
        nchan = nchans[s]
        x = np.linspace(-1.0, 1.0, nchan) if nchan > 1 else np.zeros(1)
        times = start + (np.arange(ntime) + 0.5) * interval + (float(s) if own_times else 0.0)
        keep = rng.random((ntime, nant)) >= missing_fraction        # antennas with times of their own
        ti, ai = np.nonzero(keep)
        n = len(ti)
        # (time, antenna, chan, receptor, parameter), then the kept rows, receptor-major
        full = np.empty((ntime, nant, nchan, nreceptor, len(names)), dtype=dtype)
        for i, name in enumerate(names):
            v = synthetic_value(short, name, A[None, :, None, None], R[None, None, None, :],
                                tau[:, None, None, None], x[None, None, :, None],
                                reference=np.maximum(ref_of_time, 0)[:, None, None, None])
            if not complex_:
                v = v.real
            full[..., i] = np.broadcast_to(v, full.shape[:-1])
        if noise:
            sigma = noise * (rng.standard_normal(full.shape)
                             + (1j * rng.standard_normal(full.shape) if complex_ else 0))
            if short == "Fringe":                                    # the reference antenna stays 0
                sigma[np.arange(ntime), np.maximum(ref_of_time, 0)] *= (ref_of_time < 0)[:, None, None, None]
            sigma[..., [i for i, nm in enumerate(names) if nm == "DISP_DELAY"]] = 0
            full = (full + sigma).astype(dtype)
        par = full[ti, ai].reshape(n, nchan, npar)
        for _ in range(outliers):                                    # unflagged, as real Tsys has them
            par[rng.integers(n), rng.integers(nchan), rng.integers(nreceptor) * len(names)] *= 300
        snr = np.broadcast_to((20.0 + ai)[:, None, None, None],
                              (n, nchan, nreceptor, len(names))).astype(np.float32).copy()
        snr[..., [i for i, nm in enumerate(names) if nm == "DISP_DELAY"]] = 0   # not solved, as CASA
        flag = np.repeat((rng.random((n, nchan, nreceptor)) < flag_fraction)[..., None],
                         len(names), axis=-1)
        freq = FIRST_FREQUENCY_HZ + 128e6 * s + (np.arange(nchan) + 0.5) * BANDWIDTH_HZ / nchan
        cells[s] = SpwCells(rows=nrow + np.arange(n), par=par,
                            err=(1.0 / np.maximum(snr, 1.0)).reshape(n, nchan, npar),
                            snr=snr.reshape(n, nchan, npar), flag=flag.reshape(n, nchan, npar),
                            chan_freq=freq)
        nrow += n
        main["TIME"].append(times[ti] + au.MJD_UNIX_OFFSET_S)
        main["FIELD_ID"].append(field_of_time[ti])
        main["SPECTRAL_WINDOW_ID"].append(np.full(n, s))
        main["ANTENNA1"].append(ai)
        main["ANTENNA2"].append(ref_of_time[ti])
        main["INTERVAL"].append(np.full(n, float(interval)))
        main["SCAN_NUMBER"].append(scan_of_time[ti])
        main["OBSERVATION_ID"].append(np.zeros(n, dtype=int))
    if nrow == 0:
        raise ValueError("the synthetic table has no rows (missing_fraction is 1?)")
    columns = {c: np.concatenate(v).astype(np.float64 if c in ("TIME", "INTERVAL") else np.int32)
               for c, v in main.items()}
    labels = S.receptor_labels(None, nreceptor, list(receptor_labels) if receptor_labels else None,
                               one_for_all=bool(ct and ct.one_for_all)) if ct else []
    keywords = {"ParType": "Complex" if complex_ else "Float", "MSName": "synthetic",
                "VisCal": subtype, "PolBasis": S.pol_basis_of(labels)}
    return RawCalTable(source=f"synthetic:{short}", subtype=subtype, keywords=keywords,
                       antenna_names=antenna_names, field_names=[str(f) for f in field_names],
                       spw_names=[f"spw{s}" for s in range(nspw)], main=columns, cells=cells)


def synthetic_caltab(caltype: str = "G", *, spw: Optional[Iterable[int]] = None,
                     receptor_labels: Optional[Sequence[str]] = None, **kwargs):
    """A synthetic calibration table as an ``xarray.DataTree``, in the shape
    ``open_caltab`` gives a CASA table.

    Parameters
    ----------
    caltype
        ``"G"``, ``"T"``, ``"B"``, ``"K"``, ``"Tsys"``, ``"Fringe"`` or
        ``"Accor"`` (or CASA's subType: ``"G Jones"``).  Any other name is
        a type the schema does not know: ``npar`` float parameters per
        solution, in the generic form.
    nant, nspw, ntime
        Antennas (10), spectral windows (2), solution times per window (12).
    nchan
        Channels per window: 32 for ``"B"``, otherwise 1 (give more for a
        Tsys with channels, as ALMA's); or one count per window, for
        windows that differ.
    nreceptor
        Receptors: 2 (1 for ``"T"``).
    npar
        Parameters per solution of an unknown type (4).
    antenna_names, receptor_labels, field_names
        Names; default ``A00``..., ``pol_0``/``pol_1``, two fields.
    nscan
        Scans the times are divided into (2), each on the next field.
    reference_antenna
        A number or name (default the first antenna), None, or
        ``"changing"``: the first antenna for the first half of the times,
        the second after.  K, Tsys and Accor have none, as CASA's.
    flag_fraction, missing_fraction
        The fraction of solutions flagged, and of (time, antenna) rows
        left out -- antennas with times of their own, as a real Tsys.
    noise, outliers
        Gaussian noise of that size on every value, and a number of
        unflagged values 300 times too large.
    own_times
        Each window's times a second after the last's (True), as real
        tables' differ; False for the same times in every window.
    start, interval
        The first time (Unix seconds) and the spacing (60 s).
    seed
        For the flags, the missing rows, the noise and the outliers.
    spw
        Spectral window id(s) to keep, as ``open_caltab``'s.

    Without noise, a value is ``synthetic_value(...)`` exactly (in the
    table's single precision).
    """
    raw = synthetic_raw(caltype, receptor_labels=receptor_labels, **kwargs)
    return build_tree(raw, spw=spw, receptor_labels=list(receptor_labels) if receptor_labels else None)
