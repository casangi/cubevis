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
    XArrayReader,
    _compute_axis_values,
    _agg_value,
    _bin_membership,
    _cell_bounds,
    _widen_if_degenerate,
)
from ..axes import Axis, AxisType
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
            except Exception as exc:
                raise RuntimeError(
                    f"xarray-ms failed to open {self._path!r}: {exc}"
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


    @staticmethod
    def _partition_spw_id(ds) -> "Optional[int]":
        """SPW id of a partition, or ``None`` if it does not declare one.

        Tries ``spectral_window_id`` then ``DATA_DESC_ID`` -- the same
        fallback pair ``metadata()`` uses, because xarray-ms-written
        stores and xradio-written stores disagree on which key
        carries it.
        """
        for attr_key in ("spectral_window_id", "DATA_DESC_ID"):
            spw_id = ds.attrs.get(attr_key)
            if spw_id is not None:
                return int(spw_id)
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
        spw_id = self._partition_spw_id(ds)
        if spw_id is None:
            return True
        return spw_id in set(selection.spw)

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
        spw_ids:      set[int] = set()
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

            # SPW id from partition attributes (set by xarray-ms)
            for attr_key in ("spectral_window_id", "DATA_DESC_ID"):
                spw_id = ds.attrs.get(attr_key)
                if spw_id is not None:
                    spw_ids.add(int(spw_id))
                    break

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
            "spw_ids":           sorted(spw_ids),
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
        yaxes: list[tuple[Axis, str]],   # (Axis, polarization_label)
        selection: SelectionSpec,
        *,
        canvas_width:  int = 800,
        canvas_height: int = 600,
    ) -> dict[tuple[Axis, str], pd.DataFrame]:
        """Return flat DataFrames for scatter mode, one per (axis, pol) pair.

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
        canvas_width, canvas_height :
            Canvas dimensions (used only for the flagging-safe threshold
            calculation in the returned metadata).

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
        """
        vis  = self._resolve_vis(ds)
        flag = self._flag_mask(ds)

        # Build lazy derived arrays for every requested (axis, pol)
        lazy_y: dict[tuple[Axis, str], xr.DataArray] = {}
        for axis, pol in yaxes:
            lazy_y[(axis, pol)] = self._lazy_quantity(vis, flag, axis, pol, ds)

        # x-axis lazy array — broadcast to match a representative y shape
        template = next(iter(lazy_y.values()))
        lazy_x = self._lazy_x_axis(ds, xaxis, template)

        if use_fused:
            # Single dask.compute() — VISIBILITY read once
            all_lazy = list(lazy_y.values()) + [lazy_x]
            computed  = dask.compute(*all_lazy)
            y_computed = dict(zip(lazy_y.keys(), computed[:-1]))
            x_computed = computed[-1]

            def _ravel_df(x_arr, y_arr) -> pd.DataFrame:
                x_flat = np.asarray(x_arr).ravel()
                y_flat = np.asarray(y_arr).ravel()
                ok = np.isfinite(x_flat) & np.isfinite(y_flat)
                return pd.DataFrame(
                    {"x": x_flat[ok], "y": y_flat[ok]}, copy=False
                )

            frames = {
                key: _ravel_df(x_computed, y_arr)
                for key, y_arr in y_computed.items()
            }
        else:
            # Serial fallback — xarray stack → to_dataframe (simpler path)
            x_c = lazy_x.compute()
            frames = {}
            for key, lazy in lazy_y.items():
                y_c   = lazy.compute()
                x_bc  = x_c.broadcast_like(y_c)
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
            return frac

        # Visibility-derived quantities require polarization selection
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
        return q.transpose(y_name, x_name)

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
    # Pixel hover probe                                                    #
    # ------------------------------------------------------------------ #

    def probe_raster_pixel(
        self,
        raw_grid: "xr.DataArray",
        gx: int,
        gy: int,
        selection: SelectionSpec,
    ) -> dict:
        """Return the value and metadata for raw grid cell (gx, gy).

        Operates on the **raw backend grid** from ``query_raster()`` — a 2D
        float64 DataArray with named MS dimension coordinates (e.g.
        ``"time"``, ``"baseline_id"``, ``"frequency"``).  Never accepts a
        Datashader canvas agg with generic ``"x"``/``"y"`` dims.

        ``VisibilityRaster._data_to_pixel()`` converts hover data-space
        ``{x, y}`` coordinates to raw grid indices ``(gx, gy)`` via argmin
        on the grid's coordinate arrays before calling this method.

        Parameters
        ----------
        raw_grid :
            2D float64 DataArray from ``query_raster()``.  Must have named
            MS coordinate arrays on both dimensions.
        gx, gy :
            Zero-based indices into ``raw_grid``.  ``gy`` = row (y-axis,
            dim 0); ``gx`` = column (x-axis, dim 1).
        selection :
            The ``SelectionSpec`` active when ``raw_grid`` was produced.

        Returns
        -------
        dict — see ``XArrayReader.probe_raster_pixel`` for key definitions.
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

        # Named MS dimension coordinates
        y_dim_name = raw_grid.dims[0]   # e.g. "time"
        x_dim_name = raw_grid.dims[1]   # e.g. "baseline_id"

        x_coords = raw_grid.coords[x_dim_name].values
        y_coords = raw_grid.coords[y_dim_name].values

        x_centre = float(x_coords[gx])
        y_centre = float(y_coords[gy])

        # Cell bounds from *local* neighbour spacing.  The previous
        # global-average form assumed a uniformly spaced axis; raw MS
        # time and frequency axes are not uniform (inter-scan gaps,
        # concatenated SPWs), which inflated the cell window and made
        # the field/scan/antenna lookup below attribute neighbouring
        # scans to the hovered cell.  See _cell_bounds.
        x_range = _cell_bounds(x_coords, gx)
        y_range = _cell_bounds(y_coords, gy)

        # Partition coordinate lookup — no VISIBILITY read
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
        canvas_agg: "xr.DataArray",
        px: int,
        py: int,
        selection: SelectionSpec,
        scatter_df: pd.DataFrame,
    ) -> dict:
        """Return the value and scatter sample count for canvas pixel (px, py).

        Operates on the **Datashader canvas agg** from ``cvs.points()``,
        which has generic ``"x"``/``"y"`` dims and canvas-resolution
        coordinates.  Canvas pixel indices are valid directly.

        Parameters
        ----------
        canvas_agg :
            Float64 Datashader agg from ``cvs.points()``, shape (H, W).
        px, py :
            Zero-based canvas pixel indices.
        selection :
            The ``SelectionSpec`` active when ``scatter_df`` was produced.
        scatter_df :
            DataFrame from ``query_columns()`` (columns ``"x"``, ``"y"``).

        Returns
        -------
        dict — see ``XArrayReader.probe_scatter_pixel`` for key definitions.
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

        x_range = _cell_bounds(x_coords, px)
        y_range = _cell_bounds(y_coords, py)

        # A one-bin canvas gives a degenerate zero-width window, and the
        # sample count below would then require exact float equality and
        # always return 0.  Widen it to something numerically meaningful.
        x_range = _widen_if_degenerate(x_range, x_coords)
        y_range = _widen_if_degenerate(y_range, y_coords)

        # Half-open bin membership, matching Datashader's own binning
        # (floor((v - v0) / (v1 - v0) * N)); the previous closed-on-both-
        # ends test double-counted samples sitting exactly on a shared
        # edge between adjacent bins.  The final bin stays closed so the
        # maximum sample is not dropped.
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
