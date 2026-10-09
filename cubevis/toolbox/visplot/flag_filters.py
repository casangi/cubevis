"""flag_filters.py
=================
Filters decide *which* of the selected samples a flag operation affects.

A filter never flags anything by itself.  The pipeline is::

    selection (box, in data space)  ->  filter  ->  proposal  ->  reviewer
                                                               ->  FlagDB

and the identity filter (``"all"``) is what makes a drawn box flag
everything in it -- AIPS TVFLG style immediate flagging is simply the
default filter.

Contract
--------
A filter is called with a **read-only labelled** ``xarray.Dataset`` holding
the selected samples of one spectral window:

* dims ``(time, baseline_id, frequency, polarization)`` -- MSv4 names; a
  single-dish store is presented the same way with one "baseline" per
  antenna (``ant1 == ant2``);
* data variables ``vis`` (complex), ``real``, ``imag``, ``amp``,
  ``phase`` (degrees), ``flag`` (the *effective* flag state, i.e. on-disk
  flags with the pending deltas applied), ``valid`` (``time, baseline_id``:
  False for padding), ``weight`` where the store has one;
* coordinates ``time`` (store units, see ``attrs["time_format"]``),
  ``frequency`` (Hz), ``channel`` (index in the full window),
  ``polarization``, ``baseline_antenna1_name``/``baseline_antenna2_name``
  and, per time, ``scan_name``/``field_name`` where the store has them;
* attrs ``spw`` (``SpwKey``), ``data_column``, ``time_format``.

It returns a boolean array broadcastable to ``vis``: True = "this sample
matches".  The framework then intersects the result with the selection,
with valid samples, and with the samples whose state the action can
change (unflagged ones for *flag*, flagged ones for *unflag*).  A filter
therefore never needs to look at ``flag`` itself, but may (for example to
exclude already-flagged samples from its own statistics).

The Flagging panel lists a curated set (``FlagFilter.gui``; 2026-10-09,
HRS H5 slice 2): All selected, Value range (on the quantity the panel
shows), Outlier from neighbours (running median along time or channel),
Z-Score, Amplitude outlier (MAD), Phase deviation, Grow around flags --
each with at most two controls.  The reference population is chosen
automatically ("auto") and is no longer a control.

Scope
-----
``scope="local"``
    The mask of a sample depends only on that sample (amplitude range).
``scope="reference"``
    The filter needs statistics of a *reference population* before it can
    judge a sample (Z-Score, MAD).  It supplies ``prepare(refs, **params)``
    which receives a list of reference Datasets (same layout as above) and
    returns small statistics, then ``mask(ds, stats, **params)``.  The
    reference population is chosen by the framework so that it matches
    what the user looked at: the whole current data selection of the
    baselines/correlations involved (as the scatter Z-Score colouring
    does), or per spectral window (as the raster Z-Score does).

Parameters
----------
Each filter declares ``ParamSpec``s (name, kind, default, bounds) so the GUI
can draw controls and a saved configuration can store values.  User
functions are supplied from Python (``VisibilityPlotter(flag_filters=...)``)
and are never typed into the GUI or loaded from files.

Reproducibility: a delta produced by a filter records the filter name, its
parameters and a hash of the function's code (``FilterRecord``).
"""

from __future__ import annotations

import dataclasses
import hashlib
import inspect
import logging
import math
import warnings
from dataclasses import dataclass, field as dc_field
from types import SimpleNamespace
from typing import Any, Callable, Mapping, Optional, Sequence

import numpy as np

from .flag_model import FilterRecord

log = logging.getLogger(__name__)

# Keep in sync with data._scatter_render (imported lazily there to avoid a
# pandas import at module load for pure users of this file).
_ZSCORE_RAYLEIGH_CONST = math.sqrt(2.0 * math.log(2.0))
_DEFAULT_ZSCORE_THRESHOLD = 3.5
_MAD_TO_SIGMA = 1.482602218505602


def zscore_cell_cutoff(n_samples, per_sample_cutoff: float = _DEFAULT_ZSCORE_THRESHOLD) -> float:
    """Sidak-corrected cutoff for the max of *n_samples* Z-Scores.

    Identical to ``data._scatter_render.zscore_cell_cutoff`` (duplicated so
    this module stays importable without pandas/datashader); a test pins
    the two together.
    """
    try:
        n = float(n_samples)
    except (TypeError, ValueError):
        return float(per_sample_cutoff)
    if not math.isfinite(n) or n <= 1.0:
        return float(per_sample_cutoff)
    p0 = math.exp(-0.5 * per_sample_cutoff * per_sample_cutoff)
    p = -math.expm1(math.log1p(-p0) / n)
    return math.sqrt(-2.0 * math.log(p))


# ======================================================================
# Parameter specification
# ======================================================================

@dataclass(frozen=True)
class ParamSpec:
    """One filter parameter.

    ``kind`` is ``"float"``, ``"int"``, ``"bool"`` or ``"choice"``.
    ``gui=False`` hides a parameter that the application sets itself (for
    example which dimensions form a raster cell).
    """
    name:    str
    kind:    str = "float"
    default: Any = None
    label:   str = ""
    min:     Optional[float] = None
    max:     Optional[float] = None
    choices: tuple = ()
    help:    str = ""
    gui:     bool = True

    def coerce(self, value):
        if value is None:
            return self.default
        if self.kind == "float":
            v = float(value)
        elif self.kind == "int":
            v = int(round(float(value)))
        elif self.kind == "bool":
            v = value if isinstance(value, bool) else str(value).lower() in ("1", "true", "yes", "on")
        elif self.kind == "choice":
            v = value if value in self.choices else str(value)
            if self.choices and v not in self.choices:
                raise ValueError(f"{self.name}: {value!r} not one of {self.choices}")
            return v
        elif self.kind == "any":
            return value
        else:
            raise ValueError(f"unknown ParamSpec kind {self.kind!r}")
        if self.kind in ("float", "int"):
            if self.min is not None and v < self.min:
                raise ValueError(f"{self.name}: {v} below minimum {self.min}")
            if self.max is not None and v > self.max:
                raise ValueError(f"{self.name}: {v} above maximum {self.max}")
        return v

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


# ======================================================================
# Filter object
# ======================================================================

def _code_hash(*funcs) -> str:
    h = hashlib.sha256()
    for f in funcs:
        if f is None:
            continue
        try:
            h.update(inspect.getsource(f).encode())
        except (OSError, TypeError):
            code = getattr(f, "__code__", None)
            h.update(code.co_code if code is not None else repr(f).encode())
    return h.hexdigest()[:16]


@dataclass(frozen=True, eq=False)
class FlagFilter:
    """A named sample filter.  See the module docstring for the contract."""
    name:        str
    mask_fn:     Callable
    params:      tuple = ()                 # (ParamSpec, ...)
    label:       str = ""
    description: str = ""
    scope:       str = "local"              # "local" | "reference"
    prepare_fn:  Optional[Callable] = None
    builtin:     bool = False
    code_hash:   str = ""
    # Listed in the Flagging panel.  False keeps a filter usable from
    # Python and in saved records but out of the curated list (HRS H5
    # slice 2: "Amplitude range" gave way to "Value range").
    gui:         bool = True

    def __post_init__(self):
        if self.scope not in ("local", "reference"):
            raise ValueError(f"FlagFilter {self.name!r}: scope must be 'local' or 'reference'")
        if self.scope == "reference" and self.prepare_fn is None:
            raise ValueError(f"FlagFilter {self.name!r}: scope='reference' needs prepare")
        if not self.code_hash:
            object.__setattr__(self, "code_hash", _code_hash(self.mask_fn, self.prepare_fn))
        if not self.label:
            object.__setattr__(self, "label", self.name)

    @property
    def is_identity(self) -> bool:
        return self.name == "all"

    def param_specs(self) -> dict:
        return {p.name: p for p in self.params}

    def resolve_params(self, values: Optional[Mapping] = None) -> dict:
        """Defaults overlaid with *values*, type-checked.  Unknown keys raise."""
        values = dict(values or {})
        specs = self.param_specs()
        unknown = set(values) - set(specs)
        if unknown:
            raise ValueError(f"filter {self.name!r}: unknown parameter(s) {sorted(unknown)}")
        return {n: s.coerce(values.get(n)) for n, s in specs.items()}

    def record(self, params: Mapping) -> FilterRecord:
        def frz(v):
            return tuple(frz(x) for x in v) if isinstance(v, (list, tuple)) else v
        return FilterRecord(self.name, tuple(sorted((k, frz(v)) for k, v in params.items())),
                            self.code_hash, self.builtin)

    def prepare(self, refs: Sequence, params: Mapping):
        if self.prepare_fn is None:
            return None
        return self.prepare_fn(list(refs), **params)

    def mask(self, ds, stats, params: Mapping) -> np.ndarray:
        """Evaluate on *ds*; always returns a boolean ndarray shaped like
        ``ds.vis`` (``time, baseline_id, frequency, polarization``)."""
        if self.scope == "reference":
            out = self.mask_fn(ds, stats, **params)
        else:
            out = self.mask_fn(ds, **params)
        shape = ds["vis"].shape
        if hasattr(out, "transpose") and hasattr(out, "dims"):
            dims = [d for d in ds["vis"].dims if d in out.dims]
            out = out.transpose(*dims)
            out = out.broadcast_like(ds["vis"]).transpose(*ds["vis"].dims).values
        out = np.asarray(out)
        if out.dtype != bool:
            out = out.astype(bool)
        try:
            out = np.broadcast_to(out, shape)
        except ValueError as exc:
            raise ValueError(f"filter {self.name!r} returned shape {out.shape}; "
                             f"expected something broadcastable to {shape}") from exc
        return np.array(out, dtype=bool)

    def describe(self) -> dict:
        return {"name": self.name, "label": self.label,
                "description": self.description, "scope": self.scope,
                "builtin": self.builtin,
                "params": [p.to_dict() for p in self.params]}


def make_flag_filter(func: Callable, *, name: Optional[str] = None,
                     params: Sequence[ParamSpec] = (), label: str = "",
                     description: str = "", prepare: Optional[Callable] = None,
                     elementwise: bool = False) -> FlagFilter:
    """Wrap a user function as a ``FlagFilter``.

    ``func(ds, **params) -> bool mask`` (array form, fast), or with
    ``prepare`` given: ``prepare(refs, **params) -> stats`` and
    ``func(ds, stats, **params) -> mask``.

    ``elementwise=True`` accepts a per-sample predicate
    ``func(sample, **params) -> bool`` where *sample* has attributes
    ``vis, amp, phase, real, imag, time, frequency, channel, ant1, ant2,
    polarization``.  It is called once per sample and is therefore slow;
    a warning is logged when it is used.
    """
    if elementwise:
        if prepare is not None:
            raise ValueError("elementwise filters cannot have a prepare step")
        pred = func

        def func(ds, **p):  # noqa: F811 -- wrapper
            warnings.warn(f"elementwise flag filter {name or pred.__name__!r}: "
                          "per-sample Python calls are slow", RuntimeWarning, stacklevel=2)
            return _elementwise_mask(ds, pred, p)
        func.__wrapped__ = pred
        mask_fn = func
        hash_src = (pred,)
    else:
        mask_fn = func
        hash_src = (func, prepare)
    if not params:
        params = _infer_params(pred if elementwise else func, skip=1 if prepare is None else 2)
    return FlagFilter(
        name=name or getattr(func, "__name__", "user_filter"),
        mask_fn=mask_fn, params=tuple(params), label=label,
        description=description or (inspect.getdoc(func) or "").split("\n")[0],
        scope="reference" if prepare is not None else "local",
        prepare_fn=prepare, builtin=False, code_hash=_code_hash(*hash_src),
    )


def _infer_params(func, skip: int = 1) -> tuple:
    """ParamSpecs from a plain function's keyword defaults.

    ``def loud(ds, level=20.0, per_pol=False)`` gets a float ``level`` and a
    bool ``per_pol`` control in the Flagging panel; parameters without a
    default of type bool/int/float/str are left to the function.
    """
    try:
        sig = inspect.signature(func)
    except (TypeError, ValueError):
        return ()
    out = []
    for i, p in enumerate(sig.parameters.values()):
        if i < skip or p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD):
            continue
        d = p.default
        if d is inspect.Parameter.empty:
            continue
        kind = ("bool" if isinstance(d, bool) else "int" if isinstance(d, int)
                else "float" if isinstance(d, float) else None)
        if kind is None:
            continue
        out.append(ParamSpec(p.name, kind, d, p.name.replace("_", " ")))
    return tuple(out)


def _elementwise_mask(ds, pred, params) -> np.ndarray:
    vis = ds["vis"].values
    out = np.zeros(vis.shape, dtype=bool)
    times = ds["time"].values
    freqs = ds["frequency"].values
    chans = ds["channel"].values if "channel" in ds.coords else np.full(freqs.shape, -1)
    pols = ds["polarization"].values
    a1 = ds["baseline_antenna1_name"].values
    a2 = ds["baseline_antenna2_name"].values
    for it, ib, ifr, ip in np.ndindex(vis.shape):
        v = vis[it, ib, ifr, ip]
        s = SimpleNamespace(vis=v, amp=abs(v), phase=math.degrees(np.angle(v)),
                            real=v.real, imag=v.imag, time=float(times[it]),
                            frequency=float(freqs[ifr]), channel=int(chans[ifr]),
                            ant1=str(a1[ib]), ant2=str(a2[ib]),
                            polarization=str(pols[ip]))
        out[it, ib, ifr, ip] = bool(pred(s, **params))
    return out


# ======================================================================
# Reference statistics helpers
# ======================================================================

def _ref_key(ds, ib, ip, per_spw: bool):
    a1 = str(ds["baseline_antenna1_name"].values[ib])
    a2 = str(ds["baseline_antenna2_name"].values[ib])
    pol = str(ds["polarization"].values[ip])
    spw = ds.attrs.get("spw") if per_spw else None
    return (a1, a2, pol, None if spw is None else (str(spw.ident), spw.n_chan, spw.freq_min))


def _collect_reference(refs, per_spw: bool, value_fn):
    """Group unflagged, valid reference samples by (baseline, pol[, spw]).

    Returns ``{key: 1-D array of value_fn(ds)[t, b, :, p] values}``.
    """
    groups: dict = {}
    for ds in refs:
        vals = np.asarray(value_fn(ds))
        flag = np.asarray(ds["flag"].values, dtype=bool)
        valid = np.asarray(ds["valid"].values, dtype=bool)[:, :, None, None]
        use = np.broadcast_to(~flag & valid, vals.shape)
        nb = vals.shape[1]
        npol = vals.shape[3]
        for ib in range(nb):
            for ip in range(npol):
                v = vals[:, ib, :, ip][use[:, ib, :, ip]]
                if v.size == 0:
                    continue
                key = _ref_key(ds, ib, ip, per_spw)
                groups.setdefault(key, []).append(v.ravel())
    return {k: np.concatenate(v) for k, v in groups.items()}


def _per_key_array(ds, stats, per_spw: bool, index: int):
    """Broadcast one statistic back onto ``(1, baseline, 1, pol)``."""
    nb = ds.sizes["baseline_id"]
    npol = ds.sizes["polarization"]
    out = np.full((1, nb, 1, npol), np.nan)
    for ib in range(nb):
        for ip in range(npol):
            s = stats.get(_ref_key(ds, ib, ip, per_spw))
            if s is not None:
                out[0, ib, 0, ip] = s[index]
    return out


# ======================================================================
# Built-in filters
# ======================================================================

def _all_mask(ds, **_):
    return np.ones(ds["vis"].shape, dtype=bool)


def _amp_range_mask(ds, low, high, mode, **_):
    amp = ds["amp"].values
    inside = (amp >= low) & (amp <= high)
    return inside if mode == "inside" else ~inside


def _zscore_prepare(refs, reference="selection", **_):
    per_spw = reference == "spw"
    re = _collect_reference(refs, per_spw, lambda ds: ds["real"].values)
    im = _collect_reference(refs, per_spw, lambda ds: ds["imag"].values)
    stats = {}
    for k in re:
        r, i = re[k], im[k]
        mr, mi = float(np.median(r)), float(np.median(i))
        rad = np.sqrt((r - mr) ** 2 + (i - mi) ** 2)
        stats[k] = (mr, mi, float(np.median(rad)), int(r.size))
    return stats


def zscore_values(ds, stats, reference="selection") -> np.ndarray:
    """Per-sample Z-Score of *ds* against *stats* (NaN if no reference)."""
    per_spw = reference == "spw"
    mr = _per_key_array(ds, stats, per_spw, 0)
    mi = _per_key_array(ds, stats, per_spw, 1)
    sc = _per_key_array(ds, stats, per_spw, 2)
    r = np.sqrt((ds["real"].values - mr) ** 2 + (ds["imag"].values - mi) ** 2)
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(sc > 0, r / sc * _ZSCORE_RAYLEIGH_CONST, np.nan)


def _zscore_mask(ds, stats, cutoff, granularity, reference, cell_dims, **_):
    z = zscore_values(ds, stats, reference)
    if granularity == "sample":
        with np.errstate(invalid="ignore"):
            return z > cutoff
    dims = list(ds["vis"].dims)
    axes = tuple(dims.index(d) for d in (cell_dims or ("frequency",)) if d in dims)
    if not axes:
        with np.errstate(invalid="ignore"):
            return z > cutoff
    usable = ~np.asarray(ds["flag"].values, bool) & np.asarray(ds["valid"].values, bool)[:, :, None, None]
    zz = np.where(usable, z, np.nan)
    n = 1
    for a in axes:
        n *= z.shape[a]
    thr = zscore_cell_cutoff(n, cutoff)
    with np.errstate(invalid="ignore"), warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        cell_max = np.nanmax(zz, axis=axes, keepdims=True)
    with np.errstate(invalid="ignore"):
        return np.broadcast_to(cell_max > thr, z.shape)


def _mad_prepare(refs, reference="selection", **_):
    per_spw = reference == "spw"
    amps = _collect_reference(refs, per_spw, lambda ds: ds["amp"].values)
    stats = {}
    for k, a in amps.items():
        med = float(np.median(a))
        mad = float(np.median(np.abs(a - med)))
        stats[k] = (med, mad * _MAD_TO_SIGMA, int(a.size))
    return stats


def _mad_mask(ds, stats, nsigma, reference, **_):
    per_spw = reference == "spw"
    med = _per_key_array(ds, stats, per_spw, 0)
    sig = _per_key_array(ds, stats, per_spw, 1)
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(sig > 0, np.abs(ds["amp"].values - med) / sig > nsigma, False)


def _phase_prepare(refs, reference="selection", **_):
    per_spw = reference == "spw"
    re = _collect_reference(refs, per_spw, lambda ds: ds["real"].values)
    im = _collect_reference(refs, per_spw, lambda ds: ds["imag"].values)
    return {k: (math.degrees(math.atan2(float(np.median(im[k])), float(np.median(re[k])))),
                int(re[k].size)) for k in re}


def _phase_mask(ds, stats, max_deg, reference, **_):
    per_spw = reference == "spw"
    ref = _per_key_array(ds, stats, per_spw, 0)
    d = (ds["phase"].values - ref + 180.0) % 360.0 - 180.0
    with np.errstate(invalid="ignore"):
        return np.abs(d) > max_deg


_REFERENCE_PARAM = ParamSpec(
    "reference", "choice", "auto", "Reference population",
    choices=("auto", "selection", "spw"), gui=False,
    help="'auto': match the panel being flagged. 'selection': per baseline and "
         "correlation over the whole current data selection (as the scatter "
         "Z-Score colouring); 'spw': per spectral window as well (as the raster "
         "Z-Score).")

# ---------------------------------------------------------------------- #
# Value range on the displayed quantity (HRS H5 slice 2, 2026-10-09)       #
# ---------------------------------------------------------------------- #
#
# AIPS's clip, CASA's flagdata mode='clip': flag samples whose value lies
# in a range.  It works on the quantity the panel shows, so the numbers
# typed are the numbers on the colour bar or the axis.  Either bound may
# be left empty: "Low" alone flags everything at or above it, "High"
# alone everything at or below it.

VALUE_RANGE_QUANTITIES = {
    "AMPLITUDE": ("amp", "Amplitude"),
    "PHASE":     ("phase", "Phase (deg)"),
    "REAL":      ("real", "Real"),
    "IMAGINARY": ("imag", "Imaginary"),
}


def _value_range_mask(ds, low=None, high=None, quantity="AMPLITUDE", **_):
    q = str(quantity or "AMPLITUDE").upper()
    if q not in VALUE_RANGE_QUANTITIES:
        raise ValueError(
            "Value range works on a panel showing Amplitude, Phase, Real or "
            f"Imaginary; this one shows {q.replace('_', ' ').title()}")
    v = np.asarray(ds[VALUE_RANGE_QUANTITIES[q][0]].values, dtype=np.float64)
    m = np.isfinite(v)
    with np.errstate(invalid="ignore"):
        if low is not None:
            m &= v >= float(low)
        if high is not None:
            m &= v <= float(high)
    return m


# ---------------------------------------------------------------------- #
# Filters judged against neighbouring samples (HRS H5 slice 2)             #
# ---------------------------------------------------------------------- #
#
# Both need samples OUTSIDE the box: a spike at the box's edge is judged
# against the samples next to it, and a sample next to a flagged one may
# be just outside.  They are therefore "reference" filters: prepare()
# receives the whole current selection for the baselines and
# correlations the box touches and works out the answer on that grid;
# mask() looks the box's samples up in it.  Runs are cut at scan
# boundaries -- neighbours across a scan change are other conditions,
# often another source.

RUNNING_WINDOW = 9          # samples in the running median (odd)
NEAR_MIN = 3                # fewer unflagged neighbours than this: no verdict


def _segments(ds, along: str) -> list:
    """Index runs along *along* that may be treated as neighbours."""
    if along == "frequency":
        return [np.arange(ds.sizes["frequency"])]
    n = ds.sizes["time"]
    if "scan_name" not in ds.coords or n == 0:
        return [np.arange(n)]
    scans = np.asarray(ds["scan_name"].values).astype(str)
    cut = np.flatnonzero(scans[1:] != scans[:-1]) + 1
    return np.split(np.arange(n), cut)


def _grid_key(ds, ib, ip):
    a1 = str(ds["baseline_antenna1_name"].values[ib])
    a2 = str(ds["baseline_antenna2_name"].values[ib])
    pol = str(ds["polarization"].values[ip])
    spw = ds.attrs.get("spw")
    return (a1, a2, pol, None if spw is None else (str(spw.ident), spw.n_chan, spw.freq_min))


def _grid_prepare(refs, verdict):
    """``{key: [(times, freqs, bool grid[time, freq]), ...]}`` from
    *verdict(ds, ib, ip) -> bool grid*, one entry per reference block."""
    out: dict = {}
    for ds in refs:
        t = np.asarray(ds["time"].values, dtype=np.float64)
        f = np.asarray(ds["frequency"].values, dtype=np.float64)
        ot, of = np.argsort(t, kind="stable"), np.argsort(f, kind="stable")
        for ib in range(ds.sizes["baseline_id"]):
            for ip in range(ds.sizes["polarization"]):
                g = verdict(ds, ib, ip)
                if g is None or not g.any():
                    continue
                out.setdefault(_grid_key(ds, ib, ip), []).append(
                    (t[ot], f[of], g[np.ix_(ot, of)]))
    return out


def _grid_mask(ds, stats):
    from .flag_model import TIME_TOL, FREQ_RTOL, _match_sorted
    out = np.zeros(ds["vis"].shape, dtype=bool)
    t = np.asarray(ds["time"].values, dtype=np.float64)
    f = np.asarray(ds["frequency"].values, dtype=np.float64)
    for ib in range(ds.sizes["baseline_id"]):
        for ip in range(ds.sizes["polarization"]):
            for rt, rf, g in stats.get(_grid_key(ds, ib, ip), ()):
                ti = _match_sorted(t, rt, atol=TIME_TOL)
                fi = _match_sorted(f, rf, rtol=FREQ_RTOL)
                okt, okf = np.flatnonzero(ti >= 0), np.flatnonzero(fi >= 0)
                if okt.size and okf.size:
                    out[np.ix_(okt, [ib], okf, [ip])] |= g[np.ix_(ti[okt], fi[okf])][:, None, :, None]
    return out


def _running_median(a: np.ndarray, axis: int, width: int) -> np.ndarray:
    """For every sample, the NaN-ignoring median of its neighbours along
    *axis*: the *width* samples centred on it, the sample itself left out.
    Near the ends of the run the window is moved inward, keeping *width*
    samples, instead of being cut short.  NaN where fewer than NEAR_MIN
    neighbours are usable.

    The sample is left out because, in its own window, it is often the
    median: its residual is then zero and the scatter measured from the
    residuals reads low (by a third for 9 samples), so ordinary noise
    crossed the cutoff.  A window cut short at a scan edge, or one made
    up by reflection, also scatters more than an inner one and sent edge
    samples over the cutoff; the shifted window does not.  It is off
    centre by up to width/2 samples, which matters only if the level
    changes by a noise rms within that many integrations or channels.
    """
    x = np.moveaxis(np.asarray(a, dtype=np.float64), axis, 0)
    n = x.shape[0]
    if n == 0:
        return np.moveaxis(x.copy(), 0, axis)
    w = min(int(width), n)
    start = np.clip(np.arange(n) - w // 2, 0, n - w)
    idx = start[:, None] + np.arange(w)[None, :]                 # (n, w)
    win = x[idx]                                                 # (n, w, ...)
    self_ = idx == np.arange(n)[:, None]
    win = np.where(self_.reshape(self_.shape + (1,) * (x.ndim - 1)), np.nan, win)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        med = np.nanmedian(win, axis=1)
    cnt = np.sum(np.isfinite(win), axis=1)
    out = np.where(cnt >= NEAR_MIN, med, np.nan)
    return np.moveaxis(out, 0, axis)


def _outlier_prepare(refs, along="time", nsigma=5.0, **_):
    ax = "time" if along == "time" else "frequency"

    def verdict(ds, ib, ip):
        amp = np.asarray(ds["amp"].values[:, ib, :, ip], dtype=np.float64)
        use = ~np.asarray(ds["flag"].values[:, ib, :, ip], dtype=bool)
        a = np.where(use, amp, np.nan)
        if np.sum(use) < NEAR_MIN:
            return None
        resid = np.full(a.shape, np.nan)
        axis = 0 if ax == "time" else 1
        for seg in _segments(ds, ax):
            part = np.take(a, seg, axis=axis)
            med = _running_median(part, axis, RUNNING_WINDOW)
            if axis == 0:
                resid[seg, :] = part - med
            else:
                resid[:, seg] = part - med
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            sig = _MAD_TO_SIGMA * np.nanmedian(np.abs(resid))
            level = np.nanmedian(np.abs(a))
        if not np.isfinite(sig):
            return None
        # Noise-free (simulated) data have a zero scatter: judge against a
        # tiny fraction of the level instead of dividing by zero.
        sig = max(float(sig), 1e-6 * float(level) if np.isfinite(level) else 0.0, 1e-30)
        with np.errstate(invalid="ignore"):
            return np.abs(resid) > float(nsigma) * sig
    return _grid_prepare(refs, verdict)


def _outlier_mask(ds, stats, **_):
    return _grid_mask(ds, stats)


def _grow_prepare(refs, along="both", width=1, **_):
    width = max(1, int(width))

    def dilate(fl, axis, segs):
        out = np.zeros_like(fl)
        for seg in segs:
            part = np.take(fl, seg, axis=axis)
            acc = np.zeros_like(part)
            n = part.shape[axis]
            for k in range(1, width + 1):
                if k >= n:
                    break
                sl_a = [slice(None)] * 2; sl_b = [slice(None)] * 2
                sl_a[axis] = slice(k, None); sl_b[axis] = slice(None, -k)
                acc[tuple(sl_a)] |= part[tuple(sl_b)]      # flagged k before
                acc[tuple(sl_b)] |= part[tuple(sl_a)]      # flagged k after
            if axis == 0:
                out[seg, :] = acc
            else:
                out[:, seg] = acc
        return out

    def verdict(ds, ib, ip):
        fl = np.asarray(ds["flag"].values[:, ib, :, ip], dtype=bool).copy()
        valid = np.asarray(ds["valid"].values[:, ib], dtype=bool)
        fl[~valid, :] = False                 # padding is not "flagged data"
        if not fl.any():
            return None
        near = np.zeros_like(fl)
        if along in ("time", "both"):
            near |= dilate(fl, 0, _segments(ds, "time"))
        if along in ("channel", "both"):
            near |= dilate(fl, 1, _segments(ds, "frequency"))
        return near & ~fl & valid[:, None]
    return _grid_prepare(refs, verdict)


def _grow_mask(ds, stats, **_):
    return _grid_mask(ds, stats)


BUILTIN_FILTERS: dict = {}


def _register_builtin(f: FlagFilter) -> FlagFilter:
    f = dataclasses.replace(f, builtin=True)
    BUILTIN_FILTERS[f.name] = f
    return f


ALL = _register_builtin(FlagFilter(
    "all", _all_mask, label="All selected (immediate)",
    description="Every sample in the box: AIPS-style immediate flagging. The other "
                "filters take only some of the samples in the box, so a generous box "
                "can be drawn round a bad stretch."))

VALUE_RANGE = _register_builtin(FlagFilter(
    "value_range", _value_range_mask,
    params=(ParamSpec("low", "float", None, "Low",
                      help="Flag samples at or above this value; empty for no lower "
                           "bound. In the units of the colour bar or axis."),
            ParamSpec("high", "float", None, "High",
                      help="Flag samples at or below this value; empty for no upper "
                           "bound. Low and High together: the samples between them."),
            ParamSpec("quantity", "choice", "AMPLITUDE", "Quantity", gui=False,
                      choices=tuple(VALUE_RANGE_QUANTITIES))),
    label="Value range",
    description="Samples whose value \u2014 of the quantity the panel shows: Amplitude, "
                "Phase, Real or Imaginary \u2014 lies in the range. For dropouts (High "
                "only) and strong interference (Low only); CASA's clip."))

OUTLIER = _register_builtin(FlagFilter(
    "outlier", _outlier_mask, scope="reference", prepare_fn=_outlier_prepare,
    params=(ParamSpec("along", "choice", "time", "Compare along",
                      choices=("time", "channel"),
                      help="time: each sample against the integrations before and "
                           "after it (spikes in time, brief interference); channel: "
                           "against the neighbouring channels (narrow-band "
                           "interference, a bad channel)."),
            ParamSpec("nsigma", "float", 5.0, "Cutoff (sigma)", min=0.0,
                      help="How far from its neighbours a sample must be, in robust "
                           "standard deviations of the baseline's scatter about them. "
                           "Lower takes more.")),
    label="Outlier from neighbours",
    description="Amplitudes far from the running median of the "
                f"{RUNNING_WINDOW} samples around them, per baseline and correlation, "
                "within each scan. Finds spikes that a smooth change in gain or a "
                "bandpass does not explain; CASA's tfcrop / AIPS's FLAGR in spirit."))

ZSCORE = _register_builtin(FlagFilter(
    "zscore", _zscore_mask, scope="reference", prepare_fn=_zscore_prepare,
    params=(ParamSpec("cutoff", "float", _DEFAULT_ZSCORE_THRESHOLD, "Cutoff", min=0.0,
                      help="Per-sample Z-Score cutoff (Iglewicz & Hoaglin 3.5)."),
            ParamSpec("granularity", "choice", "auto", "Match per",
                      choices=("auto", "sample", "cell"),
                      help="'sample': each sample against the cutoff. 'cell': a whole "
                           "raster cell (max over its reduced dimensions, Sidak-"
                           "corrected cutoff) as the raster colouring shows it. "
                           "'auto': 'cell' on a raster showing Z-Score, else 'sample'."),
            _REFERENCE_PARAM,
            ParamSpec("cell_dims", "any", ("frequency",), "Cell dimensions", gui=False)),
    label="Z-Score above cutoff",
    description="Robust per-baseline joint (real, imag) Z-Score, as the Z-Score "
                "colouring computes it: what the Z-Score view shows bright."))

AMPLITUDE_MAD = _register_builtin(FlagFilter(
    "amplitude_mad", _mad_mask, scope="reference", prepare_fn=_mad_prepare,
    params=(ParamSpec("nsigma", "float", 5.0, "Sigma", min=0.0,
                      help="How far from the baseline's median amplitude, in robust "
                           "standard deviations."),
            _REFERENCE_PARAM),
    label="Amplitude outlier (MAD)",
    description="|amp - median| / (1.4826 MAD) above the cutoff, per baseline over the "
                "whole selection: a baseline's samples that are off its usual level."))

PHASE_DEVIATION = _register_builtin(FlagFilter(
    "phase_deviation", _phase_mask, scope="reference", prepare_fn=_phase_prepare,
    params=(ParamSpec("max_deg", "float", 45.0, "Max deviation (deg)", min=0.0, max=180.0,
                      help="Phases further than this from the baseline's median "
                           "direction are taken."),
            _REFERENCE_PARAM),
    label="Phase deviation",
    description="Phase further than the limit from the per-baseline median direction. "
                "For a calibrator, where phases should agree."))

GROW = _register_builtin(FlagFilter(
    "grow", _grow_mask, scope="reference", prepare_fn=_grow_prepare,
    params=(ParamSpec("along", "choice", "both", "Grow along",
                      choices=("time", "channel", "both"),
                      help="time: the integrations before and after a flagged sample; "
                           "channel: the channels either side; both: either."),
            ParamSpec("width", "int", 1, "Samples", min=1, max=50,
                      help="How many samples either side of a flagged one are taken.")),
    label="Grow around flags",
    description="Unflagged samples next to flagged ones (on disk or pending). "
                "Interference has weaker edges that a cutoff misses: flag the core "
                "with another filter, then draw a box with this one; CASA's extend."))

AMPLITUDE_RANGE = _register_builtin(FlagFilter(
    "amplitude_range", _amp_range_mask,
    params=(ParamSpec("low", "float", 0.0, "Low", min=0.0),
            ParamSpec("high", "float", 1.0e30, "High", min=0.0),
            ParamSpec("mode", "choice", "inside", "Match", choices=("inside", "outside"))),
    label="Amplitude range",
    description="Samples whose amplitude is inside (or outside) [low, high]. "
                "Superseded in the panel by Value range; kept for scripts and "
                "saved records.", gui=False))


# ======================================================================
# Registry
# ======================================================================

class FilterRegistry:
    """Built-in filters plus the user's (``VisibilityPlotter(flag_filters=)``).

    *user* maps names to ``FlagFilter`` objects or plain callables
    (``func(ds, **params) -> mask``).  User names may not shadow built-ins.
    """

    def __init__(self, user: Optional[Mapping[str, Any]] = None) -> None:
        self._filters: dict = dict(BUILTIN_FILTERS)
        for name, obj in (user or {}).items():
            self.register(name, obj)

    def register(self, name: str, obj) -> FlagFilter:
        if name in BUILTIN_FILTERS:
            raise ValueError(f"flag filter name {name!r} is reserved for a built-in")
        if isinstance(obj, FlagFilter):
            f = dataclasses.replace(obj, name=name, builtin=False)
        elif callable(obj):
            f = make_flag_filter(obj, name=name)
        else:
            raise TypeError(f"flag filter {name!r}: expected a callable or FlagFilter, "
                            f"got {type(obj).__name__}")
        self._filters[name] = f
        return f

    def get(self, name: Optional[str]) -> FlagFilter:
        if not name:
            return ALL
        try:
            return self._filters[name]
        except KeyError:
            raise KeyError(f"no flag filter named {name!r}; available: "
                           f"{', '.join(self._filters)}") from None

    def names(self) -> list:
        return list(self._filters)

    def gui_names(self) -> list:
        """The filters the Flagging panel lists: the curated built-ins in
        their intended order, then the user's."""
        return [n for n, f in self._filters.items() if f.gui]

    def user_names(self) -> list:
        return [n for n, f in self._filters.items() if not f.builtin]

    def __contains__(self, name) -> bool:
        return name in self._filters

    def describe(self) -> list:
        return [f.describe() for f in self._filters.values()]
