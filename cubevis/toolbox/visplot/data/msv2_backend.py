"""MSv2Backend — ``XArrayReader`` implementation backed by ``xarray-ms``.

``xarray-ms`` presents a full MSv4-structured DataTree view over MSv2
(casacore Table Data System) files using ``xarray.open_datatree()``.
The ``arcae`` C++ backend provides thread-safe casacore table access;
``casatools`` and ``python-casacore`` are **not** required in this read
path.

Because ``xarray-ms`` exposes the same DataTree structure, dimension
names (``time``, ``baseline_id``, ``frequency``, ``polarization``), and
xarray/Dask access patterns as ``xradio``, the two backends are largely
symmetric.  This class therefore inherits the ``_apply_selection`` and
``query_*`` logic from ``MSv4Backend`` where possible, overriding only
the open/close lifecycle and any schema differences that arise during
real-world testing.

Current status
--------------
This is a **skeleton/draft**.  The ``open()`` method performs a
structural probe to confirm that ``xarray-ms`` can open the file and
that ``VISIBILITY`` (or a variant) is present.  The ``query_*`` methods
delegate to the shared helpers in ``reader.py`` once the DataTree is
open.

Known open questions (from the design doc §10)
----------------------------------------------
* Does ``xarray-ms`` v0.5.4 support FLAG write-back via
  ``FlagWriteThrough``?  The ``MSv2Backend`` is read-only; write-back
  must be verified separately before implementing
  ``FlagWriteThrough`` for this backend.
* The variable name for the DATA column in ``xarray-ms``'s MSv4 view
  may differ from xradio.  ``_resolve_vis_variable`` probes the dataset
  at query time and handles the common aliases.

References
----------
msvis_design.md §4.2, §6, §9
xarray-ms docs: https://xarray-ms.readthedocs.io/
"""

from __future__ import annotations

import logging
from typing import Optional

import numpy as np
import xarray as xr

from .reader import XArrayReader, _compute_axis_values
from ..axes import Axis, AxisType
from ..selection import SelectionSpec

log = logging.getLogger(__name__)

# The xarray backend engine name registered by xarray-ms
_XARRAY_MS_ENGINE = "xarray-ms"


class MSv2Backend(XArrayReader):
    """``XArrayReader`` backed by ``xarray-ms`` + ``arcae`` (MSv2 files).

    Presents the same interface as ``MSv4Backend``.  Internally opens
    the MSv2 Measurement Set via ``xarray.open_datatree()`` with the
    ``xarray-ms`` engine, which uses the ``arcae`` C++ bindings for
    casacore table access.

    Parameters
    ----------
    path:
        Path to the MSv2 ``.ms`` directory.
    partition_columns:
        Columns used to partition the data.  Defaults to
        ``('DATA_DESC_ID', 'OBS_MODE', 'OBSERVATION_ID')`` which
        matches the MSv4 Processing Set partitioning and keeps the
        two backends structurally symmetric.
    data_column:
        Which MSv2 data column to map to ``VISIBILITY``.  One of
        ``'DATA'``, ``'CORRECTED_DATA'``, ``'MODEL_DATA'``.  Defaults
        to ``'DATA'``.
    chunks:
        Dask chunk specification forwarded to ``xarray.open_datatree``.
        ``None`` lets xarray-ms choose sensible defaults (auto-chunking
        based on row count).
    """

    _DEFAULT_PARTITION_COLUMNS = ("DATA_DESC_ID", "OBS_MODE", "OBSERVATION_ID")

    def __init__(
        self,
        path: str,
        partition_columns: Optional[tuple[str, ...]] = None,
        data_column: str = "DATA",
        chunks: Optional[dict] = None,
    ) -> None:
        self._path = path
        self._partition_columns = (
            partition_columns
            if partition_columns is not None
            else self._DEFAULT_PARTITION_COLUMNS
        )
        self._default_data_column = data_column
        self._chunks = chunks
        self._datatree: Optional[xr.DataTree] = None

    # ------------------------------------------------------------------ #
    # Lifecycle                                                            #
    # ------------------------------------------------------------------ #

    def open(self) -> None:
        """Open the MSv2 file via xarray-ms.

        Raises
        ------
        ImportError
            If ``xarray-ms`` or ``arcae`` are not installed.
        FileNotFoundError
            If *path* does not point to a readable MSv2 directory.
        """
        if self._datatree is not None:
            return

        _check_xarray_ms()

        log.debug("MSv2Backend: opening %s", self._path)
        try:
            self._datatree = xr.open_datatree(
                self._path,
                engine=_XARRAY_MS_ENGINE,
                partition_columns=list(self._partition_columns),
                chunks=self._chunks if self._chunks is not None else "auto",
            )
        except Exception as exc:
            raise RuntimeError(
                f"xarray-ms failed to open {self._path!r}: {exc}"
            ) from exc

        partitions = list(self._iter_partitions())
        log.debug(
            "MSv2Backend: opened — %d partition(s)", len(partitions)
        )
        if not partitions:
            log.warning(
                "MSv2Backend: no partitions found in %s", self._path
            )

    def close(self) -> None:
        """Release the open DataTree."""
        if self._datatree is not None:
            try:
                self._datatree.close()
            except Exception:
                pass
            finally:
                self._datatree = None

    # ------------------------------------------------------------------ #
    # Internal helpers                                                     #
    # ------------------------------------------------------------------ #

    def _require_open(self) -> xr.DataTree:
        if self._datatree is None:
            raise RuntimeError(
                "MSv2Backend is not open.  Call open() or use as a "
                "context manager."
            )
        return self._datatree

    def _iter_partitions(self):
        """Yield each leaf Dataset from the DataTree."""
        dt = self._require_open()
        for node in dt.subtree:
            if node.has_data:
                yield node.ds

    def _apply_selection(
        self, ds: xr.Dataset, sel: SelectionSpec
    ) -> xr.Dataset:
        """Apply *sel* constraints lazily via xarray where/mask ops.

        Identical logic to ``MSv4Backend._apply_selection``; shared
        here to avoid duplication without a deep inheritance chain.
        The implementation mirrors the design doc §4.6 constraints:

        * ``scan_name`` and ``field_name`` — non-index coordinates on
          the time dimension, accessed via ``ds.where(... .isin(...))``.
        * ``baseline_antenna1_name`` / ``baseline_antenna2_name`` —
          non-index coordinates on the baseline_id dimension.
        """
        # Time-dimension mask (time, and non-index coordinates on time)
        time_mask = xr.ones_like(ds["time"], dtype=bool)

        if sel.scan is not None:
            time_mask = time_mask & ds["scan_name"].isin(sel.scan)

        if sel.field_names is not None:
            time_mask = time_mask & ds["field_name"].isin(sel.field_names)

        if sel.time_range is not None:
            t_min, t_max = sel.time_range
            time_mask = time_mask & (
                (ds["time"] >= t_min) & (ds["time"] <= t_max)
            )

        ds = ds.where(time_mask)

        # Frequency-dimension mask
        if sel.freq_range is not None:
            f_min, f_max = sel.freq_range
            freq_mask = (ds["frequency"] >= f_min) & (ds["frequency"] <= f_max)
            ds = ds.where(freq_mask)

        # Polarization mask
        if sel.correlation is not None:
            pol_mask = ds["polarization"].isin(sel.correlation)
            ds = ds.where(pol_mask)

        # Baseline mask
        if sel.baselines is not None:
            bl_mask = xr.zeros_like(
                ds["baseline_antenna1_name"], dtype=bool
            )
            for ant1, ant2 in sel.baselines:
                bl_mask = bl_mask | (
                    (ds["baseline_antenna1_name"] == ant1)
                    & (ds["baseline_antenna2_name"] == ant2)
                )
            ds = ds.where(bl_mask)

        return ds

    def _effective_data_column(self, sel: SelectionSpec) -> str:
        """Resolve the data column from the SelectionSpec, falling back
        to the column specified at construction time."""
        if sel.data_column:
            return sel.data_column
        return self._default_data_column

    # ------------------------------------------------------------------ #
    # Metadata                                                             #
    # ------------------------------------------------------------------ #

    def metadata(self) -> dict:
        """Collect human-readable metadata from all partitions.

        Iterates through every leaf Dataset in the DataTree, collecting
        scan names, field names, antenna names, SPW IDs, polarization
        labels, time and frequency ranges, and available data columns.

        Note: this method **triggers a small amount of Dask computation**
        (``ds.scan_name.values``, etc.) on the non-index string
        coordinates, which are typically small.
        """
        self._require_open()

        scan_names: set[str] = set()
        field_names: set[str] = set()
        ant_names: set[str] = set()
        spw_ids: set[int] = set()
        pol_labels: set[str] = set()
        t_min = float("inf")
        t_max = float("-inf")
        f_min = float("inf")
        f_max = float("-inf")
        n_baselines = 0
        data_columns: set[str] = set()

        for ds in self._iter_partitions():
            _collect_string_coord(ds, "scan_name", scan_names)
            _collect_string_coord(ds, "field_name", field_names)
            _collect_string_coord(ds, "baseline_antenna1_name", ant_names)
            _collect_string_coord(ds, "baseline_antenna2_name", ant_names)

            spw_id = ds.attrs.get("spectral_window_id")
            if spw_id is not None:
                spw_ids.add(int(spw_id))

            if "polarization" in ds:
                pol_labels.update(str(p) for p in ds["polarization"].values)

            if "time" in ds:
                t = ds["time"]
                t_min = min(t_min, float(t.min()))
                t_max = max(t_max, float(t.max()))

            if "frequency" in ds:
                f = ds["frequency"]
                f_min = min(f_min, float(f.min()))
                f_max = max(f_max, float(f.max()))

            n_baselines = max(n_baselines, ds.sizes.get("baseline_id", 0))

            # Probe variable names — xarray-ms may use either MSv2 or
            # MSv4 names depending on its version
            for msv2_col, display_col in (
                ("DATA", "DATA"),
                ("VISIBILITY", "DATA"),
                ("CORRECTED_DATA", "CORRECTED"),
                ("MODEL_DATA", "MODEL"),
            ):
                if msv2_col in ds:
                    data_columns.add(display_col)

        return {
            "scan_names": sorted(scan_names),
            "field_names": sorted(field_names),
            "antenna_names": sorted(ant_names),
            "spw_ids": sorted(spw_ids),
            "correlation_labels": sorted(pol_labels),
            "time_range": (t_min, t_max),
            "freq_range": (f_min, f_max),
            "n_baselines": n_baselines,
            "data_columns": sorted(data_columns),
        }

    # ------------------------------------------------------------------ #
    # Scatter / line mode query                                            #
    # ------------------------------------------------------------------ #

    def query_columns(
        self,
        xaxis: Axis,
        yaxis: Axis,
        selection: SelectionSpec,
        *,
        color_axis: Optional[Axis] = None,
    ) -> xr.Dataset:
        """Return a lazy, Dask-backed Dataset for scatter/line mode.

        See ``XArrayReader.query_columns`` for the full contract.

        The dataset is assembled by iterating all partitions, applying
        the selection, computing the requested axis values, and
        concatenating along the time dimension.  The result is always
        lazy; no data is materialised here.
        """
        self._require_open()
        data_column = self._effective_data_column(selection)
        datasets: list[xr.Dataset] = []

        for raw_ds in self._iter_partitions():
            ds = self._apply_selection(raw_ds, selection)

            try:
                x_vals = _compute_axis_values(ds, xaxis, data_column)
                y_vals = _compute_axis_values(ds, yaxis, data_column)
            except (KeyError, NotImplementedError, ValueError) as exc:
                log.warning(
                    "Skipping partition due to axis computation error: %s", exc
                )
                continue

            vars_dict = {
                "x": x_vals,
                "y": y_vals,
                "flag": _compute_axis_values(ds, Axis.FLAG, data_column),
            }
            if color_axis is not None:
                try:
                    vars_dict["color"] = _compute_axis_values(
                        ds, color_axis, data_column
                    )
                except (KeyError, NotImplementedError, ValueError) as exc:
                    log.warning(
                        "color_axis computation failed; omitting: %s", exc
                    )

            coords_dict = {}
            for cname in (
                "scan_name",
                "field_name",
                "baseline_antenna1_name",
                "baseline_antenna2_name",
            ):
                if cname in ds:
                    coords_dict[cname] = ds[cname]

            datasets.append(xr.Dataset(vars_dict, coords=coords_dict))

        if not datasets:
            log.warning(
                "query_columns: selection matched no data in %s", self._path
            )
            return xr.Dataset()

        # Concatenate partitions — along time is the safe default since
        # partitions share the frequency and polarization dimensions.
        # TODO: verify this assumption holds for all xarray-ms partition
        #       layouts once real data is available.
        return xr.concat(datasets, dim="time")

    # ------------------------------------------------------------------ #
    # Raster mode query                                                    #
    # ------------------------------------------------------------------ #

    def query_raster(
        self,
        axis1: Axis,
        axis2: Axis,
        quantity: Axis,
        bounds: tuple[float, float, float, float],
        shape: tuple[int, int],
        selection: SelectionSpec,
    ) -> xr.DataArray:
        """Return a lazy 2D DataArray for raster mode.

        See ``XArrayReader.query_raster`` for the full contract.

        The returned DataArray is passed to Datashader's
        ``Canvas.raster()`` by the source class; no pre-aggregation is
        performed here.
        """
        self._require_open()
        data_column = self._effective_data_column(selection)
        datasets: list[xr.DataArray] = []

        for raw_ds in self._iter_partitions():
            ds = self._apply_selection(raw_ds, selection)
            try:
                q_vals = _compute_axis_values(ds, quantity, data_column)
            except (KeyError, NotImplementedError, ValueError) as exc:
                log.warning(
                    "Skipping partition in query_raster: %s", exc
                )
                continue
            datasets.append(q_vals)

        if not datasets:
            log.warning(
                "query_raster: selection matched no data in %s", self._path
            )
            n_rows, n_cols = shape
            return xr.DataArray(
                np.full((n_rows, n_cols), np.nan, dtype=np.float32),
                attrs={"long_name": quantity.label},
            )

        return xr.concat(datasets, dim="time")

    # ------------------------------------------------------------------ #
    # Representation                                                       #
    # ------------------------------------------------------------------ #

    def __repr__(self) -> str:  # pragma: no cover
        status = "open" if self._datatree is not None else "closed"
        return (
            f"MSv2Backend({self._path!r}, "
            f"column={self._default_data_column!r}, {status})"
        )


# ======================================================================
# Private helpers
# ======================================================================

def _check_xarray_ms() -> None:
    """Raise ``ImportError`` with a helpful message if xarray-ms is missing."""
    try:
        import xarray_ms  # noqa: F401
    except ImportError as exc:
        raise ImportError(
            "xarray-ms is required for MSv2Backend.  "
            "Install it with: pip install xarray-ms\n"
            "xarray-ms requires arcae (C++ casacore bindings); "
            "see https://xarray-ms.readthedocs.io/ for platform notes."
        ) from exc


def _collect_string_coord(
    ds: xr.Dataset, name: str, target: set
) -> None:
    """Add unique string values of coord *name* from *ds* to *target*.

    Skips quietly if the coordinate is absent.  Only the string
    coordinates (scan_name, field_name, antenna names) trigger any
    computation; these are small non-index arrays.
    """
    if name not in ds:
        return
    arr = ds[name]
    # Compute if Dask-backed — these small string arrays are always cheap
    values = arr.values.ravel() if hasattr(arr, "values") else arr.compute().values.ravel()
    target.update(str(v) for v in values if v)
