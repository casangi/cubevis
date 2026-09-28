"""flag_model.py
===============
Data model and pure evaluator for pending flags (FlagDB v2).

Everything here is numpy only: no Bokeh, no xarray, no backend.  It is the
reference implementation that the backends, the exporters and the tests all
agree on.

Vocabulary
----------
*Sample*
    One visibility: ``(time, baseline, spectral window, channel,
    correlation)``.  Flags live on samples, never on rendered cells.

*Delta* (``FlagDelta``)
    One accepted flag or unflag operation.  Two representations share one
    class:

    * **region** -- a coordinate description (time range, baselines,
      spectral windows / channels, correlations, scans, fields).  Cheap, exact
      at any zoom, exports as a plain ``flagdata`` selection.
    * **sample set** -- an explicit, frozen list of samples (``samples``).
      Produced whenever a *value* condition decided membership (a scatter box
      on amplitude, a Z-Score cutoff, a user filter).  Value conditions
      cannot be re-evaluated later without changing meaning (a different
      data column, a different reference population, other pending flags),
      so they are materialized once, when the proposal is made, and the
      region fields of such a delta are informational only.

*Effective flag state*
    ``fold_deltas``: start from the on-disk flags and apply the accepted
    deltas **in order**; each delta sets its samples to ``delta.flag``.  A
    later unflag therefore overrides an earlier flag and vice versa.  This
    is not a union of selections and cannot be represented as one.

Coordinate conventions (MSv2 via xarray-ms and MSv4 via xradio agree)
---------------------------------------------------------------------
* **time** -- the store's ``time`` coordinate values, *as stored*.  Both
  xarray-ms (MSv2) and xradio (MSv4) present UNIX-epoch seconds
  (``time.attrs["format"] == "unix"``), **not** MJD seconds, even though
  older visplot docstrings say MJD.  ``FlagDelta.time_format`` records which
  one a delta uses, so an exporter can convert correctly.
* **frequency** -- Hz, channel centre, from each partition's own coordinate.
* **channel** -- index into the spectral window's *full* frequency axis
  (never into a selected sub-range), always paired with an ``SpwKey``.
* **baseline** -- antenna-name pair ``(ant1, ant2)``.  Integer baseline ids
  differ between MSv2 and MSv4 stores and between partitions; names do not.
* **spectral window** -- ``SpwKey``: identity *plus* the frequency span and
  channel count.  The identity alone is not unique: xarray-ms may expose only
  a window *name*, and names repeat (simulated data names every window
  ``"<Unknown>"``).  Numeric ids, where they exist, are renumbered by
  ``split``; frequencies are not.
* **padding** -- xarray-ms pads missing (time, baseline) slots and marks
  them flagged.  Such samples are *invalid*: the evaluator never changes
  them, so an unflag box can never expose padding as data.

Package location
----------------
``cubevis/cubevis/toolbox/visplot/flag_model.py``
"""

from __future__ import annotations

import base64
import dataclasses
import math
import time as _time
import uuid
from dataclasses import dataclass, field as dc_field
from typing import Any, Iterable, Optional, Sequence

import numpy as np

# Absolute tolerance used to match stored times against data times (s).
TIME_TOL = 1e-4
# Relative tolerance used to match stored frequencies against data (Hz/Hz).
FREQ_RTOL = 1e-9


# ======================================================================
# Small value types
# ======================================================================

@dataclass(frozen=True)
class SpwKey:
    """Robust identity of a spectral window.

    ``ident`` is what the store provides (a numeric id, a DATA_DESC_ID or a
    name -- see ``kind``); ``freq_min``/``freq_max``/``n_chan`` describe the
    window's *full* frequency axis and make the key unique even when names
    repeat.
    """
    ident:    Any
    kind:     str
    freq_min: float
    freq_max: float
    n_chan:   int

    def matches(self, other: "SpwKey") -> bool:
        if other is None:
            return False
        if str(self.ident) != str(other.ident) or int(self.n_chan) != int(other.n_chan):
            return False
        tol = FREQ_RTOL * max(abs(self.freq_max), 1.0)
        return (abs(self.freq_min - other.freq_min) <= tol
                and abs(self.freq_max - other.freq_max) <= tol)

    def label(self) -> str:
        return f"{self.ident}"

    def to_dict(self) -> dict:
        ident = self.ident
        if isinstance(ident, np.generic):
            ident = ident.item()
        return {"ident": ident, "kind": self.kind,
                "freq_min": float(self.freq_min), "freq_max": float(self.freq_max),
                "n_chan": int(self.n_chan)}

    @classmethod
    def from_dict(cls, d: dict) -> "SpwKey":
        return cls(d["ident"], d.get("kind", "name"), float(d["freq_min"]),
                   float(d["freq_max"]), int(d["n_chan"]))


@dataclass(frozen=True)
class SpwChannels:
    """A channel window ``[chan_lo, chan_hi]`` (inclusive) in one window."""
    spw:     SpwKey
    chan_lo: int
    chan_hi: int

    def to_dict(self) -> dict:
        return {"spw": self.spw.to_dict(), "chan_lo": int(self.chan_lo),
                "chan_hi": int(self.chan_hi)}

    @classmethod
    def from_dict(cls, d: dict) -> "SpwChannels":
        return cls(SpwKey.from_dict(d["spw"]), int(d["chan_lo"]), int(d["chan_hi"]))


@dataclass(frozen=True)
class ValueRange:
    """A value condition that decided membership (provenance / export)."""
    axis:         str               # Axis name, e.g. "AMPLITUDE"
    lo:           float
    hi:           float
    polarization: Optional[str] = None

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ValueRange":
        return cls(**d)


@dataclass(frozen=True)
class FilterRecord:
    """Which filter narrowed a proposal, with what parameters."""
    name:      str
    params:    tuple = ()          # ((key, value), ...) -- hashable, ordered
    code_hash: str = ""
    builtin:   bool = True

    @property
    def param_dict(self) -> dict:
        return dict(self.params)

    def describe(self) -> str:
        if not self.params:
            return self.name
        return self.name + "(" + ", ".join(f"{k}={v}" for k, v in self.params) + ")"

    def to_dict(self) -> dict:
        def thaw(v):
            return [thaw(x) for x in v] if isinstance(v, (list, tuple)) else v
        return {"name": self.name, "params": [[k, thaw(v)] for k, v in self.params],
                "code_hash": self.code_hash, "builtin": self.builtin}

    @classmethod
    def from_dict(cls, d: dict) -> "FilterRecord":
        def frz(v):
            return tuple(frz(x) for x in v) if isinstance(v, (list, tuple)) else v
        return cls(d["name"], tuple((k, frz(v)) for k, v in d.get("params", ())),
                   d.get("code_hash", ""), bool(d.get("builtin", True)))


# ======================================================================
# Explicit sample sets
# ======================================================================

def _match_sorted(values: np.ndarray, ref: np.ndarray, atol: float = 0.0,
                  rtol: float = 0.0) -> np.ndarray:
    """Index of each *values* entry in sorted *ref* (nearest within
    tolerance), or ``-1``."""
    values = np.asarray(values, dtype=np.float64)
    ref = np.asarray(ref, dtype=np.float64)
    if ref.size == 0 or values.size == 0:
        return np.full(values.shape, -1, dtype=np.int64)
    pos = np.clip(np.searchsorted(ref, values), 0, ref.size - 1)
    left = np.clip(pos - 1, 0, ref.size - 1)
    take_left = np.abs(ref[left] - values) < np.abs(ref[pos] - values)
    idx = np.where(take_left, left, pos)
    tol = atol + rtol * np.abs(values)
    ok = np.abs(ref[idx] - values) <= tol
    return np.where(ok, idx, -1).astype(np.int64)


def _match_labels(values: Sequence, ref: Sequence) -> np.ndarray:
    lookup = {str(v): i for i, v in enumerate(ref)}
    return np.array([lookup.get(str(v), -1) for v in values], dtype=np.int64)


def _match_pairs(ant1: Sequence, ant2: Sequence, ref1: Sequence,
                 ref2: Sequence) -> np.ndarray:
    lookup = {}
    for i, (a, b) in enumerate(zip(ref1, ref2)):
        lookup[(str(a), str(b))] = i
        lookup.setdefault((str(b), str(a)), i)
    return np.array([lookup.get((str(a), str(b)), -1)
                     for a, b in zip(ant1, ant2)], dtype=np.int64)


@dataclass(frozen=True, eq=False)
class SampleBlock:
    """An explicit set of samples within one spectral window.

    The samples are a boolean subset of the grid
    ``times x baselines x freqs x pols`` (the grid is trimmed to the rows,
    baselines, channels and correlations that contain at least one sample).
    ``encoding`` is ``"indices"`` (sorted flat indices, C order over
    ``(time, baseline, freq, pol)``) or ``"bits"`` (``np.packbits`` of the
    whole grid), whichever is smaller.
    """
    spw:      SpwKey
    times:    np.ndarray      # float64, sorted
    ant1:     tuple
    ant2:     tuple
    freqs:    np.ndarray      # float64, sorted
    chans:    np.ndarray      # int64, channel index in the full SPW
    pols:     tuple
    encoding: str
    data:     np.ndarray
    count:    int

    @property
    def shape(self) -> tuple:
        return (len(self.times), len(self.ant1), len(self.freqs), len(self.pols))

    @classmethod
    def from_mask(cls, spw: SpwKey, times, ant1, ant2, freqs, chans, pols,
                  mask: np.ndarray) -> Optional["SampleBlock"]:
        """Encode *mask* (``time, baseline, freq, pol``) or ``None`` if empty."""
        mask = np.asarray(mask, dtype=bool)
        if not mask.any():
            return None
        keep = [np.flatnonzero(mask.any(axis=tuple(a for a in range(4) if a != ax)))
                for ax in range(4)]
        sub = mask[np.ix_(*keep)]
        times = np.asarray(times, dtype=np.float64)[keep[0]]
        freqs = np.asarray(freqs, dtype=np.float64)[keep[2]]
        chans = np.asarray(chans, dtype=np.int64)[keep[2]]
        order_t = np.argsort(times, kind="stable")
        order_f = np.argsort(freqs, kind="stable")
        sub = sub[order_t][:, :, order_f]
        a1 = tuple(str(x) for x in np.asarray(ant1)[keep[1]])
        a2 = tuple(str(x) for x in np.asarray(ant2)[keep[1]])
        pl = tuple(str(x) for x in np.asarray(pols)[keep[3]])
        flat = np.flatnonzero(sub.ravel())
        count = int(flat.size)
        if count * 64 < sub.size:
            enc, data = "indices", flat.astype(np.int64)
        else:
            enc, data = "bits", np.packbits(sub.ravel())
        return cls(spw, times[order_t], a1, a2, freqs[order_f], chans[order_f],
                   pl, enc, data, count)

    def dense(self) -> np.ndarray:
        """The full boolean grid (``shape``)."""
        n = int(np.prod(self.shape))
        if self.encoding == "bits":
            return np.unpackbits(self.data, count=n).astype(bool).reshape(self.shape)
        out = np.zeros(n, dtype=bool)
        out[self.data] = True
        return out.reshape(self.shape)

    def iter_samples(self):
        """Yield ``(t_idx, b_idx, f_idx, p_idx)`` tuples (for export)."""
        if self.encoding == "bits":
            flat = np.flatnonzero(self.dense().ravel())
        else:
            flat = self.data
        return zip(*np.unravel_index(flat, self.shape))

    def _dense_cached(self) -> np.ndarray:
        d = getattr(self, "_dense_cache", None)
        if d is None:
            d = self.dense()
            object.__setattr__(self, "_dense_cache", d)
        return d

    def mask_for(self, bc: "BlockCoords", *, extend_corr: bool = False,
                 extend_chan: bool = False) -> Optional[np.ndarray]:
        """This set's membership on block *bc*, or ``None`` if disjoint.

        ``extend_corr``/``extend_chan`` widen each stored sample to every
        correlation / every channel of its (time, baseline): the stored
        grid is collapsed along that axis and broadcast, so the result is
        independent of how the data happen to be chunked.
        """
        if bc.spw is not None and not self.spw.matches(bc.spw):
            return None
        ti = _match_sorted(bc.times, self.times, atol=TIME_TOL)
        if not (ti >= 0).any():
            return None
        bi = _match_pairs(bc.ant1, bc.ant2, self.ant1, self.ant2)
        if not (bi >= 0).any():
            return None
        nf_b, np_b = len(bc.freqs), len(bc.pols)
        fi = (np.zeros(nf_b, dtype=np.int64) if extend_chan
              else _match_sorted(bc.freqs, self.freqs, rtol=FREQ_RTOL))
        pi = (np.zeros(np_b, dtype=np.int64) if extend_corr
              else _match_labels(bc.pols, self.pols))
        if not (fi >= 0).any() or not (pi >= 0).any():
            return None
        sel = [np.flatnonzero(x >= 0) for x in (ti, bi, fi, pi)]
        idx = [x[s] for x, s in zip((ti, bi, fi, pi), sel)]
        if extend_corr or extend_chan or self.encoding == "bits":
            grid = self._dense_cached()
            if extend_chan:
                grid = grid.any(axis=2, keepdims=True)
            if extend_corr:
                grid = grid.any(axis=3, keepdims=True)
            sub = grid[np.ix_(*idx)]
        else:
            nt, nb, nf, npol = self.shape
            it, ib, if_, ip = np.ix_(*idx)
            flat = ((it * nb + ib) * nf + if_) * npol + ip
            pos = np.clip(np.searchsorted(self.data, flat), 0, max(self.data.size - 1, 0))
            sub = self.data[pos] == flat if self.data.size else np.zeros(flat.shape, bool)
        out = np.zeros(bc.shape, dtype=bool)
        out[np.ix_(*sel)] = sub
        return out

    def to_dict(self, *, json_safe: bool = True) -> dict:
        data = self.data
        if json_safe:
            data = (base64.b64encode(data.tobytes()).decode("ascii")
                    if self.encoding == "bits" else data.tolist())
        return {
            "spw": self.spw.to_dict(),
            "times": self.times.tolist() if json_safe else self.times,
            "ant1": list(self.ant1), "ant2": list(self.ant2),
            "freqs": self.freqs.tolist() if json_safe else self.freqs,
            "chans": self.chans.tolist() if json_safe else self.chans,
            "pols": list(self.pols), "encoding": self.encoding,
            "data": data, "count": int(self.count),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "SampleBlock":
        enc = d["encoding"]
        data = d["data"]
        if enc == "bits":
            data = (np.frombuffer(base64.b64decode(data), dtype=np.uint8)
                    if isinstance(data, str) else np.asarray(data, dtype=np.uint8))
        else:
            data = np.asarray(data, dtype=np.int64)
        return cls(SpwKey.from_dict(d["spw"]),
                   np.asarray(d["times"], dtype=np.float64),
                   tuple(d["ant1"]), tuple(d["ant2"]),
                   np.asarray(d["freqs"], dtype=np.float64),
                   np.asarray(d["chans"], dtype=np.int64),
                   tuple(d["pols"]), enc, data, int(d["count"]))


# ======================================================================
# FlagDelta v2
# ======================================================================

def _tuple_or_none(v):
    if v is None:
        return None
    return tuple(tuple(x) if isinstance(x, list) else x for x in v)


@dataclass(frozen=True, eq=False)
class FlagDelta:
    """One accepted flag (``flag=True``) or unflag (``flag=False``) operation.

    Region fields are all optional; ``None`` means "no constraint".  When
    ``samples`` is set the delta is a *sample-set* delta and exactly those
    samples are affected; region fields then only describe where the
    samples came from.

    Backward compatible with v1 keyword construction (``flag``,
    ``time_range``, ``freq_range``, ``channel_range``, ``baseline_ids``,
    ``antenna_names``, ``scan_names``, ``field_names``, ``correlation``,
    ``extend_*``, ``source``, ``comment``).  v1's ``channel_range`` without
    an SPW was ill-defined; it is only honoured together with ``spw`` (the
    same window range in each listed window).  Prefer ``spw_channels``.

    Extend options (``extend_corr/chan/spw/scan``) apply to *region* deltas
    at evaluation time.  For sample-set deltas they are applied when the
    samples are materialized (the frozen grid holds whole spectra /
    correlations), so they are informational afterwards.
    """
    flag: bool = True

    # --- region ------------------------------------------------------ #
    time_range:     Optional[tuple] = None          # (t0, t1) inclusive
    time_format:    str = "unix"                    # "unix" | "mjd" seconds
    freq_range:     Optional[tuple] = None          # (f0, f1) Hz, any window
    channel_range:  Optional[tuple] = None          # v1 compat, needs spw
    spw:            Optional[tuple] = None          # (SpwKey, ...)
    spw_channels:   Optional[tuple] = None          # (SpwChannels, ...)
    baseline_ids:   Optional[tuple] = None          # ((a1, a2), ...)
    antenna_names:  Optional[tuple] = None
    scan_names:     Optional[tuple] = None
    field_names:    Optional[tuple] = None
    correlation:    Optional[tuple] = None

    extend_corr:    bool = False
    extend_chan:    bool = False
    extend_spw:     bool = False
    extend_scan:    bool = False

    # --- sample set -------------------------------------------------- #
    samples:        Optional[tuple] = None          # (SampleBlock, ...)
    value_ranges:   tuple = ()                      # (ValueRange, ...)
    filter:         Optional[FilterRecord] = None

    # --- identity / provenance ---------------------------------------- #
    delta_id:   str = dc_field(default_factory=lambda: uuid.uuid4().hex)
    seq:        int = 0
    created:    float = dc_field(default_factory=_time.time)
    source:     str = ""
    comment:    str = ""
    provenance: tuple = ()
    data_column: str = ""
    n_samples:  Optional[int] = None    # samples touched when proposed

    def __post_init__(self):
        # Normalise list-y v1 inputs to tuples so the delta stays immutable.
        for name in ("baseline_ids", "antenna_names", "scan_names",
                     "field_names", "correlation", "spw", "spw_channels",
                     "samples"):
            v = getattr(self, name)
            if v is not None and not isinstance(v, tuple):
                object.__setattr__(self, name, _tuple_or_none(v))
        if self.baseline_ids is not None:
            object.__setattr__(self, "baseline_ids", tuple(
                (str(a), str(b)) for a, b in self.baseline_ids))
        for name in ("time_range", "freq_range", "channel_range"):
            v = getattr(self, name)
            if v is not None:
                lo, hi = v
                object.__setattr__(self, name, (min(lo, hi), max(lo, hi)))
        for name in ("value_ranges", "provenance"):
            v = getattr(self, name)
            if not isinstance(v, tuple):
                object.__setattr__(self, name, tuple(v))

    # ------------------------------------------------------------------ #
    @property
    def is_sample_set(self) -> bool:
        return self.samples is not None

    @property
    def verb(self) -> str:
        return "flag" if self.flag else "unflag"

    def with_seq(self, seq: int) -> "FlagDelta":
        return dataclasses.replace(self, seq=int(seq))

    def describe(self) -> str:
        """One line for status bars and dialogs."""
        parts = [self.verb]
        if self.is_sample_set:
            n = sum(b.count for b in self.samples)
            parts.append(f"{n} samples")
        if self.filter is not None:
            parts.append(f"filter {self.filter.describe()}")
        if self.provenance:
            parts.append(" -> ".join(self.provenance))
        return ", ".join(parts)

    def spw_may_match(self, key: Optional[SpwKey]) -> bool:
        """Cheap test: can this delta touch window *key* at all?"""
        if key is None:
            return True
        if self.is_sample_set:
            return any(b.spw.matches(key) for b in self.samples)
        if self.extend_spw:
            return True
        if self.spw is not None and not any(k.matches(key) for k in self.spw):
            return False
        if self.spw_channels is not None and not any(
                sc.spw.matches(key) for sc in self.spw_channels):
            return False
        return True

    # ------------------------------------------------------------------ #
    # Serialization (JSONL persistence, remote wire)                       #
    # ------------------------------------------------------------------ #
    def to_dict(self, *, json_safe: bool = True) -> dict:
        d = {
            "flag": self.flag,
            "time_range": list(self.time_range) if self.time_range else None,
            "time_format": self.time_format,
            "freq_range": list(self.freq_range) if self.freq_range else None,
            "channel_range": list(self.channel_range) if self.channel_range else None,
            "spw": [k.to_dict() for k in self.spw] if self.spw is not None else None,
            "spw_channels": ([s.to_dict() for s in self.spw_channels]
                             if self.spw_channels is not None else None),
            "baseline_ids": ([list(p) for p in self.baseline_ids]
                             if self.baseline_ids is not None else None),
            "antenna_names": list(self.antenna_names) if self.antenna_names is not None else None,
            "scan_names": list(self.scan_names) if self.scan_names is not None else None,
            "field_names": list(self.field_names) if self.field_names is not None else None,
            "correlation": list(self.correlation) if self.correlation is not None else None,
            "extend_corr": self.extend_corr, "extend_chan": self.extend_chan,
            "extend_spw": self.extend_spw, "extend_scan": self.extend_scan,
            "samples": ([b.to_dict(json_safe=json_safe) for b in self.samples]
                        if self.samples is not None else None),
            "value_ranges": [v.to_dict() for v in self.value_ranges],
            "filter": self.filter.to_dict() if self.filter else None,
            "delta_id": self.delta_id, "seq": self.seq, "created": self.created,
            "source": self.source, "comment": self.comment,
            "provenance": list(self.provenance), "data_column": self.data_column,
            "n_samples": self.n_samples,
        }
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "FlagDelta":
        def tup(v):
            return tuple(v) if v is not None else None
        return cls(
            flag=bool(d.get("flag", True)),
            time_range=tup(d.get("time_range")),
            time_format=d.get("time_format", "unix"),
            freq_range=tup(d.get("freq_range")),
            channel_range=tup(d.get("channel_range")),
            spw=(tuple(SpwKey.from_dict(k) for k in d["spw"])
                 if d.get("spw") is not None else None),
            spw_channels=(tuple(SpwChannels.from_dict(s) for s in d["spw_channels"])
                          if d.get("spw_channels") is not None else None),
            baseline_ids=(tuple(tuple(p) for p in d["baseline_ids"])
                          if d.get("baseline_ids") is not None else None),
            antenna_names=tup(d.get("antenna_names")),
            scan_names=tup(d.get("scan_names")),
            field_names=tup(d.get("field_names")),
            correlation=tup(d.get("correlation")),
            extend_corr=bool(d.get("extend_corr", False)),
            extend_chan=bool(d.get("extend_chan", False)),
            extend_spw=bool(d.get("extend_spw", False)),
            extend_scan=bool(d.get("extend_scan", False)),
            samples=(tuple(SampleBlock.from_dict(b) for b in d["samples"])
                     if d.get("samples") is not None else None),
            value_ranges=tuple(ValueRange.from_dict(v) for v in d.get("value_ranges", ())),
            filter=FilterRecord.from_dict(d["filter"]) if d.get("filter") else None,
            delta_id=d.get("delta_id") or uuid.uuid4().hex,
            seq=int(d.get("seq", 0)), created=float(d.get("created", _time.time())),
            source=d.get("source", ""), comment=d.get("comment", ""),
            provenance=tuple(d.get("provenance", ())),
            data_column=d.get("data_column", ""),
            n_samples=d.get("n_samples"),
        )


# ======================================================================
# Evaluator
# ======================================================================

@dataclass
class BlockCoords:
    """Coordinates of one block of samples, canonical order
    ``(time, baseline, frequency, polarization)``.

    ``chans`` are channel indices in the *full* window (``-1`` if unknown);
    ``scans``/``fields`` are per-time labels (or ``None``).
    """
    times:  np.ndarray
    ant1:   np.ndarray
    ant2:   np.ndarray
    freqs:  np.ndarray
    pols:   np.ndarray
    spw:    Optional[SpwKey] = None
    chans:  Optional[np.ndarray] = None
    scans:  Optional[np.ndarray] = None
    fields: Optional[np.ndarray] = None

    @property
    def shape(self) -> tuple:
        return (len(self.times), len(self.ant1), len(self.freqs), len(self.pols))

    def sub(self, st, sb, sf, sp) -> "BlockCoords":
        """Coordinates of the sub-block given by four slices/index arrays."""
        return BlockCoords(
            np.asarray(self.times)[st], np.asarray(self.ant1)[sb],
            np.asarray(self.ant2)[sb], np.asarray(self.freqs)[sf],
            np.asarray(self.pols)[sp], self.spw,
            None if self.chans is None else np.asarray(self.chans)[sf],
            None if self.scans is None else np.asarray(self.scans)[st],
            None if self.fields is None else np.asarray(self.fields)[st],
        )


def _region_axis_masks(d: FlagDelta, bc: BlockCoords):
    """Four 1-D masks (time, baseline, freq, pol) or ``None`` if disjoint."""
    nt, nb, nf, npol = bc.shape
    # --- spectral window / frequency ---
    mf = np.ones(nf, dtype=bool)
    if not d.extend_spw:
        if d.spw is not None and bc.spw is not None:
            if not any(k.matches(bc.spw) for k in d.spw):
                return None
    if not d.extend_chan and not d.extend_spw:
        freqs = np.asarray(bc.freqs, dtype=np.float64)
        if d.freq_range is not None:
            f0, f1 = d.freq_range
            tol = FREQ_RTOL * np.maximum(np.abs(freqs), 1.0)
            mf &= (freqs >= f0 - tol) & (freqs <= f1 + tol)
        if d.spw_channels is not None:
            m = np.zeros(nf, dtype=bool)
            chans = bc.chans
            for sc in d.spw_channels:
                if bc.spw is None or not sc.spw.matches(bc.spw):
                    continue
                if chans is None:
                    raise ValueError("spw_channels delta needs channel indices")
                m |= (chans >= sc.chan_lo) & (chans <= sc.chan_hi)
            mf &= m
        elif d.channel_range is not None and d.spw is not None:
            if bc.chans is None:
                raise ValueError("channel_range delta needs channel indices")
            c0, c1 = d.channel_range
            mf &= (bc.chans >= c0) & (bc.chans <= c1)
    elif not d.extend_spw and d.spw_channels is not None and bc.spw is not None:
        if not any(sc.spw.matches(bc.spw) for sc in d.spw_channels):
            return None
    if not mf.any():
        return None

    # --- time / scan / field ---
    mt = np.ones(nt, dtype=bool)
    use_time = d.time_range is not None and not (d.extend_scan and d.scan_names)
    if use_time:
        t0, t1 = d.time_range
        times = np.asarray(bc.times, dtype=np.float64)
        mt &= (times >= t0 - TIME_TOL) & (times <= t1 + TIME_TOL)
    if d.scan_names is not None and bc.scans is not None:
        mt &= np.isin(np.asarray(bc.scans).astype(str), [str(s) for s in d.scan_names])
    if d.field_names is not None and bc.fields is not None:
        mt &= np.isin(np.asarray(bc.fields).astype(str), [str(s) for s in d.field_names])
    if not mt.any():
        return None

    # --- baseline / antenna ---
    mb = np.ones(nb, dtype=bool)
    a1 = np.asarray(bc.ant1).astype(str)
    a2 = np.asarray(bc.ant2).astype(str)
    if d.baseline_ids is not None:
        pairs = set()
        for x, y in d.baseline_ids:
            pairs.add((x, y)); pairs.add((y, x))
        mb &= np.array([(x, y) in pairs for x, y in zip(a1, a2)], dtype=bool)
    if d.antenna_names is not None:
        names = [str(a) for a in d.antenna_names]
        mb &= np.isin(a1, names) | np.isin(a2, names)
    if not mb.any():
        return None

    # --- correlation ---
    mp = np.ones(npol, dtype=bool)
    if d.correlation is not None and not d.extend_corr:
        mp &= np.isin(np.asarray(bc.pols).astype(str), [str(c) for c in d.correlation])
    if not mp.any():
        return None
    return mt, mb, mf, mp


def delta_mask(d: FlagDelta, bc: BlockCoords) -> Optional[np.ndarray]:
    """Boolean mask of the samples of block *bc* that *d* addresses, or
    ``None`` when it addresses none (fast path)."""
    if not d.spw_may_match(bc.spw):
        return None
    if d.is_sample_set:
        out = None
        for blk in d.samples:
            m = blk.mask_for(bc, extend_corr=d.extend_corr, extend_chan=d.extend_chan)
            if m is None:
                continue
            out = m if out is None else (out | m)
        return out
    axes = _region_axis_masks(d, bc)
    if axes is None:
        return None
    mt, mb, mf, mp = axes
    return (mt[:, None, None, None] & mb[None, :, None, None]
            & mf[None, None, :, None] & mp[None, None, None, :])


def fold_deltas(deltas: Iterable[FlagDelta], bc: BlockCoords,
                base: np.ndarray, valid: Optional[np.ndarray] = None) -> np.ndarray:
    """Effective flags of block *bc*: *base* with *deltas* applied in order.

    *base* is the on-disk flag array (``time, baseline, freq, pol``).
    *valid* (optional) marks real samples; invalid (padded) samples keep
    their base value no matter what.  Accepts ``(time, baseline)`` or the
    full 4-D shape.
    """
    out = np.array(base, dtype=bool, copy=True)
    vmask = None
    if valid is not None:
        valid = np.asarray(valid, dtype=bool)
        vmask = valid if valid.ndim == 4 else valid[:, :, None, None]
    for d in deltas:
        m = delta_mask(d, bc)
        if m is None:
            continue
        if vmask is not None:
            m = m & vmask
        out[m] = bool(d.flag)
    return out


# ======================================================================
# Counting / summaries
# ======================================================================

@dataclass
class FlagCounts:
    """Sample counts for a proposal (preview dialog, status bar, audit)."""
    n_selected:  int = 0        # valid samples inside the drawn selection
    n_matched:   int = 0        # ... that the filter kept (and were eligible)
    n_changed:   int = 0        # ... whose effective state would change
    n_total:     int = 0        # valid samples in the current data selection
    by_spw:      dict = dc_field(default_factory=dict)
    by_baseline: dict = dc_field(default_factory=dict)
    by_antenna:  dict = dc_field(default_factory=dict)
    by_pol:      dict = dc_field(default_factory=dict)
    by_scan:     dict = dc_field(default_factory=dict)
    time_span:   Optional[tuple] = None
    n_times:     int = 0

    def merge(self, other: "FlagCounts") -> "FlagCounts":
        out = FlagCounts(self.n_selected + other.n_selected,
                         self.n_matched + other.n_matched,
                         self.n_changed + other.n_changed,
                         self.n_total + other.n_total)
        for name in ("by_spw", "by_baseline", "by_antenna", "by_pol", "by_scan"):
            acc = dict(getattr(self, name))
            for k, v in getattr(other, name).items():
                acc[k] = acc.get(k, 0) + v
            setattr(out, name, acc)
        spans = [s for s in (self.time_span, other.time_span) if s is not None]
        if spans:
            out.time_span = (min(s[0] for s in spans), max(s[1] for s in spans))
        out.n_times = self.n_times + other.n_times
        return out

    @classmethod
    def from_mask(cls, mask: np.ndarray, bc: BlockCoords, *, n_selected: int = 0,
                  n_changed: int = 0, n_total: int = 0) -> "FlagCounts":
        mask = np.asarray(mask, dtype=bool)
        c = cls(n_selected=int(n_selected), n_matched=int(mask.sum()),
                n_changed=int(n_changed), n_total=int(n_total))
        if not c.n_matched:
            return c
        per_bl = mask.sum(axis=(0, 2, 3))
        a1 = np.asarray(bc.ant1).astype(str)
        a2 = np.asarray(bc.ant2).astype(str)
        for i in np.flatnonzero(per_bl):
            n = int(per_bl[i])
            c.by_baseline[f"{a1[i]}&{a2[i]}"] = c.by_baseline.get(f"{a1[i]}&{a2[i]}", 0) + n
            c.by_antenna[a1[i]] = c.by_antenna.get(a1[i], 0) + n
            if a2[i] != a1[i]:
                c.by_antenna[a2[i]] = c.by_antenna.get(a2[i], 0) + n
        per_pol = mask.sum(axis=(0, 1, 2))
        for i in np.flatnonzero(per_pol):
            c.by_pol[str(bc.pols[i])] = int(per_pol[i])
        if bc.spw is not None:
            c.by_spw[str(bc.spw.ident)] = c.n_matched
        per_t = mask.sum(axis=(1, 2, 3))
        tt = np.flatnonzero(per_t)
        c.n_times = int(tt.size)
        times = np.asarray(bc.times, dtype=np.float64)
        c.time_span = (float(times[tt].min()), float(times[tt].max()))
        if bc.scans is not None:
            scans = np.asarray(bc.scans).astype(str)
            for i in tt:
                c.by_scan[scans[i]] = c.by_scan.get(scans[i], 0) + int(per_t[i])
        return c

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "FlagCounts":
        d = dict(d)
        if d.get("time_span") is not None:
            d["time_span"] = tuple(d["time_span"])
        return cls(**d)


def time_to_datetime(t: float, time_format: str = "unix"):
    """Convert a stored time to a UTC ``datetime`` (for export/labels)."""
    import datetime as _dt
    if time_format.lower() == "mjd":
        t = t - 40587.0 * 86400.0          # MJD seconds -> UNIX seconds
    return _dt.datetime(1970, 1, 1, tzinfo=_dt.timezone.utc) + _dt.timedelta(seconds=float(t))
