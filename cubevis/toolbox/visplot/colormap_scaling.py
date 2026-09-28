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
from dataclasses import dataclass
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
#
# "threshold" (Part 6, 2026-09 -- visplot-colorize-by-axis-design.md
# §7.6/§7.10): everything under a cutoff renders neutrally, everything
# at/above it in one unmissable color -- a first-class alternative to a
# continuous gradient, motivated by rflag's own default being
# threshold-based (a flag/no-flag decision, not a graded severity), and
# by reading faster for "spot the problem" than a smooth ramp. Grouped
# under EXPLICIT_SCALINGS (Datashader has no native step-function "how="
# to delegate to), but unlike every other member of that tuple it does
# NOT go through apply_explicit_scaling's shared clip-normalize-
# transform-renormalize pipeline -- see that function's own early-exit
# branch for why: a threshold's cutoff is a single, meaningful ABSOLUTE
# value in data units (e.g. "Z-Score >= 5"), the same convention rflag's
# own timedevscale/freqdevscale already use, not a position relative to
# whatever the current view's min/max happen to be -- clipping to
# [vmin, vmax] first, the way every smooth curve here needs to, would
# discard exactly the "is this above or below vmin" distinction the
# whole scaling exists to draw.
EXPLICIT_SCALINGS = ("eq_hist", "sqrt", "square", "gamma", "power", "threshold")

ALL_SCALINGS = ("linear", "log", "eq_hist", "sqrt", "square", "gamma", "power", "threshold")


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


def _scale_threshold_binary(values: np.ndarray, cutoff: float) -> np.ndarray:
    """``1.0`` where ``values >= cutoff``, ``0.0`` elsewhere, NaN preserved.

    Deliberately NOT one of the curves in ``_SCALING_FUNCS`` above, and
    not dispatched through ``apply_explicit_scaling``'s shared clip-
    normalise-transform-renormalise pipeline (see that function's own
    early-exit branch, and ``EXPLICIT_SCALINGS``' docstring, for why):
    every other curve there takes an input already clipped to
    ``[vmin, vmax]`` and normalised to ``[0, 1]``, which would collapse
    a threshold's cutoff to a fixed, meaningless position (0.0) instead
    of leaving it as the absolute data-space value it needs to stay.
    """
    out = np.where(values >= cutoff, 1.0, 0.0)
    out[~np.isfinite(values)] = np.nan
    return out


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


# ---------------------------------------------------------------------------
# eq_hist curve/LUT split (scatter two-level rendering, 2026-09)
# ---------------------------------------------------------------------------
#
# ``equalize_histogram`` above does two things in one call: (a) build a CDF
# from *reference* (expensive -- a np.histogram over the whole selection's
# raw y-values), then (b) map *values* through that CDF via np.interp
# (cheaper, but not free, and re-run on every call). The two-level
# rendering handoff notes' §2.2 measured (a) as viewport-independent in
# color_mode="global" -- the reference population never changes across a
# pan/zoom -- so it can be built ONCE, at reference-build time, and reused
# for every subsequent Level-1 resample. (b) still runs every call (its
# input, the resampled agg, is genuinely viewport-dependent) but was cut
# from a 27.5ms np.interp binary search to a 6.4ms direct lookup by
# swapping the search for a uniform-bin integer index -- safe specifically
# because ``build_equalize_curve`` bins over a fixed [vmin, vmax] (unlike
# ``equalize_histogram``'s ``np.histogram(ref_finite, bins=nbins)``, whose
# implicit range is ref_finite's own min/max -- the same thing, just made
# explicit here so the apply side can recover a bin index arithmetically
# instead of searching for it).
#
# Deliberately NOT wired into ``equalize_histogram`` itself: that function
# is the existing Level-2 exact path (a fresh call per render, ``nbins=
# 256*256`` by default) and this split exists for a different call pattern
# (build once, apply many times, ``nbins=4096`` -- verified in the handoff
# notes to cost the same 41-52ms to build regardless of nbins in this
# range, while shrinking the LUT itself and keeping per-apply cost low).
# Changing ``equalize_histogram``'s own behavior was never part of this
# work and risks regressing the Level-2 path's existing, tested output.

@dataclass(frozen=True)
class EqualizeCurve:
    """A precomputed CDF lookup table for repeated eq_hist mapping against
    one fixed *reference* population.

    ``cdf_lut[i]`` is the equalized value for inputs falling in the i-th of
    ``len(cdf_lut)`` equal-width bins spanning ``[vmin, vmax]`` -- see
    ``apply_equalize_curve`` for the index arithmetic this implies.
    """
    vmin:    float
    vmax:    float
    cdf_lut: np.ndarray   # shape (nbins,), float64, in [0, 1], nondecreasing


def build_equalize_curve(
    reference: np.ndarray, *, nbins: int = 4096,
) -> "EqualizeCurve | None":
    """Build an :class:`EqualizeCurve` from *reference*'s distribution.

    ``None`` when *reference* has no finite values (nothing to equalize
    against) or is a single repeated value (``vmax <= vmin`` -- a curve
    would divide by zero on apply) -- callers should treat this the same
    way ``equalize_histogram`` treats an all-NaN/degenerate reference:
    pass the input through unchanged (see ``apply_equalize_curve``).
    """
    ref_finite = reference[np.isfinite(reference)]
    if ref_finite.size == 0:
        return None
    vmin = float(ref_finite.min())
    vmax = float(ref_finite.max())
    if not (vmax > vmin):
        return None
    hist, _ = np.histogram(ref_finite, bins=nbins, range=(vmin, vmax))
    cdf = hist.cumsum().astype(np.float64)
    total = cdf[-1]
    if total <= 0:
        return None
    cdf /= total
    return EqualizeCurve(vmin=vmin, vmax=vmax, cdf_lut=cdf)


def apply_equalize_curve(
    values: np.ndarray, curve: "EqualizeCurve | None",
) -> np.ndarray:
    """Map *values* through *curve* via a direct integer-indexed lookup.

    Same output convention as ``equalize_histogram``: same shape as
    *values*, in ``[0, 1]``, NaN passed through unchanged, suitable for
    ``tf.shade(..., how="linear", span=[0, 1])``. ``curve=None`` (a
    degenerate reference -- see ``build_equalize_curve``) returns an
    all-NaN array of *values*' shape rather than raising, matching how a
    fully-empty layer renders elsewhere in this pipeline (nothing to draw,
    not an error).

    Values outside ``[curve.vmin, curve.vmax]`` clip to the nearest end
    bin, mirroring ``np.interp``'s ``left=``/``right=`` clamping in
    ``equalize_histogram`` -- a resampled Level-1 value can legitimately
    fall slightly outside the reference's own observed range (e.g. a
    "nearest" upsample duplicating an edge cell), and clamping is the same
    conservative choice already made there.
    """
    out = np.full(values.shape, np.nan, dtype=np.float64)
    if curve is None:
        return out
    finite = np.isfinite(values)
    if not np.any(finite):
        return out
    nbins = curve.cdf_lut.shape[0]
    span = curve.vmax - curve.vmin
    idx = np.clip(
        ((values[finite] - curve.vmin) / span * nbins).astype(np.int64),
        0, nbins - 1,
    )
    out[finite] = curve.cdf_lut[idx]
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
        use the array's own finite min/max. For ``scaling="threshold"``,
        *vmin* alone is the cutoff (an absolute data-space value, e.g.
        "Z-Score >= 5" -- the same convention rflag's own
        timedevscale/freqdevscale already use); *vmax* is not used by
        threshold at all. ``None`` falls back to the array's own finite
        minimum, matching every other scaling's own vmin fallback.

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
    if scaling == "threshold":
        # Early exit, before the "is scaling known" check and the shared
        # pipeline below -- see _scale_threshold_binary's own docstring
        # for why this can't go through clip-normalise-transform-
        # renormalise the way every other explicit scaling does.
        finite = values[np.isfinite(values)]
        cutoff = float(vmin) if vmin is not None else (
            float(finite.min()) if finite.size else 0.0
        )
        return _scale_threshold_binary(values, cutoff)

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


# ---------------------------------------------------------------------------
# ScalarMapping — value <-> colour-position, as an object
# ---------------------------------------------------------------------------

class ScalarMapping:
    """The value-to-colour-position curve of a shade, made inspectable.

    ``apply_explicit_scaling`` and ``equalize_histogram`` transform an
    array and discard the curve they used.  That is all a shade needs,
    but a *colorbar* needs the curve itself: to place a tick at 0.5 Jy it
    must know where 0.5 lands in [0, 1], and to label a position it must
    invert.  This class captures the curve as a monotonic lookup table so
    both directions are available.

    One LUT covers every scaling, ``eq_hist`` included.  For eq_hist the
    table is the histogram CDF (which is exactly what
    ``equalize_histogram`` already interpolates against); for the
    explicit transforms it is the composed clip-normalise-transform-
    renormalise pipeline sampled on a fine grid.  Sampling rather than
    inverting analytically keeps one code path and stays correct if a new
    curve is added to ``_SCALING_FUNCS``.

    ``forward`` and ``inverse`` accept scalars or arrays and are directly
    usable as ``matplotlib.colors.FuncNorm((forward, inverse))``, which
    is what gives an exported colorbar correctly-placed ticks in data
    units under a non-linear scaling.

    Attributes
    ----------
    vmin, vmax : float
        The *effective* data range the curve spans — after any manual
        clip, and after ``color_mode`` has decided whether the reference
        was the full aggregation or just the viewport.  Not the same as
        ``ColorBand.vmin``/``vmax``, which are the user's overrides and
        are often ``None``.
    scaling : str
        Which curve this is, for labelling.
    """

    __slots__ = ("vmin", "vmax", "scaling", "_x", "_u")

    def __init__(self, x: np.ndarray, u: np.ndarray, scaling: str) -> None:
        # Enforce strict monotonicity: np.interp needs an increasing xp,
        # and a CDF plateau (a value range with no samples) would
        # otherwise make the inverse ambiguous.  Dropping duplicate u
        # keeps the first x of each plateau, which is the conventional
        # choice and keeps ticks inside the populated range.
        keep = np.concatenate(([True], np.diff(u) > 0))
        self._x = np.asarray(x, dtype=np.float64)[keep]
        self._u = np.asarray(u, dtype=np.float64)[keep]
        self.vmin = float(self._x[0])
        self.vmax = float(self._x[-1])
        self.scaling = scaling

    # -- construction ----------------------------------------------------

    @classmethod
    def from_values(
        cls,
        values: np.ndarray,
        scaling: str,
        *,
        reference: np.ndarray | None = None,
        alpha: float = 10.0,
        gamma: float = 1.0,
        vmin: float | None = None,
        vmax: float | None = None,
        nsamples: int = 512,
    ) -> "ScalarMapping | None":
        """Build the mapping a shade of *values* under *scaling* would use.

        *reference* mirrors ``equalize_histogram``: pass the full cached
        aggregation for ``color_mode="global"`` so the curve is anchored
        to the whole dataset rather than to the current viewport.

        Returns ``None`` when there is nothing to map (no finite values,
        or a degenerate range), which callers should treat as "draw no
        colorbar" rather than as an error.
        """
        ref = reference if reference is not None else values
        finite = np.asarray(ref)[np.isfinite(ref)]
        if finite.size == 0:
            return None

        if scaling == "threshold":
            # vmin here is the CUTOFF (see apply_explicit_scaling's own
            # docstring), not a clip-range lower bound the way it is for
            # every other scaling below -- the curve must span the
            # reference's FULL range regardless of where the cutoff
            # sits, so it represents both "below" and "at/above" the
            # decision boundary. Using vmin as the grid's own lower
            # bound (the convention every other scaling here uses) would
            # only ever sample the "at/above" side, since a threshold's
            # vmin is a decision boundary, not a display-range edge --
            # found by testing this exact case directly (forward(4.0)
            # wrongly returned 1.0 for a cutoff of 5.0), not by
            # inspection. vmax is genuinely unused by threshold (see
            # apply_explicit_scaling's own docstring), so it's ignored
            # here too, deliberately, not merely unread.
            cutoff = float(vmin) if vmin is not None else float(finite.min())
            grid_lo, grid_hi = float(finite.min()), float(finite.max())
            if grid_hi <= grid_lo:
                return None
            cutoff = min(max(cutoff, grid_lo), grid_hi)
            span = grid_hi - grid_lo
            eps = span * 1e-6
            x1 = cutoff - eps
            x2 = cutoff + eps
            if not (grid_lo < x1 < x2 < grid_hi):
                # Degenerate: cutoff at/beyond an edge (or a vanishingly
                # small span leaves no room for the bracketing pair).
                # Second try attempted directly, not assumed: a naive
                # 2-point [grid_lo, grid_hi] curve with u=[0,1] would
                # SEEM like a reasonable fallback here too, but sampling
                # apply_explicit_scaling on it (the same way the non-
                # threshold branch below does) confirmed it reintroduces
                # exactly the linear-ramp bug this whole branch exists to
                # avoid -- e.g. cutoff==grid_lo means EVERY value in
                # range is >= cutoff, so the curve must be constant 1.0,
                # not a ramp from 0 to 1.
                #
                # Known narrow imprecision, accepted rather than chased
                # further: when cutoff lands exactly ON grid_hi, this
                # flags the whole range "not highlighted" (u=0), even
                # though the single point at exactly grid_hi should read
                # as "at/above" by the inclusive ">=" convention
                # apply_explicit_scaling itself uses (confirmed that
                # function IS exact at this exact boundary -- only this
                # degenerate colorbar-curve fallback has the gap, not the
                # actual per-pixel rendering). Narrow enough (a single
                # exact-maximum value's own colorbar/tooltip reading) not
                # to warrant more branching here.
                everything_above = cutoff <= grid_lo
                flat_u = 1.0 if everything_above else 0.0
                return cls(np.array([grid_lo, grid_hi]),
                          np.array([flat_u, flat_u]), scaling)
            # Four strictly x- AND u-increasing points -- guaranteed to
            # ALL survive __init__'s "keep where u strictly increases"
            # monotonicity filter (no reliance on distinct-enough x
            # values the way a naive construction would need), giving a
            # near-vertical transition confined to the tiny [x1, x2]
            # window right at the cutoff instead of a shallow ramp
            # spanning the entire [grid_lo, cutoff] range the way a
            # plain 2-point curve does (confirmed directly: forward(v)
            # for v well below the cutoff showed a materially nonzero
            # "partway toward highlighted" result with that naive
            # version).
            xs = np.array([grid_lo, x1, x2, grid_hi])
            us = np.array([0.0, 1e-6, 1.0 - 1e-6, 1.0])
            return cls(xs, us, scaling)

        lo = float(finite.min()) if vmin is None else float(vmin)
        hi = float(finite.max()) if vmax is None else float(vmax)
        if not (np.isfinite(lo) and np.isfinite(hi)) or hi <= lo:
            return None

        if scaling == "eq_hist":
            # Reuse the shade's own curve rather than a parallel one:
            # sample equalize_histogram on a grid spanning [lo, hi].
            grid = np.linspace(lo, hi, nsamples)
            u = equalize_histogram(grid, reference=finite)
            if not np.any(np.isfinite(u)):
                return None
            return cls(grid, u, scaling)

        grid = np.linspace(lo, hi, nsamples)
        u = apply_explicit_scaling(
            grid, scaling, alpha=alpha, gamma=gamma, vmin=lo, vmax=hi,
        )
        if not np.any(np.isfinite(u)):
            return None
        return cls(grid, u, scaling)

    # -- the two directions ---------------------------------------------

    @property
    def curve(self) -> tuple[np.ndarray, np.ndarray]:
        """(x, u) sample arrays underlying forward()/inverse().

        Exposed so a caller on the other side of a process/wire boundary
        (the scatter remote render path -- see
        ``cubevis.toolbox.visplot.data._scatter_render``) can reconstruct
        an equivalent ``ScalarMapping`` via
        ``ScalarMapping(x, u, scaling)`` without recomputing it against
        data it doesn't have.
        """
        return self._x, self._u

    def forward(self, v):
        """Data value(s) -> colour position in [0, 1]."""
        return np.interp(v, self._x, self._u,
                         left=self._u[0], right=self._u[-1])

    def inverse(self, u):
        """Colour position in [0, 1] -> data value(s)."""
        return np.interp(u, self._u, self._x,
                         left=self._x[0], right=self._x[-1])

    # -- convenience -----------------------------------------------------

    def ticks(self, n: int = 6) -> np.ndarray:
        """*n* data values evenly spaced along the *colour* axis.

        Even spacing in colour, not in value, is what makes a non-linear
        colorbar readable: under eq_hist the ticks bunch up where the
        data is dense, which is exactly the information the scaling was
        chosen to expose.
        """
        return self.inverse(np.linspace(0.0, 1.0, max(2, n)))

    def __repr__(self) -> str:                      # pragma: no cover
        return (f"ScalarMapping(scaling={self.scaling!r}, "
                f"vmin={self.vmin:.6g}, vmax={self.vmax:.6g}, "
                f"n={len(self._x)})")


def scaling_equation_label(scaling: str) -> str:
    """Return a short human-readable equation string for UI display.

    Used by ``colormap_controls()`` to show the active transform next to
    the scaling dropdown, mirroring the interactive_clean MathML display
    in plain-text form (good enough for a Bokeh ``Div``; MathML rendering
    can be added later without changing this module's contract).
    """
    return {
        "linear":    "y = x",
        "log":       "y = log_(a+1)(a*x + 1)",
        "eq_hist":   "y = histogram-equalized(x)",
        "sqrt":      "y = sqrt(x)",
        "square":    "y = x^2",
        "gamma":     "y = x^g",
        "power":     "y = (a^x - 1) / (a - 1)",
        "threshold": "y = 1 if x >= cutoff else 0",
    }.get(scaling, scaling)
