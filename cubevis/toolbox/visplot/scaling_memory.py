"""
scaling_memory.py
=================
Per-quantity color-scaling memory for a raster panel, and the state unit that
exposes it to :mod:`view_state` (Part 6 follow-up, 2026-09).

Why this exists
---------------
A raster panel has one live set of scaling settings (scaling function, alpha,
gamma, min/max, ...).  Those settings are only meaningful for the quantity they
were tuned on: a 8-30 range chosen for Amplitude is nonsense for Phase (degrees),
and a threshold cutoff in Z-Score units is nonsense for anything else.  Two
earlier, narrower mechanisms handled this only for Z-Score (a single "pre
Z-Score" slot that also *discarded* anything tuned while on Z-Score).  This
replaces them with the general rule:

    each panel remembers its scaling settings PER QUANTITY.
    Leaving a quantity stores its settings; returning restores them;
    the first visit to a quantity uses that quantity's own default.

Ownership tracking (important)
------------------------------
The plotter resets ``panel._quantity = None`` right before ``update_axes(...)``
(a force-a-change trick), so ``update_axes`` can never learn the *previous*
quantity from ``self._quantity``.  The panel therefore tracks a separate
``_scaling_owner``: the quantity the live settings currently belong to.  All
switching logic keys on that, never on ``self._quantity``.

Z-Score and Phase have special defaults.  ``_QUANTITY_DEFAULTS`` is the one
place to give a quantity its own first-visit default.  Phase got a linear
-180..180 degree range with the cyclic colormap (HRS H3, 2026-10-07); Flag
is left alone.

The unit here (:class:`RasterScalingUnit`) is the first concrete citizen of the
save/restore framework; see ``VIEW_STATE_DESIGN.md``.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Callable, Dict, Optional

from . import colormap_scaling as _cms
from .axes import Axis
from .data._scatter_render import _DEFAULT_ZSCORE_THRESHOLD

DEFAULT_SCALING = "eq_hist"
DEFAULT_ALPHA = 10.0
DEFAULT_GAMMA = 1.0


@dataclass(frozen=True)
class ScalingSettings:
    """One quantity's scaling settings (everything a user can tune in the
    Color scaling controls, plus whether the cutoff is still automatic)."""
    scaling: str = DEFAULT_SCALING
    alpha: float = DEFAULT_ALPHA
    gamma: float = DEFAULT_GAMMA
    vmin: Optional[float] = None
    vmax: Optional[float] = None
    # True while a Z-Score threshold cutoff is the automatic n-aware one (not a
    # value the user typed or dragged). Meaningless (False) elsewhere.
    auto_cutoff: bool = False

    def to_dict(self) -> dict:
        return {"scaling": self.scaling, "alpha": self.alpha, "gamma": self.gamma,
                "vmin": self.vmin, "vmax": self.vmax, "auto_cutoff": self.auto_cutoff}

    @classmethod
    def from_dict(cls, d: dict) -> "ScalingSettings":
        scaling = d.get("scaling", DEFAULT_SCALING)
        if scaling not in _cms.ALL_SCALINGS:
            raise ValueError(f"unknown scaling {scaling!r}")
        def _num(name, default):
            v = d.get(name)
            return default if v is None else float(v)
        return cls(scaling=scaling,
                   alpha=_num("alpha", DEFAULT_ALPHA), gamma=_num("gamma", DEFAULT_GAMMA),
                   vmin=_num("vmin", None), vmax=_num("vmax", None),
                   auto_cutoff=bool(d.get("auto_cutoff", False)))


# First-visit defaults by quantity: name -> function(base) -> ScalingSettings.
# Only Z-Score is special today (see module docstring).
def _zscore_default(base: ScalingSettings) -> ScalingSettings:
    # Provisional per-sample cutoff; VisibilityRaster._apply_zscore_cell_cutoff
    # replaces it with the n-aware per-cell cutoff after the first render.
    return replace(base, scaling="threshold", vmin=_DEFAULT_ZSCORE_THRESHOLD,
                   vmax=None, auto_cutoff=True)


def _phase_default(base: ScalingSettings) -> ScalingSettings:
    # Phase is drawn with a cyclic colormap (palettes.cyclic_cmap), which
    # only means anything if the full circle maps onto the full ramp:
    # linear, fixed at -180..180 degrees.  Histogram equalization would
    # stretch the circle unevenly, and an automatic range would put the
    # seam wherever the data happened to end.
    return replace(base, scaling="linear", vmin=-180.0, vmax=180.0)


_QUANTITY_DEFAULTS: Dict[str, Callable[[ScalingSettings], ScalingSettings]] = {
    Axis.Z_SCORE.name: _zscore_default,
    Axis.PHASE.name: _phase_default,
}


def default_scaling_settings(
    quantity: Axis, alpha: float = DEFAULT_ALPHA, gamma: float = DEFAULT_GAMMA,
) -> ScalingSettings:
    """First-visit settings for *quantity* (alpha/gamma are the panel's
    constructor values, so a panel built with custom ones keeps them)."""
    base = ScalingSettings(alpha=alpha, gamma=gamma)
    special = _QUANTITY_DEFAULTS.get(getattr(quantity, "name", None))
    return special(base) if special else base


class ScalingMemory:
    """Quantity name -> last :class:`ScalingSettings` used for it."""

    def __init__(self, table: Optional[Dict[str, ScalingSettings]] = None) -> None:
        self._table: Dict[str, ScalingSettings] = dict(table or {})

    def remember(self, quantity_name: str, settings: ScalingSettings) -> None:
        self._table[quantity_name] = settings

    def recall(self, quantity_name: str) -> Optional[ScalingSettings]:
        return self._table.get(quantity_name)

    def forget(self, quantity_name: Optional[str] = None) -> None:
        if quantity_name is None:
            self._table.clear()
        else:
            self._table.pop(quantity_name, None)

    def names(self):
        return sorted(self._table)

    def __len__(self) -> int:
        return len(self._table)

    def to_dict(self) -> dict:
        return {name: s.to_dict() for name, s in sorted(self._table.items())}

    @classmethod
    def from_dict(cls, d: dict) -> "ScalingMemory":
        """Tolerant load: an entry that cannot be parsed (unknown scaling
        function from a newer build, a quantity that no longer exists) is
        dropped rather than failing the whole restore."""
        mem = cls()
        for name, raw in (d or {}).items():
            try:
                mem.remember(name, ScalingSettings.from_dict(raw))
            except (ValueError, TypeError, AttributeError):
                continue
        return mem


class RasterScalingUnit:
    """State unit (see :mod:`view_state`) for one raster panel's scaling.

    Key convention: ``panel.<slot id>.raster.scaling``.  Version 1 state::

        {"owner": "AMPLITUDE" | null,          # informational: quantity live
         "by_quantity": {"AMPLITUDE": {...ScalingSettings...}, ...}}

    ``apply`` restores the memory table and loads the settings for the panel's
    CURRENT quantity (the axes unit, ordered earlier, sets the quantity itself),
    then re-shades.  The raster does the work; this class only adapts it.
    """
    version = 1
    order = 50                       # after layout/axes (10-40), before overlays
    scopes = frozenset({"display"})

    def __init__(self, raster, key: str) -> None:
        self.raster = raster
        self.key = key

    def capture(self) -> dict:
        return self.raster.capture_scaling_state()

    def apply(self, state: dict) -> None:
        self.raster.apply_scaling_state(state)
