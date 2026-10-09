"""Averaged scatter points (HRS H6, 2026-10-09).

What it is for
--------------
A scatter panel draws one point per sample.  For a commissioning
spectrum (amplitude and phase against frequency) or time series that is
noise: the AIPS POSSM / plotms view averages each baseline's samples
over time (a spectrum per scan) or over channels (a time series) first.
This module turns the scatter's per-sample frames into one point per

    baseline x correlation x spectral window x time window x channel window

* **Time windows** (``SelectionSpec.avg_time``): ``"off"`` -- every
  integration on its own; ``"scan"`` -- one window per scan; a number of
  seconds -- consecutive windows of that length from the start of each
  scan.  A window never spans two scans (without scan information, never
  a gap in time).
* **Channel windows** (``avg_chan``): ``"off"``; a number of channels --
  blocks of that many, counted from channel 0 of the window; ``"all"`` --
  the whole spectral window.
* **Vector or scalar** (``averaging``, as for raster cells): vector
  averages the complex visibilities and takes amplitude and phase from
  the mean, so amplitude drops where the samples do not add coherently;
  scalar averages the amplitudes.  Phase is the vector-mean direction in
  vector mode and the mean direction of the unit phasors in scalar mode.
  Real and Imaginary are the means either way.

Only Amplitude, Phase, Real and Imaginary are averaged; other Y
quantities (Phase RMS, Coherence, Z-Score, ...) already describe windows
or are not averages, and are drawn as before.

The X value of a point is the mean of its samples' X values (mean time,
mean frequency, mean UV distance...).  Other columns (scan, field,
baseline, spectral window, used by colouring and by the hover readout)
are those of the window's first sample; a window never mixes scans, so
they are the same for all of its samples except time and frequency,
which are the means.

Flags: averaging happens after the flag view is applied, so a point is
the average of the samples drawn in that view (the unflagged ones,
normally).  ``group_rows`` returns which rows went into each point, so a
flag box on an averaged plot flags the samples behind the points it
encloses.

Everything works on the cached raw frames (one per Y quantity, row-
aligned: the same samples in the same order for every quantity of one
correlation), so changing the averaging re-reads nothing.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd

from ..axes import Axis

#: Y quantities that are averaged.
AVERAGED_QUANTITIES = (Axis.AMPLITUDE, Axis.PHASE, Axis.REAL, Axis.IMAGINARY)

#: Columns that identify a sample; must agree between the frames of one
#: correlation for them to be combined row by row.
_IDENTITY = ("time", "baseline_id", "__spw", "__chan")


def normalize_avg_time(value):
    """``"off"``, ``"scan"`` or a positive float (seconds)."""
    if value is None or value == "":
        return "off"
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("off", "scan"):
            return v
        try:
            value = float(v)
        except ValueError:
            raise ValueError("time averaging must be 'off', 'scan' or a number of "
                             f"seconds; got {value!r}") from None
    value = float(value)
    if not np.isfinite(value) or value <= 0:
        raise ValueError(f"time averaging must be positive; got {value!r}")
    return value


def normalize_avg_chan(value):
    """``"off"``, ``"all"`` or an int >= 2 (0 and 1 mean ``"off"``)."""
    if value is None or value == "":
        return "off"
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("off", "all"):
            return v
        try:
            value = int(float(v))
        except ValueError:
            raise ValueError("channel averaging must be 'off', 'all' or a number of "
                             f"channels; got {value!r}") from None
    value = int(value)
    if value < 0:
        raise ValueError(f"channel averaging must not be negative; got {value!r}")
    return "off" if value < 2 else value


@dataclass(frozen=True)
class ScatterAverage:
    time: object = "off"
    chan: object = "off"
    mode: str = "vector"

    @property
    def active(self) -> bool:
        return self.time != "off" or self.chan != "off"

    def describe(self) -> str:
        """Words for a title: ``"vector avg: scan, 8 ch"``."""
        parts = []
        if self.time != "off":
            parts.append("scan" if self.time == "scan" else f"{self.time:g} s")
        if self.chan != "off":
            parts.append("all channels" if self.chan == "all" else f"{self.chan} ch")
        return f"{self.mode} avg: " + ", ".join(parts) if parts else ""


def scatter_average_of(selection) -> Optional[ScatterAverage]:
    """The averaging *selection* asks for, or ``None`` when there is none."""
    try:
        t = normalize_avg_time(getattr(selection, "avg_time", "off"))
        c = normalize_avg_chan(getattr(selection, "avg_chan", "off"))
    except ValueError:
        return None
    mode = str(getattr(selection, "averaging", "vector") or "vector")
    spec = ScatterAverage(t, c, "scalar" if mode == "scalar" else "vector")
    return spec if spec.active else None


def averages(y_axis) -> bool:
    return y_axis in AVERAGED_QUANTITIES


def _codes(values) -> np.ndarray:
    return pd.factorize(np.asarray(values), sort=False)[0].astype(np.int64)


def _time_segments(df) -> np.ndarray:
    """A scan (or, without scan information, gap-free run) id per row."""
    if "scan_name" in df.columns:
        seg = _codes(df["scan_name"].astype(str).to_numpy())
        if "field_name" in df.columns:
            seg = seg * (int(_codes(df["field_name"].astype(str).to_numpy()).max()) + 1) \
                + _codes(df["field_name"].astype(str).to_numpy())
        return seg
    t = df["time"].to_numpy(np.float64)
    ut = np.unique(t)
    if ut.size < 2:
        return np.zeros(len(t), np.int64)
    d = np.diff(ut)
    step = np.median(d)
    seg_of_ut = np.concatenate([[0], np.cumsum(d > 1.5 * step)])
    return seg_of_ut[np.searchsorted(ut, t)].astype(np.int64)


def group_rows(df: pd.DataFrame, spec: ScatterAverage) -> np.ndarray:
    """The averaged point (0..n-1) each row of *df* belongs to."""
    if len(df) == 0:
        return np.zeros(0, np.int64)
    keys = [_codes(df["baseline_id"].to_numpy()) if "baseline_id" in df.columns
            else _codes(df["baseline_name"].astype(str).to_numpy()),
            df["__spw"].to_numpy().astype(np.int64)]
    if "baseline_antenna1_name" in df.columns:
        # baseline_id is per partition: two partitions may number the
        # same antenna pair differently, or different pairs alike.
        keys[0] = _codes(df["baseline_antenna1_name"].astype(str).to_numpy()
                         + "&" + df["baseline_antenna2_name"].astype(str).to_numpy())
    t = df["time"].to_numpy(np.float64)
    if spec.time == "off":
        keys.append(_codes(t))
    else:
        seg = _time_segments(df)
        keys.append(seg)
        if spec.time != "scan":
            t0 = pd.Series(t).groupby(seg).transform("min").to_numpy()
            keys.append(np.floor((t - t0) / float(spec.time) + 1e-9).astype(np.int64))
    ch = df["__chan"].to_numpy().astype(np.int64)
    if spec.chan == "off":
        keys.append(ch)
    elif spec.chan != "all":
        keys.append(ch // int(spec.chan))
    frame = pd.DataFrame({f"k{i}": k for i, k in enumerate(keys)})
    return frame.groupby(list(frame.columns), sort=False).ngroup().to_numpy(np.int64)


def aligned(a: pd.DataFrame, b: pd.DataFrame) -> bool:
    """Do *a* and *b* hold the same samples in the same order?"""
    if len(a) != len(b):
        return False
    for c in _IDENTITY:
        if c in a.columns and c in b.columns:
            if not np.array_equal(a[c].to_numpy(), b[c].to_numpy()):
                return False
    return True


def average_frame(y_axis, base: pd.DataFrame, real: np.ndarray, imag: np.ndarray,
                  codes: np.ndarray, spec: ScatterAverage) -> pd.DataFrame:
    """One row per averaged point.

    *base* is the per-sample frame being averaged (its ``x`` and identity
    columns are used), *real* / *imag* the same samples' complex parts,
    *codes* from ``group_rows``.  The result has *base*'s columns, ``x``
    and ``y`` replaced by the averages, ``time`` / ``frequency`` by the
    means, and ``avg_n`` (samples per point) added.
    """
    if len(base) == 0:
        out = base.iloc[:0].copy()
        out["avg_n"] = np.zeros(0, np.int64)
        return out
    n = int(codes.max()) + 1
    cnt = np.bincount(codes, minlength=n).astype(np.float64)

    def mean(v):
        return np.bincount(codes, weights=np.asarray(v, np.float64), minlength=n) / cnt
    re_m, im_m = mean(real), mean(imag)
    if y_axis == Axis.AMPLITUDE:
        y = np.hypot(re_m, im_m) if spec.mode == "vector" else mean(np.hypot(real, imag))
    elif y_axis == Axis.PHASE:
        if spec.mode == "vector":
            y = np.degrees(np.arctan2(im_m, re_m))
        else:
            a = np.hypot(real, imag)
            with np.errstate(invalid="ignore", divide="ignore"):
                ur = np.where(a > 0, real / a, 0.0)
                ui = np.where(a > 0, imag / a, 0.0)
            y = np.degrees(np.arctan2(mean(ui), mean(ur)))
    elif y_axis == Axis.REAL:
        y = re_m
    elif y_axis == Axis.IMAGINARY:
        y = im_m
    else:
        raise ValueError(f"{y_axis} is not averaged")
    first = np.unique(codes, return_index=True)[1]
    out = base.iloc[first].reset_index(drop=True)
    out["x"] = mean(base["x"].to_numpy(np.float64))
    out["y"] = y
    for c in ("time", "frequency"):
        if c in base.columns:
            out[c] = mean(base[c].to_numpy(np.float64))
    out["avg_n"] = cnt.astype(np.int64)
    return out
