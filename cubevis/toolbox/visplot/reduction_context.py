"""reduction_context.py
=======================
``ReductionContext`` — abstract interface for calibration, flagging, and
data manipulation operations.

This module defines:

1. **``ReductionBackend``** — ``str`` enum that selects which
   ``ReductionContext`` implementation ``open_ms`` / ``open_ps`` should
   construct (``"auto"``, ``"casa6"``, ``"radps"``, ``"remote"``,
   ``"null"``).  Accepting plain strings makes the astronomer-facing
   constructor ergonomic without requiring an explicit import.

2. **Data Transfer Objects (DTOs)** — typed dataclasses that cross the
   ``ReductionContext`` boundary.  These are deliberately free of CASA-,
   RADPS-, or cluster-specific types so that every implementation speaks
   the same language.

3. **``ReductionContext``** — abstract base class (ABC).  Concrete
   subclasses implement the methods for a specific execution environment.

4. **``NullReductionContext``** — a no-op implementation used when no
   reduction backend is available.  Display and flagging accumulation work
   normally; calibration buttons in ``VisibilityPlotter`` are simply
   disabled when the active context is a ``NullReductionContext``.

Relationship to ``VisibilityReader``
-------------------------------------
``ReductionContext`` and ``VisibilityReader`` are **sibling** interfaces,
both held by ``VisibilityPlotter``.  For local sessions they are separate
objects.  For remote sessions, a single ``RemoteReductionContext`` object
satisfies *both* interfaces — it is passed as both the ``reader`` and
``context`` arguments to ``VisibilityPlotter``::

    # Local
    reader  = LocalVisibilityReader(MSv2Backend(path))
    context = Casa6ReductionContext(path)
    plotter = VisibilityPlotter(metadata, reader, context, flag_db)

    # Remote — one object fills both roles
    remote  = RemoteReductionContext(endpoint="slurm://cluster/data.ms")
    plotter = VisibilityPlotter(metadata, remote, remote, flag_db)

``RemoteReductionContext`` achieves this by inheriting from
``ReductionContext`` *and* implementing the ``VisibilityReader`` protocol.
The ``VisibilityReader`` methods dispatch ``query_raster`` / ``query_columns``
to the remote worker; the ``ReductionContext`` methods dispatch calibration
and flagging tasks.

Concrete implementations (present and future)
---------------------------------------------
``NullReductionContext``
    Defined here.  No-op for all operations.

``Casa6ReductionContext`` (future — ``casa6_reduction_context.py``)
    Wraps ``casatasks``.  ``commit_flags`` calls ``flagdata()``.
    ``bandpass`` calls ``casatasks.bandpass()``.  ``list_fields`` etc.
    delegate to the MSv2Backend ``metadata()`` call via a held reference
    to the open backend.

``RadpsReductionContext`` (future — ``radps_reduction_context.py``)
    Calls RADPS / AstroVIPER task equivalents.  ``commit_flags`` writes
    to the MSv4 Zarr flag arrays.

``RemoteReductionContext`` (future — ``remote_reduction_context.py``)
    Serialises ``ReductionOperation`` objects and dispatches to a remote
    endpoint (HTTP, gRPC, or Dask distributed).  Returns real
    ``concurrent.futures.Future`` objects from ``submit()``.  Also
    implements ``VisibilityReader`` — remote query results (small agg
    arrays) are returned over the same transport.

Package location
----------------
``cubevis/cubevis/toolbox/visplot/reduction_context.py``
"""

from __future__ import annotations

import abc
import logging
from concurrent.futures import Future
from dataclasses import dataclass, field as dc_field
from enum import Enum
from typing import Any, Optional

log = logging.getLogger(__name__)


# ======================================================================
# ReductionBackend — context selection enum
# ======================================================================

class ReductionBackend(str, Enum):
    """Selects which ``ReductionContext`` implementation ``open_ms`` /
    ``open_ps`` should construct.

    Inherits from ``str`` so that plain strings are accepted wherever a
    ``ReductionBackend`` is expected — ``"casa6"`` and
    ``ReductionBackend.CASA6`` are identical at every call site.

    Members
    -------
    AUTO
        Probe in priority order and use the best available backend.

        * ``open_ms``: try ``casatasks`` first, then RADPS, then
          ``NullReductionContext``.
        * ``open_ps``: try RADPS, then ``NullReductionContext``.
          (``casatasks`` is never probed for MSv4/PS — CASA6 has no
          MSv4 write path and cannot commit flags to a Processing Set.)
    CASA6
        Require ``casatasks``; raise ``RuntimeError`` if not importable.
        Only valid for ``open_ms`` — passing ``CASA6`` to ``open_ps``
        raises ``ValueError`` immediately.
    RADPS
        Require RADPS / AstroVIPER; raise ``RuntimeError`` if not
        available.  Valid for both ``open_ms`` and ``open_ps``.
    REMOTE
        Use ``RemoteReductionContext``; requires ``remote_endpoint`` to
        be supplied to the factory function.  Valid for both functions.
        (``RemoteReductionContext`` is not yet implemented; this path
        raises ``NotImplementedError`` for the preview release.)
    NULL
        Explicitly construct ``NullReductionContext`` without probing.
        Useful for display-only sessions where probe latency is
        undesirable, or to suppress the auto-detection warning log when
        no backend is expected.
    """
    AUTO   = "auto"
    CASA6  = "casa6"
    RADPS  = "radps"
    REMOTE = "remote"
    NULL   = "null"


# ======================================================================
# Data Transfer Objects
# ======================================================================

# ---------------------------------------------------------------------- #
# Observation metadata DTOs                                               #
# ---------------------------------------------------------------------- #

@dataclass(frozen=True)
class FieldInfo:
    """Metadata for a single field (source / pointing)."""
    field_id:   int
    name:       str
    ra_deg:     Optional[float] = None  # J2000 right ascension, degrees
    dec_deg:    Optional[float] = None  # J2000 declination, degrees
    intent:     str             = ""    # e.g. "OBSERVE_TARGET"


@dataclass(frozen=True)
class SpwInfo:
    """Metadata for a single spectral window."""
    spw_id:         int
    centre_freq_hz: float
    bandwidth_hz:   float
    n_channels:     int
    polarizations:  tuple[str, ...]   # e.g. ("XX", "YY")
    name:           str = ""


@dataclass(frozen=True)
class AntennaInfo:
    """Metadata for a single antenna."""
    antenna_id:  int
    name:        str
    station:     str  = ""
    diameter_m:  Optional[float] = None


@dataclass(frozen=True)
class ScanInfo:
    """Metadata for a single scan."""
    scan_id:    int
    name:       str
    field_name: str
    intent:     str
    t_start:    float   # MJD seconds
    t_end:      float   # MJD seconds


@dataclass(frozen=True)
class CaltableInfo:
    """Metadata for a calibration table on disk (or in the Processing Set)."""
    path:       str
    cal_type:   str   # "bandpass", "gaincal", "fluxscale", ...
    field_names: tuple[str, ...] = ()
    spw_ids:    tuple[int, ...] = ()


@dataclass(frozen=True)
class FlagVersionInfo:
    """A saved flag version (analogous to a flagmanager entry)."""
    name:    str
    comment: str = ""


@dataclass(frozen=True)
class ObservationMetadata:
    """Lightweight, immutable summary of an open MS / Processing Set.

    Produced once by the ``open_ms`` / ``open_ps`` factory functions and
    held by ``VisibilityPlotter`` to populate sidebar dropdowns.  Neither
    ``XArrayReader`` nor ``ReductionContext`` is responsible for holding
    this struct — it is a first-class parameter of ``VisibilityPlotter``.

    All string identifiers use human-readable names, not integer indices.
    """
    fields:          tuple[FieldInfo,   ...]
    spws:            tuple[SpwInfo,     ...]
    antennas:        tuple[AntennaInfo, ...]
    scans:           tuple[ScanInfo,    ...]
    data_columns:    tuple[str, ...]          # e.g. ("DATA", "CORRECTED")
    time_range:      tuple[float, float]      # MJD seconds
    freq_range_hz:   tuple[float, float]      # Hz, across all SPWs
    n_baselines:     int
    source_path:     str = ""                 # path to the MS / PS for display

    @classmethod
    def from_backend_metadata(cls, meta: dict, source_path: str = "") -> "ObservationMetadata":
        """Construct from the dict returned by ``XArrayReader.metadata()``.

        This is a convenience bridge for the transition period.  Once
        ``ReductionContext.list_fields()`` etc. are fully implemented,
        this factory can be replaced by direct construction from those
        calls.
        """
        # field_id: prefer the backend's own authoritative FIELD_IDs
        # (meta["field_ids"], aligned by position with field_names) when
        # present. Falls back to a bare positional index otherwise --
        # this is WRONG whenever the source FIELD_IDs are non-contiguous
        # (confirmed on a real MS: field_names sorted alphabetically
        # does not line up with FIELD_ID order at all), but is kept as
        # a graceful degradation for backends that don't populate
        # "field_ids" yet (MSv4Backend, as of this fix, is one -- ADD
        # A COMPARABLE FIELD_ID SOURCE THERE before relying on numeric
        # field= selection against Processing Sets).
        field_names = meta.get("field_names", [])
        raw_field_ids = meta.get("field_ids")
        if (raw_field_ids is not None
                and len(raw_field_ids) == len(field_names)
                and all(fid is not None for fid in raw_field_ids)):
            field_ids = raw_field_ids
        else:
            field_ids = list(range(len(field_names)))
        fields = tuple(
            FieldInfo(field_id=fid, name=n)
            for fid, n in zip(field_ids, field_names)
        )
        spws = tuple(
            SpwInfo(
                spw_id=sid,
                centre_freq_hz=0.0,   # not available from raw metadata dict
                bandwidth_hz=0.0,
                n_channels=0,
                polarizations=tuple(meta.get("correlation_labels", [])),
            )
            for sid in meta.get("spw_ids", [])
        )
        antennas = tuple(
            AntennaInfo(antenna_id=i, name=n)
            for i, n in enumerate(sorted(meta.get("antenna_names", [])))
        )
        scans = tuple(
            ScanInfo(
                scan_id=i,
                name=n,
                field_name="",
                intent="",
                t_start=meta.get("time_range", (0.0, 0.0))[0],
                t_end=meta.get("time_range", (0.0, 0.0))[1],
            )
            for i, n in enumerate(meta.get("scan_names", []))
        )
        return cls(
            fields=fields,
            spws=spws,
            antennas=antennas,
            scans=scans,
            data_columns=tuple(meta.get("data_columns", ["DATA"])),
            time_range=tuple(meta.get("time_range", (0.0, 0.0))),
            freq_range_hz=tuple(meta.get("freq_range", (0.0, 0.0))),
            n_baselines=meta.get("n_baselines", 0),
            source_path=source_path,
        )


# ---------------------------------------------------------------------- #
# Flag DTOs                                                               #
# ---------------------------------------------------------------------- #

@dataclass
class FlagDelta:
    """A single pending flag operation from ``FlagDB``.

    A ``FlagDelta`` describes a coordinate-range selection in data space
    and whether the matching rows should be flagged or unflagged.  The
    ``ReductionContext`` implementation is responsible for converting this
    to the concrete flag write mechanism (``flagdata``, casacore write,
    Zarr region write, etc.).

    Coordinate fields use the same units as ``SelectionSpec``:
    * ``time_range`` — MJD seconds
    * ``freq_range`` — Hz
    * ``channel_range`` — integer channel indices
    * ``baseline_ids`` — list of (ant1_name, ant2_name) string pairs
    * ``antenna_names`` — if set, flags all baselines involving any of these
    * ``scan_names`` — scan name strings
    * ``field_names`` — field name strings
    * ``correlation`` — polarization product labels e.g. ["XX", "YY"]

    Extend flags
    ------------
    The ``extend_*`` fields mirror the plotms flag extension parameters.
    ``ReductionContext.commit_flags()`` applies these before writing.
    """
    flag: bool = True   # True = flag, False = unflag

    # Coordinate ranges — all optional; unset means "all"
    time_range:     Optional[tuple[float, float]] = None
    freq_range:     Optional[tuple[float, float]] = None
    channel_range:  Optional[tuple[int, int]]     = None
    baseline_ids:   Optional[list[tuple[str, str]]] = None
    antenna_names:  Optional[list[str]] = None
    scan_names:     Optional[list[str]] = None
    field_names:    Optional[list[str]] = None
    correlation:    Optional[list[str]] = None

    # Extend flags
    extend_corr:    bool = False   # extend to all correlations
    extend_chan:    bool = False   # extend to all channels in SPW
    extend_spw:     bool = False   # extend to all SPWs
    extend_scan:    bool = False   # extend to all times in scan

    # Provenance (for JSONL persistence and audit)
    source:  str = ""   # "raster_box", "scatter_box", "point_flag", ...
    comment: str = ""


@dataclass(frozen=True)
class FlagSummary:
    """Result returned by ``ReductionContext.commit_flags()``.

    ``n_flagged`` is the number of individual visibility samples
    (time × baseline × channel × correlation) affected by the commit.
    ``fraction_flagged`` is the global fraction across the entire MS.
    Per-SPW and per-antenna fractions are provided for the flag summary
    sidebar widget.
    """
    n_flagged:          int
    fraction_flagged:   float
    by_spw:             dict[int, float]   = dc_field(default_factory=dict)
    by_antenna:         dict[str, float]   = dc_field(default_factory=dict)
    message:            str = ""


# ---------------------------------------------------------------------- #
# Calibration parameter DTOs                                              #
# ---------------------------------------------------------------------- #

@dataclass
class BandpassParams:
    """Parameters for a bandpass calibration run.

    Fields mirror the most commonly used ``casatasks.bandpass()``
    parameters.  RADPS and future backends map these to their own
    equivalent parameters.
    """
    vis:           str
    caltable:      str
    field:         str = ""
    spw:           str = ""
    refant:        str = ""
    solint:        str = "inf"
    combine:       str = ""
    minblperant:   int = 4
    minsnr:        float = 3.0
    gaintable:     list[str] = dc_field(default_factory=list)
    interp:        list[str] = dc_field(default_factory=list)


@dataclass
class GaincalParams:
    """Parameters for a gain calibration run."""
    vis:           str
    caltable:      str
    field:         str = ""
    spw:           str = ""
    refant:        str = ""
    calmode:       str = "ap"   # "p", "a", "ap"
    solint:        str = "int"
    combine:       str = ""
    minblperant:   int = 4
    minsnr:        float = 3.0
    gaintable:     list[str] = dc_field(default_factory=list)
    interp:        list[str] = dc_field(default_factory=list)


@dataclass
class FluxscaleParams:
    """Parameters for flux scale transfer."""
    vis:        str
    caltable:   str
    fluxtable:  str
    reference:  list[str] = dc_field(default_factory=list)
    transfer:   list[str] = dc_field(default_factory=list)


@dataclass
class ApplycalParams:
    """Parameters for applying calibration tables."""
    vis:        str
    field:      str = ""
    spw:        str = ""
    gaintable:  list[str] = dc_field(default_factory=list)
    interp:     list[str] = dc_field(default_factory=list)
    calwt:      bool = True
    flagbackup: bool = True


@dataclass
class SplitParams:
    """Parameters for splitting/averaging an MS."""
    vis:         str
    outputvis:   str
    field:       str = ""
    spw:         str = ""
    scan:        str = ""
    timebin:     str = "0s"
    width:       int = 1       # channel averaging width
    datacolumn:  str = "corrected"
    keepflags:   bool = True


# ---------------------------------------------------------------------- #
# Remote execution DTOs                                                   #
# ---------------------------------------------------------------------- #

@dataclass
class ReductionOperation:
    """Serialisable description of a single reduction task.

    Used by ``RemoteReductionContext.submit()`` to dispatch work to a
    remote worker.  The ``operation`` field is a string key understood by
    the remote server (e.g. ``"bandpass"``, ``"commit_flags"``).  The
    ``params`` field is the corresponding DTO serialised to a dict.

    This DTO exists so that the remote transport layer is decoupled from
    the specific parameter types — the server deserialises ``params``
    back into the appropriate DTO using the ``operation`` key.
    """
    operation: str
    params:    dict[str, Any] = dc_field(default_factory=dict)


@dataclass
class ReductionResult:
    """Result returned from a completed ``ReductionOperation``."""
    operation: str
    success:   bool
    payload:   Any  = None    # operation-specific result (FlagSummary, CaltableInfo, …)
    error:     str  = ""


# ======================================================================
# Abstract Base Class
# ======================================================================

class ReductionContext(abc.ABC):
    """Abstract interface for calibration, flagging, and data manipulation.

    All methods that change the on-disk state of an MS / Processing Set
    pass through this interface.  The implementation decides how and where
    operations execute — in-process CASA6, a remote SLURM job, or a
    RADPS service endpoint.

    Lifecycle
    ---------
    ``ReductionContext`` does *not* define ``open`` / ``close``.  It is
    constructed with enough configuration to know how to reach the data
    (path, endpoint URL, credentials) and is ready to accept calls
    immediately.  Connections to remote services are established lazily
    on the first call and reused thereafter.

    Thread safety
    -------------
    Implementations should be safe to call from the Bokeh I/O loop thread
    (which runs all CommMgr j2p handlers).  Long-running operations
    (``bandpass``, ``gaincal``) should either run in a thread pool or
    return a ``Future`` via ``submit()`` so the I/O loop is not blocked.

    Calibration methods
    -------------------
    Each calibration method has a ``*Params`` DTO argument.  The DTO
    carries only the fields that are semantically meaningful across
    implementations.  Fields that are CASA-specific (e.g. ``uvrange``,
    ``scan``) are not included in the DTO; they can be passed via
    ``SelectionSpec`` or added to the DTO if a compelling cross-platform
    case arises.

    Subclasses that do not support a particular operation should raise
    ``NotImplementedError`` with a descriptive message rather than
    silently succeeding.
    """

    # ------------------------------------------------------------------ #
    # Observation metadata                                                 #
    # ------------------------------------------------------------------ #

    @abc.abstractmethod
    def list_fields(self) -> list[FieldInfo]:
        """Return all fields present in the open MS / Processing Set."""

    @abc.abstractmethod
    def list_spws(self) -> list[SpwInfo]:
        """Return all spectral windows."""

    @abc.abstractmethod
    def list_antennas(self) -> list[AntennaInfo]:
        """Return all antennas."""

    @abc.abstractmethod
    def list_scans(self) -> list[ScanInfo]:
        """Return all scans."""

    @abc.abstractmethod
    def list_data_columns(self) -> list[str]:
        """Return available data columns, e.g. ``['DATA', 'CORRECTED']``."""

    @abc.abstractmethod
    def list_caltables(self) -> list[CaltableInfo]:
        """Return existing calibration tables associated with this MS."""

    # ------------------------------------------------------------------ #
    # Flag operations                                                      #
    # ------------------------------------------------------------------ #

    @abc.abstractmethod
    def commit_flags(self, flag_deltas: list[FlagDelta]) -> FlagSummary:
        """Write pending flag deltas to the MS / Processing Set.

        This is the single point where ``FlagDB`` entries become
        permanent.  The implementation applies ``extend_*`` fields in
        each delta before writing, so callers need not pre-expand them.

        For MSv2, the expected implementation calls ``flagdata()`` with
        the appropriate selection parameters.  For MSv4 Zarr, it writes
        region-selected boolean arrays via ``to_zarr(region=...)``.  For
        remote execution, it serialises the deltas and dispatches to the
        remote worker.

        Parameters
        ----------
        flag_deltas :
            List of ``FlagDelta`` objects accumulated in ``FlagDB`` since
            the last commit.  The list is consumed in order.

        Returns
        -------
        FlagSummary
            Counts and fractions of flagged data after the commit.
        """

    @abc.abstractmethod
    def save_flag_version(self, name: str, comment: str = "") -> None:
        """Save the current flag state under *name*.

        Equivalent to ``flagmanager(mode='save', versionname=name)``
        for MSv2.  For MSv4, copies the current flag arrays to a named
        group in the Zarr store.
        """

    @abc.abstractmethod
    def restore_flag_version(self, name: str) -> None:
        """Restore flags from the named saved version.

        Equivalent to ``flagmanager(mode='restore', versionname=name)``
        for MSv2.
        """

    @abc.abstractmethod
    def list_flag_versions(self) -> list[FlagVersionInfo]:
        """Return all saved flag versions."""

    # ------------------------------------------------------------------ #
    # Calibration operations                                               #
    # ------------------------------------------------------------------ #

    @abc.abstractmethod
    def bandpass(self, params: BandpassParams) -> CaltableInfo:
        """Run bandpass calibration.

        Returns a ``CaltableInfo`` describing the newly written table.
        ``VisibilityPlotter`` uses this to update the caltable dropdown
        and offer to apply the new solution immediately.
        """

    @abc.abstractmethod
    def gaincal(self, params: GaincalParams) -> CaltableInfo:
        """Run gain calibration."""

    @abc.abstractmethod
    def fluxscale(self, params: FluxscaleParams) -> CaltableInfo:
        """Transfer flux scale from reference to transfer fields."""

    @abc.abstractmethod
    def applycal(self, params: ApplycalParams) -> None:
        """Apply calibration tables to the MS.

        After a successful ``applycal``, ``VisibilityPlotter`` should
        be notified (via an event or callback) so that it can offer to
        switch the active data column to ``CORRECTED``.
        """

    # ------------------------------------------------------------------ #
    # Data manipulation                                                    #
    # ------------------------------------------------------------------ #

    @abc.abstractmethod
    def split(self, params: SplitParams) -> "ReductionContext":
        """Split / average the MS and return a new context pointing to it.

        The returned ``ReductionContext`` is of the same concrete type
        as the caller (local stays local, remote stays remote) but points
        at the newly written output MS / Processing Set.
        """

    # ------------------------------------------------------------------ #
    # Asynchronous / remote execution                                      #
    # ------------------------------------------------------------------ #

    @abc.abstractmethod
    def submit(self, operation: ReductionOperation) -> "Future[ReductionResult]":
        """Submit a reduction operation for asynchronous execution.

        Local implementations (``Casa6ReductionContext``) return an
        immediately-resolved ``Future`` backed by a ``ThreadPoolExecutor``.
        Remote implementations (``RemoteReductionContext``) submit the
        serialised ``ReductionOperation`` to the cluster and return a
        real ``Future`` that resolves when the job completes.

        ``VisibilityPlotter`` uses this method to run long operations
        (``bandpass``, ``gaincal``) without blocking the Bokeh I/O loop.
        A progress callback can be wired to the ``Future``'s ``add_done_callback``.
        """

    # ------------------------------------------------------------------ #
    # Capability introspection                                             #
    # ------------------------------------------------------------------ #

    def supports_calibration(self) -> bool:
        """Return ``True`` if calibration methods are implemented.

        ``VisibilityPlotter`` uses this to enable or disable calibration
        buttons in the toolbar.  The default implementation returns
        ``True``; ``NullReductionContext`` overrides to ``False``.
        """
        return True

    def supports_remote_execution(self) -> bool:
        """Return ``True`` if ``submit()`` dispatches to a remote worker.

        Used by ``VisibilityPlotter`` to show a cluster-mode indicator in
        the status bar.  Default is ``False``.
        """
        return False


# ======================================================================
# NullReductionContext — no-op implementation
# ======================================================================

class NullReductionContext(ReductionContext):
    """No-op ``ReductionContext`` used when no backend is available.

    All metadata methods return empty lists.  All mutation methods
    (``commit_flags``, ``bandpass``, etc.) raise ``NotImplementedError``
    with a descriptive message so that accidental calls surface clearly
    during development rather than silently succeeding.

    ``VisibilityPlotter`` uses this as the default context when no
    ``ReductionContext`` is supplied by the caller.  Calibration buttons
    in the GUI are disabled when ``supports_calibration()`` returns
    ``False``.

    Usage
    -----
    ::

        reader  = LocalVisibilityReader(MSv2Backend(path))
        context = NullReductionContext()
        plotter = VisibilityPlotter(metadata, reader, context, flag_db)
    """

    def list_fields(self)       -> list[FieldInfo]:       return []
    def list_spws(self)         -> list[SpwInfo]:         return []
    def list_antennas(self)     -> list[AntennaInfo]:     return []
    def list_scans(self)        -> list[ScanInfo]:        return []
    def list_data_columns(self) -> list[str]:             return ["DATA"]
    def list_caltables(self)    -> list[CaltableInfo]:    return []
    def list_flag_versions(self)-> list[FlagVersionInfo]: return []

    def commit_flags(self, flag_deltas: list[FlagDelta]) -> FlagSummary:
        raise NotImplementedError(
            "NullReductionContext: commit_flags() is not implemented. "
            "Provide a Casa6ReductionContext or RadpsReductionContext."
        )

    def save_flag_version(self, name: str, comment: str = "") -> None:
        raise NotImplementedError(
            "NullReductionContext: save_flag_version() is not implemented."
        )

    def restore_flag_version(self, name: str) -> None:
        raise NotImplementedError(
            "NullReductionContext: restore_flag_version() is not implemented."
        )

    def bandpass(self, params: BandpassParams) -> CaltableInfo:
        raise NotImplementedError(
            "NullReductionContext: bandpass() is not implemented."
        )

    def gaincal(self, params: GaincalParams) -> CaltableInfo:
        raise NotImplementedError(
            "NullReductionContext: gaincal() is not implemented."
        )

    def fluxscale(self, params: FluxscaleParams) -> CaltableInfo:
        raise NotImplementedError(
            "NullReductionContext: fluxscale() is not implemented."
        )

    def applycal(self, params: ApplycalParams) -> None:
        raise NotImplementedError(
            "NullReductionContext: applycal() is not implemented."
        )

    def split(self, params: SplitParams) -> "ReductionContext":
        raise NotImplementedError(
            "NullReductionContext: split() is not implemented."
        )

    def submit(self, operation: ReductionOperation) -> "Future[ReductionResult]":
        raise NotImplementedError(
            "NullReductionContext: submit() is not implemented."
        )

    def supports_calibration(self) -> bool:
        return False

    def __repr__(self) -> str:  # pragma: no cover
        return "NullReductionContext()"
