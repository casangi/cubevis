"""
colormap_scaling.py
====================
Shared value-transfer scaling functions for ``VisibilityRaster`` and
``VisibilityScatter`` colormap controls.

Ported from the interactive_clean ``quantize()`` transfer-function design
(log / sqrt / square / gamma / power), adapted to operate on generic
Datashader aggregation arrays rather than a single 2D image plane.

Motivation
----------
Linear value-to-color mapping saturates badly on real visibility data: a
small high-amplitude population dominates the colormap while the populous
low-amplitude region collapses into a featureless gradient (observed
directly during scatter testing — amplitude vs UVdist rendered as a
near-solid colour field below amplitude ~40, with structure visible only
near the top of the range). This is a value-distribution problem, not a
bit-depth problem; a wider bit depth under the same linear map saturates
identically.

``eq_hist`` (histogram equalization) is Datashader's own data-driven
answer and is the recommended default — see ``DATASHADER_HOW`` below for
how it's selected at the ``tf.shade()`` call site directly. The functions
in this module are the *manual override* path: explicit non-linear
transforms a user can dial in when ``eq_hist`` over- or under-compensates,
or when they specifically want the compressed/expanded range it is not
giving them (e.g. deliberately suppressing the noise floor to emphasise
outliers).

Two scaling mechanisms exist side by side
-------------------------------------------
1. **Datashader's built-in ``how=`` reduction** (``linear``, ``log``,
   ``cbrt``, ``eq_hist``) — applied directly in ``tf.shade(agg, how=...)``.
   No array transform needed; Datashader handles this internally and
   efficiently. This is the default path (``eq_hist``).
2. **Explicit pre-transform via this module** (``sqrt``, ``square``,
   ``gamma``, ``power``, plus this module's own ``log``) — applied to the
   aggregation array *before* calling ``tf.shade(..., how="linear")``.
   Used when the user picks a scaling not covered by Datashader's
   built-ins, or wants explicit alpha/gamma control over the curve shape.

Package location
-----------------
``cubevis/cubevis/toolbox/visplot/colormap_scaling.py``
"""

from __future__ import annotations

import sys
from typing import Callable

import numpy as np

# ---------------------------------------------------------------------------
# Datashader built-in "how" values usable directly in tf.shade(how=...)
# ---------------------------------------------------------------------------

# Maps a user-facing scaling name to the Datashader how= value, for the
# subset of scalings Datashader implements natively *and* that accept a
# span= argument. Used directly at the tf.shade() call site — no array
# pre-transform needed.
#
# "eq_hist" is deliberately NOT included here even though Datashader
# implements it natively: Datashader raises ValueError if span= is passed
# together with how="eq_hist" ("span is not (yet) valid to use with
# eq_hist"), which means there is no way to anchor eq_hist's colour
# mapping to a fixed external range — color_mode="global" would be a
# silent no-op for it. To make global/local meaningful for eq_hist too
# (useful when zoomed in and wanting the option to either lock colours to
# the full data range or let them auto-equalize to the visible crop),
# eq_hist is implemented as an explicit pre-transform instead — see
# ``equalize_histogram`` below — and is classified under
# EXPLICIT_SCALINGS rather than DATASHADER_HOW.
DATASHADER_HOW = {
    "linear":  "linear",
    "log":     "log",
}

# Scaling names handled by explicit array pre-transform in this module.
# Either Datashader has no built-in equivalent, the user wants explicit
# alpha/gamma control over the curve shape, or (eq_hist specifically)
# Datashader's native implementation doesn't support a span= anchor.
EXPLICIT_SCALINGS = ("eq_hist", "sqrt", "square", "gamma", "power")

ALL_SCALINGS = ("linear", "log", "eq_hist", "sqrt", "square", "gamma", "power")


# ---------------------------------------------------------------------------
# Explicit scaling functions
# ---------------------------------------------------------------------------
#
# Each function maps a non-negative numpy array to a non-negative numpy
# array of the same shape, suitable for tf.shade(..., how="linear") after
# the transform. Functions accept **kwargs so a single dispatch table
# (_SCALING_FUNCS below) can be called uniformly regardless of which
# parameters a given scaling uses.
#
#   linear: y = x
#   log:    y = log_(alpha+1)(alpha*x + 1)
#   sqrt:   y = sqrt(x)
#   square: y = x^2
#   gamma:  y = x^gamma
#   power:  y = (alpha^x - 1) / (alpha - 1)

def _scale_linear(x: np.ndarray, **_kwargs) -> np.ndarray:
    return x


def _scale_log(x: np.ndarray, alpha: float = 10.0, **_kwargs) -> np.ndarray:
    alpha = max(alpha, 1e-6)
    return np.log1p(alpha * x) / np.log1p(alpha)


def _scale_sqrt(x: np.ndarray, **_kwargs) -> np.ndarray:
    return np.sqrt(np.clip(x, 0, None))


def _scale_square(x: np.ndarray, **_kwargs) -> np.ndarray:
    return np.square(x)


def _scale_gamma(x: np.ndarray, gamma: float = 1.0, **_kwargs) -> np.ndarray:
    gamma = max(gamma, 1e-6)
    return np.power(np.clip(x, 0, None), gamma)


def _scale_power(x: np.ndarray, alpha: float = 10.0, **_kwargs) -> np.ndarray:
    alpha = alpha if alpha > 0 and alpha != 1.0 else 1.0 + 1e-6
    return (np.power(alpha, x) - 1.0) / (alpha - 1.0)


def equalize_histogram(
    values: np.ndarray,
    *,
    reference: np.ndarray | None = None,
    nbins: int = 256 * 256,
) -> np.ndarray:
    """Histogram-equalize *values*, optionally against a different array.

    Reimplements Datashader's own ``eq_hist`` algorithm (histogram, then
    map each value through the cumulative distribution via
    ``np.interp``) rather than calling ``tf.shade(..., how="eq_hist")``
    directly, because Datashader rejects ``span=`` for ``"eq_hist"``
    (raises ``ValueError: span is not (yet) valid to use with eq_hist``)
    — there is no way to anchor its colour mapping to an external range
    through the public API. Reimplementing it here as an explicit
    pre-transform restores that capability: the *reference* parameter
    lets the CDF be built from a different array (e.g. the full cached
    aggregation) than the one being mapped (e.g. the current viewport
    crop), which is exactly what ``color_mode="global"`` needs.

    Parameters
    ----------
    values : np.ndarray
        The array to equalize (e.g. the current viewport crop's agg
        values). May contain NaN for empty cells.
    reference : np.ndarray | None
        The array whose distribution defines the equalization curve.
        ``None`` (default) uses ``values`` itself — equivalent to
        Datashader's native ``how="eq_hist"`` behaviour, i.e. "local"
        mode where the mapping always matches what's currently visible.
        Pass the full cached aggregation's values here for "global" mode
        — colours stay anchored to the full data's distribution
        regardless of how far zoomed in the viewport is.
    nbins : int
        Histogram bin count, matching Datashader's own default.

    Returns
    -------
    np.ndarray
        Same shape as *values*, in ``[0, 1]``. NaN values pass through
        unchanged. Suitable for ``tf.shade(..., how="linear",
        span=[0, 1])``.
    """
    ref = reference if reference is not None else values
    ref_finite = ref[np.isfinite(ref)]
    if ref_finite.size == 0:
        return values  # nothing to equalize against; pass through

    hist, bin_edges = np.histogram(ref_finite, bins=nbins)
    bin_centers = (bin_edges[:-1] + bin_edges[1:]) / 2.0
    keep = hist > 0
    if not np.any(keep):
        return values
    hist = hist[keep]
    bin_centers = bin_centers[keep]

    cdf = hist.cumsum().astype(np.float64)
    cdf /= cdf[-1]

    finite_mask = np.isfinite(values)
    out = np.full(values.shape, np.nan, dtype=np.float64)
    if np.any(finite_mask):
        out[finite_mask] = np.interp(
            values[finite_mask], bin_centers, cdf,
            left=cdf[0], right=cdf[-1],
        )
    return out


_SCALING_FUNCS: dict[str, Callable[..., np.ndarray]] = {
    "linear": _scale_linear,
    "log":    _scale_log,
    "sqrt":   _scale_sqrt,
    "square": _scale_square,
    "gamma":  _scale_gamma,
    "power":  _scale_power,
}


def apply_explicit_scaling(
    values: np.ndarray,
    scaling: str,
    *,
    alpha: float = 10.0,
    gamma: float = 1.0,
    vmin: float | None = None,
    vmax: float | None = None,
) -> np.ndarray:
    """Apply an explicit (non-Datashader-native) scaling transform.

    Parameters
    ----------
    values : np.ndarray
        Raw aggregation values (may contain NaN for empty cells).
    scaling : str
        One of ``EXPLICIT_SCALINGS`` or ``"linear"``.
    alpha : float
        Used by ``log`` and ``power`` scalings.
    gamma : float
        Used by ``gamma`` scaling.
    vmin, vmax : float | None
        Optional manual clip range applied before scaling. ``None`` means
        use the array's own finite min/max.

    Returns
    -------
    np.ndarray
        Transformed array, same shape as ``values``, normalised to
        ``[0, 1]`` so it can be passed to ``tf.shade(..., how="linear")``
        with a ``span=[0, 1]`` and get a full-range colormap regardless
        of the original value distribution.

    Notes
    -----
    NaN values pass through unchanged (Datashader's shade treats NaN as
    transparent/missing, which is the desired behaviour for empty cells).
    """
    if scaling not in _SCALING_FUNCS:
        print(
            f"colormap_scaling: unknown scaling {scaling!r}, using 'linear'",
            file=sys.stderr,
        )
        scaling = "linear"

    finite = values[np.isfinite(values)]
    if finite.size == 0:
        return values

    lo = float(finite.min()) if vmin is None else float(vmin)
    hi = float(finite.max()) if vmax is None else float(vmax)
    if hi <= lo:
        hi = lo + 1.0

    clipped = np.clip(values, lo, hi)
    normalised = (clipped - lo) / (hi - lo)   # -> [0, 1], NaN stays NaN

    func = _SCALING_FUNCS[scaling]
    scaled = func(normalised, alpha=alpha, gamma=gamma)

    # Re-normalise the transform's own output range to [0, 1] so the
    # colormap span is always exactly [0, 1] regardless of which curve
    # was applied (e.g. square compresses toward 0, sqrt expands toward 1).
    s_finite = scaled[np.isfinite(scaled)]
    if s_finite.size == 0:
        return scaled
    s_lo, s_hi = float(s_finite.min()), float(s_finite.max())
    if s_hi <= s_lo:
        return scaled
    return (scaled - s_lo) / (s_hi - s_lo)


def scaling_equation_label(scaling: str) -> str:
    """Return a short human-readable equation string for UI display.

    Used by ``colormap_controls()`` to show the active transform next to
    the scaling dropdown, mirroring the interactive_clean MathML display
    in plain-text form (good enough for a Bokeh ``Div``; MathML rendering
    can be added later without changing this module's contract).
    """
    return {
        "linear":  "y = x",
        "log":     "y = log_(a+1)(a*x + 1)",
        "eq_hist": "y = histogram-equalized(x)",
        "sqrt":    "y = sqrt(x)",
        "square":  "y = x^2",
        "gamma":   "y = x^g",
        "power":   "y = (a^x - 1) / (a - 1)",
    }.get(scaling, scaling)
