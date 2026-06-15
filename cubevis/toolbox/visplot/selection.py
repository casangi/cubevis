"""Data-selection specification shared across all visplot layers.

``SelectionSpec`` is the single, portable representation of a data
selection in native MS coordinate terms.  It is used by:

* GUI controls (to reflect the user's current selection),
* ``XArrayReader.query_columns`` / ``query_raster`` (as input),
* ``FlagOperation`` records (to record the coordinate range that was
  flagged), and
* raster-to-scatter mode transfers (the zoom viewport becomes the
  initial scatter selection).

All identifiers are **human-readable strings** — antenna names, field
name strings, scan name strings — never internal integer indices.
``XArrayReader`` translates these to xarray index/mask operations
internally so that no layer above ever needs to know the MSv4 integer
coordinate model.

References
----------
msvis_design.md §4.2 (SelectionSpec), §4.5 (mode transfer), §4.6
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class SelectionSpec:
    """Explicit, portable representation of a data selection.

    All fields default to ``None``, meaning "include everything".
    Partial specifications are valid: set only the fields you wish to
    constrain and leave the rest as ``None``.

    Parameters
    ----------
    field_names:
        Field name strings to include, or ``None`` for all fields.
        Example: ``['3C286', 'J1331+305']``.
    scan:
        Scan name strings to include, or ``None`` for all scans.
        Example: ``['3', '5', '7']``.
        Translated internally to
        ``ds.where(ds.scan_name.isin(selection.scan))``.
    spw:
        Spectral window indices to include, or ``None`` for all SPWs.
    time_range:
        ``(t_min, t_max)`` as MJD seconds, or ``None`` for all times.
    baselines:
        List of ``(ant1_name, ant2_name)`` string pairs, or ``None``
        for all baselines.
        Example: ``[('DV01', 'DV02'), ('DA41', 'DV03')]``.
    freq_range:
        ``(f_min, f_max)`` in Hz, or ``None`` for all frequencies.
    correlation:
        Polarization product labels to include, or ``None`` for all.
        Example: ``['XX', 'YY']``.
    data_column:
        Which visibility column to read: ``'DATA'``, ``'CORRECTED'``,
        or ``'MODEL'``.  Defaults to ``'DATA'``.
    """

    # Selection axes ---------------------------------------------------- #

    field_names: Optional[list[str]] = None
    """Field name strings, or ``None`` = all fields."""

    scan: Optional[list[str]] = None
    """Scan name strings, or ``None`` = all scans."""

    spw: Optional[list[int]] = None
    """SPW indices, or ``None`` = all SPWs."""

    time_range: Optional[tuple[float, float]] = None
    """``(t_min, t_max)`` MJD seconds, or ``None`` = all times."""

    baselines: Optional[list[tuple[str, str]]] = None
    """``[(ant1_name, ant2_name), ...]``, or ``None`` = all baselines."""

    freq_range: Optional[tuple[float, float]] = None
    """``(f_min, f_max)`` in Hz, or ``None`` = all frequencies."""

    correlation: Optional[list[str]] = None
    """Polarization product labels, or ``None`` = all correlations."""

    data_column: str = "DATA"
    """Visibility column: ``'DATA'``, ``'CORRECTED'``, or ``'MODEL'``."""

    # ------------------------------------------------------------------ #

    def is_empty(self) -> bool:
        """Return ``True`` if this spec places no constraints at all."""
        return (
            self.field_names is None
            and self.scan is None
            and self.spw is None
            and self.time_range is None
            and self.baselines is None
            and self.freq_range is None
            and self.correlation is None
        )

    def copy(self) -> "SelectionSpec":
        """Return a shallow copy (list fields are copied, not shared)."""
        return SelectionSpec(
            field_names=list(self.field_names) if self.field_names is not None else None,
            scan=list(self.scan) if self.scan is not None else None,
            spw=list(self.spw) if self.spw is not None else None,
            time_range=self.time_range,
            baselines=list(self.baselines) if self.baselines is not None else None,
            freq_range=self.freq_range,
            correlation=list(self.correlation) if self.correlation is not None else None,
            data_column=self.data_column,
        )

    # ------------------------------------------------------------------ #
    # Convenience constructors                                             #
    # ------------------------------------------------------------------ #

    @classmethod
    def from_time_freq_bounds(
        cls,
        t_min: float,
        t_max: float,
        f_min: float,
        f_max: float,
        data_column: str = "DATA",
    ) -> "SelectionSpec":
        """Construct a spec covering a time/frequency bounding box.

        Useful for creating a ``SelectionSpec`` from a raster viewport
        zoom to pass into scatter mode (§4.5).
        """
        return cls(
            time_range=(t_min, t_max),
            freq_range=(f_min, f_max),
            data_column=data_column,
        )
