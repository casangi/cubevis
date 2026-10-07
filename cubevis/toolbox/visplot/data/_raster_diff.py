"""
_raster_diff.py
===============
Difference-from-running-mean raster quantities (HRS H2, 2026-10-07).

What they are
-------------
Two per-sample quantities, the counterparts of the AIPS TVFLG / SPFLG
display modes of the same names:

``AMP_VDIFF``   ``|V - M|``: the amplitude of the vector difference
                between a sample and the vector mean ``M`` of the other
                samples in its time window.
``PHASE_DIFF``  ``|arg(V conj(M))|`` in degrees, 0 to 180: how far the
                sample's phase is from the phase of that mean.

Amplitude and Phase show what is there; these show what *changed*.  A
steady source, a bandpass shape or a constant phase offset all subtract
out, and what is left is short-lived: an interference burst, a phase
jump, one bad integration.  That is the AIPS CookBook's advice too:
amplitude finds long-lived problems, the vector difference short ones.

The mean
--------
``M`` is taken along **time**, separately for every baseline, channel
and polarization, within a window: a scan by default, or a number of
seconds (the same *Time window* control the Phase RMS quantity uses,
and the same ``time_blocks``, so a window never spans a scan boundary).

``M`` leaves the sample itself out (the mean of the *other* samples in
the window).  Including it biases every difference low by ``1 - 1/n``
and, worse, lets a single strong outlier pull the mean toward itself
and hide.  A window with fewer than two unflagged samples has no mean
to compare with and gives NaN.

Windows are blocks, not a sliding buffer, like the Phase RMS windows:
the reference steps at window edges.  AIPS uses a rolling buffer
centred on each sample; the difference matters only when the source
itself changes on the scale of one window.

After this module the caller reduces the per-sample values to the two
displayed axes with a plain mean, like any other per-sample quantity
(both are non-negative, so nothing cancels).

Package location
----------------
``cubevis/cubevis/toolbox/visplot/data/_raster_diff.py``
"""

from __future__ import annotations

import warnings

import numpy as np
import xarray as xr

from ..axes import Axis
from ._raster_stats import _scan_labels, normalize_time_window, time_blocks

DIFF_QUANTITIES = (Axis.AMP_VDIFF, Axis.PHASE_DIFF)


def resolve_diff_window(window):
    """The time window the reference mean is taken over.

    ``"auto"`` and ``"off"`` both mean a scan: there is no difference
    from a mean without a window to take the mean over, and a mean
    across scans would compare one source with another.
    """
    window = normalize_time_window(window)
    return "scan" if window in ("auto", "off") else window


def describe_diff_window(window) -> str:
    """Short text for a title: ``"vs scan mean"``, ``"vs 60 s mean"``."""
    window = resolve_diff_window(window)
    if window == "scan":
        return "vs scan mean"
    return f"vs {window:g} s mean"


def _diff_kernel(z: np.ndarray, *, blocks, want_phase: bool) -> np.ndarray:
    """*z*: complex, time last, NaN where flagged.  Returns float64 of
    the same shape."""
    out = np.full(z.shape, np.nan, dtype=np.float64)
    for a, b in blocks:
        seg = z[..., a:b]
        ok = np.isfinite(seg.real) & np.isfinite(seg.imag)
        n = ok.sum(axis=-1, keepdims=True)
        total = np.where(ok, seg, 0).sum(axis=-1, keepdims=True)
        with np.errstate(invalid="ignore", divide="ignore"):
            # Mean of the OTHER samples in the window.
            ref = (total - np.where(ok, seg, 0)) / (n - 1)
            if want_phase:
                val = np.abs(np.angle(seg * np.conj(ref))) * (180.0 / np.pi)
                # A zero reference has no direction.
                val = np.where(np.abs(ref) > 0, val, np.nan)
            else:
                val = np.abs(seg - ref)
        out[..., a:b] = np.where(ok & (n >= 2), val, np.nan)
    return out


def diff_from_window_mean(vis: xr.DataArray, flag: xr.DataArray,
                          quantity: Axis, time_window="auto") -> xr.DataArray:
    """Per-sample ``AMP_VDIFF`` or ``PHASE_DIFF``, same shape as *vis*,
    lazily if *vis* is lazy.  Flagged samples are NaN and do not
    contribute to any mean."""
    if quantity not in DIFF_QUANTITIES:
        raise ValueError(f"not a difference quantity: {quantity!r}")
    if "time" not in vis.dims:
        raise ValueError("difference from a running mean needs a time axis")
    blocks = time_blocks(vis.coords["time"], resolve_diff_window(time_window),
                         _scan_labels(vis))
    z = vis.where(~flag)
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore", message="Casting complex values to real")
        out = xr.apply_ufunc(
            _diff_kernel, z,
            input_core_dims=[["time"]],
            output_core_dims=[["time"]],
            kwargs=dict(blocks=tuple(blocks),
                        want_phase=(quantity == Axis.PHASE_DIFF)),
            dask="parallelized",
            output_dtypes=[np.float64],
            dask_gufunc_kwargs=dict(allow_rechunk=True),
        )
    return out.transpose(*vis.dims)
