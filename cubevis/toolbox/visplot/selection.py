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


AVERAGING_MODES = ("scalar", "vector")
"""Valid values of ``SelectionSpec.averaging``."""

DEFAULT_AVERAGING = "vector"
"""The averaging used when none is given -- the one place to change it.

Vector since 2026-10-05 (it was ``"scalar"`` when the option was
introduced two days earlier): the HRS commissioning users this is being
built for come from AIPS, and AIPS and plotms both average visibilities
vectorially unless told otherwise.  Every default in the package
(``SelectionSpec.averaging``, ``VisibilityRaster``, ``VisibilityPlotter``,
the backends' ``_raster_2d``) refers to this name.
"""


def normalize_averaging(value) -> str:
    """Return *value* as a member of ``AVERAGING_MODES``.

    ``None`` / empty means ``DEFAULT_AVERAGING``; anything else
    unrecognised raises ``ValueError`` naming the valid choices.
    """
    if value is None or value == "":
        return DEFAULT_AVERAGING
    v = str(value).strip().lower()
    if v not in AVERAGING_MODES:
        raise ValueError(
            f"averaging must be one of {AVERAGING_MODES}; got {value!r}")
    return v


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
    averaging:
        How raster cells combine the samples they cover: ``'vector'``
        (default) or ``'scalar'``.  See the field's own docstring.
    detrend:
        Remove a linear phase slope before Phase RMS / Coherence raster
        statistics (default ``True``).  See the field's own docstring.
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
    """``[(ant1_name, ant2_name), ...]``, or ``None`` = all baselines.

    Selects only the exact listed antenna pairs.  To select all baselines
    *involving* one or more antennas (regardless of the other end), use
    ``antenna_names`` instead.
    """

    antenna_names: Optional[list[str]] = None
    """Antenna name strings, or ``None`` = all antennas.

    Selects every baseline for which ``ant1`` OR ``ant2`` is in this list.
    This is the natural PlotMS "select by antenna" operation and is more
    convenient than listing every individual baseline pair.  If both
    ``baselines`` and ``antenna_names`` are set, ``baselines`` takes
    precedence and ``antenna_names`` is ignored.

    Only meaningful for ``MSv2Backend`` and ``MSv4Backend``; not used by
    calibration table readers.
    """

    freq_range: Optional[tuple[float, float]] = None
    """``(f_min, f_max)`` in Hz, or ``None`` = all frequencies."""

    channel_range: Optional[tuple[int, int]] = None
    """``(c_start, c_end)`` integer channel indices (half-open), or ``None``.

    Selects ``ds.isel(frequency=slice(c_start, c_end))``.  Takes
    precedence over ``freq_range`` when both are set; use one or the
    other.  Channel indices are zero-based within the partition's SPW.

    This is more efficient than ``freq_range`` when the plotter knows
    exactly which channel slice it wants (e.g. after a zoom), because
    it avoids scanning the frequency coordinate array.
    """

    correlation: Optional[list[str]] = None
    """Polarization product labels, or ``None`` = all correlations."""

    data_column: str = "DATA"
    """Visibility column: ``'DATA'``, ``'CORRECTED'``, or ``'MODEL'``."""

    averaging: str = DEFAULT_AVERAGING
    """How a raster cell combines the samples it covers (HRS H1, 2026-10).

    * ``"vector"`` (default) -- the complex visibilities are averaged
      first and Amplitude / Phase are taken from that mean.  Amplitude
      then drops where the samples are incoherent (noise, residual delay
      or rate), which is what AIPS and plotms users expect of an averaged
      visibility; Phase is weighted by amplitude.
    * ``"scalar"`` -- Amplitude is the mean of the per-sample amplitudes
      (the only behaviour before this field existed).  Phase is the
      circular mean: the direction of the mean *unit* phasor, every sample
      weighted equally.

    Real and Imaginary are linear, so both modes give the same value.
    Flag fraction and Z-Score ignore this field.  In neither mode is Phase
    the arithmetic mean of wrapped per-sample phases (which gives ~0 deg
    for samples straddling +/-180 deg): that was a bug, not a mode.

    Not a row constraint: excluded from ``is_empty()``, preserved by
    ``copy()``.

    This field is transport, not GUI state.  Averaging is a property of
    each raster panel (``VisibilityRaster.averaging``): the panel stamps
    its own mode onto a copy of the selection just before it queries, so
    two rasters can show the same data averaged differently, and the
    selection the plotter shares between panels never changes because
    of it (scatter frames, which do not depend on averaging, are
    therefore never invalidated by a mode change).  Use
    ``normalize_averaging`` to validate.
    """

    detrend: bool = True
    """Remove a linear phase slope before a Phase RMS / Coherence raster
    statistic is taken (HRS H2, 2026-10).

    ``True`` (default): per cell, the residual delay (slope along
    frequency) and rate (slope along time) are estimated and removed
    along whichever of those dimensions the cell is reduced over, so the
    statistic measures scatter about the slope.  ``False``: the statistic
    is of the data as they are, slope included.  Ignored by every other
    quantity.  See ``data/_raster_stats.py``.

    Like ``averaging`` this is transport, not GUI state: it belongs to
    each raster panel (``VisibilityRaster.detrend``), which stamps it
    onto a copy of the selection at query time.  Not a row constraint.
    """

    stat_time_window: object = "auto"
    """Time window for Phase RMS / Coherence raster statistics (HRS H2
    slice 2, 2026-10): ``"auto"``, ``"off"``, ``"scan"``, or a number of
    seconds.

    The statistic is taken within each window.  Where time is a displayed
    axis, every integration shows its window's value (the grid does not
    change).  Where time is reduced, the windows are pooled into the
    cell, each about its own mean phase and slope.

    * ``"off"``  -- no windowing: one integration per cell where time is
      displayed, the whole selected range where it is reduced.
    * ``"scan"`` -- one window per contiguous run of integrations.
    * seconds    -- each run cut into windows of that length.
    * ``"auto"`` (default) -- ``"off"`` where time is displayed,
      ``"scan"`` where it is reduced, so that scan-to-scan phase jumps
      and changes of source do not read as scatter.

    Windows never span a gap.  Transport, per raster panel, exactly like
    ``detrend``.  See ``data/_raster_stats.py``.
    """

    stat_chan_window: object = "off"
    """Channel window for Phase RMS / Coherence raster statistics:
    ``"off"`` (default) or a number of channels.  Same rules as
    ``stat_time_window``: painted back where frequency is displayed,
    pooled where it is reduced.
    """

    cache_generation: int = 0
    """Data-freshness token -- NOT a constraint on which rows are selected.

    The backend keeps the per-row frames it reads (see
    ``data.reader.XArrayReader._query_columns_cached``) so a pan, zoom or
    recolor does not re-read the data.  A cached frame is only valid for the
    generation it was read under; the plotter bumps this when the user presses
    Reload, so the next query re-reads from disk.  It rides on the selection
    because that object already travels to the backend on every call, locally
    or in a remote worker, so no new call is needed.  Deliberately excluded
    from ``is_empty()`` and from the cache's selection fingerprint (it is
    compared separately), but preserved by ``copy()``.
    """

    pending_version: int = 0
    """Pending-flag freshness token (FlagDB v2) -- NOT a row constraint.

    ``FlagDB.version`` at the time this selection was built.  The backend
    applies pending flags as if they were on disk (``XArrayReader.
    _flag_mask``), so a cached frame is valid only for the pending state it
    was read under; the frame cache compares this together with
    ``cache_generation``.  Excluded from the selection fingerprint and from
    ``is_empty()``, preserved by ``copy()``.
    """

    flag_view: str = "effective"
    """Which flag state the backend renders with (FlagDB v2):

    * ``"effective"`` -- on-disk flags with the pending deltas applied
      (default; pending-flagged samples disappear);
    * ``"disk"`` -- on-disk flags only (pending flags drawn as an overlay);
    * ``"pending"`` -- only the samples whose state the pending deltas
      change are unflagged (used to draw the pending overlay);
    * ``"proposal"`` -- only the samples a proposal under review would
      change (used to draw the proposal overlay);
    * ``"flagged"`` -- only the samples that are flagged now (on disk and/or
      pending), never padding (the "show flagged data" overlay).

    Part of the frame-cache fingerprint (it decides which rows are drawn).
    """

    # ------------------------------------------------------------------ #

    def is_empty(self) -> bool:
        """Return ``True`` if this spec places no constraints at all."""
        return (
            self.field_names is None
            and self.scan is None
            and self.spw is None
            and self.time_range is None
            and self.baselines is None
            and self.antenna_names is None
            and self.freq_range is None
            and self.channel_range is None
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
            antenna_names=list(self.antenna_names) if self.antenna_names is not None else None,
            freq_range=self.freq_range,
            channel_range=self.channel_range,
            correlation=list(self.correlation) if self.correlation is not None else None,
            data_column=self.data_column,
            averaging=self.averaging,
            detrend=self.detrend,
            stat_time_window=self.stat_time_window,
            stat_chan_window=self.stat_chan_window,
            cache_generation=self.cache_generation,
            pending_version=self.pending_version,
            flag_view=self.flag_view,
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
