"""MSv2Backend — ``XArrayReader`` implementation backed by ``xarray-ms``.

``xarray-ms`` presents a full MSv4-structured DataTree view over MSv2
(casacore Table Data System) files using ``xarray.open_datatree()``.
The ``arcae`` C++ backend provides thread-safe casacore table access;
``casatools`` and ``python-casacore`` are **not** required in the read
path.

Because ``xarray-ms`` exposes the same DataTree structure, dimension
names (``time``, ``baseline_id``, ``frequency``, ``polarization``), and
xarray/Dask access patterns as the MSv4 schema, the two backends share
the same ``query_columns`` / ``query_raster`` interface.

Design decisions confirmed by test_01 through test_11
------------------------------------------------------
* Engine name is ``"xarray-ms:msv2"`` (not ``"xarray-ms"``).
* Partition schema: ``["DATA_DESC_ID", "OBSERVATION_ID"]``.
  The boilerplate used ``partition_columns``; xarray-ms uses
  ``partition_schema``.
* VISIBILITY variable name is always ``"VISIBILITY"`` in the MSv4 view
  that xarray-ms presents (even for MSv2 DATA/CORRECTED_DATA/MODEL_DATA
  columns — the renaming happens inside xarray-ms).
* WEIGHT is 4D ``(time, baseline_id, frequency, polarization)`` in the
  real sis14 dataset.  It may be all-NaN; the weighted mean path guards
  against this with ``weight_sum.where(weight_sum > 0)``.
* EFFECTIVE_INTEGRATION_TIME dims are ``(time, baseline_id)`` and NaN
  for padded (missing-autocorrelation) slots.
* ~40% of baseline_id slots are NaN-padded in ALMA cross-correlation-
  only datasets (``IrregularBaselineGridWarning``).  This is benign;
  FLAG is set True for padded positions so ``.where(~flag)`` handles
  them correctly.
* FLAG dtype is uint8; must be cast to bool before use as a mask.
* arcae supports concurrent reads from multiple threads with independent
  ``open_datatree()`` handles (verified by test_11).
* Fused ``dask.compute()`` across all derived quantities gives ~16×
  speedup over sequential per-quantity compute by reading VISIBILITY
  only once (verified by test_11).
* ``ds.dims`` FutureWarning: use ``ds.sizes`` throughout.

FLAG write-back
---------------
xarray-ms is a read-only backend.  ``FlagWriteThrough`` requires
``casatools.table`` as an isolated write adapter at ``FlagDB.commit()``
time only (§10 of design doc).  This class never writes to the MS.

References
----------
msvis_design.md §4.2, §6, §9, §10
xarray-ms docs: https://xarray-ms.readthedocs.io/
test_01 – test_11 in msvis/tests/
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

import numpy as np
import pandas as pd
import xarray as xr

try:
    import dask
    import dask.array as da
    HAS_DASK = True
except ImportError:
    HAS_DASK = False

from .reader import (
    channel_axis_is_unambiguous,
    to_channel_index,
    XArrayReader,
    _compute_axis_values,
    ScatterLayerSpec,
    ScatterRenderResult,
    ScanInfo,
    SpwInfo,
    IdentityTables,
)
from . import _scatter_render
from ..axes import Axis, AxisInfo, AxisType
from ..selection import SelectionSpec

log = logging.getLogger(__name__)

# Engine name registered by xarray-ms (confirmed test_01)
_XARRAY_MS_ENGINE = "xarray-ms:msv2"

# Adaptive pipeline thresholds (confirmed by test_11):
#   < _THRESH_FUSED  → serial xarray stack (simple, debuggable)
#   >= _THRESH_FUSED → fused dask.compute() + numpy ravel
#   >= _THRESH_PAR   → + parallel Datashader passes (ThreadPoolExecutor)
_THRESH_FUSED = 500_000     # samples
_THRESH_PAR   = 5_000_000   # samples

# Speed of light for uvdist_lambda computation
_C_MS = 299_792_458.0


class MSv2Backend(XArrayReader):
    """``XArrayReader`` backed by ``xarray-ms`` + ``arcae`` (MSv2 files).

    Presents the same interface as ``MSv4Backend``.  Internally opens
    the MSv2 Measurement Set via ``xarray.open_datatree()`` with the
    ``"xarray-ms:msv2"`` engine, which uses the ``arcae`` C++ bindings
    for casacore table access.

    Parameters
    ----------
    path :
        Path to the MSv2 ``.ms`` directory.
    partition_schema :
        Columns used to partition the DataTree.  Defaults to
        ``['DATA_DESC_ID', 'OBSERVATION_ID']`` which matches xarray-ms
        defaults and keeps partitions symmetric with MSv4.
    data_column :
        Which MSv2 data column to expose as ``VISIBILITY``.  One of
        ``'DATA'``, ``'CORRECTED_DATA'``, ``'MODEL_DATA'``.  Defaults
        to ``'DATA'``.  Passed to xarray-ms via ``column=`` kwarg.
    chunks :
        Dask chunk specification forwarded to ``xarray.open_datatree``.
        ``None`` uses ``{"time": 100, "baseline_id": 100}`` which
        balances memory and task-graph size for typical ALMA datasets.
    """

    _DEFAULT_PARTITION_SCHEMA = ["DATA_DESC_ID", "OBSERVATION_ID"]
    _DEFAULT_CHUNKS = {"time": 100, "baseline_id": 100}

    def __init__(
        self,
        path: str,
        partition_schema: Optional[list[str]] = None,
        data_column: str = "DATA",
        chunks: Optional[dict] = None,
    ) -> None:
        self._path = path
        self._partition_schema = (
            partition_schema
            if partition_schema is not None
            else self._DEFAULT_PARTITION_SCHEMA
        )
        self._data_column = data_column
        self._chunks = chunks if chunks is not None else self._DEFAULT_CHUNKS
        self._datatree: Optional[xr.DataTree] = None
        # Lock protects _datatree during open/close; reads are lock-free
        # because arcae supports concurrent reads (confirmed test_11).
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ #
    # Lifecycle                                                            #
    # ------------------------------------------------------------------ #

    def open(self) -> None:
        """Open the MSv2 file via xarray-ms.

        Idempotent — safe to call multiple times.

        Raises
        ------
        ImportError
            If ``xarray-ms`` or ``arcae`` are not installed.
        RuntimeError
            If xarray-ms fails to open the file (wraps the original
            exception with the path for context).
        """
        with self._lock:
            if self._datatree is not None:
                return
            _check_xarray_ms()
            log.debug("MSv2Backend: opening %s (column=%s)",
                      self._path, self._data_column)
            try:
                self._datatree = xr.open_datatree(
                    self._path,
                    engine=_XARRAY_MS_ENGINE,
                    partition_schema=self._partition_schema,
                    chunks=self._chunks,
                    # Note: xarray-ms 0.5.x does not expose a column= kwarg
                    # at the open_datatree level.  DATA/CORRECTED_DATA/MODEL
                    # selection is handled by _resolve_vis() which probes
                    # available variable names in each partition Dataset.
                    # self._data_column is kept for metadata() reporting.
                )
            except NotImplementedError as exc:
                raise RuntimeError(
                    f"{self._path!r} not supported: {exc}"
                ) from exc
            except Exception as exc:
                raise RuntimeError(
                    f"xarray-ms failed to open {self._path!r}: {exc} <{type(exc)}>"
                ) from exc

        n = sum(1 for _ in self._iter_visibility_partitions())
        log.debug("MSv2Backend: opened — %d visibility partition(s)", n)
        if n == 0:
            log.warning("MSv2Backend: no visibility partitions in %s",
                        self._path)

    def close(self) -> None:
        """Release the open DataTree."""
        with self._lock:
            if self._datatree is not None:
                try:
                    self._datatree.close()
                except Exception:
                    pass
                finally:
                    self._datatree = None

    def __enter__(self) -> "MSv2Backend":
        self.open()
        return self

    def __exit__(self, *_) -> None:
        self.close()

    # ------------------------------------------------------------------ #
    # Internal helpers                                                     #
    # ------------------------------------------------------------------ #

    def _require_open(self) -> xr.DataTree:
        dt = self._datatree
        if dt is None:
            raise RuntimeError(
                "MSv2Backend is not open.  Call open() or use as a "
                "context manager."
            )
        return dt



    _SUPPORTS_RASTER_CHANNEL_INDEX = True
    """Whether ``query_raster`` plots ``Axis.CHANNEL`` as a channel index.

    Named for the *raster path specifically*: ``query_columns`` (which
    the scatter uses) already returns a real channel index via
    ``_compute_axis_values``, so capability is per-path and a single
    backend-level flag made the two panels of one plotter disagree.

    ``True`` since 2026-08-18: ``query_raster`` relabels the frequency
    coordinate as a channel index (``to_channel_index``) when a single
    spectral window survives selection, so the values match the label.
    ``axis_info`` reads this to decide whether the single-window case may
    honestly report a channel index.

    Set to ``False`` to fall back to plotting frequency under a
    substituted label -- the behaviour before the relabelling existed.

    Channel number is CASA's public selection key, not an internal
    index: an RFI spike found in the plot is acted on with
    ``flagdata(spw='0:137~139')``, and edge-channel and bandpass work are
    channel-domain by nature.  It is worth implementing -- see the
    handoff -- and flipping this flag is what turns it on here.
    """

    def axis_info(self, axis, selection=None, query="columns"):
        """Resolve *axis* for *selection*, substituting where necessary.

        ``Axis.CHANNEL`` is the case that matters.  ``_axis_to_dim`` maps
        it to the ``"frequency"`` dimension, the same as
        ``Axis.FREQUENCY``, and ``query_raster`` takes its extent from
        that coordinate -- so before this method existed, selecting
        CHANNEL produced an axis labelled "Channel" with ticks in Hz.
        Correct picture, wrong label.

        A channel index is unique *within* a partition but not across
        them: with four SPWs there are four channels numbered 5, and
        ``query_raster`` concatenates partitions.  Frequency has no such
        problem -- globally unique, monotonic, and it orders the
        partitions correctly -- so the backend plots frequency and must
        say so rather than leaving the user to infer it from tick
        magnitudes.

        There is no global channel index available, and MSv4 deliberately
        has no notion of a global spectral axis (SPWs may differ in
        channel count and width, and may overlap).  Inventing one would
        break whenever ``partition_schema`` or a selection changed, and it
        is not needed for flagging: frequency inverts exactly to
        ``(partition, local channel index)`` via a lookup in that
        partition's ``frequency`` coordinate, and ``FlagDB`` already
        stores coordinate ranges rather than indices.

        The partition count must come from the iteration, not from
        ``len(selection.spw)``: a non-default partition schema splitting
        by scan or field yields several partitions within one SPW, and
        their frequency coordinates would be identical.
        """
        info = super().axis_info(axis, selection, query)
        try:
            # MSv2's _axis_to_dim takes the axis alone; MSv4's also takes
            # a baseline dim.  Do not unify these by guessing -- passing
            # MSv4's signature here raised AttributeError on the missing
            # _baseline_dim and was swallowed into dim="".
            dim = _axis_to_dim(axis)
        except ValueError:
            # Derived axes have no dimension; that is not an error here.
            dim = ""

        if axis is not Axis.CHANNEL:
            return AxisInfo(axis=info.axis, requested=info.requested,
                            dim=dim, is_index=info.is_index)

        parts = list(self._iter_visibility_partitions(selection))
        # Count distinct SPWs, NOT partitions.  Channel numbering is a
        # property of the spectral window, so partitions that share an
        # SPW share its frequency coordinate exactly and its channel
        # numbering is identical -- several partitions carrying one SPW
        # is completely unambiguous.  Partition schemas routinely split
        # on other keys: MSv2's default is
        # ["DATA_DESC_ID", "OBSERVATION_ID"], and a multi-field MS then
        # yields several partitions per SPW.  Counting partitions here
        # substituted frequency for datasets where the channel index was
        # perfectly well defined.
        spw_ids = {self._partition_spw_id(ds) for ds in parts}
        spw_ids.discard(None)
        n_spws = len(spw_ids)

        if n_spws > 1:
            return AxisInfo.substituted(
                Axis.CHANNEL, Axis.FREQUENCY, dim=dim,
                note=("channel index is not unique across the "
                      + str(n_spws) + " selected spectral windows; showing "
                      "frequency. Select a single spw to plot channel "
                      "number."),
            )

        if n_spws == 0:
            # No partition declared an SPW id, so uniqueness is
            # unknowable.  Substitute rather than claim a channel index
            # that might span several windows -- the same tolerance
            # _spw_selected applies in the opposite direction, where
            # keeping an undeclared partition is the safe choice.
            return AxisInfo.substituted(
                Axis.CHANNEL, Axis.FREQUENCY, dim=dim,
                note=("spectral window is not declared by this dataset, so "
                      "channel numbering cannot be shown to be unique; "
                      "showing frequency."),
            )

        if query == "raster" and not self._SUPPORTS_RASTER_CHANNEL_INDEX:
            # One partition, so the index *would* be unambiguous -- but
            # query_raster still resolves Axis.CHANNEL through
            # _axis_to_dim to the "frequency" dimension and takes its
            # extent from ds.coords["frequency"].  Labelling this axis
            # "Channel" while the ticks carry Hz is the original defect,
            # reasserted more confidently.  Substitute until the values
            # follow the label; flipping _SUPPORTS_CHANNEL_INDEX is then
            # the only change needed here.
            return AxisInfo.substituted(
                Axis.CHANNEL, Axis.FREQUENCY, dim=dim,
                note=("the raster path does not yet plot a channel index; "
                      "showing frequency.  Scatter panels can plot "
                      "channel number."),
            )

        # Unambiguous, and the backend plots the index.  Carry the spw so
        # the number is never orphaned from the scope it indexes into --
        # "Channel 137" is ambiguous, "Channel (spw 0)" is what a user
        # retypes as spw='0:137'.
        # Name the identifier honestly: "ddid 3" is not "spw 3" unless
        # the MS has a single polarization setup.  See
        # _partition_spw_ident.
        kinds = {self._partition_spw_ident(ds)[1] for ds in parts}
        kinds.discard("none")
        spw_id = next(iter(spw_ids)) if spw_ids else None

        # Qualifier wording follows the kind, and the kind must be read
        # rather than guessed: an earlier two-way branch predated the
        # "name" kind and rendered a spectral window *name* as
        # "ddid ALMA_RB_07#BB_2#SW-01#FULL_RES" -- a label asserting the
        # number is a DATA_DESC_ID when it is not a number at all.
        #
        # A name is self-describing, so it takes no prefix.  A numeric id
        # does, because "137" alone says nothing about what it indexes.
        kind = kinds.pop() if len(kinds) == 1 else None
        if spw_id is None:
            context = None
        elif kind == "name":
            context = str(spw_id)
        elif kind in ("spw", "ddid"):
            context = f"{kind} {spw_id}"
        else:
            # Partitions disagree about how they identify their window;
            # say nothing rather than pick one and be wrong about the
            # rest.
            context = None

        return AxisInfo.direct(
            Axis.CHANNEL, dim=dim, is_index=True, context=context,
        )

    @classmethod
    def _partition_spw_id(cls, ds) -> "Optional[int]":
        """SPW id of a partition, or ``None`` if it does not declare one.

        Tries ``spectral_window_id`` then ``DATA_DESC_ID`` -- the same
        fallback pair ``metadata()`` uses, because xarray-ms-written
        stores and xradio-written stores disagree on which key
        carries it.
        """
        ident, _kind = cls._partition_spw_ident(ds)
        return ident

    @staticmethod
    def _partition_spw_ident(ds):
        """``(identity, kind)`` for the partition's spectral window.

        *kind* is ``"spw"`` (a numeric id, directly usable as CASA's
        ``spw=``), ``"ddid"`` (a DATA_DESC_ID -- **not** the same number,
        it indexes DATA_DESCRIPTION, a (spw, polarization setup) pair),
        ``"name"`` (the spectral window name), or ``"none"``.

        Lookup order, and why it ends where it does:

        1. ``ds.attrs["spectral_window_id"]`` -- xradio-written stores.
        2. ``ds.attrs["DATA_DESC_ID"]`` -- some stores; reported as
           ``"ddid"`` so a caller never prints it as an spw.
        3. ``ds.frequency.attrs["spectral_window_name"]`` -- what
           **xarray-ms 0.5.6 actually provides**.  Verified 2026-08-18 on
           a real MS: ``ds.attrs`` carries none of the above, and the only
           spectral identity in the MSv4 view is the name
           (``'ALMA_RB_07#BB_2#SW-01#FULL_RES'``).

        **The numeric id is therefore not always obtainable.** Turning a
        name into CASA's ``spw=N`` needs a NAME -> row lookup in the MS's
        SPECTRAL_WINDOW subtable, which this backend cannot reach.  That
        is why FlagDB keys on *frequency ranges*: exact, always available,
        and they survive the ``split``/``mstransform`` renumbering that
        would invalidate an id anyway.
        """
        spw_id = ds.attrs.get("spectral_window_id")
        if spw_id is not None:
            return int(spw_id), "spw"
        ddid = ds.attrs.get("DATA_DESC_ID")
        if ddid is not None:
            return int(ddid), "ddid"
        freq = ds.coords.get("frequency") if hasattr(ds, "coords") else None
        if freq is not None:
            name = getattr(freq, "attrs", {}).get("spectral_window_name")
            if name:
                return str(name), "name"
        return None, "none"

    @staticmethod
    def _partition_channel_width(ds):
        """Channel width in Hz from ``frequency.attrs``, or ``None``.

        Stored as ``{"attrs": {...}, "data": 610351.5625}`` in xarray-ms
        0.5.6.  Exists so the uniform-width assumption behind treating
        channel index and frequency as affine can be *checked* rather
        than assumed -- an irregular or concatenated window breaks the
        affinity silently.
        """
        freq = ds.coords.get("frequency") if hasattr(ds, "coords") else None
        if freq is None:
            return None
        cw = getattr(freq, "attrs", {}).get("channel_width")
        if isinstance(cw, dict):
            cw = cw.get("data")
        try:
            return float(cw) if cw is not None else None
        except (TypeError, ValueError):
            return None


    def _spw_selected(self, ds, selection) -> bool:
        """Whether *ds* passes *selection*'s SPW filter.

        A partition that declares no SPW id is **kept**: refusing to plot
        data because a store omits an optional attribute would be a worse
        failure than plotting slightly more than asked for, and
        ``metadata()`` has the same tolerance when collecting ``spw_ids``.
        """
        if selection is None or getattr(selection, "spw", None) is None:
            return True
        ident, kind = self._partition_spw_ident(ds)
        if ident is None:
            return True
        # Compare identities directly, whatever kind they are.
        # ``SelectionSpec.spw`` holds whatever ``_partition_spw_ident``
        # returns -- a numeric id where the store provides one, otherwise
        # the spectral window *name* -- so a name selects a name.  An
        # earlier version bailed out on names because the field was
        # assumed to hold parsed integers; that made SPW filtering a
        # silent no-op on every xarray-ms store (see the handoff).
        wanted = set(selection.spw)
        if ident in wanted:
            return True
        # A caller may still supply a name where the store reports an id,
        # or the reverse, if it resolved against different metadata.
        # Compare stringified as a last resort rather than dropping the
        # partition, which would silently show *less* data than asked
        # for -- the worse of the two failure directions.
        if str(ident) in {str(w) for w in wanted}:
            return True
        return False

    def _iter_visibility_partitions(self, selection=None):
        """Yield each leaf Dataset that contains visibility data.

        Filters to nodes that have both a non-empty time dimension and
        a VISIBILITY (or DATA) data variable.  This excludes metadata
        subtables (ANTENNA, FIELD, SOURCE, etc.) that xarray-ms attaches
        as child nodes in the DataTree — those have data variables like
        ANTENNA_POSITION but no VISIBILITY column.

        Checking for VISIBILITY/DATA is more reliable than checking for
        time>0 alone because some metadata subtables (e.g. ANTENNA) are
        broadcast onto the time dimension and would otherwise pass through.

        When *selection* carries an ``spw`` constraint, partitions whose
        SPW is not listed are skipped.  Filtering here rather than in
        ``_apply_selection`` is deliberate: with the default partition
        schema ``["DATA_DESC_ID", "OBSERVATION_ID"]`` SPW is a *partition*
        property, not a dimension within one, so skipping avoids reading
        the partition at all.

        SPW selection was silently ignored before 2026-08 -- see the MSv4
        backend's equivalent docstring for the full history.  Callers that
        must see the whole store (``open()``, ``metadata()``) pass no
        *selection* and are unaffected.
        """
        dt = self._require_open()
        n_total = n_kept = 0
        for node in dt.subtree:
            if not node.has_data:
                continue
            ds = node.ds
            if ds.sizes.get("time", 0) == 0:
                continue
            # Must contain a visibility data variable
            if not any(v in ds.data_vars for v in ("VISIBILITY", "DATA",
                                                     "CORRECTED_DATA",
                                                     "MODEL_DATA")):
                continue
            n_total += 1
            if not self._spw_selected(ds, selection):
                continue
            n_kept += 1
            yield ds

        if n_total and not n_kept:
            # Every partition filtered out.  Callers handle "no data"
            # gracefully, but silence here would look identical to an
            # empty selection range, so say which constraint emptied it.
            log.warning(
                "SPW selection %r matched none of the %d partitions in %s",
                getattr(selection, "spw", None), n_total, self._path,
            )

    def _flag_mask(self, ds: xr.Dataset) -> xr.DataArray:
        """Return boolean FLAG DataArray (True = flagged or padded).

        FLAG dtype is uint8 in xarray-ms v0.5.x (confirmed test_03).
        Padded baseline slots have FLAG=True set by xarray-ms, so
        .where(~flag) correctly excludes them without special handling.
        """
        return ds["FLAG"].astype(bool)

    def _resolve_vis(self, ds: xr.Dataset) -> xr.DataArray:
        """Return the VISIBILITY DataArray.

        xarray-ms always names it ``VISIBILITY`` in its MSv4 view
        regardless of the underlying MSv2 column name (confirmed test_01).
        Falls back to ``DATA`` for forward-compatibility.
        """
        for name in ("VISIBILITY", "DATA"):
            if name in ds.data_vars:
                return ds[name]
        raise KeyError(
            f"No VISIBILITY or DATA variable in partition with "
            f"dims={dict(ds.sizes)}.  Available: {list(ds.data_vars)}"
        )

    def _uvdist_m(self, ds: xr.Dataset) -> xr.DataArray:
        """UV-distance in metres, shape (time, baseline_id)."""
        uvw = ds["UVW"]
        u = uvw.sel(uvw_label="u")
        v = uvw.sel(uvw_label="v")
        return np.sqrt(u**2 + v**2)

    def _uvdist_lambda(self, ds: xr.Dataset) -> xr.DataArray:
        """UV-distance in wavelengths, shape (time, baseline_id, frequency).

        Broadcast of uvdist_m (time, baseline_id) × freq (frequency) / c.
        """
        return self._uvdist_m(ds) * ds.coords["frequency"] / _C_MS

    def _estimate_samples(
        self,
        ds: xr.Dataset,
        sel: SelectionSpec,
        n_quantities: int,
    ) -> int:
        """Estimate sample count for the adaptive pipeline decision.

        Uses coordinate metadata only — no data is read.
        """
        n_time = _count_selected_time(ds, sel)
        n_bl   = _count_selected_baselines(ds, sel)
        n_chan  = _count_selected_channels(ds, sel)
        return n_time * n_bl * n_chan * n_quantities

    # ------------------------------------------------------------------ #
    # Selection                                                            #
    # ------------------------------------------------------------------ #

    def _apply_selection(
        self, ds: xr.Dataset, sel: SelectionSpec
    ) -> xr.Dataset:
        """Apply *sel* constraints lazily via xarray isel/where.

        All selections operate in native MS axes (time, baseline_id,
        frequency, polarization) so that flag operations remain
        well-defined (design doc §4.6, §flagging-axis-note).

        The returned Dataset is still lazy; no Dask compute is triggered.
        """
        # --- time dimension ---
        time_mask = None

        if "field_name" in ds.coords and sel.field_names is not None:
            m = ds.coords["field_name"].isin(sel.field_names)
            time_mask = m if time_mask is None else time_mask & m

        if "scan_name" in ds.coords and sel.scan is not None:
            m = ds.coords["scan_name"].isin(sel.scan)
            time_mask = m if time_mask is None else time_mask & m

        if sel.time_range is not None:
            t0, t1 = sel.time_range
            m = (ds.coords["time"] >= t0) & (ds.coords["time"] <= t1)
            time_mask = m if time_mask is None else time_mask & m

        if time_mask is not None:
            ds = ds.isel(time=time_mask.values)

        # --- frequency dimension ---
        if sel.freq_range is not None:
            f0, f1 = sel.freq_range
            freq_mask = (
                (ds.coords["frequency"] >= f0) &
                (ds.coords["frequency"] <= f1)
            )
            ds = ds.isel(frequency=freq_mask.values)

        if sel.channel_range is not None:
            c0, c1 = sel.channel_range
            ds = ds.isel(frequency=slice(c0, c1))

        # --- polarization dimension ---
        if sel.correlation is not None:
            pol_mask = ds.coords["polarization"].isin(sel.correlation)
            ds = ds.isel(polarization=pol_mask.values)

        # --- baseline_id dimension ---
        # baselines takes precedence over antenna_names.
        # Guard with coord presence check — metadata subtables (ANTENNA etc.)
        # share the baseline_id dim but lack baseline_antenna*_name coords.
        _has_bl_coords = ("baseline_antenna1_name" in ds.coords and
                          "baseline_antenna2_name" in ds.coords)
        if sel.baselines is not None and _has_bl_coords:
            ant1 = ds.coords["baseline_antenna1_name"].values
            ant2 = ds.coords["baseline_antenna2_name"].values
            bl_mask = np.zeros(len(ant1), dtype=bool)
            for a1, a2 in sel.baselines:
                bl_mask |= (ant1 == a1) & (ant2 == a2)
            ds = ds.isel(baseline_id=bl_mask)
        elif sel.antenna_names is not None and _has_bl_coords:
            ant1 = ds.coords["baseline_antenna1_name"].values
            ant2 = ds.coords["baseline_antenna2_name"].values
            ant_set = set(sel.antenna_names)
            bl_mask = np.isin(ant1, list(ant_set)) | np.isin(ant2, list(ant_set))
            ds = ds.isel(baseline_id=bl_mask)

        return ds

    # ------------------------------------------------------------------ #
    # Metadata                                                             #
    # ------------------------------------------------------------------ #

    def _field_id_map(self) -> dict:
        """Authoritative name -> real FIELD_ID mapping.

        xarray-ms's DataTree has no separate field catalog node — its
        children are only the visibility partitions themselves
        (confirmed by direct inspection: ``self._datatree.children``
        lists only ``..._partition_NNN`` entries). Its per-row
        ``field_name`` coordinate carries names only, and collecting
        unique names into a ``set()`` then ``sorted()``-ing them (as
        ``metadata()`` does for display) discards the real FIELD_ID
        entirely and does not preserve source order -- confirmed on a
        real MS with non-contiguous FIELD_IDs (0, 2, 3, 5, 6): the
        alphabetically-sorted name list does not line up with FIELD_ID
        order at all.

        Reads the FIELD subtable directly via ``arcae`` (already a hard
        dependency of this class, not a new one) instead -- by CASA
        convention, FIELD_ID *is* the row index into this subtable, the
        same convention ``plotms`` itself relies on for ``field='N'``
        selection.

        Returns an empty dict (never raises) if the subtable can't be
        read for any reason; callers should treat a missing name as
        "authoritative ID unavailable" and fall back accordingly, not
        as a fatal error.
        """
        from arcae import table as _arcae_table
        field_table_path = f"{self._path}/FIELD"
        try:
            ft = _arcae_table(field_table_path)
            try:
                names = ft.getcol("NAME")
            finally:
                ft.close()
        except Exception:
            log.debug("MSv2Backend: could not read FIELD subtable at %s "
                      "for authoritative field IDs", field_table_path,
                      exc_info=True)
            return {}
        return {name: i for i, name in enumerate(names)}

    def metadata(self) -> dict:
        """Collect human-readable metadata from all visibility partitions.

        Triggers a small amount of compute on the non-index string
        coordinates (scan_name, field_name, antenna names) which are
        always small in-memory arrays.  All numeric metadata is derived
        from coordinate values only — VISIBILITY is not read.
        """
        self._require_open()

        scan_names:   set[str] = set()
        field_names:  set[str] = set()
        ant_names:    set[str] = set()
        spw_ids:      set = set()
        spw_detail:   dict = {}
        pol_labels:   set[str] = set()
        t_min = float("inf");  t_max = float("-inf")
        f_min = float("inf");  f_max = float("-inf")
        n_baselines  = 0
        data_columns: set[str] = set()

        for ds in self._iter_visibility_partitions():
            _collect_string_coord(ds, "scan_name",              scan_names)
            _collect_string_coord(ds, "field_name",             field_names)
            _collect_string_coord(ds, "baseline_antenna1_name", ant_names)
            _collect_string_coord(ds, "baseline_antenna2_name", ant_names)

            # Spectral window identity and per-window detail.
            #
            # Goes through _partition_spw_ident rather than reading
            # attrs directly: xarray-ms 0.5.6 puts the identity on the
            # *frequency coordinate* as a name, not in ds.attrs, so the
            # old attrs-only lookup reported no spectral windows at all
            # ("spws=0") on a perfectly ordinary MS.  With nothing to
            # list, the SPW control had nothing to filter by and
            # _spw_selected fell through to keeping every partition.
            ident, ident_kind = self._partition_spw_ident(ds)
            if ident is not None:
                spw_ids.add(ident)
                if ident not in spw_detail and "frequency" in ds.coords:
                    fv = np.asarray(ds.coords["frequency"].values,
                                    dtype=np.float64)
                    width = self._partition_channel_width(ds)
                    if fv.size:
                        # Bandwidth spans the channel *edges*, not the
                        # centres, so a single-channel window still has a
                        # non-zero width.
                        half = (width or 0.0) / 2.0
                        spw_detail[ident] = {
                            "id":             ident,
                            "kind":           ident_kind,
                            "name":           str(
                                ds.coords["frequency"].attrs.get(
                                    "spectral_window_name", "") or ""),
                            "n_channels":     int(fv.size),
                            "centre_freq_hz": float((fv.min() + fv.max()) / 2),
                            "bandwidth_hz":   float(
                                (fv.max() + half) - (fv.min() - half)),
                            "channel_width_hz": width,
                            "freq_min_hz":    float(fv.min()),
                            "freq_max_hz":    float(fv.max()),
                        }

            if "polarization" in ds.coords:
                pol_labels.update(
                    str(p) for p in ds.coords["polarization"].values
                )

            if "time" in ds.coords:
                t = ds.coords["time"].values
                t_min = min(t_min, float(t.min()))
                t_max = max(t_max, float(t.max()))

            if "frequency" in ds.coords:
                f = ds.coords["frequency"].values
                f_min = min(f_min, float(f.min()))
                f_max = max(f_max, float(f.max()))

            n_baselines = max(n_baselines, ds.sizes.get("baseline_id", 0))

            # Data column probe — VISIBILITY is the MSv4 name;
            # the underlying MSv2 column is what the user cares about
            if "VISIBILITY" in ds.data_vars:
                data_columns.add(self._data_column)

        sorted_field_names = sorted(field_names)
        field_id_map = self._field_id_map()
        # Parallel list, aligned by position with sorted_field_names.
        # None for any name the subtable read didn't resolve (shouldn't
        # normally happen, but _field_id_map() never raises, so this
        # stays graceful rather than crashing metadata() entirely).
        field_ids = [field_id_map.get(n) for n in sorted_field_names]

        return {
            "scan_names":        sorted(scan_names),
            "field_names":       sorted_field_names,
            "field_ids":         field_ids,
            "antenna_names":     sorted(ant_names),
            "spw_ids":           sorted(spw_ids, key=lambda v: (isinstance(v, str), v)),
            "spws": [spw_detail[k] for k in
                     sorted(spw_detail, key=lambda v: (isinstance(v, str), v))],
            "correlation_labels": sorted(pol_labels),
            "time_range":        (t_min, t_max),
            "freq_range":        (f_min, f_max),
            "n_baselines":       n_baselines,
            "data_columns":      sorted(data_columns),
        }

    # ------------------------------------------------------------------ #
    # Scatter / line mode query                                            #
    # ------------------------------------------------------------------ #

    def query_columns(
        self,
        xaxis: Axis,
        layers: list[ScatterLayerSpec],
        selection: SelectionSpec,
        *,
        x_range: Optional[tuple[float, float]] = None,
        y_range: Optional[tuple[float, float]] = None,
        color_mode: str = "global",
        width: int = 800,
        height: int = 600,
        probe_grid_max_cells: int = 3072,
    ) -> ScatterRenderResult:
        """Query, bin, and shade scatter layers; return a bounded render result.

        Replaces the pre-2026-09 contract (a raw ``dict[(Axis,pol),
        DataFrame]``) -- see ``ScatterRenderResult``'s docstring in
        ``reader.py`` for why. Step 1 below (the actual per-partition
        data query, via ``_query_columns_raw``) is unchanged from that
        contract; only what happens to the resulting DataFrames, and
        what gets returned, is new -- binning and shading now happen
        here instead of in ``VisibilityScatter``, so only a small
        bounded result per layer (see ``ScatterLayerRender``) ever
        leaves this method, whether it's called in-process
        (``LocalVisibilityReader``) or from a remote worker subprocess
        (``VisplotRemoteBackend``).

        Parameters
        ----------
        xaxis :
            Axis for the x column.
        layers :
            Rendering parameters for each layer -- see
            ``ScatterLayerSpec``. Always pass every layer regardless of
            its current ``alpha`` (including hidden ones): the adaptive
            canvas-size calculation and the "hidden (alpha=0)" skip
            reason both need to see the full layer list, exactly as
            ``VisibilityScatter`` did locally before this moved here. A
            layer's *opacity* is applied by the caller afterward (see
            ``ScatterLayerRender``'s docstring) -- this only decides
            whether alpha==0 means "don't bother shading".
        selection :
            Data selection constraints.
        x_range, y_range :
            Viewport extent. ``None`` (either or both) -> use the full
            resolved data extent for that axis. Pass both ``None`` for
            a fresh axis/selection/layer change; pass the current
            viewport for a pan/zoom re-render (this now requires a
            fresh call -- see the scatter remote-execution design notes
            for why that's no longer free the way it was when the
            DataFrame lived client-side).
        color_mode :
            ``"global"`` or ``"local"`` -- see
            ``VisibilityScatter.set_color_mode``'s docstring; behavior
            is unchanged, just relocated.
        width, height :
            Requested canvas size; the actual size used (after the
            sparse-data adaptive shrink) is returned in
            ``ScatterRenderResult.canvas_width/height``.
        probe_grid_max_cells :
            Resolution cap for the coarse per-bin identity grid computed
            alongside each layer's image -- see
            ``ScatterLayerRender.id_grid_*``'s docstring and
            ``_scatter_render._id_grid_size``.

        Returns
        -------
        ScatterRenderResult
        """
        self._require_open()
        if not layers:
            raise ValueError("query_columns: layers must be non-empty")

        yaxes = [(lyr.y_axis, lyr.polarization) for lyr in layers]
        dataframes = self._query_columns_raw(xaxis, yaxes, selection)

        x0_all, x1_all, y0_all, y1_all = [], [], [], []
        for lyr in layers:
            df = dataframes.get((lyr.y_axis, lyr.polarization))
            if df is not None and len(df) > 0:
                x0_all.append(float(df["x"].min())); x1_all.append(float(df["x"].max()))
                y0_all.append(float(df["y"].min())); y1_all.append(float(df["y"].max()))
        full_x_range = (min(x0_all), max(x1_all)) if x0_all else (0.0, 1.0)
        full_y_range = (min(y0_all), max(y1_all)) if y0_all else (0.0, 1.0)

        xr_ = x_range if x_range is not None else full_x_range
        yr_ = y_range if y_range is not None else full_y_range
        x0, x1 = (min(xr_), max(xr_))
        y0, y1 = (min(yr_), max(yr_))

        canvas_w, canvas_h = _scatter_render.compute_canvas_size(
            dataframes, layers, x0, x1, y0, y1, width, height,
        )

        rendered = tuple(
            _scatter_render.render_layer(
                dataframes.get((lyr.y_axis, lyr.polarization)), lyr,
                x0, x1, y0, y1, canvas_w, canvas_h, color_mode, full_y_range,
                probe_grid_max_cells=probe_grid_max_cells,
            )
            for lyr in layers
        )

        return ScatterRenderResult(
            x_range=full_x_range, y_range=full_y_range,
            canvas_width=canvas_w, canvas_height=canvas_h,
            layers=rendered,
        )

    def _query_columns_raw(
        self,
        xaxis: Axis,
        yaxes: list[tuple[Axis, str]],   # (Axis, polarization_label)
        selection: SelectionSpec,
    ) -> dict[tuple[Axis, str], pd.DataFrame]:
        """Return flat DataFrames for scatter mode, one per (axis, pol) pair.

        Internal step of ``query_columns`` (2026-09) -- unchanged since
        before that redesign; the eager, per-partition-concatenated
        DataFrame this builds was never the problem (see
        ``ScatterRenderResult``'s docstring in ``reader.py``), and stays
        exactly as it was. What changed is that this result no longer
        leaves this process -- ``query_columns`` now bins and shades it
        here instead of returning it directly.

        Uses the adaptive pipeline from test_11:
          < 500K samples  → serial xarray stack
          500K–5M samples → fused dask.compute() + numpy ravel
          > 5M  samples   → + parallel Datashader-ready DataFrames

        Each DataFrame has columns ``x`` and ``y`` with NaN rows already
        dropped — ready for ``datashader.Canvas.points(df, "x", "y")``.

        Parameters
        ----------
        xaxis :
            Axis for the x column (e.g. ``Axis.TIME``, ``Axis.UVDIST``).
        yaxes :
            List of (Axis, polarization) pairs for the y column.
            E.g. ``[(Axis.AMPLITUDE, "XX"), (Axis.AMPLITUDE, "YY")]``.
        selection :
            Data selection constraints.

        Returns
        -------
        dict mapping each (Axis, pol) key to a pandas DataFrame with
        columns ``x``, ``y``.
        """
        self._require_open()

        # Accumulate DataFrames across partitions
        partition_frames: dict[tuple[Axis, str], list[pd.DataFrame]] = {
            key: [] for key in yaxes
        }

        for raw_ds in self._iter_visibility_partitions(selection):
            ds = self._apply_selection(raw_ds, selection)
            if ds.sizes.get("time", 0) == 0:
                continue

            n_samples = self._estimate_samples(ds, selection, len(yaxes))
            use_fused    = HAS_DASK and n_samples >= _THRESH_FUSED
            use_parallel = HAS_DASK and n_samples >= _THRESH_PAR

            frames = self._query_partition_scatter(
                ds, xaxis, yaxes, use_fused=use_fused, use_parallel=use_parallel
            )
            for key, df in frames.items():
                if df is not None and len(df) > 0:
                    partition_frames[key].append(df)

        # Concatenate across partitions
        result = {}
        for key, frames in partition_frames.items():
            if frames:
                result[key] = pd.concat(frames, ignore_index=True)
            else:
                result[key] = pd.DataFrame({"x": [], "y": []})
        return result

    def _query_partition_scatter(
        self,
        ds: xr.Dataset,
        xaxis: Axis,
        yaxes: list[tuple[Axis, str]],
        *,
        use_fused: bool,
        use_parallel: bool,
    ) -> dict[tuple[Axis, str], pd.DataFrame]:
        """Build scatter DataFrames for a single partition.

        Returns a dict mapping (Axis, pol) → DataFrame(x, y).

        Not every partition carries every polarization requested in
        ``yaxes`` (see ``_raster_2d`` for the identical condition on the
        raster path -- an MS can split one nominal SPW across DDIDs with
        different correlation setups, e.g. RR-only vs LL-only vs full-pol
        scans on the same spectral window; confirmed on 3c84scan1.ms).
        ``yaxes`` is filtered down to this partition's locally-present
        polarizations *before* any lazy array is built.  A partition
        missing every requested polarization contributes no keys at all;
        the caller's accumulator dict is pre-seeded with every key in
        the full, unfiltered ``yaxes``, so a key missing here just stays
        empty for this partition while other partitions that do carry it
        still contribute normally -- no polarization becomes globally
        unreachable, only absent from partitions that never recorded it.
        """
        vis  = self._resolve_vis(ds)
        flag = self._flag_mask(ds)

        local_pols = ({str(p) for p in ds.coords["polarization"].values}
                      if "polarization" in ds.coords else set())
        yaxes_local = [key for key in yaxes if key[1] in local_pols]
        if not yaxes_local:
            return {}

        # Build lazy derived arrays for every requested (axis, pol) this
        # partition actually carries.
        lazy_y: dict[tuple[Axis, str], xr.DataArray] = {}
        for axis, pol in yaxes_local:
            lazy_y[(axis, pol)] = self._lazy_quantity(vis, flag, axis, pol, ds)

        # x-axis lazy array — broadcast to match a representative y shape
        template = next(iter(lazy_y.values()))
        lazy_x = self._lazy_x_axis(ds, xaxis, template)

        # Hover-probe redesign piece 2 (2026-09): native-coordinate
        # columns alongside x/y, broadcast to the same template shape --
        # same pattern as lazy_x above, just from ds.coords directly
        # rather than a derived quantity. Conditional per-coordinate: a
        # partition missing one (unexpected, but not fatal) just omits
        # that column, which _scatter_render.render_layer already
        # handles gracefully (see its "id_cols" gate). These feed
        # ScatterLayerRender.id_grid_* -- see that field's docstring for
        # why the identity grid needs raw per-sample native coordinates
        # rather than anything already computed above.
        lazy_id_cols: dict[str, xr.DataArray] = {}
        for coord_name in ("time", "baseline_id", "frequency"):
            if coord_name in ds.coords:
                lazy_id_cols[coord_name] = (
                    ds.coords[coord_name].broadcast_like(template)
                )

        if use_fused:
            # Single dask.compute() — VISIBILITY read once
            id_col_names = list(lazy_id_cols.keys())
            all_lazy = (list(lazy_y.values()) + [lazy_x] +
                        [lazy_id_cols[c] for c in id_col_names])
            computed  = dask.compute(*all_lazy)
            n_y = len(lazy_y)
            y_computed = dict(zip(lazy_y.keys(), computed[:n_y]))
            x_computed = computed[n_y]
            id_computed = dict(zip(id_col_names, computed[n_y + 1:]))

            def _ravel_df(x_arr, y_arr) -> pd.DataFrame:
                x_flat = np.asarray(x_arr).ravel()
                y_flat = np.asarray(y_arr).ravel()
                ok = np.isfinite(x_flat) & np.isfinite(y_flat)
                cols = {"x": x_flat[ok], "y": y_flat[ok]}
                for cname, carr in id_computed.items():
                    cols[cname] = np.asarray(carr).ravel()[ok]
                return pd.DataFrame(cols, copy=False)

            frames = {
                key: _ravel_df(x_computed, y_arr)
                for key, y_arr in y_computed.items()
            }
        else:
            # Serial fallback — xarray stack → to_dataframe (simpler path)
            x_c = lazy_x.compute()
            id_c = {c: arr.compute() for c, arr in lazy_id_cols.items()}
            keep_cols = ["x", "y"] + list(id_c.keys())
            frames = {}
            for key, lazy in lazy_y.items():
                y_c   = lazy.compute()
                x_bc  = x_c.broadcast_like(y_c)
                data_vars = {"x": x_bc, "y": y_c}
                for cname, carr in id_c.items():
                    data_vars[cname] = carr.broadcast_like(y_c)
                stacked = xr.Dataset(data_vars).stack(
                    sample=list(y_c.dims)
                )
                frames[key] = stacked.to_dataframe()[keep_cols].dropna(
                    subset=["x", "y"]
                )

        return frames

    def _lazy_quantity(
        self,
        vis: xr.DataArray,
        flag: xr.DataArray,
        axis: Axis,
        pol: str,
        ds: Optional[xr.Dataset] = None,
    ) -> xr.DataArray:
        """Return a lazy DataArray for the requested axis and polarization.

        Masked with NaN at flagged/padded positions — except U/V, see
        below.

        Uses dask.array.absolute() and dask.array.angle() directly for
        AMPLITUDE and PHASE rather than xr.apply_ufunc().  This avoids
        the ComplexWarning that fires during Dask's meta-inference pass
        when apply_ufunc evaluates np.abs on a zero-element complex128
        meta array.  The dask.array operations are complex-aware and
        produce the correct float64 output dtype without any warnings.

        ``ds`` : the source Dataset, required only for ``Axis.U``/
        ``Axis.V`` (needs the ``UVW`` array, which isn't derivable from
        ``vis``/``flag`` alone the way the other quantities are) —
        optional for every other axis, which don't need it.
        """
        vis_pol  = vis.sel(polarization=pol)
        flag_pol = flag.sel(polarization=pol)

        if axis in (Axis.U, Axis.V):
            # Geometry-derived, not visibility-derived: the same value
            # regardless of polarization, and deliberately NOT flag-
            # masked -- matches _lazy_x_axis's existing Axis.U/Axis.V
            # handling exactly (same quantity, same semantics, whether
            # it's playing the X or Y role). A UV-coverage plot should
            # show every baseline sample that was actually observed,
            # flagged or not -- the point is showing sampling coverage,
            # not current data quality, and masking would misleadingly
            # hide real coverage. Added to let Axis.V work as a scatter
            # Y-axis (previously NotImplementedError'd here) -- see
            # visplot-testing-handoff's u-vs-v UV-coverage test.
            if ds is None:
                raise ValueError(
                    f"Axis.{axis.name} requires ds (the source Dataset) "
                    f"for the UVW array; pass ds= from the caller."
                )
            label = "u" if axis == Axis.U else "v"
            return ds["UVW"].sel(uvw_label=label).broadcast_like(vis_pol)

        if axis == Axis.AMPLITUDE:
            # da.absolute() is complex-aware: |a+bj| -> sqrt(a²+b²), float64
            q = xr.DataArray(
                da.absolute(vis_pol.data),
                coords={k: v for k, v in vis_pol.coords.items()},
                dims=vis_pol.dims,
                attrs=vis_pol.attrs,
            )
        elif axis == Axis.PHASE:
            # da.angle() returns phase in radians; convert to degrees
            q = xr.DataArray(
                da.angle(vis_pol.data) * (180.0 / np.pi),
                coords={k: v for k, v in vis_pol.coords.items()},
                dims=vis_pol.dims,
                attrs=vis_pol.attrs,
            )
        elif axis == Axis.REAL:
            q = vis_pol.real
        elif axis == Axis.IMAGINARY:
            q = vis_pol.imag
        else:
            raise NotImplementedError(
                f"Axis {axis} is not a supported scatter y-axis. "
                f"Use AMPLITUDE, PHASE, REAL, IMAGINARY, U, or V."
            )

        return q.where(~flag_pol)

    def _lazy_x_axis(
        self,
        ds: xr.Dataset,
        xaxis: Axis,
        template: xr.DataArray,
    ) -> xr.DataArray:
        """Return a lazy x-axis DataArray broadcast to *template*'s shape."""
        if xaxis == Axis.TIME:
            return ds.coords["time"].broadcast_like(template)
        elif xaxis == Axis.UVDIST:
            uvdist = self._uvdist_m(ds)   # (time, baseline_id)
            return uvdist.broadcast_like(template)
        elif xaxis == Axis.UVDIST_LAMBDA:
            uvdist_lambda = self._uvdist_lambda(ds)      # (time, baseline_id, frequency)
            # template has (time, baseline_id, frequency) after pol-sel
            return uvdist_lambda.broadcast_like(template)
        elif xaxis == Axis.FREQUENCY:
            return ds.coords["frequency"].broadcast_like(template)
        elif xaxis == Axis.CHANNEL:
            chan = xr.DataArray(
                np.arange(ds.sizes["frequency"]),
                dims=["frequency"],
            )
            return chan.broadcast_like(template)
        elif xaxis == Axis.U:
            return ds["UVW"].sel(uvw_label="u").broadcast_like(template)
        elif xaxis == Axis.V:
            return ds["UVW"].sel(uvw_label="v").broadcast_like(template)
        else:
            raise NotImplementedError(
                f"Axis {xaxis} is not a supported scatter x-axis."
            )

    # ------------------------------------------------------------------ #
    # Raster mode query                                                    #
    # ------------------------------------------------------------------ #

    def query_raster(
        self,
        y_dim: Axis,
        x_dim: Axis,
        quantity: Axis,
        selection: SelectionSpec,
        polarization: Optional[str] = None,
        max_cells: int = 2_000_000,
    ) -> tuple[xr.DataArray, tuple[float, float], tuple[float, float], bool]:
        """Return a computed 2D DataArray, coordinate extents, and decimation flag.

        Reduces the selected data to a 2D float64 array by averaging over
        dimensions not in (y_dim, x_dim), then decimates to at most
        ``max_cells`` cells via a uniform stride applied before ``.compute()``
        so Dask only reads the strided rows/columns from disk.

        See ``XArrayReader.query_raster`` for the full two-level rendering
        contract and parameter documentation.

        Axis combinations:
          - TIME × BASELINE  : average over frequency (and pol)
          - FREQUENCY × BASELINE : average over time (and pol)
          - TIME × FREQUENCY : single baseline via ``selection.baselines``
        """
        self._require_open()

        # Resolve dimension names up front — needed by empty fallback and concat.
        y_name = _axis_to_dim(y_dim)
        x_name = _axis_to_dim(x_dim)

        partitions_2d: list[xr.DataArray] = []

        # Track global coordinate extents across all partitions before any
        # decimation, so x_range/y_range always reflect true data bounds.
        all_x_vals: list[float] = []
        all_y_vals: list[float] = []
        # Frequency coordinates, for the Axis.CHANNEL relabelling below.
        freq_coords: list = []

        for raw_ds in self._iter_visibility_partitions(selection):
            ds = self._apply_selection(raw_ds, selection)
            if ds.sizes.get("time", 0) == 0:
                continue

            # Capture full coordinate extents before decimation
            if x_name in ds.coords:
                xv = ds.coords[x_name].values
                all_x_vals.extend([float(xv.min()), float(xv.max())])
            if y_name in ds.coords:
                yv = ds.coords[y_name].values
                all_y_vals.extend([float(yv.min()), float(yv.max())])

            if "frequency" in ds.coords:
                freq_coords.append(np.asarray(ds.coords["frequency"].values))

            arr = self._raster_2d(ds, y_dim, x_dim, quantity, polarization)
            if arr is not None:
                # Decimate per-partition before .compute() so Dask only
                # reads the strided rows from disk.  Each partition's stride
                # is computed independently from its local cell count; the
                # global stride is re-applied after concat if needed.
                arr, _ = _decimate_agg(arr, y_name, x_name, max_cells)
                partitions_2d.append(arr.compute())

        if not partitions_2d:
            log.warning("query_raster: no data matched selection in %s",
                        self._path)
            empty = xr.DataArray(
                np.full((1, 1), np.nan, dtype=np.float32),
                dims=[y_name, x_name],
                coords={
                    y_name: np.array([0.0]),
                    x_name: np.array([0.0]),
                },
            )
            return empty, (0.0, 1.0), (0.0, 1.0), False

        if len(partitions_2d) == 1:
            agg = partitions_2d[0]
        else:
            try:
                agg = xr.concat(
                    partitions_2d,
                    dim=y_name,
                    join="outer",
                    coords="minimal",
                    compat="override",
                )
            except Exception as exc:
                log.warning("query_raster: could not concat partitions: %s", exc)
                agg = partitions_2d[0]

        # Sort along both display axes -- unconditional, not just in the
        # multi-partition branch. xr.concat() above only concatenates in
        # _iter_visibility_partitions()'s iteration order, which reflects
        # how the partitions were split (by intent/OBS_MODE on this
        # backend), NOT necessarily ascending coordinate order. A given
        # intent revisited at multiple non-contiguous times (e.g. a
        # phase calibrator checked periodically through an observation,
        # a normal ALMA cadence) lands in a single partition covering a
        # non-contiguous time range; concatenating it as one contiguous
        # block after/before its neighbors silently scrambles true
        # chronological order in the result. This was found by directly
        # comparing a visplot Time-vs-Baseline raster against msview's
        # equivalent on the same MS -- a real, confirmed feature (a
        # small flagged/dark region) appeared at a different relative
        # Time position in each tool's rendering, only explainable by
        # an actual ordering difference, not a display-convention one
        # (both tools' Time axes were independently confirmed to
        # increase upward from their tick label positions). Applied
        # even in the single-partition branch as a defensive guarantee,
        # not just where concat is involved -- cheap on already-sorted
        # data, and doesn't rely on assuming a single partition is
        # necessarily already in coordinate order.
        sort_dims = [d for d in (y_name, x_name) if d in agg.dims]
        if sort_dims:
            agg = agg.sortby(sort_dims)

        # Use pre-decimation extents collected partition-by-partition above.
        # Do NOT derive extents from agg.coords here because: (a) the
        # per-partition _decimate_agg pass may have already strided away the
        # last coordinate value, and (b) the global pass below will do so again.
        if all_x_vals:
            x_range = (min(all_x_vals), max(all_x_vals))
        else:
            x_coords_full = agg.coords[x_name].values if x_name in agg.coords else np.array([0.0, 1.0])
            x_range = (float(x_coords_full.min()), float(x_coords_full.max()))
        if all_y_vals:
            y_range = (min(all_y_vals), max(all_y_vals))
        else:
            y_coords_full = agg.coords[y_name].values if y_name in agg.coords else np.array([0.0, 1.0])
            y_range = (float(y_coords_full.min()), float(y_coords_full.max()))

        # Final decimation pass on the concatenated agg to enforce max_cells
        # globally (the per-partition pass above may have been under-strict
        # because each partition didn't know the total cell count).
        agg, is_decimated = _decimate_agg(agg, y_name, x_name, max_cells)

        # Relabel the frequency axis as a channel index when asked, and
        # when a channel index is actually well defined.  Done *after*
        # the final decimation so the indices are true positions in the
        # original coordinate -- striding retains channels 0, 4, 8 ...,
        # and numbering those consecutively would produce an axis that
        # looks right and is wrong, precisely on zoomed-out views.
        #
        # The reference frequencies ride along in the agg's attrs so the
        # mapping stays invertible: the probe has to turn a channel range
        # back into Hz to look up fields, scans and antennas, and FlagDB
        # needs frequencies because they outlive spectral-window
        # renumbering.
        if x_dim is Axis.CHANNEL and channel_axis_is_unambiguous(freq_coords):
            agg = to_channel_index(agg, "x", freq_coords[0])
            x_range = (float(agg.coords[x_name].values.min()),
                       float(agg.coords[x_name].values.max()))
        elif y_dim is Axis.CHANNEL and channel_axis_is_unambiguous(freq_coords):
            agg = to_channel_index(agg, "y", freq_coords[0])
            y_range = (float(agg.coords[y_name].values.min()),
                       float(agg.coords[y_name].values.max()))

        return agg, x_range, y_range, is_decimated

    def _raster_2d(
        self,
        ds: xr.Dataset,
        y_dim: Axis,
        x_dim: Axis,
        quantity: Axis,
        polarization: Optional[str],
    ) -> Optional[xr.DataArray]:
        """Reduce a single partition to a 2D DataArray for raster mode."""
        vis  = self._resolve_vis(ds)
        flag = self._flag_mask(ds)
        eit  = ds.get("EFFECTIVE_INTEGRATION_TIME")  # (time, baseline_id) or None

        y_name = _axis_to_dim(y_dim)
        x_name = _axis_to_dim(x_dim)

        # --- compute the raw quantity array ---
        if quantity == Axis.FLAG:
            # FLAG: mean over dims not in (y_dim, x_dim) gives flag fraction.
            # Mask padded slots via EIT (NaN for padded baseline positions).
            frac = flag.astype(float).mean(
                dim=[d for d in flag.dims
                     if d not in (y_name, x_name)],
                skipna=True,
            )
            if eit is not None:
                frac = frac.where(np.isfinite(eit))
            return _drop_non_raster_coords(frac, y_name, x_name)

        # Visibility-derived quantities require polarization selection
        if polarization is None:
            log.warning(
                "query_raster: polarization required for %s; "
                "defaulting to first available", quantity
            )
            polarization = str(ds.coords["polarization"].values[0])

        # Not every partition carries every polarization product: an MS
        # can split one nominal SPW across DDIDs with different
        # correlation setups (e.g. RR-only vs LL-only vs full-pol scans
        # on the same spectral window -- confirmed on 3c84scan1.ms).  A
        # partition that doesn't have the requested polarization simply
        # has nothing to contribute to *this* render -- exactly like a
        # partition outside the requested time/baseline/SPW range -- so
        # it is skipped here rather than raising.  ``query_raster``'s
        # caller already tolerates a ``None`` return (see the ``arr is
        # not None`` check around its ``_raster_2d`` call).  This does
        # not make the data unreachable: selecting a polarization that a
        # given partition *does* carry (e.g. via the sidebar Correlation
        # control) still renders it normally.
        if polarization not in {str(p) for p in ds.coords["polarization"].values}:
            return None

        vis_pol  = vis.sel(polarization=polarization)
        flag_pol = flag.sel(polarization=polarization)

        if quantity == Axis.AMPLITUDE:
            q = xr.DataArray(
                da.absolute(vis_pol.data),
                coords={k: v for k, v in vis_pol.coords.items()},
                dims=vis_pol.dims,
                attrs=vis_pol.attrs,
            ).where(~flag_pol)
        elif quantity == Axis.PHASE:
            q = xr.DataArray(
                da.angle(vis_pol.data) * (180.0 / np.pi),
                coords={k: v for k, v in vis_pol.coords.items()},
                dims=vis_pol.dims,
                attrs=vis_pol.attrs,
            ).where(~flag_pol)
        elif quantity == Axis.REAL:
            q = vis_pol.real.where(~flag_pol)
        elif quantity == Axis.IMAGINARY:
            q = vis_pol.imag.where(~flag_pol)
        else:
            raise NotImplementedError(
                f"Raster quantity {quantity.name} not supported. "
                f"Use AMPLITUDE, PHASE, REAL, IMAGINARY, or FLAG."
            )

        # --- reduce to 2D (y_name × x_name) ---
        reduce_dims = [d for d in q.dims if d not in (y_name, x_name)]
        if reduce_dims:
            q = q.mean(dim=reduce_dims, skipna=True)

        # Verify we ended up with the right 2D shape
        if set(q.dims) != {y_name, x_name}:
            log.warning(
                "_raster_2d: unexpected dims %s after reduction "
                "(expected {%s, %s}); skipping partition",
                q.dims, y_name, x_name,
            )
            return None

        # Transpose to (y_name, x_name) as required by Canvas.raster()
        return _drop_non_raster_coords(q, y_name, x_name).transpose(y_name, x_name)

    # ------------------------------------------------------------------ #
    # UV-coverage (special case — both axes from UVW)                     #
    # ------------------------------------------------------------------ #

    def query_uv_coverage(
        self,
        selection: SelectionSpec,
        include_conjugate: bool = True,
    ) -> pd.DataFrame:
        """Return a flat DataFrame of (u, v) points for UV-coverage plots.

        No VISIBILITY access — only UVW coordinates are read.
        NaN-padded baseline slots are dropped automatically.

        Parameters
        ----------
        selection :
            Data selection (time_range, field_names, etc. applied).
        include_conjugate :
            If True (default), adds (-u, -v) conjugate baseline points.
        """
        self._require_open()
        u_parts, v_parts = [], []

        for raw_ds in self._iter_visibility_partitions(selection):
            ds = self._apply_selection(raw_ds, selection)
            if ds.sizes.get("time", 0) == 0:
                continue

            uvw = ds["UVW"].compute()
            u = uvw.sel(uvw_label="u").values.ravel()
            v = uvw.sel(uvw_label="v").values.ravel()
            finite = np.isfinite(u) & np.isfinite(v)
            u_parts.append(u[finite])
            v_parts.append(v[finite])

        if not u_parts:
            return pd.DataFrame({"x": [], "y": []})

        u_all = np.concatenate(u_parts)
        v_all = np.concatenate(v_parts)

        if include_conjugate:
            u_all = np.concatenate([u_all, -u_all])
            v_all = np.concatenate([v_all, -v_all])

        return pd.DataFrame({"x": u_all, "y": v_all})

    # ------------------------------------------------------------------ #
    # Flagging-safe threshold detection                                   #
    # ------------------------------------------------------------------ #

    def samples_per_pixel(
        self,
        y_dim: Axis,
        x_dim: Axis,
        selection: SelectionSpec,
        canvas_width: int,
        canvas_height: int,
    ) -> tuple[float, float]:
        """Return estimated (x_ratio, y_ratio) grid cells per canvas pixel.

        Used by VisibilityPlotter to decide whether to enable flag
        interaction.  Both ratios ≤ 1.0 → flagging-safe threshold:
        each canvas pixel covers at most one grid cell.

        Parameters
        ----------
        y_dim, x_dim :
            Native raster axes, e.g. ``Axis.TIME``, ``Axis.BASELINE``.
        """
        self._require_open()

        x_name = _axis_to_dim(x_dim)
        y_name = _axis_to_dim(y_dim)

        total_x = total_y = 0
        for raw_ds in self._iter_visibility_partitions(selection):
            ds = self._apply_selection(raw_ds, selection)
            total_x = max(total_x, ds.sizes.get(x_name, 0))
            total_y = max(total_y, ds.sizes.get(y_name, 0))

        ratio_x = total_x / canvas_width  if canvas_width  > 0 else float("inf")
        ratio_y = total_y / canvas_height if canvas_height > 0 else float("inf")
        return ratio_x, ratio_y


    # ------------------------------------------------------------------ #
    # Scatter region probe (hover-probe redesign piece 3, click-to-exact) #
    # ------------------------------------------------------------------ #

    def probe_scatter_region(
        self,
        x_axis: Axis,
        yaxes: list[tuple[Axis, str]],
        selection: SelectionSpec,
        x_range: tuple[float, float],
        y_range: tuple[float, float],
        max_samples: int = 200_000,
    ) -> dict[tuple[Axis, str], dict]:
        """Exact per-sample identity spans for a scatter rectangle.

        See ``XArrayReader.probe_scatter_region`` for the full contract.
        Replaces the pre-"coarse but free" ``probe_raster_pixel``/
        ``probe_scatter_pixel`` pair that used to live here -- see the
        "Pixel hover probe" note above ``probe_scatter_region``'s
        abstract declaration in ``reader.py`` for why those were dead
        code, not a working precedent this could have extended.

        Implementation shape mirrors ``_query_partition_scatter``: one
        partition pass, VISIBILITY/FLAG resolved once per partition and
        reused across every requested layer that partition locally
        carries. Unlike ``_query_partition_scatter``, each layer's mask
        is ``.compute()``-ed individually rather than fused into one
        ``dask.compute()`` call across all layers -- simpler, and an
        acceptable cost here: this runs once per click/drag, not once
        per render, so the fused-compute optimization's payoff (avoiding
        N-times VISIBILITY reads on a hot path) does not apply the same
        way. Worth revisiting only if clicks turn out to be far more
        frequent in practice than designed for.
        """
        self._require_open()
        if not yaxes:
            raise ValueError("probe_scatter_region: yaxes must be non-empty")

        x0, x1 = min(x_range), max(x_range)
        y0, y1 = min(y_range), max(y_range)

        # Per-layer running state, keyed by (y_axis, polarization).
        counts:      dict = {key: 0 for key in yaxes}
        too_many:    set  = set()
        t_lo:  dict = {}; t_hi:  dict = {}
        bl_lo: dict = {}; bl_hi: dict = {}
        # Discrete matched baseline_ids, not just their min/max -- see
        # probe_scatter_region's docstring for why bl_range alone is not
        # enough for exact antenna-pair resolution.
        bl_ids_seen: dict = {key: set() for key in yaxes}
        f_lo:  dict = {}; f_hi:  dict = {}

        def _update_range(lo_map, hi_map, key, lo, hi):
            if key not in lo_map:
                lo_map[key], hi_map[key] = lo, hi
            else:
                lo_map[key] = min(lo_map[key], lo)
                hi_map[key] = max(hi_map[key], hi)

        for raw_ds in self._iter_visibility_partitions(selection):
            if len(too_many) == len(yaxes):
                break   # every requested layer already over budget

            ds = self._apply_selection(raw_ds, selection)
            if ds.sizes.get("time", 0) == 0:
                continue

            local_pols = ({str(p) for p in ds.coords["polarization"].values}
                          if "polarization" in ds.coords else set())
            local_keys = [k for k in yaxes
                          if k[1] in local_pols and k not in too_many]
            if not local_keys:
                continue

            vis  = self._resolve_vis(ds)
            flag = self._flag_mask(ds)

            for (yaxis, pol) in local_keys:
                key = (yaxis, pol)
                lazy_y = self._lazy_quantity(vis, flag, yaxis, pol, ds)
                lazy_x = self._lazy_x_axis(ds, x_axis, lazy_y)
                mask = ((lazy_x >= x0) & (lazy_x <= x1) &
                        (lazy_y >= y0) & (lazy_y <= y1))
                mask_c = mask.compute()
                n = int(mask_c.values.sum())
                if n == 0:
                    continue

                counts[key] += n
                if counts[key] > max_samples:
                    too_many.add(key)
                    continue

                if "time" in mask_c.dims:
                    other = [d for d in mask_c.dims if d != "time"]
                    t_reduced = (mask_c.any(dim=other).values if other
                                 else mask_c.values)
                    matched_t = ds.coords["time"].values[t_reduced]
                    if matched_t.size:
                        _update_range(t_lo, t_hi, key,
                                      float(matched_t.min()),
                                      float(matched_t.max()))

                if "baseline_id" in mask_c.dims:
                    other = [d for d in mask_c.dims if d != "baseline_id"]
                    bl_reduced = (mask_c.any(dim=other).values if other
                                  else mask_c.values)
                    matched_bl = ds.coords["baseline_id"].values[bl_reduced]
                    if matched_bl.size:
                        _update_range(bl_lo, bl_hi, key,
                                      float(matched_bl.min()),
                                      float(matched_bl.max()))
                        bl_ids_seen[key].update(int(b) for b in matched_bl)

                if "frequency" in mask_c.dims:
                    other = [d for d in mask_c.dims if d != "frequency"]
                    f_reduced = (mask_c.any(dim=other).values if other
                                 else mask_c.values)
                    matched_f = ds.coords["frequency"].values[f_reduced]
                    if matched_f.size:
                        _update_range(f_lo, f_hi, key,
                                      float(matched_f.min()),
                                      float(matched_f.max()))

        result: dict = {}
        for key in yaxes:
            if key in too_many:
                result[key] = {
                    "status":     "too_many_points",
                    "n_samples":  counts[key],
                    "t_range":    None, "bl_range": None, "bl_ids": None,
                    "freq_range": None,
                }
            elif counts[key] == 0:
                result[key] = {
                    "status":     "no_data",
                    "n_samples":  0,
                    "t_range":    None, "bl_range": None, "bl_ids": None,
                    "freq_range": None,
                }
            else:
                result[key] = {
                    "status":     "ok",
                    "n_samples":  counts[key],
                    "t_range":    (t_lo[key], t_hi[key]) if key in t_lo else None,
                    "bl_range":   (bl_lo[key], bl_hi[key]) if key in bl_lo else None,
                    "bl_ids":     (sorted(bl_ids_seen[key])
                                   if bl_ids_seen[key] else None),
                    "freq_range": (f_lo[key], f_hi[key]) if key in f_lo else None,
                }
        return result

    def identity_tables(
        self,
        selection: SelectionSpec,
        *,
        polarization: Optional[str] = None,
    ) -> IdentityTables:
        """Static per-selection identity tables -- see ``IdentityTables``'s
        docstring in ``reader.py`` for the full rationale.

        Scans partition coordinate arrays only, exactly the same access
        pattern ``probe_raster_pixel``'s pre-2026-09 inline
        identity-gathering used -- this replaces that per-hover scan
        with one done once per (selection, polarization), consumed
        locally afterward via ``VisibilityPlot._match_identity``.
        """
        self._require_open()

        scans: dict[tuple[str, str], list] = {}
        baseline_antennas: dict[int, tuple[str, str]] = {}
        spw_freqs: dict = {}   # spw_id -> (frequencies ndarray, channel_width)

        for raw_ds in self._iter_visibility_partitions(selection):
            ds = self._apply_selection(raw_ds, selection)
            if ds.sizes.get("time", 0) == 0:
                continue

            # Same rationale as probe_raster_pixel's `polarization`
            # parameter: a partition that doesn't carry the displayed
            # polarization contributed nothing to any rendered pixel,
            # so its identity shouldn't be reported either.
            if (polarization is not None and "polarization" in ds.coords
                    and polarization not in
                        {str(p) for p in ds.coords["polarization"].values}):
                continue

            if ("scan_name" in ds.coords and "field_name" in ds.coords
                    and "time" in ds.coords):
                t  = ds.coords["time"].values
                sc = ds.coords["scan_name"].values.astype(str)
                fl = ds.coords["field_name"].values.astype(str)
                local = pd.DataFrame({"scan": sc, "field": fl, "t": t})
                grouped = local.groupby(["scan", "field"], sort=False)["t"] \
                                .agg(["min", "max"])
                for (s, f), row in grouped.iterrows():
                    key = (s, f)
                    lo, hi = float(row["min"]), float(row["max"])
                    if key not in scans:
                        scans[key] = [lo, hi]
                    else:
                        entry = scans[key]
                        entry[0] = min(entry[0], lo)
                        entry[1] = max(entry[1], hi)

            if ("baseline_antenna1_name" in ds.coords
                    and "baseline_id" in ds.coords):
                bl_ids = ds.coords["baseline_id"].values.astype(np.int64)
                ant1 = ds.coords["baseline_antenna1_name"].values.astype(str)
                ant2 = ds.coords["baseline_antenna2_name"].values.astype(str)
                uniq_ids, first_idx = np.unique(bl_ids, return_index=True)
                for bid, idx in zip(uniq_ids, first_idx):
                    bid_i = int(bid)
                    if bid_i not in baseline_antennas:
                        baseline_antennas[bid_i] = (
                            str(ant1[idx]), str(ant2[idx])
                        )

            if "frequency" in ds.coords:
                key = self._partition_spw_id(raw_ds)
                if key is not None and key not in spw_freqs:
                    freqs = np.asarray(
                        ds.coords["frequency"].values, dtype=np.float64
                    ).copy()
                    width = self._partition_channel_width(raw_ds)
                    spw_freqs[key] = (freqs, width)

        scan_infos = tuple(
            ScanInfo(scan_name=k[0], field_name=k[1],
                     t_start=v[0], t_end=v[1])
            for k, v in scans.items()
        )
        spw_infos = tuple(
            SpwInfo(spw_id=k, frequencies=freqs, channel_width_hz=width)
            for k, (freqs, width) in spw_freqs.items()
        )
        return IdentityTables(
            scans=scan_infos,
            baseline_antennas=baseline_antennas,
            spws=spw_infos,
        )

    # ------------------------------------------------------------------ #
    # Representation                                                       #
    # ------------------------------------------------------------------ #

    def __repr__(self) -> str:  # pragma: no cover
        status = "open" if self._datatree is not None else "closed"
        return (
            f"MSv2Backend({self._path!r}, "
            f"column={self._data_column!r}, {status})"
        )


# ======================================================================
# Module-level helpers
# ======================================================================

def _check_xarray_ms() -> None:
    """Raise ImportError with a helpful message if xarray-ms is absent."""
    try:
        import xarray_ms  # noqa: F401
    except ImportError as exc:
        raise ImportError(
            "xarray-ms is required for MSv2Backend.\n"
            "Install: pip install xarray-ms\n"
            "xarray-ms requires arcae (C++ casacore bindings); "
            "see https://xarray-ms.readthedocs.io/ for platform notes.\n"
            "Ensure pyarrow version matches what arcae was built against "
            "(arcae 0.5.x requires pyarrow 23)."
        ) from exc


def _collect_string_coord(
    ds: xr.Dataset, name: str, target: set
) -> None:
    """Add unique non-empty string values of *name* from *ds* to *target*.

    Uses explicit key-in-mapping checks rather than truthiness tests to
    avoid the xarray "truth value of array is ambiguous" ValueError that
    fires when an xr.DataArray is used in a boolean context.
    """
    if name in ds.coords:
        da = ds.coords[name]
    elif name in ds.data_vars:
        da = ds.data_vars[name]
    else:
        return
    vals = da.values if hasattr(da, "values") else da.compute().values
    target.update(str(v) for v in vals.ravel() if v)


def _drop_non_raster_coords(
    arr: xr.DataArray, y_name: str, x_name: str,
) -> xr.DataArray:
    """Strip every coordinate except the two the 2D raster actually needs.

    ``_raster_2d`` builds its quantity array from ``vis_pol``/``flag``,
    which — as an MSv2 partition Dataset's derived arrays — carry every
    coordinate that happened to ride along the ``baseline_id`` (or
    ``time``/``frequency``) dimension, including auxiliary *display*
    labels such as ``baseline_antenna1_name`` / ``baseline_antenna2_name``
    (string dtype). Reduction (``.mean(dim=...)``) only drops coordinates
    *along the reduced dimensions*, so these survive into the returned
    2D array untouched.

    That is harmless for a single partition, but ``query_raster()`` then
    concatenates partitions with ``xr.concat(..., join="outer")``. When
    partitions disagree on which categorical-axis members are present —
    which fields observe which antennas differs in practice — that
    concat has to reconcile the categorical (``baseline_id``) dimension
    across partitions, which drags these string auxiliary coordinates
    into an alignment/fill-value computation library code does not
    expect a string dtype in. Confirmed as the source of
    ``ufunc 'minimum' did not contain a loop with signature matching
    types (dtype('<U...')...)`` on a Field change/step in duo mode
    (I-1's manual round-trip testing, August 2026) — the same message a
    manual Field ``Select`` + Plot ▶ would produce, since both paths
    funnel through this exact function.

    Safe to drop unconditionally: the probe/hover path
    (``_probe_pixel`` and friends) re-reads ``baseline_antenna1_name`` /
    ``baseline_antenna2_name`` / ``field_name`` / ``scan_name`` directly
    from the source partition's own ``ds.coords`` — never from
    ``_raster_2d``'s return value — so nothing downstream of this
    function actually uses these coordinates. Only ``y_name``/``x_name``
    (the dimension coordinates ``Canvas.raster()`` and the extent
    computation in ``query_raster()`` need) are kept.
    """
    extra = [c for c in arr.coords if c not in (y_name, x_name)]
    return arr.drop_vars(extra) if extra else arr


def _axis_to_dim(axis: Axis) -> str:
    """Map an Axis enum value to its xarray-ms dimension name.

    Only native axes that correspond directly to DataTree dimensions are
    supported here.  Derived axes (AMPLITUDE, PHASE, etc.) do not have
    a dimension and are not valid inputs.
    """
    _MAP = {
        Axis.TIME:        "time",
        Axis.BASELINE:    "baseline_id",   # Axis.BASELINE → baseline_id dim
        Axis.FREQUENCY:   "frequency",
        Axis.CHANNEL:     "frequency",     # channel index shares the freq dim
        Axis.CORRELATION: "polarization",
    }
    dim = _MAP.get(axis)
    if dim is None:
        raise ValueError(
            f"Axis.{axis.name} does not correspond to a native MS dimension. "
            f"Supported: {[a.name for a in _MAP]}"
        )
    return dim



def _decimate_agg(
    agg: xr.DataArray,
    y_name: str,
    x_name: str,
    max_cells: int,
) -> tuple[xr.DataArray, bool]:
    """Stride a 2D agg DataArray to fit within ``max_cells`` cells.

    Computes the stride in each dimension that reduces the total cell count
    to at most ``max_cells`` while preserving the aspect ratio of the grid.
    The stride is applied via ``isel()`` *before* ``.compute()`` so that
    Dask only reads the selected rows/columns from disk.

    Parameters
    ----------
    agg :
        Lazy or computed 2D DataArray, shape (n_y, n_x).
    y_name, x_name :
        Dimension names for the y (row) and x (column) axes.
    max_cells :
        Maximum allowed cells in the output.

    Returns
    -------
    agg_out : xr.DataArray
        Strided DataArray.  Identical to ``agg`` if no stride was needed.
    is_decimated : bool
        ``True`` if any stride > 1 was applied.
    """
    import math
    n_y, n_x = agg.sizes[y_name], agg.sizes[x_name]
    total = n_y * n_x

    if total <= max_cells:
        return agg, False

    # Compute per-dimension strides preserving aspect ratio:
    #   stride_y / stride_x ≈ n_y / n_x
    # From: (n_y / stride_y) * (n_x / stride_x) <= max_cells
    #       stride_y = stride_x * (n_y / n_x)
    # Substituting: (n_x / stride_x)^2 * (n_y / n_x) <= max_cells
    #   stride_x = ceil( sqrt(n_x^2 / (max_cells * n_x / n_y)) )
    #            = ceil( sqrt(n_x * n_y / max_cells) )
    scale = math.sqrt(total / max_cells)
    stride_y = max(1, math.ceil(scale * math.sqrt(n_y / n_x)))
    stride_x = max(1, math.ceil(scale * math.sqrt(n_x / n_y)))

    # Clamp so we always keep at least 2 cells on each axis
    stride_y = min(stride_y, n_y // 2 or 1)
    stride_x = min(stride_x, n_x // 2 or 1)

    log.debug(
        "_decimate_agg: (%d, %d) -> stride (%d, %d) -> (~%d, ~%d)  max_cells=%d",
        n_y, n_x, stride_y, stride_x,
        math.ceil(n_y / stride_y), math.ceil(n_x / stride_x),
        max_cells,
    )

    agg_out = agg.isel(
        {y_name: slice(None, None, stride_y),
         x_name: slice(None, None, stride_x)},
    )
    return agg_out, True


def _count_selected_time(ds: xr.Dataset, sel: SelectionSpec) -> int:
    """Estimate time samples surviving selection without reading data."""
    total = ds.sizes.get("time", 0)
    if sel.time_range is not None and "time" in ds.coords:
        t = ds.coords["time"].values
        t0, t1 = sel.time_range
        return int(np.sum((t >= t0) & (t <= t1)))
    if sel.field_names is not None and "field_name" in ds.coords:
        field = ds.coords["field_name"].values
        return int(np.isin(field, list(sel.field_names)).sum())
    return total


def _count_selected_baselines(ds: xr.Dataset, sel: SelectionSpec) -> int:
    """Estimate baseline_id samples surviving selection without reading data."""
    total = ds.sizes.get("baseline_id", 0)
    # baselines takes precedence over antenna_names (matches _apply_selection)
    if sel.baselines is not None:
        return min(len(sel.baselines), total)
    if sel.antenna_names is not None and "baseline_antenna1_name" in ds.coords:
        ant1 = ds.coords["baseline_antenna1_name"].values
        ant2 = ds.coords["baseline_antenna2_name"].values
        return int((np.isin(ant1, list(sel.antenna_names)) |
                    np.isin(ant2, list(sel.antenna_names))).sum())
    return total


def _count_selected_channels(ds: xr.Dataset, sel: SelectionSpec) -> int:
    """Estimate frequency samples surviving selection without reading data."""
    total = ds.sizes.get("frequency", 0)
    if sel.channel_range is not None:
        c0, c1 = sel.channel_range
        return min(c1, total) - max(c0, 0)
    if sel.freq_range is not None and "frequency" in ds.coords:
        f = ds.coords["frequency"].values
        f0, f1 = sel.freq_range
        return int(((f >= f0) & (f <= f1)).sum())
    return total
