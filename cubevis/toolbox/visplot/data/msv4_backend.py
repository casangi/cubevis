"""MSv4Backend — ``XArrayReader`` implementation for MSv4 Zarr Processing Sets.

MSv4 Processing Sets (``*.ps.zarr``) are plain Zarr DataTrees produced by
``xradio.measurement_set.convert_msv2_to_processing_set()`` or by writing
an ``xarray-ms``-opened MSv2 DataTree to Zarr via ``DataTree.to_zarr()``.
They are opened here with ``xarray.open_datatree(path, engine="zarr")``,
which is xarray's own Zarr engine — no xradio or xarray-ms dependency is
required in the read path.

The on-disk structure is a DataTree whose leaf nodes fall into two categories:

1. **Visibility partition nodes** — identified by ``ds.attrs["type"] == "visibility"``.
   These contain the correlated data and share the same dimension vocabulary
   as ``MSv2Backend``:

       Dimensions: (time, baseline_id, frequency, polarization, uvw_label)
       Coords:     time, baseline_id, frequency, polarization, uvw_label,
                   field_name (on time), scan_name (on time),
                   baseline_antenna1_name (on baseline_id),
                   baseline_antenna2_name (on baseline_id)
       Data vars:  VISIBILITY, FLAG, UVW, WEIGHT, EFFECTIVE_INTEGRATION_TIME

2. **Sub-dataset nodes** — ``antenna_xds``, ``field_and_source_*_xds``, etc.
   These are children of the visibility partition nodes and are skipped by
   all query methods.

Key differences from ``MSv2Backend``
--------------------------------------
* **Open path**: ``xr.open_datatree(path, engine="zarr")`` with
  ``consolidated=True`` (fast single-file metadata read) falling back to the
  default walk if consolidated metadata is absent.
* **Partition cache**: ``self._partitions`` is built once at ``open()`` and
  reused by every query call, avoiding repeated DataTree subtree walks that
  grow linearly with partition count.
* **Partition filter**: visibility nodes are identified by
  ``ds.attrs.get("type") == "visibility"``.
* **UVW label coordinate**: MSv4 Zarr uses string labels ``"u"/"v"/"w"``
  on the ``uvw_label`` dimension.  ``sel(uvw_label="u")`` works identically.
* **SPW attribute key**: ``ds.attrs.get("spectral_window_id")``.

Performance optimizations for large Processing Sets
----------------------------------------------------
Three complementary optimizations apply at TB-scale while remaining neutral
or beneficial for small stores like sis14:

**A — Partition cache** (``self._partitions``, built at ``open()``):
  Eliminates repeated DataTree subtree walks.  Each walk is O(N_nodes) Python
  overhead; at 100 partitions × 3 nodes/partition = 300 checks per call.
  Two calls per ``query_raster`` = 600 checks avoided per render.

**B — Cross-partition fused ``dask.compute()``**:
  ``query_raster`` and ``query_columns`` collect all per-partition lazy arrays
  and issue a single ``dask.compute(*all_lazy)`` instead of one ``.compute()``
  per partition.  Dask's threaded scheduler then parallelises reads across
  partitions.  Benchmarked at 16x speedup on 16 partitions (all-zeros arrays)
  versus the sequential loop.  Zarr reads are thread-safe.

**C — Two-step chunk-aligned ``isel`` for ``time_range`` and ``freq_range``**:
  When ``sel.time_range`` or ``sel.freq_range`` is the sole constraint on that
  dimension, ``_apply_selection`` first issues a chunk-aligned ``isel(slice)``
  to tell Dask which Zarr chunks to read (eliminating partial chunk reads at
  the boundaries), then immediately applies an exact boolean mask within the
  already-narrowed dataset to trim to the precise requested bounds.  The two
  steps are cheap: the slice controls which chunks Dask schedules; the mask
  runs on a small in-memory/dask array.  The snapped region is at most
  ``chunk_size - 1`` elements wider than the requested range per boundary —
  but the final result is always exactly the requested range, preserving the
  correctness invariant that ``_apply_selection`` returns only what was asked
  for.  At 100K rows and chunk_size=100, the worst-case overhead is 99 extra
  rows read in step 1, immediately discarded in step 2.

Conversion from MSv2
---------------------
The recommended way to create test data is to convert the existing
``sis14_twhya_calibrated_flagged.ms`` MSv2 to a ``.ps.zarr`` using the
``create_test_msv4.py`` script included alongside this file, which uses
``xarray-ms`` to open the MSv2 and then ``DataTree.to_zarr()`` to write it.

References
----------
msvis_design.md §4.2 (XArrayReader), §4.6 (MSv4 coordinate model)
xarray-ms docs: https://xarray-ms.readthedocs.io/
xarray Zarr IO: https://docs.xarray.dev/en/stable/user-guide/io.html#zarr
"""

from __future__ import annotations

import logging
import math
import threading
from typing import Optional, Iterator

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
    XArrayReader,
    _compute_axis_values,
    _agg_value,
    _bin_membership,
    _cell_bounds,
    _widen_if_degenerate,
)
from ..axes import Axis, AxisInfo, AxisType
from ..selection import SelectionSpec

log = logging.getLogger(__name__)

# Zarr engine name — xarray's own built-in engine, no extra backend needed
_ZARR_ENGINE = "zarr"

# Adaptive pipeline thresholds — identical to MSv2Backend (confirmed by test_11)
_THRESH_FUSED = 500_000
_THRESH_PAR   = 5_000_000

# Speed of light
_C_MS = 299_792_458.0


class MSv4Backend(XArrayReader):
    """``XArrayReader`` backed by MSv4 Zarr Processing Sets.

    Opens an MSv4 ``*.ps.zarr`` store produced by ``xradio`` or by exporting
    an ``xarray-ms`` DataTree to Zarr.  Uses ``xarray.open_datatree`` with
    ``engine="zarr"`` — xarray's own Zarr backend.  No xradio or xarray-ms
    dependency is required in the read path.

    The interface is identical to ``MSv2Backend``.  The query, selection, and
    probe methods share the same signatures and return types.

    Parameters
    ----------
    path :
        Path to the ``*.ps.zarr`` directory (the Processing Set root).
    chunks :
        Dask chunk specification forwarded to ``xarray.open_datatree``.
        ``None`` uses ``{"time": 100, "baseline_id": 100}`` which matches
        the ``MSv2Backend`` default.
    data_group :
        Which MSv4 ``data_group`` to read visibility and flag data from.
        MSv4 stores can contain multiple copies of VISIBILITY/FLAG/WEIGHT/UVW
        (e.g. ``'base'`` for raw data, ``'imaging'`` for calibrated data, or
        pipeline-specific names like ``'VLASS_v3'``).  The ``data_group``
        attribute in the dataset specifies which variable names to use —
        for example ``{'correlated_data': 'VISIBILITY_CORRECTED', 'flag':
        'FLAG', 'weight': 'WEIGHT_IMAGING', 'uvw': 'UVW'}``.

        ``None`` (default) uses the first available data group if
        ``data_groups`` is present in ``ds.attrs``, or falls back to reading
        ``VISIBILITY`` / ``DATA`` / ``SPECTRUM`` directly by name if
        ``data_groups`` is absent (the xarray-ms-written store case).

        This is the one parameter that cannot be auto-detected: if a store
        has both raw and calibrated visibility arrays, the backend cannot
        know which one the caller wants.
    observation_mode :
        ``'interferometer'``, ``'single_dish'``, or ``'auto'`` (default).
        Controls which primary data variable and baseline dimension to expect:

        * ``'interferometer'``: VISIBILITY on ``[time, baseline_id, frequency,
          polarization]``.  Baseline-based queries (UV-coverage, antenna pair
          selection) are available.
        * ``'single_dish'``: SPECTRUM on ``[time, antenna_name, frequency,
          polarization]``.  Baseline-based queries return empty results.
        * ``'auto'``: detected at ``open()`` time by inspecting the first
          partition's dimensions.  If ``baseline_id`` is present it is
          interferometer; if ``antenna_name`` is present it is single dish.

        In practice ``'auto'`` is almost always the right choice.  An explicit
        value is useful when opening a store before the first partition's
        structure can be inspected (e.g. for schema validation tooling).
    """

    _DEFAULT_CHUNKS: dict = {"time": 100, "baseline_id": 100}

    #: Valid observation mode strings accepted at construction time.
    OBSERVATION_MODES = frozenset({"interferometer", "single_dish", "auto"})

    def __init__(
        self,
        path: str,
        chunks: Optional[dict] = None,
        data_group: Optional[str] = None,
        observation_mode: str = "auto",
    ) -> None:
        if observation_mode not in self.OBSERVATION_MODES:
            raise ValueError(
                f"observation_mode must be one of {sorted(self.OBSERVATION_MODES)}, "
                f"got {observation_mode!r}"
            )
        self._path             = path
        self._chunks           = chunks if chunks is not None else self._DEFAULT_CHUNKS
        self._data_group       = data_group
        self._observation_mode = observation_mode
        self._datatree: Optional[xr.DataTree] = None
        # OPT-A: partition cache — populated at open(), cleared at close()
        self._partitions: list[xr.Dataset] = []
        # Resolved at open() time — 'interferometer' or 'single_dish'
        self._resolved_mode: Optional[str] = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ #
    # Lifecycle                                                            #
    # ------------------------------------------------------------------ #

    def open(self) -> None:
        """Open the ``.ps.zarr`` store via ``xarray.open_datatree``.

        Idempotent — safe to call multiple times.

        Also builds the partition cache (``self._partitions``) and resolves
        ``self._resolved_mode`` (``'interferometer'`` or ``'single_dish'``)
        by inspecting the first partition's dimensions.

        Raises
        ------
        ImportError
            If ``zarr`` is not installed.
        RuntimeError
            If xarray fails to open the Zarr store (wraps original error).
        """
        with self._lock:
            if self._datatree is not None:
                return
            _check_zarr()
            log.debug("MSv4Backend: opening %s", self._path)
            try:
                self._datatree = _open_zarr_datatree(
                    self._path, chunks=self._chunks
                )
            except Exception as exc:
                raise RuntimeError(
                    f"MSv4Backend failed to open {self._path!r}: {exc}"
                ) from exc
            # OPT-A: build partition cache — single walk at open time
            self._partitions = list(_collect_visibility_partitions(self._datatree))
            # Resolve observation mode
            self._resolved_mode = _resolve_observation_mode(
                self._observation_mode, self._partitions
            )

        n = len(self._partitions)
        log.debug(
            "MSv4Backend: opened — %d partition(s), mode=%s",
            n, self._resolved_mode,
        )
        if n == 0:
            log.warning("MSv4Backend: no visibility partitions in %s",
                        self._path)

    def close(self) -> None:
        """Release the open DataTree and clear the partition cache."""
        with self._lock:
            if self._datatree is not None:
                try:
                    self._datatree.close()
                except Exception:
                    pass
                finally:
                    self._datatree    = None
                    self._partitions  = []
                    self._resolved_mode = None

    def __enter__(self) -> "MSv4Backend":
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
                "MSv4Backend is not open.  Call open() or use as a "
                "context manager."
            )
        return dt



    def axis_info(self, axis, selection=None):
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
        info = super().axis_info(axis, selection)
        try:
            dim = _axis_to_dim(axis, self._baseline_dim)
        except ValueError:
            # Derived axes have no dimension; that is not an error here.
            dim = ""

        if axis is not Axis.CHANNEL:
            return AxisInfo(axis=info.axis, requested=info.requested,
                            dim=dim, is_index=info.is_index)

        n_parts = sum(1 for _ in self._iter_visibility_partitions(selection))
        if n_parts <= 1:
            # One partition means one SPW's channel numbering: unambiguous.
            return AxisInfo.direct(Axis.CHANNEL, dim=dim, is_index=True)

        return AxisInfo.substituted(
            Axis.CHANNEL, Axis.FREQUENCY, dim=dim,
            note=("channel index is not unique across the "
                  + str(n_parts) + " selected spectral windows; showing "
                  "frequency. Select a single spw to plot channel number."),
        )

    @staticmethod
    def _partition_spw_id(ds) -> "Optional[int]":
        """SPW id of a partition, or ``None`` if it does not declare one.

        Read from ``ds.attrs["spectral_window_id"]``, the same key
        ``metadata()`` uses to collect ``spw_ids``.
        """
        spw_id = ds.attrs.get("spectral_window_id")
        return None if spw_id is None else int(spw_id)

    def _spw_selected(self, ds, selection) -> bool:
        """Whether *ds* passes *selection*'s SPW filter.

        A partition that declares no SPW id is **kept**: refusing to plot
        data because a store omits an optional attribute would be a worse
        failure than plotting slightly more than asked for, and
        ``metadata()`` has the same tolerance when collecting ``spw_ids``.
        """
        if selection is None or getattr(selection, "spw", None) is None:
            return True
        spw_id = self._partition_spw_id(ds)
        if spw_id is None:
            return True
        return spw_id in set(selection.spw)

    def _iter_visibility_partitions(
        self, selection=None
    ) -> Iterator[xr.Dataset]:
        """Yield each cached visibility partition Dataset.

        When *selection* carries an ``spw`` constraint, partitions whose
        SPW is not listed are skipped entirely.  Filtering here rather
        than in ``_apply_selection`` is deliberate: SPW is a *partition*
        property in MSv4, not a dimension within one, so skipping avoids
        reading the partition at all.

        SPW selection was silently ignored before 2026-08: the field
        existed on ``SelectionSpec``, ``VisibilityPlotter.__init__``
        accepted ``spw=``, ``_parse_spw_string`` converted it to ids, and
        ``_build_selection`` populated ``SelectionSpec.spw`` -- and then
        no backend ever read it.  ``visplot(ms=..., spw='0')`` plotted
        every SPW, with no error and no warning.

        Callers that must see the whole store regardless of selection --
        ``metadata()``, and MSv2's ``open()`` -- pass no *selection* and
        are unaffected.

        After ``open()``, simply iterates ``self._partitions`` — a Python
        list of ``xr.Dataset`` objects — avoiding repeated DataTree subtree
        walks (OPT-A).

        Falls back to a live DataTree walk if called before the cache is
        populated (e.g. from within ``open()`` itself before the cache is
        ready), preserving the same behaviour as the original implementation.
        """
        if self._partitions:
            source = iter(self._partitions)
        else:
            # Fallback: live walk (only used transiently during open())
            source = _collect_visibility_partitions(self._require_open())

        n_total = n_kept = 0
        for ds in source:
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

    @property
    def is_single_dish(self) -> bool:
        """True if this store contains single-dish (spectrum) data.

        Resolved at ``open()`` time.  Always ``False`` before ``open()``
        is called.
        """
        return self._resolved_mode == "single_dish"

    def _get_data_group(self, ds: xr.Dataset) -> Optional[dict]:
        """Return the resolved data_group dict for *ds*, or ``None``.

        Looks up ``ds.attrs['data_groups']`` and returns the entry for
        ``self._data_group``.  If ``self._data_group`` is ``None``, returns
        the first data group found.  Returns ``None`` if ``data_groups`` is
        absent from ``ds.attrs`` (xarray-ms written stores, which don't
        populate this attribute).
        """
        data_groups = ds.attrs.get("data_groups")
        if not data_groups:
            return None
        if self._data_group is not None:
            group = data_groups.get(self._data_group)
            if group is None:
                available = list(data_groups.keys())
                raise KeyError(
                    f"data_group {self._data_group!r} not found in partition.  "
                    f"Available: {available}"
                )
            return group
        # Default: first group (insertion order preserved in Python 3.7+)
        return next(iter(data_groups.values()))

    def _flag_mask(self, ds: xr.Dataset) -> xr.DataArray:
        """Return boolean FLAG DataArray (True = flagged or nonzero bit).

        Respects ``data_group`` to select the correct FLAG variable.  The
        MSv4 FLAG can be bool, uint8, uint16, uint32, or uint64 (bit-field
        encoding of flag reasons); ``astype(bool)`` handles all cases
        uniformly — any nonzero value is treated as flagged.
        """
        group = self._get_data_group(ds)
        flag_name = group["flag"] if group and "flag" in group else "FLAG"
        if flag_name not in ds.data_vars:
            log.warning(
                "_flag_mask: %r not in data_vars; falling back to 'FLAG'",
                flag_name,
            )
            flag_name = "FLAG"
        return ds[flag_name].astype(bool)

    def _resolve_vis(self, ds: xr.Dataset) -> xr.DataArray:
        """Return the correlated data DataArray for this partition.

        Respects ``data_group`` to select the correct variable.  Falls back
        through the standard MSv4 names in priority order when no
        ``data_groups`` attr is present:

        1. Variable named by ``data_group['correlated_data']`` (if set)
        2. ``VISIBILITY`` (interferometer, canonical MSv4 name)
        3. ``SPECTRUM``   (single dish, canonical MSv4 name)
        4. ``DATA``       (xarray-ms written stores using the MSv2 column name)
        """
        group = self._get_data_group(ds)
        if group and "correlated_data" in group:
            var_name = group["correlated_data"]
            if var_name in ds.data_vars:
                return ds[var_name]
            log.warning(
                "_resolve_vis: data_group variable %r not found; "
                "falling back to name-based search",
                var_name,
            )
        for name in ("VISIBILITY", "SPECTRUM", "DATA"):
            if name in ds.data_vars:
                return ds[name]
        raise KeyError(
            f"No correlated data variable found in partition with "
            f"dims={dict(ds.sizes)}.  Available: {list(ds.data_vars)}"
        )

    def _resolve_uvw(self, ds: xr.Dataset) -> xr.DataArray:
        """Return the UVW DataArray, respecting ``data_group``."""
        group = self._get_data_group(ds)
        uvw_name = group["uvw"] if group and "uvw" in group else "UVW"
        if uvw_name not in ds.data_vars:
            log.warning(
                "_resolve_uvw: %r not in data_vars; falling back to 'UVW'",
                uvw_name,
            )
            uvw_name = "UVW"
        return ds[uvw_name]
        """Return the VISIBILITY DataArray.

        MSv4 always names it ``VISIBILITY``.  Falls back to ``DATA`` for
        stores written by tools that use the MSv2 column name directly.
        """
        for name in ("VISIBILITY", "DATA"):
            if name in ds.data_vars:
                return ds[name]
        raise KeyError(
            f"No VISIBILITY or DATA variable in partition with "
            f"dims={dict(ds.sizes)}.  Available: {list(ds.data_vars)}"
        )

    def _uvdist_m(self, ds: xr.Dataset) -> xr.DataArray:
        """UV-distance in metres, shape (time, baseline_id).

        Raises ``NotImplementedError`` for single-dish data, which has no
        baselines or UVW coordinates.
        """
        if self.is_single_dish:
            raise NotImplementedError(
                "UV-distance is not defined for single-dish (spectrum) data: "
                "there are no baselines, only individual antenna pointings."
            )
        uvw = self._resolve_uvw(ds)
        u = uvw.sel(uvw_label="u")
        v = uvw.sel(uvw_label="v")
        return np.sqrt(u**2 + v**2)

    def _uvdist_lambda(self, ds: xr.Dataset) -> xr.DataArray:
        """UV-distance in wavelengths, shape (time, baseline_id, frequency)."""
        return self._uvdist_m(ds) * ds.coords["frequency"] / _C_MS

    def _estimate_samples(
        self,
        ds: xr.Dataset,
        sel: SelectionSpec,
        n_quantities: int,
    ) -> int:
        """Estimate sample count for the adaptive pipeline decision."""
        n_time = _count_selected_time(ds, sel)
        n_bl   = _count_selected_baselines(ds, sel, self._baseline_dim)
        n_chan  = _count_selected_channels(ds, sel)
        return n_time * n_bl * n_chan * n_quantities

    @property
    def _baseline_dim(self) -> str:
        """The primary 'second dimension' name for the current observation mode.

        * Interferometer: ``"baseline_id"`` — antenna pairs
        * Single dish:    ``"antenna_name"`` — individual antennas

        This is the single point in the class where the two modes diverge for
        dimension naming.  All methods that need to reference this dimension
        should use this property rather than hardcoding ``"baseline_id"``.
        """
        return "antenna_name" if self.is_single_dish else "baseline_id"

    # ------------------------------------------------------------------ #
    # Selection  (OPT-C: chunk-aligned isel for range constraints)        #
    # ------------------------------------------------------------------ #

    def _apply_selection(
        self, ds: xr.Dataset, sel: SelectionSpec
    ) -> xr.Dataset:
        """Apply *sel* constraints via xarray isel.

        All selections reduce true array sizes (not just NaN-mask), matching
        ``MSv2Backend._apply_selection()`` exactly.

        OPT-C — two-step range selection for time and frequency:
        When ``sel.time_range`` is the only constraint on the time dimension
        (no field/scan name mask), the implementation first issues a
        chunk-aligned ``isel(time=slice(...))`` to load only the Zarr chunks
        that overlap the requested range, then applies an exact boolean mask
        within that reduced dataset to trim to the precise requested bounds.

        The two-step approach preserves the correctness invariant
        (``_apply_selection`` always returns exactly the requested range, not
        a chunk-boundary-expanded superset) while still keeping the Dask task
        graph simple: the slice tells Dask which chunks to read, and the mask
        runs cheaply on the already-narrowed in-memory/dask array.

        The same treatment applies to ``freq_range`` when ``channel_range`` is
        not also set.

        Antenna / baseline selection
        ----------------------------
        * **Interferometer**: ``sel.baselines`` selects exact antenna pairs
          from ``baseline_antenna1_name`` / ``baseline_antenna2_name``.
          ``sel.antenna_names`` selects all baselines that involve any of the
          named antennas.
        * **Single dish**: ``sel.baselines`` is ignored (no baselines exist).
          ``sel.antenna_names`` selects from the ``antenna_name`` dimension
          directly — it is an explicit per-antenna selection, not a baseline
          filter.
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
            if time_mask is None and HAS_DASK:
                # OPT-C step 1: chunk-aligned read boundary — only loads relevant chunks
                t_coord = ds.coords["time"].values   # already numpy (pre-loaded)
                t_chunk = _get_chunk_size(ds, "time")
                ds = ds.isel(time=_chunk_aligned_range(t_coord, t0, t1, t_chunk))
                # OPT-C step 2: exact trim within the loaded chunks
                t_exact = (ds.coords["time"] >= t0) & (ds.coords["time"] <= t1)
                ds = ds.isel(time=t_exact.values)
            else:
                # Field/scan mask already in play — combine as boolean and apply together
                m = (ds.coords["time"] >= t0) & (ds.coords["time"] <= t1)
                time_mask = m if time_mask is None else time_mask & m

        if time_mask is not None:
            ds = ds.isel(time=time_mask.values)

        # --- frequency dimension ---
        if sel.freq_range is not None and sel.channel_range is None:
            f0, f1 = sel.freq_range
            f_coord = ds.coords["frequency"].values   # already numpy
            f_chunk = _get_chunk_size(ds, "frequency")
            # OPT-C step 1: chunk-aligned read boundary
            ds = ds.isel(frequency=_chunk_aligned_range(f_coord, f0, f1, f_chunk))
            # OPT-C step 2: exact trim
            f_exact = (ds.coords["frequency"] >= f0) & (ds.coords["frequency"] <= f1)
            ds = ds.isel(frequency=f_exact.values)
        elif sel.freq_range is not None and sel.channel_range is not None:
            # channel_range takes precedence; apply freq_range as exact bool
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

        # --- baseline / antenna dimension ---
        if self.is_single_dish:
            # Single dish: antenna_name is the second dimension.
            # sel.baselines is meaningless (no pairs) and is silently ignored.
            # sel.antenna_names selects individual antennas by name.
            if sel.antenna_names is not None and "antenna_name" in ds.coords:
                ant_vals = ds.coords["antenna_name"].values
                ant_set  = set(sel.antenna_names)
                ant_mask = np.isin(ant_vals, list(ant_set))
                ds = ds.isel(antenna_name=ant_mask)
        else:
            # Interferometer: baseline_id dimension with pair coords.
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

    def metadata(self) -> dict:
        """Collect human-readable metadata from all visibility partitions.

        Reads only coordinate values (no VISIBILITY), so it is fast and
        does not trigger any large Dask computes.  Uses the partition cache
        (OPT-A) to avoid DataTree subtree walks.

        The returned dict is identical in structure for both interferometer and
        single-dish modes.  In single-dish mode ``n_baselines`` is replaced by
        the antenna count, and ``antenna_names`` is collected from the
        ``antenna_name`` dimension coordinate directly rather than from the
        ``baseline_antenna1/2_name`` pair coordinates.
        """
        self._require_open()

        scan_names:   set[str] = set()
        field_names:  set[str] = set()
        ant_names:    set[str] = set()
        spw_ids:      set[int] = set()
        pol_labels:   set[str] = set()
        t_min = float("inf");  t_max = float("-inf")
        f_min = float("inf");  f_max = float("-inf")
        n_baselines  = 0
        data_columns: set[str] = set()

        for ds in self._iter_visibility_partitions():
            _collect_string_coord(ds, "scan_name",  scan_names)
            _collect_string_coord(ds, "field_name", field_names)

            if self.is_single_dish:
                # Single dish: antennas are a direct dimension coordinate
                _collect_string_coord(ds, "antenna_name", ant_names)
                n_baselines = max(n_baselines, ds.sizes.get("antenna_name", 0))
            else:
                # Interferometer: antennas appear as pair coordinates on baseline_id
                _collect_string_coord(ds, "baseline_antenna1_name", ant_names)
                _collect_string_coord(ds, "baseline_antenna2_name", ant_names)
                n_baselines = max(n_baselines, ds.sizes.get("baseline_id", 0))

            # SPW id from partition attributes
            spw_id = ds.attrs.get("spectral_window_id")
            if spw_id is not None:
                spw_ids.add(int(spw_id))

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

            # Data column detection
            if "VISIBILITY" in ds.data_vars or "SPECTRUM" in ds.data_vars:
                data_columns.add("DATA")
            if "CORRECTED_DATA" in ds.data_vars:
                data_columns.add("CORRECTED")
            if "MODEL_DATA" in ds.data_vars:
                data_columns.add("MODEL")

        return {
            "scan_names":         sorted(scan_names),
            "field_names":        sorted(field_names),
            "antenna_names":      sorted(ant_names),
            "spw_ids":            sorted(spw_ids),
            "correlation_labels": sorted(pol_labels),
            "time_range":         (t_min, t_max),
            "freq_range":         (f_min, f_max),
            "n_baselines":        n_baselines,
            "data_columns":       sorted(data_columns),
        }

    # ------------------------------------------------------------------ #
    # Scatter / line mode query  (OPT-B: cross-partition fused compute)   #
    # ------------------------------------------------------------------ #

    def query_columns(
        self,
        xaxis: Axis,
        yaxes: list[tuple[Axis, str]],   # (Axis, polarization_label)
        selection: SelectionSpec,
        *,
        canvas_width:  int = 800,
        canvas_height: int = 600,
    ) -> dict[tuple[Axis, str], pd.DataFrame]:
        """Return flat DataFrames for scatter mode, one per (axis, pol) pair.

        Mirrors ``MSv2Backend.query_columns`` exactly — same signature, same
        return type, same adaptive pipeline logic.

        OPT-B: when the total sample count across all partitions exceeds
        ``_THRESH_FUSED``, all per-partition lazy arrays (y values for every
        requested axis×pol pair, plus x values) are collected first and then
        materialised with a single ``dask.compute(*all_lazy)`` call.  This
        lets Dask schedule reads from all partitions in parallel (Zarr is
        thread-safe) rather than computing them one by one.
        """
        self._require_open()

        # Collect selected partitions and estimate total sample count
        selected: list[xr.Dataset] = []
        total_samples = 0
        for raw_ds in self._iter_visibility_partitions(selection):
            ds = self._apply_selection(raw_ds, selection)
            if ds.sizes.get("time", 0) == 0:
                continue
            selected.append(ds)
            total_samples += self._estimate_samples(ds, selection, len(yaxes))

        if not selected:
            return {key: pd.DataFrame({"x": [], "y": []}) for key in yaxes}

        use_fused    = HAS_DASK and total_samples >= _THRESH_FUSED
        use_parallel = HAS_DASK and total_samples >= _THRESH_PAR

        if use_fused and len(selected) > 1:
            # OPT-B: collect ALL lazy arrays across ALL partitions, compute once
            return self._query_all_partitions_scatter_fused(
                selected, xaxis, yaxes
            )
        else:
            # Per-partition path (serial, or single partition)
            partition_frames: dict[tuple[Axis, str], list[pd.DataFrame]] = {
                key: [] for key in yaxes
            }
            for ds in selected:
                n_samp = self._estimate_samples(ds, selection, len(yaxes))
                fused_part = HAS_DASK and n_samp >= _THRESH_FUSED
                frames = self._query_partition_scatter(
                    ds, xaxis, yaxes,
                    use_fused=fused_part,
                    use_parallel=use_parallel,
                )
                for key, df in frames.items():
                    if df is not None and len(df) > 0:
                        partition_frames[key].append(df)
            result = {}
            for key, frames in partition_frames.items():
                result[key] = (
                    pd.concat(frames, ignore_index=True) if frames
                    else pd.DataFrame({"x": [], "y": []})
                )
            return result

    def _query_all_partitions_scatter_fused(
        self,
        selected: list[xr.Dataset],
        xaxis: Axis,
        yaxes: list[tuple[Axis, str]],
    ) -> dict[tuple[Axis, str], pd.DataFrame]:
        """Materialise all partition scatter data with one ``dask.compute()``.

        OPT-B core: builds the full list of lazy DataArrays across every
        partition, issues a single ``dask.compute(*all_lazy)`` so Dask can
        schedule all Zarr chunk reads in parallel, then ravels and filters
        each result into a DataFrame.
        """
        # Build flat list: [y00, y01, ..., y0K, x0, y10, ..., y1K, x1, ...]
        # where index encodes (partition_idx, yaxis_idx | x_sentinel)
        all_lazy: list[xr.DataArray] = []
        # Track (partition_idx, key) for each lazy y, and partition_idx for x
        layout: list[tuple[int, tuple | None]] = []  # None = x axis

        for p_idx, ds in enumerate(selected):
            vis  = self._resolve_vis(ds)
            flag = self._flag_mask(ds)
            for key in yaxes:
                axis, pol = key
                lazy_y = self._lazy_quantity(vis, flag, axis, pol)
                all_lazy.append(lazy_y)
                layout.append((p_idx, key))
            # x — use a representative y to get the right shape for broadcast
            template = self._lazy_quantity(vis, flag, yaxes[0][0], yaxes[0][1])
            all_lazy.append(self._lazy_x_axis(ds, xaxis, template))
            layout.append((p_idx, None))   # None marks the x entry

        # Single fused compute across all partitions
        computed = dask.compute(*all_lazy)

        # Reconstruct: group (x, y) pairs by partition then by key
        # First extract x arrays per partition
        x_by_partition: dict[int, np.ndarray] = {}
        y_by_partition_key: dict[tuple[int, tuple], np.ndarray] = {}
        for i, (p_idx, key) in enumerate(layout):
            if key is None:
                x_by_partition[p_idx] = np.asarray(computed[i])
            else:
                y_by_partition_key[(p_idx, key)] = np.asarray(computed[i])

        # Ravel, mask NaN, build DataFrames per key
        accumulator: dict[tuple, list[pd.DataFrame]] = {k: [] for k in yaxes}
        for p_idx in range(len(selected)):
            x_arr = x_by_partition[p_idx]
            for key in yaxes:
                y_arr = y_by_partition_key[(p_idx, key)]
                x_flat = x_arr.ravel()
                y_flat = y_arr.ravel()
                # broadcast x to y shape if needed (e.g. time-only x vs time×bl×freq y)
                if x_flat.shape != y_flat.shape:
                    x_flat = np.broadcast_to(x_arr, y_arr.shape).ravel()
                ok = np.isfinite(x_flat) & np.isfinite(y_flat)
                if ok.any():
                    accumulator[key].append(
                        pd.DataFrame(
                            {"x": x_flat[ok], "y": y_flat[ok]},
                            copy=False,
                        )
                    )

        return {
            key: (
                pd.concat(frames, ignore_index=True) if frames
                else pd.DataFrame({"x": [], "y": []})
            )
            for key, frames in accumulator.items()
        }

    def _query_partition_scatter(
        self,
        ds: xr.Dataset,
        xaxis: Axis,
        yaxes: list[tuple[Axis, str]],
        *,
        use_fused: bool,
        use_parallel: bool,
    ) -> dict[tuple[Axis, str], pd.DataFrame]:
        """Build scatter DataFrames for a single partition."""
        vis  = self._resolve_vis(ds)
        flag = self._flag_mask(ds)

        lazy_y: dict[tuple[Axis, str], xr.DataArray] = {}
        for axis, pol in yaxes:
            lazy_y[(axis, pol)] = self._lazy_quantity(vis, flag, axis, pol)

        template = next(iter(lazy_y.values()))
        lazy_x   = self._lazy_x_axis(ds, xaxis, template)

        if use_fused:
            all_lazy = list(lazy_y.values()) + [lazy_x]
            computed  = dask.compute(*all_lazy)
            y_computed = dict(zip(lazy_y.keys(), computed[:-1]))
            x_computed = computed[-1]

            def _ravel_df(x_arr, y_arr) -> pd.DataFrame:
                x_flat = np.asarray(x_arr).ravel()
                y_flat = np.asarray(y_arr).ravel()
                if x_flat.shape != y_flat.shape:
                    x_flat = np.broadcast_to(np.asarray(x_arr), np.asarray(y_arr).shape).ravel()
                ok = np.isfinite(x_flat) & np.isfinite(y_flat)
                return pd.DataFrame(
                    {"x": x_flat[ok], "y": y_flat[ok]}, copy=False
                )

            return {
                key: _ravel_df(x_computed, y_arr)
                for key, y_arr in y_computed.items()
            }
        else:
            x_c = lazy_x.compute()
            frames = {}
            for key, lazy in lazy_y.items():
                y_c  = lazy.compute()
                x_bc = x_c.broadcast_like(y_c)
                stacked = xr.Dataset({"x": x_bc, "y": y_c}).stack(
                    sample=list(y_c.dims)
                )
                frames[key] = stacked.to_dataframe()[["x", "y"]].dropna()
            return frames

    def _lazy_quantity(
        self,
        vis: xr.DataArray,
        flag: xr.DataArray,
        axis: Axis,
        pol: str,
    ) -> xr.DataArray:
        """Return a lazy DataArray for the requested axis and polarization."""
        vis_pol  = vis.sel(polarization=pol)
        flag_pol = flag.sel(polarization=pol)

        if axis == Axis.AMPLITUDE:
            q = xr.DataArray(
                da.absolute(vis_pol.data),
                coords={k: v for k, v in vis_pol.coords.items()},
                dims=vis_pol.dims,
                attrs=vis_pol.attrs,
            )
        elif axis == Axis.PHASE:
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
                f"Use AMPLITUDE, PHASE, REAL, or IMAGINARY."
            )

        return q.where(~flag_pol)

    def _lazy_x_axis(
        self,
        ds: xr.Dataset,
        xaxis: Axis,
        template: xr.DataArray,
    ) -> xr.DataArray:
        """Return a lazy x-axis DataArray broadcast to *template*'s shape.

        ``Axis.UVDIST``, ``Axis.UVDIST_LAMBDA``, ``Axis.U``, and ``Axis.V`` are not
        applicable in single-dish mode and raise ``NotImplementedError``
        immediately so the caller receives a clear message rather than a
        confusing KeyError from a missing UVW variable.
        """
        _UV_AXES = (Axis.UVDIST, Axis.UVDIST_LAMBDA, Axis.U, Axis.V)
        if self.is_single_dish and xaxis in _UV_AXES:
            raise NotImplementedError(
                f"Axis.{xaxis.name} is not applicable for single-dish data: "
                "no UVW coordinates exist. Use Axis.TIME, Axis.FREQUENCY, "
                "or Axis.CHANNEL as the x-axis."
            )
        if xaxis == Axis.TIME:
            return ds.coords["time"].broadcast_like(template)
        elif xaxis == Axis.UVDIST:
            return self._uvdist_m(ds).broadcast_like(template)
        elif xaxis == Axis.UVDIST_LAMBDA:
            return self._uvdist_lambda(ds).broadcast_like(template)
        elif xaxis == Axis.FREQUENCY:
            return ds.coords["frequency"].broadcast_like(template)
        elif xaxis == Axis.CHANNEL:
            chan = xr.DataArray(
                np.arange(ds.sizes["frequency"]),
                dims=["frequency"],
            )
            return chan.broadcast_like(template)
        elif xaxis == Axis.U:
            return self._resolve_uvw(ds).sel(uvw_label="u").broadcast_like(template)
        elif xaxis == Axis.V:
            return self._resolve_uvw(ds).sel(uvw_label="v").broadcast_like(template)
        else:
            raise NotImplementedError(
                f"Axis {xaxis} is not a supported scatter x-axis."
            )

    # ------------------------------------------------------------------ #
    # Raster mode query  (OPT-B: cross-partition fused compute)           #
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

        Mirrors ``MSv2Backend.query_raster`` exactly — see that method's
        docstring for the full two-level rendering contract.

        OPT-B: all per-partition lazy 2D arrays (after reduction and optional
        per-partition decimation) are collected and then materialised with a
        single ``dask.compute(*all_lazy)`` so that Zarr reads across partitions
        execute in parallel.
        """
        self._require_open()

        y_name = _axis_to_dim(y_dim, self._baseline_dim)
        x_name = _axis_to_dim(x_dim, self._baseline_dim)

        lazy_arrs:  list[xr.DataArray] = []
        all_x_vals: list[float]        = []
        all_y_vals: list[float]        = []

        for raw_ds in self._iter_visibility_partitions(selection):
            ds = self._apply_selection(raw_ds, selection)
            if ds.sizes.get("time", 0) == 0:
                continue

            if x_name in ds.coords:
                xv = ds.coords[x_name].values
                if np.issubdtype(xv.dtype, np.number):
                    all_x_vals.extend([float(xv.min()), float(xv.max())])
            if y_name in ds.coords:
                yv = ds.coords[y_name].values
                if np.issubdtype(yv.dtype, np.number):
                    all_y_vals.extend([float(yv.min()), float(yv.max())])

            arr = self._raster_2d(ds, y_dim, x_dim, quantity, polarization)
            if arr is not None:
                arr, _ = _decimate_agg(arr, y_name, x_name, max_cells)
                lazy_arrs.append(arr)

        if not lazy_arrs:
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

        # OPT-B: single fused compute across all partitions
        if HAS_DASK and len(lazy_arrs) > 1:
            computed_arrs = list(dask.compute(*lazy_arrs))
        else:
            computed_arrs = [arr.compute() for arr in lazy_arrs]

        if len(computed_arrs) == 1:
            agg = computed_arrs[0]
        else:
            try:
                agg = xr.concat(
                    computed_arrs,
                    dim=y_name,
                    join="outer",
                    coords="minimal",
                    compat="override",
                )
            except Exception as exc:
                log.warning("query_raster: could not concat partitions: %s", exc)
                agg = computed_arrs[0]

        if all_x_vals:
            x_range = (min(all_x_vals), max(all_x_vals))
        else:
            if x_name in agg.coords:
                xv = agg.coords[x_name].values
                if np.issubdtype(xv.dtype, np.number):
                    x_range = (float(xv.min()), float(xv.max()))
                else:
                    # String dimension (e.g. antenna_name): use integer index range
                    x_range = (0.0, float(len(xv) - 1))
            else:
                x_range = (0.0, 1.0)
        if all_y_vals:
            y_range = (min(all_y_vals), max(all_y_vals))
        else:
            if y_name in agg.coords:
                yv = agg.coords[y_name].values
                if np.issubdtype(yv.dtype, np.number):
                    y_range = (float(yv.min()), float(yv.max()))
                else:
                    y_range = (0.0, float(len(yv) - 1))
            else:
                y_range = (0.0, 1.0)

        agg, is_decimated = _decimate_agg(agg, y_name, x_name, max_cells)
        return agg, x_range, y_range, is_decimated

    def _raster_2d(
        self,
        ds: xr.Dataset,
        y_dim: Axis,
        x_dim: Axis,
        quantity: Axis,
        polarization: Optional[str],
    ) -> Optional[xr.DataArray]:
        """Reduce a single partition to a lazy 2D DataArray for raster mode.

        Returns a lazy (not computed) DataArray.  The caller collects these
        across all partitions and issues a single ``dask.compute()`` (OPT-B).
        """
        vis  = self._resolve_vis(ds)
        flag = self._flag_mask(ds)
        eit  = ds.get("EFFECTIVE_INTEGRATION_TIME")

        y_name = _axis_to_dim(y_dim, self._baseline_dim)
        x_name = _axis_to_dim(x_dim, self._baseline_dim)

        if quantity == Axis.FLAG:
            frac = flag.astype(float).mean(
                dim=[d for d in flag.dims if d not in (y_name, x_name)],
                skipna=True,
            )
            if eit is not None:
                frac = frac.where(np.isfinite(eit))
            return frac

        if polarization is None:
            log.warning(
                "query_raster: polarization required for %s; "
                "defaulting to first available", quantity
            )
            polarization = str(ds.coords["polarization"].values[0])

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

        reduce_dims = [d for d in q.dims if d not in (y_name, x_name)]
        if reduce_dims:
            q = q.mean(dim=reduce_dims, skipna=True)

        if set(q.dims) != {y_name, x_name}:
            log.warning(
                "_raster_2d: unexpected dims %s after reduction "
                "(expected {%s, %s}); skipping partition",
                q.dims, y_name, x_name,
            )
            return None

        return q.transpose(y_name, x_name)

    # ------------------------------------------------------------------ #
    # UV-coverage                                                          #
    # ------------------------------------------------------------------ #

    def query_uv_coverage(
        self,
        selection: SelectionSpec,
        include_conjugate: bool = True,
    ) -> pd.DataFrame:
        """Return a flat DataFrame of (u, v) points for UV-coverage plots.

        No VISIBILITY access — only UVW coordinates are read.
        NaN UVW values are dropped automatically.

        OPT-B: all per-partition UVW arrays are collected and materialised
        with a single ``dask.compute()`` when more than one partition is
        present.

        Single-dish note
        ----------------
        Single-dish data has no baselines or UVW coordinates, so UV-coverage
        is undefined.  This method returns an empty DataFrame in single-dish
        mode rather than raising an error, allowing calling code to handle the
        case gracefully (e.g. hiding the UV-coverage panel in the plotter).
        """
        self._require_open()

        if self.is_single_dish:
            log.info(
                "query_uv_coverage: returning empty result for single-dish "
                "store %s — no UVW coordinates exist for spectrum data.",
                self._path,
            )
            return pd.DataFrame({"x": [], "y": []})

        selected: list[xr.Dataset] = []
        for raw_ds in self._iter_visibility_partitions(selection):
            ds = self._apply_selection(raw_ds, selection)
            if ds.sizes.get("time", 0) > 0:
                selected.append(ds)

        if not selected:
            return pd.DataFrame({"x": [], "y": []})

        lazy_uvws = [self._resolve_uvw(ds) for ds in selected]
        if HAS_DASK and len(lazy_uvws) > 1:
            computed_uvws = list(dask.compute(*lazy_uvws))
        else:
            computed_uvws = [uvw.compute() for uvw in lazy_uvws]

        u_parts, v_parts = [], []
        for uvw in computed_uvws:
            u = uvw.sel(uvw_label="u").values.ravel()
            v = uvw.sel(uvw_label="v").values.ravel()
            finite = np.isfinite(u) & np.isfinite(v)
            if finite.any():
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
    # Flagging-safe threshold detection                                    #
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

        Uses the partition cache (OPT-A) for the iteration.
        ``Axis.BASELINE`` maps to ``"baseline_id"`` for interferometer data
        and ``"antenna_name"`` for single-dish data via ``_baseline_dim``.
        """
        self._require_open()

        x_name = _axis_to_dim(x_dim, self._baseline_dim)
        y_name = _axis_to_dim(y_dim, self._baseline_dim)

        total_x = total_y = 0
        for raw_ds in self._iter_visibility_partitions(selection):
            ds = self._apply_selection(raw_ds, selection)
            total_x = max(total_x, ds.sizes.get(x_name, 0))
            total_y = max(total_y, ds.sizes.get(y_name, 0))

        ratio_x = total_x / canvas_width  if canvas_width  > 0 else float("inf")
        ratio_y = total_y / canvas_height if canvas_height > 0 else float("inf")
        return ratio_x, ratio_y

    # ------------------------------------------------------------------ #
    # Pixel hover probe                                                    #
    # ------------------------------------------------------------------ #

    def probe_raster_pixel(
        self,
        raw_grid: xr.DataArray,
        gx: int,
        gy: int,
        selection: SelectionSpec,
    ) -> dict:
        """Return the value and metadata for raw grid cell (gx, gy).

        Mirrors ``MSv2Backend.probe_raster_pixel`` exactly.  Uses the
        partition cache (OPT-A) for the coordinate lookup pass.
        """
        self._require_open()

        if raw_grid.ndim != 2:
            raise ValueError(f"raw_grid must be 2D; got {raw_grid.ndim}D")

        h, w = raw_grid.shape
        if not (0 <= gx < w and 0 <= gy < h):
            raise IndexError(
                f"Pixel ({gx}, {gy}) out of range for grid ({w}×{h})"
            )

        value = _agg_value(raw_grid.values, gy, gx)

        y_dim_name = raw_grid.dims[0]
        x_dim_name = raw_grid.dims[1]

        x_coords = raw_grid.coords[x_dim_name].values
        y_coords = raw_grid.coords[y_dim_name].values

        # For numeric coordinates (time, baseline_id, frequency) the centre is
        # the coordinate value itself, and cell bounds come from *local*
        # neighbour spacing — MSv4 time and frequency axes are no more
        # uniform than MSv2's, and a global-average half-width lets the
        # metadata lookup below reach into neighbouring scans/SPWs.
        # See MSv2Backend's _cell_bounds for the full rationale.
        # For string/categorical coordinates (antenna_name in single-dish
        # mode) the centre is the integer index, matching the index-range
        # x_range/y_range returned by query_raster.
        if np.issubdtype(x_coords.dtype, np.number):
            x_centre = float(x_coords[gx])
            x_range  = _cell_bounds(x_coords, gx)
        else:
            x_centre = float(gx)
            x_range  = (x_centre - 0.5, x_centre + 0.5)

        if np.issubdtype(y_coords.dtype, np.number):
            y_centre = float(y_coords[gy])
            y_range  = _cell_bounds(y_coords, gy)
        else:
            y_centre = float(gy)
            y_range  = (y_centre - 0.5, y_centre + 0.5)

        field_names:    set[str]              = set()
        scan_names:     set[str]              = set()
        antenna_pairs:  list[tuple[str, str]] = []
        freq_range_ghz: Optional[tuple[float, float]] = None

        for raw_ds in self._iter_visibility_partitions(selection):
            ds = self._apply_selection(raw_ds, selection)
            if ds.sizes.get("time", 0) == 0:
                continue

            if x_dim_name == "time" or y_dim_name == "time":
                t_vals     = ds.coords["time"].values
                t_lo, t_hi = x_range if x_dim_name == "time" else y_range
                t_mask     = (t_vals >= t_lo) & (t_vals <= t_hi)
                if t_mask.any():
                    for coord, target in (
                        ("field_name", field_names),
                        ("scan_name",  scan_names),
                    ):
                        if coord in ds.coords:
                            target.update(
                                str(v) for v in
                                ds.coords[coord].values[t_mask] if v
                            )

            if x_dim_name == "baseline_id" or y_dim_name == "baseline_id":
                bl_lo, bl_hi = (
                    x_range if x_dim_name == "baseline_id" else y_range
                )
                if "baseline_antenna1_name" in ds.coords:
                    bl_vals = ds.coords["baseline_id"].values
                    bl_mask = (bl_vals >= bl_lo) & (bl_vals <= bl_hi)
                    if bl_mask.any():
                        ant1 = ds.coords["baseline_antenna1_name"].values
                        ant2 = ds.coords["baseline_antenna2_name"].values
                        for a1, a2 in zip(ant1[bl_mask], ant2[bl_mask]):
                            pair = (str(a1), str(a2))
                            if pair not in antenna_pairs:
                                antenna_pairs.append(pair)

            if x_dim_name == "frequency" or y_dim_name == "frequency":
                f_lo, f_hi = (
                    x_range if x_dim_name == "frequency" else y_range
                )
                freq_vals = ds.coords["frequency"].values
                f_mask    = (freq_vals >= f_lo) & (freq_vals <= f_hi)
                if not f_mask.any():
                    f_centre = (f_lo + f_hi) / 2
                    nearest  = freq_vals[np.argmin(np.abs(freq_vals - f_centre))]
                    f_mask   = freq_vals == nearest
                if f_mask.any():
                    f_sub = freq_vals[f_mask]
                    freq_range_ghz = (
                        float(f_sub.min()) / 1e9,
                        float(f_sub.max()) / 1e9,
                    )

        return {
            "value":          value,
            "x_range":        x_range,
            "y_range":        y_range,
            "x_centre":       x_centre,
            "y_centre":       y_centre,
            "field_names":    sorted(field_names),
            "scan_names":     sorted(scan_names),
            "antenna_pairs":  antenna_pairs,
            "freq_range_ghz": freq_range_ghz,
        }

    def probe_scatter_pixel(
        self,
        canvas_agg: xr.DataArray,
        px: int,
        py: int,
        selection: SelectionSpec,
        scatter_df: pd.DataFrame,
    ) -> dict:
        """Return the value and scatter sample count for canvas pixel (px, py).

        Mirrors ``MSv2Backend.probe_scatter_pixel`` exactly.
        """
        self._require_open()

        if canvas_agg.ndim != 2:
            raise ValueError(f"canvas_agg must be 2D; got {canvas_agg.ndim}D")

        h, w = canvas_agg.shape
        if not (0 <= px < w and 0 <= py < h):
            raise IndexError(
                f"Pixel ({px}, {py}) out of range for canvas ({w}×{h})"
            )

        # Dtype-aware empty test: mean/max aggs leave NaN, count leaves 0.
        value = _agg_value(canvas_agg.values, py, px)

        x_coords = canvas_agg.coords[canvas_agg.dims[1]].values
        y_coords = canvas_agg.coords[canvas_agg.dims[0]].values

        x_centre = float(x_coords[px])
        y_centre = float(y_coords[py])

        x_range = _widen_if_degenerate(_cell_bounds(x_coords, px), x_coords)
        y_range = _widen_if_degenerate(_cell_bounds(y_coords, py), y_coords)

        # Half-open bin membership, matching Datashader's own binning;
        # see MSv2Backend.probe_scatter_pixel.
        n_scatter = 0
        if len(scatter_df) > 0:
            mask = (
                _bin_membership(scatter_df["x"], x_range, px, len(x_coords)) &
                _bin_membership(scatter_df["y"], y_range, py, len(y_coords))
            )
            n_scatter = int(mask.sum())

        return {
            "value":             value,
            "x_range":           x_range,
            "y_range":           y_range,
            "x_centre":          x_centre,
            "y_centre":          y_centre,
            "n_scatter_samples": n_scatter,
        }

    # ------------------------------------------------------------------ #
    # Representation                                                        #
    # ------------------------------------------------------------------ #

    def __repr__(self) -> str:  # pragma: no cover
        status = "open" if self._datatree is not None else "closed"
        parts = [f"MSv4Backend({self._path!r}, {status}"]
        if status == "open":
            parts.append(f"{len(self._partitions)} partition(s)")
            parts.append(self._resolved_mode or "unknown mode")
        if self._data_group is not None:
            parts.append(f"data_group={self._data_group!r}")
        return ", ".join(parts) + ")"


# ======================================================================
# Module-level helpers
# ======================================================================

def _check_zarr() -> None:
    """Raise ImportError with a helpful message if zarr is absent."""
    try:
        import zarr  # noqa: F401
    except ImportError as exc:
        raise ImportError(
            "zarr is required for MSv4Backend.\n"
            "Install: pip install 'zarr>=2.10'\n"
            "See https://zarr.readthedocs.io/ for details."
        ) from exc


def _open_zarr_datatree(
    path: str,
    chunks: Optional[dict],
) -> xr.DataTree:
    """Open a Zarr DataTree, trying consolidated metadata first.

    Zarr stores produced by ``xarray-ms dt.to_zarr()`` write ``zarr.json``
    (Zarr V3 consolidated metadata) and those produced by xradio write
    ``.zmetadata`` (Zarr V2 consolidated metadata).  In both cases,
    ``consolidated=True`` lets xarray read a single metadata file instead
    of walking every array sub-directory, giving a 10–100× speedup on open
    for stores with many partitions.

    Falls back silently to the default open if the store does not have
    consolidated metadata or if the option is unsupported.
    """
    try:
        return xr.open_datatree(
            path,
            engine=_ZARR_ENGINE,
            chunks=chunks,
            consolidated=True,
        )
    except Exception:
        log.debug(
            "_open_zarr_datatree: consolidated open failed for %s; "
            "falling back to default open",
            path,
        )
        return xr.open_datatree(
            path,
            engine=_ZARR_ENGINE,
            chunks=chunks,
        )


def _resolve_observation_mode(
    requested: str,
    partitions: list,
) -> str:
    """Resolve ``'auto'`` observation mode to ``'interferometer'`` or ``'single_dish'``.

    Detection is based on the primary dimension of the first partition:
    * ``baseline_id`` present → interferometer (VISIBILITY)
    * ``antenna_name`` present → single dish (SPECTRUM)

    If no partitions are available (empty store), defaults to
    ``'interferometer'``.  An explicit non-``'auto'`` value is returned
    unchanged.
    """
    if requested != "auto":
        return requested
    if not partitions:
        return "interferometer"
    ds = partitions[0]
    if "baseline_id" in ds.dims or "baseline_id" in ds.coords:
        return "interferometer"
    if "antenna_name" in ds.dims or "antenna_name" in ds.coords:
        return "single_dish"
    # Ambiguous — assume interferometer and warn
    log.warning(
        "_resolve_observation_mode: could not detect mode from dims %s; "
        "defaulting to 'interferometer'",
        list(ds.dims),
    )
    return "interferometer"


def _collect_visibility_partitions(dt: xr.DataTree) -> list:
    """Walk a DataTree once and return all visibility partition Datasets.

    Called once at ``open()`` to populate ``MSv4Backend._partitions``
    (OPT-A partition cache).

    Handles two structural variants:

    **xarray-ms style** (our test stores):
    Partition nodes sit directly in the DataTree and are identified by
    ``ds.attrs["type"] == "visibility"`` or by the presence of a
    VISIBILITY/SPECTRUM/DATA variable.

    **xradio-native style** (future RADPS-produced stores):
    Each partition is an ``ms_xdt`` node whose correlated data lives in
    a ``correlated_xds`` child.  The ``ms_xdt`` node itself may have
    ``attrs["type"] == "visibility"`` or its child ``correlated_xds``
    will have the actual VISIBILITY/SPECTRUM variable.  Both patterns
    are handled: if the node's own Dataset contains the data vars it is
    used directly; otherwise its ``correlated_xds`` child is checked.

    A node is a visibility partition when:
    * it has data (``node.has_data``),
    * its time dimension is non-empty, and
    * either ``attrs["type"] in ("visibility", "radiometer")``
      (canonical MSv4 marker — ``"radiometer"`` covers single-dish), or
      as a fallback for stores without the type attribute, it contains a
      VISIBILITY / SPECTRUM / DATA variable.
    """
    result: list = []
    for node in dt.subtree:
        if not node.has_data:
            continue
        ds = node.ds
        if ds.sizes.get("time", 0) == 0:
            continue
        node_type = ds.attrs.get("type", "")

        if node_type in ("visibility", "radiometer"):
            result.append(ds)
            continue

        # Fallback: check for correlated data variables by name
        _CORR_VARS = ("VISIBILITY", "SPECTRUM", "DATA")
        if node_type == "" and any(v in ds.data_vars for v in _CORR_VARS):
            result.append(ds)
            continue

        # xradio-native: check correlated_xds child node
        corr_child = None
        for child_name in getattr(node, "children", {}):
            if child_name == "correlated_xds":
                child_node = node[child_name]
                if child_node.has_data:
                    corr_child = child_node.ds
                break
        if corr_child is not None and corr_child.sizes.get("time", 0) > 0:
            if any(v in corr_child.data_vars for v in _CORR_VARS):
                result.append(corr_child)

    return result


def _get_chunk_size(ds: xr.Dataset, dim: str) -> Optional[int]:
    """Return the first chunk size along *dim* from any Dask-backed variable.

    Returns ``None`` if no Dask-backed variable with that dimension is found,
    or if ``dask`` is not installed.  Used by OPT-C to snap index bounds to
    Zarr chunk boundaries.
    """
    if not HAS_DASK:
        return None
    for var in ds.data_vars.values():
        if dim in var.dims and hasattr(var.data, "chunks"):
            idx = var.dims.index(dim)
            chunks = var.data.chunks[idx]
            if chunks:
                return int(chunks[0])
    return None


def _chunk_aligned_range(
    coord: np.ndarray,
    lo: float,
    hi: float,
    chunk_size: Optional[int],
) -> slice:
    """Return a ``slice`` covering [lo, hi] in *coord*, snapped to chunk boundaries.

    OPT-C: when ``chunk_size`` is known, the start index is snapped down to
    the nearest chunk boundary and the end index is snapped up, ensuring the
    resulting slice maps exactly onto whole Zarr chunks.  This keeps the Dask
    task graph simple (pure slice, no mask application) and avoids partial
    chunk reads.

    When ``chunk_size`` is ``None`` (unavailable), returns a plain index-range
    slice without snapping — behaviour is identical to the boolean-mask path
    but expressed as a slice for consistency.

    Parameters
    ----------
    coord :
        1D numpy array of coordinate values (already loaded; not dask-backed).
    lo, hi :
        Inclusive selection bounds in coordinate units.
    chunk_size :
        Number of elements per chunk along this dimension, or ``None``.

    Returns
    -------
    slice
        A ``slice(start, stop)`` suitable for ``ds.isel(dim=slice(...))``.
        ``stop`` is exclusive (standard Python convention).
    """
    start = int(np.searchsorted(coord, lo, side="left"))
    stop  = int(np.searchsorted(coord, hi, side="right"))

    if chunk_size is not None and chunk_size > 1:
        start = (start // chunk_size) * chunk_size
        stop  = min(
            ((stop + chunk_size - 1) // chunk_size) * chunk_size,
            len(coord),
        )
    else:
        # No snapping possible; clamp to valid range
        start = max(0, start)
        stop  = min(stop, len(coord))

    return slice(start, stop)


def _collect_string_coord(
    ds: xr.Dataset, name: str, target: set
) -> None:
    """Add unique non-empty string values of *name* from *ds* to *target*.

    Uses explicit key-in-mapping checks to avoid the xarray
    "truth value of array is ambiguous" ValueError.
    """
    if name in ds.coords:
        da = ds.coords[name]
    elif name in ds.data_vars:
        da = ds.data_vars[name]
    else:
        return
    vals = da.values if hasattr(da, "values") else da.compute().values
    target.update(str(v) for v in vals.ravel() if v)


def _axis_to_dim(axis: Axis, baseline_dim: str = "baseline_id") -> str:
    """Map an Axis enum value to its MSv4 dimension name.

    Parameters
    ----------
    axis :
        The axis to map.
    baseline_dim :
        The resolved second-dimension name — ``"baseline_id"`` for
        interferometer data or ``"antenna_name"`` for single-dish data.
        Callers should pass ``self._baseline_dim`` rather than hardcoding
        ``"baseline_id"``.  The default maintains backward compatibility for
        module-level use and tests.
    """
    _MAP = {
        Axis.TIME:        "time",
        Axis.BASELINE:    baseline_dim,   # mode-aware
        Axis.FREQUENCY:   "frequency",
        Axis.CHANNEL:     "frequency",
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
    """Stride a 2D agg DataArray to fit within ``max_cells`` cells."""
    n_y, n_x = agg.sizes[y_name], agg.sizes[x_name]
    total = n_y * n_x

    if total <= max_cells:
        return agg, False

    scale    = math.sqrt(total / max_cells)
    stride_y = max(1, math.ceil(scale * math.sqrt(n_y / n_x)))
    stride_x = max(1, math.ceil(scale * math.sqrt(n_x / n_y)))
    stride_y = min(stride_y, n_y // 2 or 1)
    stride_x = min(stride_x, n_x // 2 or 1)

    log.debug(
        "_decimate_agg: (%d, %d) -> stride (%d, %d) -> (~%d, ~%d)  max_cells=%d",
        n_y, n_x, stride_y, stride_x,
        math.ceil(n_y / stride_y), math.ceil(n_x / stride_x),
        max_cells,
    )

    return agg.isel(
        {y_name: slice(None, None, stride_y),
         x_name: slice(None, None, stride_x)},
    ), True


def _count_selected_time(ds: xr.Dataset, sel: SelectionSpec) -> int:
    total = ds.sizes.get("time", 0)
    if sel.time_range is not None and "time" in ds.coords:
        t = ds.coords["time"].values
        t0, t1 = sel.time_range
        return int(np.sum((t >= t0) & (t <= t1)))
    if sel.field_names is not None and "field_name" in ds.coords:
        field = ds.coords["field_name"].values
        return int(np.isin(field, list(sel.field_names)).sum())
    return total


def _count_selected_baselines(
    ds: xr.Dataset, sel: SelectionSpec, baseline_dim: str = "baseline_id"
) -> int:
    """Count the number of baseline_id (or antenna_name) slots after selection.

    Parameters
    ----------
    baseline_dim :
        Either ``"baseline_id"`` (interferometer) or ``"antenna_name"``
        (single dish).  Pass ``self._baseline_dim`` from the backend.
    """
    total = ds.sizes.get(baseline_dim, 0)
    if baseline_dim == "antenna_name":
        # Single dish: only antenna_names selection applies
        if sel.antenna_names is not None and "antenna_name" in ds.coords:
            ant_vals = ds.coords["antenna_name"].values
            return int(np.isin(ant_vals, list(sel.antenna_names)).sum())
        return total
    # Interferometer path
    if sel.baselines is not None:
        return min(len(sel.baselines), total)
    if sel.antenna_names is not None and "baseline_antenna1_name" in ds.coords:
        ant1 = ds.coords["baseline_antenna1_name"].values
        ant2 = ds.coords["baseline_antenna2_name"].values
        return int((np.isin(ant1, list(sel.antenna_names)) |
                    np.isin(ant2, list(sel.antenna_names))).sum())
    return total


def _count_selected_channels(ds: xr.Dataset, sel: SelectionSpec) -> int:
    total = ds.sizes.get("frequency", 0)
    if sel.channel_range is not None:
        c0, c1 = sel.channel_range
        return min(c1, total) - max(c0, 0)
    if sel.freq_range is not None and "frequency" in ds.coords:
        f = ds.coords["frequency"].values
        f0, f1 = sel.freq_range
        return int(((f >= f0) & (f <= f1)).sum())
    return total
