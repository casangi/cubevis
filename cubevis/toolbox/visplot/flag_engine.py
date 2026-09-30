"""flag_engine.py
=================
Backend-side half of FlagDB v2: runs where the data lives (in-process, or in
the remote worker), shared by ``MSv2Backend`` and ``MSv4Backend`` through
``XArrayReader``.

Two jobs:

1. **Apply pending flags** (``apply_pending``).  Every render path of both
   backends obtains its flags from ``_flag_mask(ds)``; ``XArrayReader``
   routes that through here, folding the pending deltas over the on-disk
   flags lazily (``dask.map_blocks``, block by block).  Raster, scatter,
   probe, the Flag-fraction quantity and the Z-Score reference therefore
   all see the same effective state, locally and remotely.

2. **Evaluate a flag request** (``evaluate_request``).  Turns "the user drew
   this box on this panel, with this filter" into one ``FlagDelta`` plus
   exact sample counts.  It reuses the backend's own axis and quantity code
   (``_apply_selection``, ``_lazy_x_axis``, ``_lazy_quantity``) so that what
   is flagged is exactly what was displayed.

Box semantics (what a drawn box addresses)
------------------------------------------
Raster
    A raster cell is an *aggregate*: the mean (max for Z-Score) of every
    sample reduced into it -- e.g. all selected channels of the displayed
    correlation for a Time x Baseline cell.  A cell is addressed when the
    box overlaps it (cell extents use local coordinate spacing, as the
    hover probe does), and addressing a cell addresses **every sample
    reduced into it**, within the current data selection and the displayed
    correlation.  Decimated views make no difference: the box is resolved
    against the data coordinates, not the strided image, so every sample
    inside the box is included whether or not it was drawn.  To act on
    only *some* of a cell's samples, use a filter (it sees the individual
    samples).
Scatter
    A scatter point is one sample of one layer (x value, y value).  A box
    addresses the samples of every *visible* layer whose (x, y) lies inside
    it and which is displayed: finite, not hidden by a categorical colouring
    ("hide" mode), and -- for Flag -- currently unflagged (flagged samples
    are not drawn).  An Unflag box addresses currently flagged samples in
    the box.

Filters and eligibility
-----------------------
The filter sees the addressed samples; the result is intersected with valid
(non-padding) samples and with the samples the action can change (unflagged
ones for Flag, flagged ones for Unflag).

Representation chosen
---------------------
* Raster box + identity filter -> a **region** delta, *verified*: the
  region is re-evaluated against every partition's coordinates and must
  reproduce the box exactly, otherwise the exact sample set is stored
  instead.
* Anything decided by values (scatter box, any non-identity filter) ->
  a **sample-set** delta, materialized now from the frozen selection.

Package location
----------------
``cubevis/cubevis/toolbox/visplot/flag_engine.py``
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import numpy as np
import xarray as xr

from .axes import Axis
from .flag_filters import BUILTIN_FILTERS, ALL, FlagFilter, zscore_values
from .flag_model import (
    BlockCoords, FlagCounts, FlagDelta, SampleBlock, SpwChannels, SpwKey,
    ValueRange, _match_sorted, _region_axis_masks, fold_deltas, FREQ_RTOL,
)

log = logging.getLogger(__name__)

try:  # dask is optional for pure numpy datasets
    import dask.array as _da
except Exception:  # pragma: no cover
    _da = None


# ======================================================================
# Coordinates
# ======================================================================

def _bdim(backend) -> str:
    try:
        return backend._baseline_dim
    except Exception:
        return "baseline_id"


def _values(coord) -> np.ndarray:
    v = coord.values if hasattr(coord, "values") else coord
    return np.asarray(v)


def time_format(ds) -> str:
    fmt = str(ds.coords["time"].attrs.get("format", "unix")).lower()
    return "mjd" if "mjd" in fmt else "unix"


def spw_table(backend) -> list:
    """``[(SpwKey, raw_frequencies), ...]`` for every window in the store.

    Built once per backend from the raw (unselected) partitions.
    """
    tab = getattr(backend, "_cv_spw_table", None)
    if tab is not None:
        return tab
    tab = []
    for ds in backend._iter_visibility_partitions():
        ident, kind = backend._partition_spw_ident(ds)
        f = np.asarray(ds.coords["frequency"].values, dtype=np.float64)
        if f.size == 0:
            continue
        key = SpwKey(ident if not isinstance(ident, np.generic) else ident.item(),
                     kind, float(f.min()), float(f.max()), int(f.size))
        if not any(k.matches(key) for k, _ in tab):
            tab.append((key, f))
    backend._cv_spw_table = tab
    return tab


def spw_key_of(backend, ds) -> tuple:
    """``(SpwKey | None, channel indices in the full window)`` for a
    (possibly selected) partition."""
    ident, _kind = backend._partition_spw_ident(ds)
    f = np.asarray(ds.coords["frequency"].values, dtype=np.float64)
    for key, raw in spw_table(backend):
        if str(key.ident) != str(ident) or f.size > raw.size:
            continue
        order = np.argsort(raw, kind="stable")
        pos = _match_sorted(f, raw[order], rtol=FREQ_RTOL)
        if f.size and (pos >= 0).all():
            return key, order[pos].astype(np.int64)
    return None, np.full(f.shape, -1, dtype=np.int64)


def block_coords(backend, ds) -> BlockCoords:
    bd = _bdim(backend)
    if bd == "baseline_id" and "baseline_antenna1_name" in ds.coords:
        a1 = _values(ds.coords["baseline_antenna1_name"]).astype(str)
        a2 = _values(ds.coords["baseline_antenna2_name"]).astype(str)
    else:
        names = _values(ds.coords[bd]).astype(str)
        a1 = a2 = names
    key, chans = spw_key_of(backend, ds)
    scans = _values(ds.coords["scan_name"]).astype(str) if "scan_name" in ds.coords else None
    fields = _values(ds.coords["field_name"]).astype(str) if "field_name" in ds.coords else None
    return BlockCoords(
        times=np.asarray(ds.coords["time"].values, dtype=np.float64),
        ant1=a1, ant2=a2,
        freqs=np.asarray(ds.coords["frequency"].values, dtype=np.float64),
        pols=_values(ds.coords["polarization"]).astype(str),
        spw=key, chans=chans, scans=scans, fields=fields,
    )


def valid_mask(backend, ds) -> Optional[np.ndarray]:
    """``(time, baseline)`` mask of real (non-padding) samples, or ``None``.

    xarray-ms pads missing (time, baseline) slots, sets FLAG there and leaves
    EFFECTIVE_INTEGRATION_TIME NaN; xradio-written stores use NaN the same
    way.  Samples that fail this test are never changed by a pending flag.
    """
    eit = ds.get("EFFECTIVE_INTEGRATION_TIME") if hasattr(ds, "get") else None
    if eit is None:
        return None
    bd = _bdim(backend)
    try:
        v = np.asarray(eit.transpose("time", bd).values, dtype=np.float64)
    except Exception:
        return None
    return np.isfinite(v)


def _canon(backend) -> tuple:
    return ("time", _bdim(backend), "frequency", "polarization")


# ======================================================================
# 1. Apply pending flags
# ======================================================================

def apply_pending(backend, ds, base: xr.DataArray, deltas) -> xr.DataArray:
    """*base* (on-disk flags of *ds*) with *deltas* folded in, lazily."""
    if not deltas:
        return base
    canon = _canon(backend)
    if set(base.dims) != set(canon):
        log.debug("apply_pending: unexpected FLAG dims %s; pending ignored", base.dims)
        return base
    bc = block_coords(backend, ds)
    relevant = [d for d in deltas if d.spw_may_match(bc.spw)]
    if not relevant:
        return base
    dims = list(base.dims)
    perm = [dims.index(c) for c in canon]
    inv = list(np.argsort(perm))
    valid = valid_mask(backend, ds)

    def fold_block(block, slices):
        sl = dict(zip(dims, slices))
        sub = bc.sub(sl["time"], sl[canon[1]], sl["frequency"], sl["polarization"])
        b = np.transpose(np.asarray(block, dtype=bool), perm)
        v = None if valid is None else valid[sl["time"], sl[canon[1]]]
        return np.transpose(fold_deltas(relevant, sub, b, v), inv)

    data = base.data
    if _da is not None and isinstance(data, _da.Array):
        def _mb(block, block_info=None):
            loc = block_info[0]["array-location"]
            return fold_block(block, [slice(a, b) for a, b in loc])
        new = data.map_blocks(_mb, dtype=bool)
    else:
        new = fold_block(np.asarray(data), [slice(None)] * 4)
    return base.copy(data=new)


# ======================================================================
# 2. Evaluate a flag request
# ======================================================================

def _overlap(coords: np.ndarray, lo: float, hi: float,
             half: Optional[float] = None) -> np.ndarray:
    """Cells (centred on *coords*) overlapping ``[lo, hi]``."""
    c = np.asarray(coords, dtype=np.float64)
    lo, hi = min(lo, hi), max(lo, hi)
    if c.size == 0:
        return np.zeros(0, dtype=bool)
    if half is not None:
        clo, chi = c - half, c + half
    elif c.size == 1:
        clo = chi = c
    else:
        order = np.argsort(c, kind="stable")
        cs = c[order]
        mids = (cs[1:] + cs[:-1]) / 2.0
        lo_s = np.concatenate([[cs[0] - (mids[0] - cs[0])], mids])
        hi_s = np.concatenate([mids, [cs[-1] + (cs[-1] - mids[-1])]])
        clo = np.empty_like(c); chi = np.empty_like(c)
        clo[order] = lo_s; chi[order] = hi_s
    return (chi >= lo) & (clo <= hi)


def _raster_axis_index(axis: Axis) -> int:
    return {Axis.TIME: 0, Axis.BASELINE: 1, Axis.FREQUENCY: 2, Axis.CHANNEL: 2}[axis] \
        if axis in (Axis.TIME, Axis.BASELINE, Axis.FREQUENCY, Axis.CHANNEL) else _bad_axis(axis)


def _bad_axis(axis):
    raise ValueError(f"raster flagging on axis {axis.name} is not supported")


def _raster_axis_values(axis: Axis, bc: BlockCoords, ds, backend) -> np.ndarray:
    """A partition's coordinate values along a raster axis, in plot units."""
    if axis == Axis.TIME:
        return np.asarray(bc.times, dtype=np.float64)
    if axis == Axis.BASELINE:
        ids = np.asarray(ds.coords[_bdim(backend)].values)
        if not np.issubdtype(ids.dtype, np.number):
            ids = np.arange(ids.size)
        return ids.astype(np.float64)
    if axis == Axis.FREQUENCY:
        return np.asarray(bc.freqs, dtype=np.float64)
    if axis == Axis.CHANNEL:
        return np.asarray(bc.chans, dtype=np.float64)
    _bad_axis(axis)


def _raster_axis_mask(axis: Axis, bc: BlockCoords, ds, backend, lo, hi):
    """(canonical axis index, mask) for one raster axis."""
    if axis == Axis.TIME:
        return 0, _overlap(bc.times, lo, hi)
    if axis == Axis.BASELINE:
        ids = np.asarray(ds.coords[_bdim(backend)].values)
        if not np.issubdtype(ids.dtype, np.number):
            ids = np.arange(ids.size)
        return 1, _overlap(ids.astype(np.float64), lo, hi, half=0.5)
    if axis == Axis.FREQUENCY:
        return 2, _overlap(bc.freqs, lo, hi)
    if axis == Axis.CHANNEL:
        return 2, _overlap(bc.chans.astype(np.float64), lo, hi, half=0.5) & (bc.chans >= 0)
    raise ValueError(f"raster flagging on axis {axis.name} is not supported")


def _filter_dataset(backend, ds, bc: BlockCoords, data_column: str) -> xr.Dataset:
    """The filter-contract Dataset for (a subset of) one partition."""
    canon = _canon(backend)
    bd = canon[1]
    vis = backend._resolve_vis(ds).transpose(*canon)
    flag = backend._flag_mask(ds).transpose(*canon)
    vis_v = np.asarray(vis.values)
    flag_v = np.asarray(flag.values, dtype=bool)
    valid = valid_mask(backend, ds)
    if valid is None:
        valid = np.ones(vis_v.shape[:2], dtype=bool)
    # padding may hold NaN / garbage: keep it out of every statistic
    flag_v = flag_v | ~valid[:, :, None, None]
    dims = ("time", "baseline_id", "frequency", "polarization")
    coords = {
        "time": ("time", bc.times),
        "baseline_id": ("baseline_id", np.arange(len(bc.ant1))),
        "baseline_antenna1_name": ("baseline_id", np.asarray(bc.ant1)),
        "baseline_antenna2_name": ("baseline_id", np.asarray(bc.ant2)),
        "frequency": ("frequency", bc.freqs),
        "channel": ("frequency", bc.chans),
        "polarization": ("polarization", np.asarray(bc.pols)),
    }
    if bc.scans is not None:
        coords["scan_name"] = ("time", bc.scans)
    if bc.fields is not None:
        coords["field_name"] = ("time", bc.fields)
    data = {
        "vis": (dims, vis_v), "real": (dims, vis_v.real), "imag": (dims, vis_v.imag),
        "amp": (dims, np.abs(vis_v)), "phase": (dims, np.degrees(np.angle(vis_v))),
        "flag": (dims, flag_v), "valid": (("time", "baseline_id"), valid),
    }
    if "WEIGHT" in ds.data_vars and set(ds["WEIGHT"].dims) == set(canon):
        data["weight"] = (dims, np.asarray(ds["WEIGHT"].transpose(*canon).values))
    out = xr.Dataset(data, coords=coords)
    out.attrs.update({"spw": bc.spw, "data_column": data_column,
                      "time_format": time_format(ds)})
    return out


def _isel_canon(backend, ds, it=None, ib=None, if_=None, ip=None):
    canon = _canon(backend)
    idx = {}
    for d, v in zip(canon, (it, ib, if_, ip)):
        if v is not None:
            idx[d] = np.asarray(v)
    return ds.isel(idx) if idx else ds


def _category_values(axis: Axis, bc: BlockCoords, shape3) -> Optional[np.ndarray]:
    """Per-sample category labels (time, baseline, freq) for colorize-hide."""
    nt, nb, nf = shape3
    if axis == Axis.SCAN and bc.scans is not None:
        return np.broadcast_to(bc.scans[:, None, None], shape3)
    if axis == Axis.FIELD and bc.fields is not None:
        return np.broadcast_to(bc.fields[:, None, None], shape3)
    if axis == Axis.ANTENNA1:
        return np.broadcast_to(np.asarray(bc.ant1)[None, :, None], shape3)
    if axis == Axis.ANTENNA2:
        return np.broadcast_to(np.asarray(bc.ant2)[None, :, None], shape3)
    if axis == Axis.BASELINE:
        names = np.array([f"{a}&{b}" for a, b in zip(bc.ant1, bc.ant2)])
        return np.broadcast_to(names[None, :, None], shape3)
    if axis == Axis.SPW and bc.spw is not None:
        return np.full(shape3, str(bc.spw.ident))
    return None


def _axis(v) -> Axis:
    return v if isinstance(v, Axis) else Axis[str(v)]


def _resolve_filter(req: dict):
    fobj = req.get("filter_obj")
    spec = req.get("filter") or {}
    if fobj is None:
        name = spec.get("name") or "all"
        if name not in BUILTIN_FILTERS:
            raise KeyError(
                f"flag filter {name!r} is not available where the data are "
                "(user-supplied filters run only with local data)")
        fobj = BUILTIN_FILTERS[name]
    params = fobj.resolve_params(spec.get("params"))
    return fobj, params


def evaluate_request(backend, req: dict) -> dict:
    """Resolve a flag request into ``{"delta": dict|None, "counts": dict,
    "warnings": [...]}`` (plain data, so it crosses the remote wire).

    Request keys
    ------------
    ``flag`` (bool), ``selection`` (SelectionSpec), ``kind`` ("raster" |
    "scatter"), ``x_axis``/``x0``/``x1``, ``y_axis``/``y0``/``y1``
    (raster y axis; for scatter the y range applies to each layer's own
    y axis), ``polarization`` (raster), ``layers`` (scatter: list of dicts
    with ``y_axis``, ``polarization`` and optional ``hide_axis`` /
    ``hide_values``), ``filter`` ({"name", "params"}) or ``filter_obj``
    (a ``FlagFilter``, local only), ``extend`` (dict of extend_* flags),
    ``source``, ``comment``, ``provenance`` (list), ``data_column``,
    ``force_samples`` (bool).
    """
    flag = bool(req.get("flag", True))
    sel = req["selection"]
    kind = req.get("kind", "raster")
    fobj, params = _resolve_filter(req)
    extend = {k: bool(v) for k, v in (req.get("extend") or {}).items()}
    data_column = req.get("data_column") or getattr(sel, "data_column", "") or ""
    warnings: list = []

    x_axis = _axis(req["x_axis"])
    x0, x1 = sorted((float(req["x0"]), float(req["x1"])))
    y0, y1 = sorted((float(req["y0"]), float(req["y1"])))
    y_axis = _axis(req["y_axis"]) if req.get("y_axis") else None

    params = resolve_auto_params(params, kind, req.get("quantity"))
    if kind == "raster" and fobj.name == "zscore":
        reduced = [d for d in ("time", "baseline_id", "frequency")
                   if d not in {_raster_dim(x_axis), _raster_dim(y_axis)}]
        params = dict(params, cell_dims=tuple(reduced))

    parts = []           # per partition: (ds, bc, box4d or axis masks)
    visited = []
    for raw in backend._iter_visibility_partitions(sel):
        ds = backend._apply_selection(raw, sel)
        if any(ds.sizes.get(d, 0) == 0 for d in _canon(backend)):
            continue
        visited.append((ds, block_coords(backend, ds)))

    if kind == "raster":
        # Cells are those of the DISPLAYED grid: the raster concatenates the
        # partitions, so its axis is the union of their coordinates.  A
        # coordinate is addressed when its cell on that union grid overlaps
        # the box -- per-partition spacing would widen cells wherever
        # partitions interleave (e.g. two windows with different channels).
        pol = req.get("polarization")
        chosen = {}
        for ax, lo, hi in ((x_axis, x0, x1), (y_axis, y0, y1)):
            vals = [_raster_axis_values(ax, bc, ds, backend) for ds, bc in visited]
            union = np.unique(np.concatenate(vals)) if vals else np.zeros(0)
            half = 0.5 if ax in (Axis.BASELINE, Axis.CHANNEL) else None
            chosen[ax] = union[_overlap(union, lo, hi, half=half)]
        for ds, bc in visited:
            mp = bc.pols == str(pol) if pol is not None else np.ones(len(bc.pols), bool)
            if not mp.any():
                continue
            axes = [np.ones(n, dtype=bool) for n in bc.shape[:3]] + [mp]
            for ax in (x_axis, y_axis):
                i = _raster_axis_index(ax)
                v = _raster_axis_values(ax, bc, ds, backend)
                sel_v = chosen[ax]
                if ax in (Axis.TIME, Axis.FREQUENCY):
                    m = _match_sorted(v, sel_v, atol=1e-6 if ax == Axis.TIME else 0.0,
                                      rtol=0.0 if ax == Axis.TIME else FREQ_RTOL) >= 0
                else:
                    m = np.isin(v, sel_v)
                if ax == Axis.CHANNEL:
                    m &= bc.chans >= 0
                axes[i] = axes[i] & m
            if all(a.any() for a in axes):
                parts.append((ds, bc, tuple(axes)))
    else:
        for ds, bc in visited:
            box = _scatter_box(backend, ds, bc, req, x_axis, (x0, x1), (y0, y1), flag, parts_all=None)
            if box is not None and box.any():
                parts.append((ds, bc, box))

    if not parts:
        return {"delta": None, "counts": FlagCounts().to_dict(),
                "warnings": ["the box contains no data in the current selection"]}

    # ---------------- region fast path (raster, identity filter) -------- #
    if kind == "raster" and fobj.is_identity and not req.get("force_samples"):
        delta = _region_delta(backend, parts, req, sel, x_axis, y_axis, flag, extend,
                              data_column)
        if delta is not None:
            counts = FlagCounts()
            for ds, bc, axes in parts:
                counts = counts.merge(_region_counts(backend, ds, bc, axes, flag))
            delta = _with_n(delta, counts.n_changed)
            return {"delta": delta.to_dict(json_safe=False), "counts": counts.to_dict(),
                    "warnings": warnings}
        warnings.append("box is not expressible as a coordinate region; "
                        "stored as explicit samples")

    # ---------------- sample path ------------------------------------- #
    stats = None
    if fobj.scope == "reference":
        stats = _prepare_reference(backend, fobj, params, parts, sel)

    blocks, counts = [], FlagCounts()
    for ds, bc, box in parts:
        if isinstance(box, tuple):          # raster axis masks -> 4D
            mt, mb, mf, mp = box
            box = (mt[:, None, None, None] & mb[None, :, None, None]
                   & mf[None, None, :, None] & mp[None, None, None, :])
        keep = [np.flatnonzero(box.any(axis=tuple(a for a in range(4) if a != ax)))
                for ax in range(4)]
        sub = _isel_canon(backend, ds, *keep)
        sbc = bc.sub(*keep)
        fds = _filter_dataset(backend, sub, sbc, data_column)
        sbox = box[np.ix_(*keep)]
        valid4 = np.broadcast_to(fds["valid"].values[:, :, None, None], sbox.shape)
        eff = fds["flag"].values
        selected = sbox & valid4
        if kind == "scatter":
            # A scatter box addresses what is displayed: for Flag, the drawn
            # (unflagged) points; for Unflag, the flagged points in the box.
            selected = selected & (~eff if flag else eff)
        eligible = selected & (~eff if flag else eff)
        if fobj.is_identity:
            fmask = np.ones(sbox.shape, bool)
        else:
            fmask = fobj.mask(fds, stats, params)
        matched = eligible & fmask
        c = FlagCounts.from_mask(matched, sbc, n_selected=int(selected.sum()),
                                 n_changed=int(matched.sum()))
        counts = counts.merge(c)
        blk = SampleBlock.from_mask(sbc.spw or SpwKey("?", "none", 0.0, 0.0, 0),
                                    sbc.times, sbc.ant1, sbc.ant2, sbc.freqs,
                                    sbc.chans, sbc.pols, matched)
        if blk is not None:
            blocks.append(blk)

    if not blocks:
        warnings.append("no samples matched" + ("" if fobj.is_identity
                                                 else f" filter {fobj.name!r}"))
        return {"delta": None, "counts": counts.to_dict(), "warnings": warnings}

    value_ranges = []
    if kind == "scatter":
        value_ranges.append(ValueRange(x_axis.name, x0, x1))
        for lyr in req.get("layers") or ():
            value_ranges.append(ValueRange(_axis(lyr["y_axis"]).name, y0, y1,
                                           lyr.get("polarization")))
    prov = list(req.get("provenance") or [])
    if not fobj.is_identity:
        prov.append(fobj.record(params).describe())
    delta = FlagDelta(
        flag=flag, time_format=time_format(parts[0][0]),
        samples=tuple(blocks), value_ranges=tuple(value_ranges),
        filter=None if fobj.is_identity else fobj.record(params),
        correlation=None, source=req.get("source", ""), comment=req.get("comment", ""),
        provenance=tuple(prov), data_column=data_column, n_samples=counts.n_matched,
        **{k: v for k, v in extend.items() if k in ("extend_corr", "extend_chan")},
    )
    return {"delta": delta.to_dict(json_safe=False), "counts": counts.to_dict(),
            "warnings": warnings}


def resolve_auto_params(params: dict, kind: str, quantity: Optional[str]) -> dict:
    """Resolve ``"auto"`` filter parameters so the filter matches what the
    flagged panel shows: the raster Z-Score is computed per spectral window
    and reported per cell; the scatter Z-Score over the whole selection, per
    sample."""
    params = dict(params)
    if params.get("reference") == "auto":
        params["reference"] = "spw" if kind == "raster" else "selection"
    if params.get("granularity") == "auto":
        params["granularity"] = ("cell" if kind == "raster" and
                                 str(quantity or "").upper() == "Z_SCORE" else "sample")
    if kind != "raster" and params.get("granularity") == "cell":
        params["granularity"] = "sample"      # a scatter point is one sample
    return params


def _with_n(delta: FlagDelta, n: int) -> FlagDelta:
    import dataclasses
    return dataclasses.replace(delta, n_samples=int(n))


def _raster_dim(axis: Optional[Axis]) -> Optional[str]:
    if axis is None:
        return None
    return {Axis.TIME: "time", Axis.BASELINE: "baseline_id",
            Axis.FREQUENCY: "frequency", Axis.CHANNEL: "frequency"}.get(axis)


# ---------------------------------------------------------------------- #
# Raster region                                                            #
# ---------------------------------------------------------------------- #

def _region_delta(backend, parts, req, sel, x_axis, y_axis, flag, extend, data_column):
    axes_used = {x_axis, y_axis}
    kw: dict = {}
    # time
    if Axis.TIME in axes_used:
        ts = np.concatenate([bc.times[a[0]] for _ds, bc, a in parts])
        kw["time_range"] = (float(ts.min()), float(ts.max()))
    elif sel.time_range is not None:
        kw["time_range"] = tuple(float(t) for t in sel.time_range)
    if sel.scan is not None:
        kw["scan_names"] = tuple(str(s) for s in sel.scan)
    if sel.field_names is not None:
        kw["field_names"] = tuple(str(s) for s in sel.field_names)
    # baselines
    if Axis.BASELINE in axes_used:
        pairs = []
        for _ds, bc, a in parts:
            for i in np.flatnonzero(a[1]):
                p = (str(bc.ant1[i]), str(bc.ant2[i]))
                if p not in pairs:
                    pairs.append(p)
        kw["baseline_ids"] = tuple(pairs)
    elif sel.baselines is not None:
        kw["baseline_ids"] = tuple((str(a), str(b)) for a, b in sel.baselines)
    elif sel.antenna_names is not None:
        kw["antenna_names"] = tuple(str(a) for a in sel.antenna_names)
    # spectral
    keys = []
    for _ds, bc, _a in parts:
        if bc.spw is not None and not any(k.matches(bc.spw) for k in keys):
            keys.append(bc.spw)
    if Axis.CHANNEL in axes_used or (sel.channel_range is not None
                                      and Axis.FREQUENCY not in axes_used):
        scs = []
        for _ds, bc, a in parts:
            ch = bc.chans[a[2]]
            if bc.spw is None or ch.size == 0 or (ch < 0).any():
                return None
            scs.append(SpwChannels(bc.spw, int(ch.min()), int(ch.max())))
        kw["spw_channels"] = tuple(scs)
    elif Axis.FREQUENCY in axes_used:
        fs = np.concatenate([bc.freqs[a[2]] for _ds, bc, a in parts])
        kw["freq_range"] = (float(fs.min()), float(fs.max()))
        kw["spw"] = tuple(keys)
    else:
        if sel.freq_range is not None:
            kw["freq_range"] = tuple(float(f) for f in sel.freq_range)
        if sel.spw is not None or len(keys) < len(spw_table(backend)):
            kw["spw"] = tuple(keys)
    # correlation
    pol = req.get("polarization")
    if pol is not None:
        kw["correlation"] = (str(pol),)
    elif sel.correlation is not None:
        kw["correlation"] = tuple(sel.correlation)

    delta = FlagDelta(flag=flag, time_format=time_format(parts[0][0]),
                      source=req.get("source", ""), comment=req.get("comment", ""),
                      provenance=tuple(req.get("provenance") or ()),
                      data_column=data_column,
                      **{k: v for k, v in extend.items() if k.startswith("extend_")},
                      **kw)
    # Verify: the region must reproduce the box on every partition of the
    # store (not only the visited ones -- a region must not reach data the
    # box did not address) before extend options widen it.
    check = FlagDelta(**{**{f: getattr(delta, f) for f in
                            ("flag", "time_range", "time_format", "freq_range", "spw",
                             "spw_channels", "baseline_ids", "antenna_names",
                             "scan_names", "field_names", "correlation")}})
    visited = {id(ds): axes for ds, _bc, axes in parts}
    for raw in backend._iter_visibility_partitions():
        ds_sel = backend._apply_selection(raw, sel)
        bc = block_coords(backend, raw)
        got = _region_axis_masks(check, bc)
        if got is None:
            continue
        want_full = _box_on_raw(backend, raw, bc, parts, sel)
        mt, mb, mf, mp = got
        region = (mt[:, None, None, None] & mb[None, :, None, None]
                  & mf[None, None, :, None] & mp[None, None, None, :])
        if not np.array_equal(region, want_full):
            log.debug("region verification failed; falling back to samples")
            return None
    return delta


def _box_on_raw(backend, raw, bc_raw: BlockCoords, parts, sel) -> np.ndarray:
    """The requested box expressed on the *raw* partition grid."""
    out = np.zeros(bc_raw.shape, dtype=bool)
    for ds, bc, (mt, mb, mf, mp) in parts:
        if bc.spw is not None and bc_raw.spw is not None and not bc.spw.matches(bc_raw.spw):
            continue
        ti = _match_sorted(bc.times[mt], np.sort(bc_raw.times), atol=1e-4)
        order_t = np.argsort(bc_raw.times, kind="stable")
        ti = ti[ti >= 0]
        if ti.size == 0:
            continue
        ti = order_t[ti]
        pair_idx = {(str(a), str(b)): i for i, (a, b) in enumerate(zip(bc_raw.ant1, bc_raw.ant2))}
        bi = np.array([pair_idx.get((str(bc.ant1[i]), str(bc.ant2[i])), -1)
                       for i in np.flatnonzero(mb)])
        bi = bi[bi >= 0]
        order_f = np.argsort(bc_raw.freqs, kind="stable")
        fi = _match_sorted(bc.freqs[mf], bc_raw.freqs[order_f], rtol=FREQ_RTOL)
        fi = order_f[fi[fi >= 0]]
        pl = list(bc_raw.pols)
        pi = np.array([pl.index(p) for p in bc.pols[mp] if p in pl])
        if bi.size and fi.size and pi.size:
            out[np.ix_(ti, bi, fi, pi)] = True
    return out


def _region_counts(backend, ds, bc, axes, flag) -> FlagCounts:
    mt, mb, mf, mp = axes
    keep = [np.flatnonzero(a) for a in axes]
    sub = _isel_canon(backend, ds, *keep)
    eff = np.asarray(backend._flag_mask(sub).transpose(*_canon(backend)).values, bool)
    valid = valid_mask(backend, sub)
    v4 = np.ones(eff.shape, bool) if valid is None else np.broadcast_to(
        valid[:, :, None, None], eff.shape)
    changed = v4 & (~eff if flag else eff)
    return FlagCounts.from_mask(changed, bc.sub(*keep), n_selected=int(v4.sum()),
                                n_changed=int(changed.sum()))


# ---------------------------------------------------------------------- #
# Scatter box                                                              #
# ---------------------------------------------------------------------- #

def _scatter_layer_masks(backend, ds, bc, req, x_axis, xr_, yr_):
    """Per visible layer: ``(layer_index, pol_index, mask3d)`` of the samples
    whose drawn (x, y) lies in the box and that are not hidden by a
    categorical colouring.  ``mask3d`` is ``(time, baseline, frequency)``.
    Flag state is NOT applied here (see ``_scatter_box`` / ``probe_region``).
    """
    canon = _canon(backend)
    vis = backend._resolve_vis(ds)
    no_flag = xr.zeros_like(backend._disk_flag_mask(ds))
    shape3 = bc.shape[:3]
    out = []
    for li, lyr in enumerate(req.get("layers") or ()):
        pol = str(lyr["polarization"])
        if pol not in list(bc.pols):
            continue
        ip = list(bc.pols).index(pol)
        yax = _axis(lyr["y_axis"])
        if yax == Axis.Z_SCORE:
            y = _scatter_zscore(backend, ds, bc, req, pol)
            template = backend._lazy_quantity(vis, no_flag, Axis.REAL, pol)
        else:
            q = backend._lazy_quantity(vis, no_flag, yax, pol)
            template = q
            y = np.asarray(q.transpose(*canon[:3]).values, dtype=np.float64)
        x = backend._lazy_x_axis(ds, x_axis, template)
        x = np.asarray(x.transpose(*canon[:3]).values, dtype=np.float64)
        x = np.broadcast_to(x, shape3)
        with np.errstate(invalid="ignore"):
            m = (np.isfinite(x) & np.isfinite(y) & (x >= xr_[0]) & (x <= xr_[1])
                 & (y >= yr_[0]) & (y <= yr_[1]))
        hide_axis = lyr.get("hide_axis")
        hide_vals = lyr.get("hide_values")
        if hide_axis and hide_vals:
            cats = _category_values(_axis(hide_axis), bc, shape3)
            if cats is not None:
                m &= ~np.isin(cats, [str(v) for v in hide_vals])
            elif _axis(hide_axis) == Axis.CORRELATION and pol in [str(v) for v in hide_vals]:
                m[:] = False
        out.append((li, ip, m))
    return out


def _scatter_box(backend, ds, bc, req, x_axis, xr_, yr_, flag, parts_all=None):
    """4D mask of samples a scatter box addresses on partition *ds*."""
    out = np.zeros(bc.shape, dtype=bool)
    for _li, ip, m in _scatter_layer_masks(backend, ds, bc, req, x_axis, xr_, yr_):
        out[..., ip] |= m
    return out


def probe_region(backend, req: dict) -> dict:
    """Exact identity of a scatter box -- the InfoTool's view of exactly
    what the FlagTool would address with the same box.

    Uses the same per-layer box resolution as ``evaluate_request`` (same
    axes, visible layers, hidden categories, padding) and reports, per
    layer, the samples that are **displayed** in the box, i.e. unflagged in
    the effective state -- precisely the samples a Flag box would flag with
    the identity filter.  Also returns ``flag_n`` (samples a Flag box would
    change) and ``unflag_n`` (flagged samples an Unflag box would restore),
    both over the union of layers.

    Returns ``{"layers": {"<AXIS>|<pol>": {...probe_scatter_region keys...}},
    "flag_n": int, "unflag_n": int}`` -- plain data for the remote wire.
    """
    sel = req["selection"]
    x_axis = _axis(req["x_axis"])
    x0, x1 = sorted((float(req["x0"]), float(req["x1"])))
    y0, y1 = sorted((float(req["y0"]), float(req["y1"])))
    max_samples = int(req.get("max_samples", 200_000))
    layers = list(req.get("layers") or ())
    keys = [f"{_axis(l['y_axis']).name}|{l['polarization']}" for l in layers]
    acc = {k: {"n": 0, "t": [], "bl": set(), "f": []} for k in keys}
    flag_n = unflag_n = 0
    for raw in backend._iter_visibility_partitions(sel):
        ds = backend._apply_selection(raw, sel)
        if any(ds.sizes.get(d, 0) == 0 for d in _canon(backend)):
            continue
        bc = block_coords(backend, ds)
        lm = _scatter_layer_masks(backend, ds, bc, req, x_axis, (x0, x1), (y0, y1))
        if not any(m.any() for _li, _ip, m in lm):
            continue
        eff = np.asarray(backend._flag_mask(ds).transpose(*_canon(backend)).values, bool)
        valid = valid_mask(backend, ds)
        v3 = np.ones(bc.shape[:3], bool) if valid is None else np.broadcast_to(
            valid[:, :, None], bc.shape[:3])
        union_f = np.zeros(bc.shape, bool)
        union_u = np.zeros(bc.shape, bool)
        ids = np.asarray(ds.coords[_bdim(backend)].values)
        for li, ip, m in lm:
            shown = m & v3 & ~eff[..., ip]
            union_f[..., ip] |= shown
            union_u[..., ip] |= m & v3 & eff[..., ip]
            n = int(shown.sum())
            if not n:
                continue
            a = acc[keys[li]]
            a["n"] += n
            tt = shown.any(axis=(1, 2)); bb = shown.any(axis=(0, 2)); ff = shown.any(axis=(0, 1))
            a["t"] += [float(bc.times[tt].min()), float(bc.times[tt].max())]
            if np.issubdtype(ids.dtype, np.number):
                a["bl"].update(int(b) for b in ids[bb])
            a["f"] += [float(bc.freqs[ff].min()), float(bc.freqs[ff].max())]
        flag_n += int(union_f.sum())
        unflag_n += int(union_u.sum())
    out = {}
    for k in keys:
        a = acc[k]
        if a["n"] == 0:
            out[k] = {"status": "no_data", "n_samples": 0, "t_range": None,
                      "bl_range": None, "bl_ids": None, "freq_range": None}
        elif a["n"] > max_samples:
            out[k] = {"status": "too_many_points", "n_samples": a["n"], "t_range": None,
                      "bl_range": None, "bl_ids": None, "freq_range": None}
        else:
            bl = sorted(a["bl"])
            out[k] = {"status": "ok", "n_samples": a["n"],
                      "t_range": (min(a["t"]), max(a["t"])),
                      "bl_range": (float(bl[0]), float(bl[-1])) if bl else None,
                      "bl_ids": bl or None,
                      "freq_range": (min(a["f"]), max(a["f"]))}
    return {"layers": out, "flag_n": flag_n, "unflag_n": unflag_n}


def _scatter_zscore(backend, ds, bc, req, pol) -> np.ndarray:
    """Per-sample Z-Score exactly as the scatter colouring computes it:
    reference = the whole current selection (every partition), per
    baseline, for this correlation."""
    cache = req.setdefault("_zscore_stats", {})
    if pol not in cache:
        refs = []
        for raw in backend._iter_visibility_partitions(req["selection"]):
            r = backend._apply_selection(raw, req["selection"])
            if pol not in [str(p) for p in r.coords["polarization"].values]:
                continue
            r = r.sel(polarization=[pol])
            refs.append(_filter_dataset(backend, r, block_coords(backend, r), ""))
        from .flag_filters import _zscore_prepare
        cache[pol] = _zscore_prepare(refs, reference="selection")
    sub = ds.sel(polarization=[pol])
    fds = _filter_dataset(backend, sub, block_coords(backend, sub), "")
    z = zscore_values(fds, cache[pol], "selection")[..., 0]
    eff = fds["flag"].values[..., 0]
    return np.where(eff, np.nan, z)


# ---------------------------------------------------------------------- #
# Reference populations                                                    #
# ---------------------------------------------------------------------- #

def _prepare_reference(backend, fobj: FlagFilter, params, parts, sel):
    """Reference datasets: the whole current selection for the baselines
    and correlations the box touches (per spectral window partition)."""
    want_pairs, want_pols = set(), set()
    for ds, bc, box in parts:
        if isinstance(box, tuple):
            bl, pl = box[1], box[3]
        else:
            bl = box.any(axis=(0, 2, 3)); pl = box.any(axis=(0, 1, 2))
        for i in np.flatnonzero(bl):
            want_pairs.add((str(bc.ant1[i]), str(bc.ant2[i])))
        want_pols.update(str(p) for p in bc.pols[pl])
    refs = []
    for raw in backend._iter_visibility_partitions(sel):
        ds = backend._apply_selection(raw, sel)
        if any(ds.sizes.get(d, 0) == 0 for d in _canon(backend)):
            continue
        bc = block_coords(backend, ds)
        ib = [i for i, p in enumerate(zip(bc.ant1, bc.ant2))
              if (str(p[0]), str(p[1])) in want_pairs]
        ip = [i for i, p in enumerate(bc.pols) if str(p) in want_pols]
        if not ib or not ip:
            continue
        sub = _isel_canon(backend, ds, None, ib, None, ip)
        refs.append(_filter_dataset(backend, sub, bc.sub(slice(None), ib, slice(None), ip), ""))
    return fobj.prepare(refs, params)


# ======================================================================
# 3. Flag views on cached scatter frames (no re-read on a flag change)
# ======================================================================
#
# The scatter frame cache stores RAW frames: every valid (non-padding)
# sample, with its on-disk flag (``__disk_flag``) and the identity needed
# to evaluate pending deltas row by row (``__spw`` code, ``__chan``, and
# the existing ``time``/``frequency``/antenna/scan/field columns).  A flag
# change then only re-evaluates which rows are drawn -- the MS is not read
# again.  ``frame_keep_mask`` is the row-wise twin of ``apply_pending``
# and must agree with it exactly (test_flagdb_v2 pins this).

RAW_HELPER_COLUMNS = ("__disk_flag", "__spw", "__chan")


def spw_code(backend, key) -> int:
    table = backend.__dict__.setdefault("_cv_spw_codes", [])
    for i, k in enumerate(table):
        if key is not None and k.matches(key):
            return i
    table.append(key)
    return len(table) - 1


def annotate_raw_frames(backend, ds, frames: dict) -> dict:
    """Add ``__disk_flag``/``__spw``/``__chan`` to the frames of one
    partition (built under flag view ``"none"``).  Marks the backend
    ``_cv_raw_unsupported`` if the frames lack the identity columns."""
    bd = _bdim(backend)
    if bd != "baseline_id":
        backend._cv_raw_unsupported = True
        return frames
    disk = np.asarray(backend._disk_flag_mask(ds).transpose(*_canon(backend)).values, bool)
    times = np.asarray(ds.coords["time"].values, dtype=np.float64)
    bids = np.asarray(ds.coords[bd].values)
    freqs = np.asarray(ds.coords["frequency"].values, dtype=np.float64)
    pols = [str(p) for p in ds.coords["polarization"].values]
    key, chans = spw_key_of(backend, ds)
    code = spw_code(backend, key)
    ot, ob, of = (np.argsort(times, kind="stable"), np.argsort(bids, kind="stable"),
                  np.argsort(freqs, kind="stable"))
    for (_axis, pol), df in frames.items():
        if df is None:
            continue
        if not all(c in df.columns for c in ("time", "baseline_id", "frequency")):
            backend._cv_raw_unsupported = True
            return frames
        n = len(df)
        if n == 0 or pol not in pols:
            df["__disk_flag"] = np.zeros(n, bool)
            df["__spw"] = np.full(n, code, np.int16)
            df["__chan"] = np.full(n, -1, np.int32)
            continue
        ti = ot[np.clip(np.searchsorted(times[ot], df["time"].to_numpy()), 0, len(ot) - 1)]
        bi = ob[np.clip(np.searchsorted(bids[ob], df["baseline_id"].to_numpy()), 0, len(ob) - 1)]
        fi = of[np.clip(np.searchsorted(freqs[of], df["frequency"].to_numpy()), 0, len(of) - 1)]
        df["__disk_flag"] = disk[ti, bi, fi, pols.index(pol)]
        df["__spw"] = np.full(n, code, np.int16)
        df["__chan"] = chans[fi].astype(np.int32)
    return frames


def _codes(df, col):
    s = df[col]
    if str(s.dtype) != "category":
        s = s.astype("category")
    return np.asarray(s.cat.codes), np.asarray(s.cat.categories).astype(str)


class _Rows:
    """Row identity of one raw frame (lazily decoded)."""

    def __init__(self, backend, df, pol):
        self.df, self.pol = df, str(pol)
        self.times = df["time"].to_numpy(dtype=np.float64)
        self.freqs = df["frequency"].to_numpy(dtype=np.float64)
        self.chans = df["__chan"].to_numpy()
        self.spw = df["__spw"].to_numpy()
        self.spw_table = backend.__dict__.get("_cv_spw_codes", [])
        self.c1, self.cat1 = _codes(df, "baseline_antenna1_name")
        self.c2, self.cat2 = _codes(df, "baseline_antenna2_name")
        self._scan = self._field = None

    @property
    def n(self):
        return len(self.times)

    def scan(self):
        if self._scan is None:
            self._scan = _codes(self.df, "scan_name") if "scan_name" in self.df else (None, None)
        return self._scan

    def field(self):
        if self._field is None:
            self._field = _codes(self.df, "field_name") if "field_name" in self.df else (None, None)
        return self._field

    def spw_codes_matching(self, keys) -> np.ndarray:
        return np.array([i for i, k in enumerate(self.spw_table)
                         if k is not None and any(k.matches(x) for x in keys)], dtype=np.int64)

    def pair_matrix(self, pairs) -> np.ndarray:
        """Row lookup ``M[code1, code2]`` -> index into *pairs* or -1."""
        idx = {}
        for i, (a, b) in enumerate(pairs):
            idx.setdefault((str(a), str(b)), i)
            idx.setdefault((str(b), str(a)), i)
        m = np.full((max(len(self.cat1), 1), max(len(self.cat2), 1)), -1, dtype=np.int64)
        for i, a in enumerate(self.cat1):
            for j, b in enumerate(self.cat2):
                m[i, j] = idx.get((a, b), -1)
        return m


def _row_region_mask(d: FlagDelta, R: _Rows) -> np.ndarray:
    from .flag_model import TIME_TOL
    m = np.ones(R.n, dtype=bool)
    if not d.extend_spw and d.spw is not None:
        m &= np.isin(R.spw, R.spw_codes_matching(d.spw))
    if not d.extend_chan and not d.extend_spw:
        if d.freq_range is not None:
            f0, f1 = d.freq_range
            tol = FREQ_RTOL * np.maximum(np.abs(R.freqs), 1.0)
            m &= (R.freqs >= f0 - tol) & (R.freqs <= f1 + tol)
        if d.spw_channels is not None:
            mc = np.zeros(R.n, dtype=bool)
            for sc in d.spw_channels:
                codes = R.spw_codes_matching([sc.spw])
                mc |= np.isin(R.spw, codes) & (R.chans >= sc.chan_lo) & (R.chans <= sc.chan_hi)
            m &= mc
        elif d.channel_range is not None and d.spw is not None:
            c0, c1 = d.channel_range
            m &= (R.chans >= c0) & (R.chans <= c1)
    elif not d.extend_spw and d.spw_channels is not None:
        m &= np.isin(R.spw, R.spw_codes_matching([sc.spw for sc in d.spw_channels]))
    if d.time_range is not None and not (d.extend_scan and d.scan_names):
        t0, t1 = d.time_range
        m &= (R.times >= t0 - TIME_TOL) & (R.times <= t1 + TIME_TOL)
    if d.scan_names is not None:
        codes, cats = R.scan()
        if codes is not None:
            m &= np.isin(cats, [str(s) for s in d.scan_names])[codes]
    if d.field_names is not None:
        codes, cats = R.field()
        if codes is not None:
            m &= np.isin(cats, [str(s) for s in d.field_names])[codes]
    if d.baseline_ids is not None:
        m &= R.pair_matrix(d.baseline_ids)[R.c1, R.c2] >= 0
    if d.antenna_names is not None:
        names = [str(a) for a in d.antenna_names]
        m &= np.isin(R.cat1, names)[R.c1] | np.isin(R.cat2, names)[R.c2]
    if d.correlation is not None and not d.extend_corr:
        if R.pol not in [str(c) for c in d.correlation]:
            m[:] = False
    return m


def _row_sample_mask(d: FlagDelta, R: _Rows) -> np.ndarray:
    from .flag_model import TIME_TOL, _match_sorted
    out = np.zeros(R.n, dtype=bool)
    for blk in d.samples:
        rows = np.flatnonzero(np.isin(R.spw, R.spw_codes_matching([blk.spw])))
        if rows.size == 0:
            continue
        ti = _match_sorted(R.times[rows], blk.times, atol=TIME_TOL)
        bi = R.pair_matrix(list(zip(blk.ant1, blk.ant2)))[R.c1[rows], R.c2[rows]]
        if d.extend_chan:
            fi = np.zeros(rows.size, dtype=np.int64)
        else:
            fi = _match_sorted(R.freqs[rows], blk.freqs, rtol=FREQ_RTOL)
        if d.extend_corr:
            pi = 0
        else:
            pl = list(blk.pols)
            if R.pol not in pl:
                continue
            pi = pl.index(R.pol)
        ok = (ti >= 0) & (bi >= 0) & (fi >= 0)
        if not ok.any():
            continue
        grid = blk._dense_cached()
        if d.extend_chan:
            grid = grid.any(axis=2, keepdims=True)
        if d.extend_corr:
            grid = grid.any(axis=3, keepdims=True)
        hit = np.zeros(rows.size, dtype=bool)
        hit[ok] = grid[ti[ok], bi[ok], fi[ok], pi]
        out[rows] |= hit
    return out


def row_delta_mask(d: FlagDelta, R: _Rows) -> np.ndarray:
    return _row_sample_mask(d, R) if d.is_sample_set else _row_region_mask(d, R)


def frame_keep_mask(backend, df, pol, view: str) -> np.ndarray:
    """Rows of a raw frame drawn in flag *view* (see SelectionSpec.flag_view)."""
    disk = df["__disk_flag"].to_numpy(dtype=bool)
    if view == "none":
        return np.ones(len(df), dtype=bool)
    if view == "disk":
        return ~disk
    pend = backend._pending_deltas()
    R = _Rows(backend, df, pol) if (pend or view == "proposal") else None
    eff = disk.copy()
    for d in pend:
        m = row_delta_mask(d, R)
        eff[m] = bool(d.flag)
    if view == "effective":
        return ~eff
    if view == "pending":
        return eff != disk
    if view == "proposal":
        prop = getattr(backend, "_cv_proposal", None)
        if prop is None:
            return np.zeros(len(df), dtype=bool)
        new = eff.copy()
        new[row_delta_mask(prop, R)] = bool(prop.flag)
        return new != eff
    raise ValueError(f"unknown flag view {view!r}")
