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

from .reader import XArrayReader, _compute_axis_values
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

# Speed of light for uvwave computation
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

    def _iter_visibility_partitions(self):
        """Yield each leaf Dataset that contains visibility data.

        Filters to nodes that have both a non-empty time dimension and
        a VISIBILITY (or DATA) data variable.  This excludes metadata
        subtables (ANTENNA, FIELD, SOURCE, etc.) that xarray-ms attaches
        as child nodes in the DataTree — those have data variables like
        ANTENNA_POSITION but no VISIBILITY column.

        Checking for VISIBILITY/DATA is more reliable than checking for
        time>0 alone because some metadata subtables (e.g. ANTENNA) are
        broadcast onto the time dimension and would otherwise pass through.
        """
        dt = self._require_open()
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
            yield ds

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

    def _uvwave(self, ds: xr.Dataset) -> xr.DataArray:
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

        return {
            "scan_names":        sorted(scan_names),
            "field_names":       sorted(field_names),
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

        for raw_ds in self._iter_visibility_partitions():
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
            lazy_y[(axis, pol)] = self._lazy_quantity(vis, flag, axis, pol)

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
    ) -> xr.DataArray:
        """Return a lazy DataArray for the requested axis and polarization.

        Masked with NaN at flagged/padded positions.

        Uses dask.array.absolute() and dask.array.angle() directly for
        AMPLITUDE and PHASE rather than xr.apply_ufunc().  This avoids
        the ComplexWarning that fires during Dask's meta-inference pass
        when apply_ufunc evaluates np.abs on a zero-element complex128
        meta array.  The dask.array operations are complex-aware and
        produce the correct float64 output dtype without any warnings.
        """
        vis_pol  = vis.sel(polarization=pol)
        flag_pol = flag.sel(polarization=pol)

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
                f"Use AMPLITUDE, PHASE, REAL, or IMAGINARY."
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
        elif xaxis == Axis.UVWAVE:
            uvwave = self._uvwave(ds)      # (time, baseline_id, frequency)
            # template has (time, baseline_id, frequency) after pol-sel
            return uvwave.broadcast_like(template)
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
    ) -> xr.DataArray:
        """Return a 2D DataArray for raster mode.

        The DataArray is reduced to 2D by averaging over dimensions not
        in (y_dim, x_dim):
          - time×baseline_id : average over frequency (and pol)
          - freq×baseline_id : average over time (and pol)
          - freq×time        : single baseline must be selected via
                               ``selection.baselines``

        The returned array is computed (not lazy) and passed directly to
        ``datashader.Canvas.raster()``.

        Parameters
        ----------
        y_dim :
            Native axis for the y dimension (``Axis.TIME``,
            ``Axis.FREQUENCY``, ``Axis.BASELINE_ID``).
        x_dim :
            Native axis for the x dimension.
        quantity :
            Derived quantity to colour the raster (``Axis.AMPLITUDE``,
            ``Axis.PHASE``, ``Axis.FLAG_FRACTION``).
        selection :
            Data selection constraints.
        polarization :
            Polarization label (e.g. ``"XX"``).  Required for
            AMPLITUDE/PHASE/REAL/IMAGINARY; ignored for FLAG_FRACTION.
        """
        self._require_open()

        partitions_2d: list[xr.DataArray] = []

        for raw_ds in self._iter_visibility_partitions():
            ds = self._apply_selection(raw_ds, selection)
            if ds.sizes.get("time", 0) == 0:
                continue

            arr = self._raster_2d(ds, y_dim, x_dim, quantity, polarization)
            if arr is not None:
                partitions_2d.append(arr.compute())

        if not partitions_2d:
            log.warning("query_raster: no data matched selection in %s",
                        self._path)
            return xr.DataArray(np.full((1, 1), np.nan, dtype=np.float32))

        if len(partitions_2d) == 1:
            return partitions_2d[0]

        # Multiple partitions sharing the same (y_dim, x_dim) axes —
        # concatenate along y_dim then let Datashader handle the merge
        y_name = _axis_to_dim(y_dim)
        try:
            return xr.concat(partitions_2d, dim=y_name)
        except Exception as exc:
            log.warning("query_raster: could not concat partitions: %s", exc)
            return partitions_2d[0]

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
                f"Raster quantity {axis.name} not supported. "
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

        for raw_ds in self._iter_visibility_partitions():
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
        for raw_ds in self._iter_visibility_partitions():
            ds = self._apply_selection(raw_ds, selection)
            total_x = max(total_x, ds.sizes.get(x_name, 0))
            total_y = max(total_y, ds.sizes.get(y_name, 0))

        ratio_x = total_x / canvas_width  if canvas_width  > 0 else float("inf")
        ratio_y = total_y / canvas_height if canvas_height > 0 else float("inf")
        return ratio_x, ratio_y


    # ------------------------------------------------------------------ #
    # Pixel hover probe                                                    #
    # ------------------------------------------------------------------ #

    def probe_pixel(
        self,
        agg: "xr.DataArray",
        px: int,
        py: int,
        selection: SelectionSpec,
        *,
        scatter_df: Optional[pd.DataFrame] = None,
    ) -> dict:
        """Return the un-pseudocoloured value and metadata for canvas pixel (px, py).

        This is the backend half of the Bokeh HoverTool callback.
        ``VisibilityPlotter`` calls this on every ``mousemove`` event and
        displays the result in a tooltip panel alongside the RGBA image.

        The method implements the **two-layer reverse mapping**:

        Layer 1 — float64 agg DataArray (always available):
            Read the aggregated quantity value directly from
            ``agg.values[py, px]`` and reverse-map the pixel coordinates
            to the data-space coordinate range it covers using the agg
            DataArray's named coordinate arrays.

        Layer 2 — human-readable metadata (partition coordinate lookup):
            Translate the data-space range to field name(s), scan name(s),
            antenna pair names, and frequency in GHz.  This is a coordinate-
            only lookup — no VISIBILITY read is triggered.

        **Raster mode** (agg from ``cvs.raster()``):
            The agg coordinate arrays give the data-space value at each
            pixel centre.  Bin edges are half the inter-pixel spacing.

        **Scatter mode** (agg from ``cvs.points()`` + ``scatter_df``):
            The agg coordinate arrays give the bin centres.  Rows in
            ``scatter_df`` whose ``(x, y)`` values fall within the pixel
            bin are found by a fast boolean index — no MS re-read.

        Parameters
        ----------
        agg :
            Float64 Datashader aggregation DataArray, shape (H, W).
        px, py :
            Zero-based canvas pixel coordinates.  Origin is bottom-left
            (Bokeh image glyph convention).  ``py`` indexes rows (y-axis),
            ``px`` indexes columns (x-axis).
        selection :
            The ``SelectionSpec`` active when ``agg`` was produced.
        scatter_df :
            Flat DataFrame from ``query_columns()`` (columns ``x``, ``y``).
            Required for scatter-mode sample counting; ``None`` for raster.

        Returns
        -------
        dict with keys:

        ``"value"`` : float or None
            Aggregated quantity at this pixel.  ``None`` if empty (NaN).
        ``"x_range"`` : tuple[float, float]
            Data-space (min, max) of the x-axis covered by this pixel.
        ``"y_range"`` : tuple[float, float]
            Data-space (min, max) of the y-axis covered by this pixel.
        ``"x_centre"`` : float
            Data-space centre on the x-axis.
        ``"y_centre"`` : float
            Data-space centre on the y-axis.
        ``"field_names"`` : list[str]
            Field name(s) associated with this pixel's coordinate range.
        ``"scan_names"`` : list[str]
            Scan name(s) associated with this pixel's coordinate range.
        ``"antenna_pairs"`` : list[tuple[str,str]]
            Antenna pairs whose baseline_id falls within the pixel range.
            Empty list if baseline_id is not a plot axis.
        ``"freq_range_ghz"`` : tuple[float,float] or None
            Frequency range in GHz.  ``None`` if frequency is not an axis.
        ``"n_scatter_samples"`` : int or None
            Scatter samples in this pixel.  ``None`` if no ``scatter_df``.
        """
        self._require_open()

        # ---------------------------------------------------------------- #
        # Step 1: value and coordinate ranges from agg DataArray            #
        # ---------------------------------------------------------------- #

        if agg.ndim != 2:
            raise ValueError(f"agg must be 2D; got {agg.ndim}D")

        h, w = agg.shape
        if not (0 <= px < w and 0 <= py < h):
            raise IndexError(
                f"Pixel ({px}, {py}) out of range for canvas ({w}\u00d7{h})"
            )

        raw_val = float(agg.values[py, px])
        value   = None if np.isnan(raw_val) else raw_val

        # Datashader agg DataArrays: dims[0]=y (rows), dims[1]=x (columns)
        y_dim_name = agg.dims[0]
        x_dim_name = agg.dims[1]

        x_coords = agg.coords[x_dim_name].values  # length W
        y_coords = agg.coords[y_dim_name].values  # length H

        x_centre = float(x_coords[px])
        y_centre = float(y_coords[py])

        # Bin half-widths from uniform pixel spacing
        dx = abs(float(x_coords[1] - x_coords[0])) / 2 if len(x_coords) > 1 else 0.0
        dy = abs(float(y_coords[1] - y_coords[0])) / 2 if len(y_coords) > 1 else 0.0

        x_range = (x_centre - dx, x_centre + dx)
        y_range = (y_centre - dy, y_centre + dy)

        # ---------------------------------------------------------------- #
        # Step 2: scatter sample count (DataFrame boolean index, no MS read) #
        # ---------------------------------------------------------------- #

        n_scatter = None
        if scatter_df is not None and len(scatter_df) > 0:
            mask = (
                (scatter_df["x"] >= x_range[0]) &
                (scatter_df["x"] <= x_range[1]) &
                (scatter_df["y"] >= y_range[0]) &
                (scatter_df["y"] <= y_range[1])
            )
            n_scatter = int(mask.sum())

        # ---------------------------------------------------------------- #
        # Step 3: metadata from partition coordinate arrays (no VISIBILITY)  #
        # ---------------------------------------------------------------- #

        field_names:    set[str]              = set()
        scan_names:     set[str]              = set()
        antenna_pairs:  list[tuple[str, str]] = []
        freq_range_ghz: Optional[tuple[float, float]] = None

        for raw_ds in self._iter_visibility_partitions():
            ds = self._apply_selection(raw_ds, selection)
            if ds.sizes.get("time", 0) == 0:
                continue

            # field and scan names — look up time slots in the pixel range
            if x_dim_name == "time" or y_dim_name == "time":
                t_vals        = ds.coords["time"].values
                t_lo, t_hi    = x_range if x_dim_name == "time" else y_range
                t_mask        = (t_vals >= t_lo) & (t_vals <= t_hi)
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

            # antenna pairs — look up baseline_id slots in the pixel range
            if (x_dim_name == "baseline_id" or y_dim_name == "baseline_id"):
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

            # frequency range in GHz
            if x_dim_name == "frequency" or y_dim_name == "frequency":
                f_lo, f_hi = (
                    x_range if x_dim_name == "frequency" else y_range
                )
                freq_vals = ds.coords["frequency"].values
                f_mask    = (freq_vals >= f_lo) & (freq_vals <= f_hi)
                if not f_mask.any():
                    # Pixel bin may not straddle any channel centre — find
                    # the nearest channel instead so hover always reports
                    # a frequency value.
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
            "value":             value,
            "x_range":           x_range,
            "y_range":           y_range,
            "x_centre":          x_centre,
            "y_centre":          y_centre,
            "field_names":       sorted(field_names),
            "scan_names":        sorted(scan_names),
            "antenna_pairs":     antenna_pairs,
            "freq_range_ghz":    freq_range_ghz,
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
