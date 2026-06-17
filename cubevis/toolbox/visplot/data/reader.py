"""XArrayReader — abstract base class and MSv4 backend.

``XArrayReader`` is the single data-access abstraction used by all
``visplot`` source classes.  It wraps either ``xarray-ms`` (for MSv2
files) or ``xradio`` (for MSv4 Zarr), and presents an identical
MSv4-structured DataTree interface to every layer above.

Key design principles
---------------------
* **No Bokeh dependency** — pure Python / xarray / Dask.
* **Accepts ``Axis`` members**, not bare strings, for all axis
  arguments; uses ``Axis.axis_type`` to dispatch the correct xarray
  operation (coordinate passthrough vs. derived computation).
* **Read-only** — flag persistence is entirely the ``FlagDB``'s
  responsibility; no write methods here.
* **Always includes metadata labels** — ``query_columns`` returns
  ``scan_name``, ``field_name``, ``baseline_antenna1_name``, and
  ``baseline_antenna2_name`` alongside the requested data so that
  layers above can annotate plots without a second round-trip.
* **Lazy / Dask-backed** — nothing is materialised until Datashader
  calls ``.compute()`` implicitly during aggregation.

Module layout
-------------
``XArrayReader``     — abstract base class (this file)
``MSv4Backend``      — xradio implementation (this file)
``MSv2Backend``      — xarray-ms/arcae implementation (msv2_backend.py)

References
----------
msvis_design.md §4.2 (XArrayReader), §4.6 (MSv4 coordinate model),
§6 (MSv2 I/O)
"""

from __future__ import annotations

import abc
import logging
from typing import Optional, Union

import numpy as np
import pandas as pd
import xarray as xr

from .axes import Axis, AxisType
from .selection import SelectionSpec

log = logging.getLogger(__name__)


# ======================================================================
# Axis dispatch helpers
# ======================================================================

def _compute_axis_values(
    ds: xr.Dataset,
    axis: Axis,
    data_column: str = "DATA",
) -> xr.DataArray:
    """Compute the values for *axis* from *ds*.

    Returns a DataArray with dimensions drawn from the MSv4 DataTree
    schema (``time``, ``baseline_id``, ``frequency``, ``polarization``).

    Parameters
    ----------
    ds:
        An xarray Dataset from a single partition of the MSv4 DataTree.
        Expected data variables: ``VISIBILITY`` (or whatever column is
        selected), ``UVW``, ``FLAG``, ``WEIGHT``, ``WEIGHT_SPECTRUM``.
    axis:
        The axis to compute.
    data_column:
        One of ``'DATA'``, ``'CORRECTED'``, ``'MODEL'`` — selects which
        xarray variable holds the complex visibilities.  The MSv4 schema
        uses ``VISIBILITY`` for the primary data column; mapping from the
        MSv2 column names is done at open time in the backend.

    Raises
    ------
    NotImplementedError
        For calibration axes (handled by a future CalTableReader).
    ValueError
        For axis members that cannot be extracted from a visibility DS.
    """
    vis_var = _resolve_vis_variable(ds, data_column)

    match axis:
        # --- native continuous: return the coordinate directly ----------
        case Axis.TIME:
            return ds["time"]
        case Axis.FREQUENCY:
            return ds["frequency"]
        case Axis.CHANNEL:
            # channel index along the frequency dimension
            freq = ds["frequency"]
            chan = xr.DataArray(
                np.arange(freq.sizes["frequency"]),
                dims=["frequency"],
                attrs={"long_name": "Channel", "units": ""},
            )
            return chan
        case Axis.VELOCITY:
            # radio velocity — requires rest_frequency attribute on the
            # frequency coordinate; fall back gracefully
            freq = ds["frequency"]
            rest = float(freq.attrs.get("rest_frequency", np.nan))
            if np.isnan(rest):
                raise ValueError(
                    "Axis.VELOCITY requires rest_frequency to be set "
                    "on the 'frequency' coordinate."
                )
            c = 299_792_458.0  # m/s
            return (c * (rest - freq) / rest).assign_attrs(
                {"long_name": "Radio Velocity", "units": "m/s"}
            )
        case Axis.U:
            return ds["UVW"].sel(uvw_index=0).drop_vars("uvw_index")
        case Axis.V:
            return ds["UVW"].sel(uvw_index=1).drop_vars("uvw_index")
        case Axis.W:
            return ds["UVW"].sel(uvw_index=2).drop_vars("uvw_index")
        case Axis.UVDIST:
            u = ds["UVW"].sel(uvw_index=0)
            v = ds["UVW"].sel(uvw_index=1)
            return np.sqrt(u ** 2 + v ** 2).assign_attrs(
                {"long_name": "UV Distance", "units": "m"}
            )
        case Axis.INTERVAL:
            return ds["INTERVAL"]
        case Axis.ROW:
            # Row is MSv2 provenance info; in MSv4 we use a simple index
            n = ds.sizes.get("time", 1) * ds.sizes.get("baseline_id", 1)
            return xr.DataArray(
                np.arange(n),
                attrs={"long_name": "Row", "units": ""},
            )

        # --- native discrete: return non-index coordinate labels --------
        case Axis.BASELINE:
            # Return a composite label "ant1 & ant2" for display
            a1 = ds["baseline_antenna1_name"]
            a2 = ds["baseline_antenna2_name"]
            return (a1 + " & " + a2).assign_attrs({"long_name": "Baseline"})
        case Axis.ANTENNA1:
            return ds["baseline_antenna1_name"].assign_attrs(
                {"long_name": "Antenna 1"}
            )
        case Axis.ANTENNA2:
            return ds["baseline_antenna2_name"].assign_attrs(
                {"long_name": "Antenna 2"}
            )
        case Axis.CORRELATION:
            return ds["polarization"].assign_attrs({"long_name": "Correlation"})
        case Axis.SCAN:
            return ds["scan_name"].assign_attrs({"long_name": "Scan"})
        case Axis.FIELD:
            return ds["field_name"].assign_attrs({"long_name": "Field"})
        case Axis.SPW:
            # SPW is a partition-level scalar in MSv4; expose as a
            # broadcast scalar DataArray for consistency
            spw_id = int(ds.attrs.get("spectral_window_id", -1))
            return xr.DataArray(spw_id, attrs={"long_name": "SPW"})
        case Axis.OBSERVATION:
            obs_id = int(ds.attrs.get("observation_id", -1))
            return xr.DataArray(obs_id, attrs={"long_name": "Observation"})
        case Axis.INTENT:
            intent = str(ds.attrs.get("intent", ""))
            return xr.DataArray(intent, attrs={"long_name": "Intent"})

        # --- derived: compute from visibility data ----------------------
        case Axis.AMPLITUDE:
            return np.abs(ds[vis_var]).assign_attrs(
                {"long_name": "Amplitude", "units": ""}
            )
        case Axis.PHASE:
            return np.angle(ds[vis_var]).assign_attrs(
                {"long_name": "Phase", "units": "rad"}
            )
        case Axis.REAL:
            return ds[vis_var].real.assign_attrs(
                {"long_name": "Real", "units": ""}
            )
        case Axis.IMAGINARY:
            return ds[vis_var].imag.assign_attrs(
                {"long_name": "Imaginary", "units": ""}
            )
        case Axis.WEIGHT:
            return ds["WEIGHT"].assign_attrs({"long_name": "Weight"})
        case Axis.WEIGHT_SPECTRUM:
            return ds["WEIGHT_SPECTRUM"].assign_attrs(
                {"long_name": "Weight Spectrum"}
            )
        case Axis.FLAG:
            return ds["FLAG"].assign_attrs({"long_name": "Flag"})
        case Axis.UVDIST_LAMBDA:
            u = ds["UVW"].sel(uvw_index=0)
            v = ds["UVW"].sel(uvw_index=1)
            uvdist_m = np.sqrt(u ** 2 + v ** 2)
            freq = ds["frequency"]
            c = 299_792_458.0
            wavelength = c / freq  # broadcasts over (baseline_id, frequency)
            return (uvdist_m / wavelength).assign_attrs(
                {"long_name": "UV Distance", "units": "λ"}
            )
        case Axis.AZIMUTH | Axis.ELEVATION | Axis.HOUR_ANGLE | Axis.PARALLACTIC_ANGLE:
            # Observational geometry: requires POINTING subtable.
            # Stub — implementation deferred until POINTING integration.
            raise NotImplementedError(
                f"{axis.label} requires the POINTING subtable; "
                "not yet implemented."
            )

        # --- calibration table axes ------------------------------------
        case (
            Axis.GAIN_AMPLITUDE | Axis.GAIN_PHASE | Axis.DELAY
            | Axis.TSYS | Axis.SNR | Axis.OPACITY
        ):
            raise NotImplementedError(
                f"{axis.label} is a calibration-table axis and requires "
                "a CalTableReader; not supported on a visibility dataset."
            )

        case _:
            raise ValueError(f"Unknown Axis member: {axis!r}")


def _resolve_vis_variable(ds: xr.Dataset, data_column: str) -> str:
    """Map *data_column* name to the variable name in *ds*.

    MSv4 uses 'VISIBILITY' for the primary column; the backend may
    expose 'CORRECTED_DATA' and 'MODEL_DATA' as-is or under translated
    names.  This function probes the dataset and returns the first name
    that matches.
    """
    column_map = {
        "DATA": ["VISIBILITY", "DATA"],
        "CORRECTED": ["CORRECTED_DATA", "CORRECTED"],
        "MODEL": ["MODEL_DATA", "MODEL"],
    }
    candidates = column_map.get(data_column.upper(), [data_column])
    for name in candidates:
        if name in ds:
            return name
    raise KeyError(
        f"data_column={data_column!r} not found in dataset.  "
        f"Available variables: {list(ds.data_vars)}"
    )


# ======================================================================
# Abstract base class
# ======================================================================

class XArrayReader(abc.ABC):
    """Abstract base class for MSv2 and MSv4 data readers.

    Subclasses wrap either ``xarray-ms`` (MSv2 via ``arcae``) or
    ``xradio`` (MSv4 Zarr), presenting an identical MSv4-structured
    interface to the layers above.

    All public methods accept ``Axis`` members — never bare strings —
    for axis arguments.  Selection parameters are conveyed through
    ``SelectionSpec`` instances whose fields use human-readable string
    identifiers.

    Instances are **read-only**.  Flag persistence is the
    ``FlagDB``'s responsibility.
    """

    # ------------------------------------------------------------------ #
    # Lifecycle                                                            #
    # ------------------------------------------------------------------ #

    @abc.abstractmethod
    def open(self) -> None:
        """Open the underlying data source.

        Called once after construction.  Should be idempotent if called
        more than once.
        """

    @abc.abstractmethod
    def close(self) -> None:
        """Release resources held by the underlying data source."""

    def __enter__(self) -> "XArrayReader":
        self.open()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # ------------------------------------------------------------------ #
    # Metadata                                                             #
    # ------------------------------------------------------------------ #

    @abc.abstractmethod
    def metadata(self) -> dict:
        """Return human-readable metadata for populating GUI controls.

        The returned dict has the following keys (all values are
        human-readable strings or Python scalars, never internal
        integer indices):

        ``scan_names`` : list[str]
            All scan name strings present in the dataset.
        ``field_names`` : list[str]
            All field name strings.
        ``antenna_names`` : list[str]
            All antenna name strings (sorted).
        ``spw_ids`` : list[int]
            Spectral window indices present.
        ``correlation_labels`` : list[str]
            Polarization product labels, e.g. ``['XX', 'YY']``.
        ``time_range`` : tuple[float, float]
            ``(t_min, t_max)`` in MJD seconds.
        ``freq_range`` : tuple[float, float]
            ``(f_min, f_max)`` in Hz, across all SPWs.
        ``n_baselines`` : int
            Total number of unique baselines.
        ``data_columns`` : list[str]
            Available data columns, e.g. ``['DATA', 'CORRECTED']``.
        """

    # ------------------------------------------------------------------ #
    # Scatter / line mode query                                            #
    # ------------------------------------------------------------------ #

    @abc.abstractmethod
    def query_columns(
        self,
        xaxis: Axis,
        yaxis: Axis,
        selection: SelectionSpec,
        *,
        color_axis: Optional[Axis] = None,
    ) -> xr.Dataset:
        """Return a lazy, Dask-backed Dataset for scatter/line mode.

        The returned dataset always includes:

        * ``x`` — values for *xaxis*, computed via ``_compute_axis_values``.
        * ``y`` — values for *yaxis*.
        * ``flag`` — the FLAG column (``bool``), for three-colour overlay.
        * ``scan_name`` — non-index string coordinate on the time dim.
        * ``field_name`` — non-index string coordinate on the time dim.
        * ``baseline_antenna1_name`` — non-index coordinate on baseline dim.
        * ``baseline_antenna2_name`` — non-index coordinate on baseline dim.
        * ``color`` — values for *color_axis* if supplied, else absent.

        Datashader consumes this Dataset directly via
        ``Canvas.points()``.  No pre-averaging is performed; Datashader
        aggregates to pixel resolution.

        Parameters
        ----------
        xaxis:
            Axis member for the horizontal axis.
        yaxis:
            Axis member for the vertical axis.
        selection:
            Data selection specification.
        color_axis:
            Optional axis to encode as point colour.

        Returns
        -------
        xr.Dataset
            Lazy, Dask-backed Dataset.  Call ``.compute()`` only inside
            Datashader (never materialise the full array in Python).
        """

    # ------------------------------------------------------------------ #
    # Raster mode query                                                    #
    # ------------------------------------------------------------------ #

    @abc.abstractmethod
    def query_raster(
        self,
        axis1: Axis,
        axis2: Axis,
        quantity: Axis,
        bounds: tuple[float, float, float, float],
        shape: tuple[int, int],
        selection: SelectionSpec,
    ) -> xr.DataArray:
        """Return a 2D DataArray covering *bounds* at viewport pixel *shape*.

        Parameters
        ----------
        axis1:
            Axis for the first raster dimension (e.g. ``Axis.BASELINE``).
        axis2:
            Axis for the second raster dimension (e.g. ``Axis.TIME``).
        quantity:
            Axis whose values colour the raster cells (e.g.
            ``Axis.AMPLITUDE``).
        bounds:
            ``(x_min, x_max, y_min, y_max)`` in native *axis1* / *axis2*
            coordinate units.  The reader need not clip to the stored
            data extent — out-of-bounds regions become NaN.
        shape:
            ``(n_rows, n_cols)`` output pixel shape, matching the
            current Bokeh canvas size.
        selection:
            Additional data selection constraints beyond the viewport
            bounds.

        Returns
        -------
        xr.DataArray
            2D, Dask-backed DataArray with dimensions
            ``(axis1_label, axis2_label)`` and *quantity* values.
            Passed to Datashader ``Canvas.raster()``.
        """

    # ------------------------------------------------------------------ #
    # Pixel hover probe                                                    #
    # ------------------------------------------------------------------ #

    @abc.abstractmethod
    def probe_pixel(
        self,
        agg: xr.DataArray,
        px: int,
        py: int,
        selection: SelectionSpec,
        *,
        scatter_df: Optional["pd.DataFrame"] = None,
    ) -> dict:
        """Return the un-pseudocoloured value and metadata for pixel (px, py).

        This is the backend half of the Bokeh HoverTool callback.
        ``VisibilityPlotter`` calls this on every ``mousemove`` event.

        The method implements a two-layer reverse mapping:

        * **Layer 1** — read the aggregated float64 value directly from
          ``agg.values[py, px]`` and reverse-map ``(px, py)`` to the
          data-space coordinate range it covers using the agg DataArray's
          named coordinate arrays.
        * **Layer 2** — translate that range to human-readable metadata
          (field names, scan names, antenna pairs, frequency in GHz) via
          a coordinate-only lookup against the open partitions.  No
          VISIBILITY data is read.

        Parameters
        ----------
        agg :
            Float64 Datashader aggregation DataArray, shape (H, W), from
            the most recent ``cvs.raster()`` or ``cvs.points()`` call.
        px, py :
            Zero-based canvas pixel coordinates.  Origin is bottom-left
            (Bokeh image_rgba glyph convention).  ``py`` indexes rows
            (y-axis), ``px`` indexes columns (x-axis).
        selection :
            The ``SelectionSpec`` that was active when ``agg`` was produced.
            Used to narrow the partition scan for metadata retrieval.
        scatter_df :
            Flat pandas DataFrame returned by ``query_columns()`` (columns
            ``"x"`` and ``"y"``).  When supplied, the method counts how
            many individual scatter samples fell in this pixel by a boolean
            index on the DataFrame — no MS re-read.  Pass ``None`` for
            raster mode where the sample count is not meaningful.

        Returns
        -------
        dict with keys:

        ``"value"`` : float or None
            Aggregated quantity at this pixel.  ``None`` if the pixel is
            empty (NaN in agg).
        ``"x_range"`` : tuple[float, float]
            Data-space (min, max) of the x-axis covered by this pixel.
        ``"y_range"`` : tuple[float, float]
            Data-space (min, max) of the y-axis covered by this pixel.
        ``"x_centre"`` : float
            Data-space centre of the pixel on the x-axis.
        ``"y_centre"`` : float
            Data-space centre of the pixel on the y-axis.
        ``"field_names"`` : list[str]
            Field name(s) associated with this pixel's coordinate range.
        ``"scan_names"`` : list[str]
            Scan name(s) associated with this pixel's coordinate range.
        ``"antenna_pairs"`` : list[tuple[str, str]]
            Antenna pairs whose baseline_id falls within the pixel range.
            Empty list if ``baseline_id`` is not a plot axis.
        ``"freq_range_ghz"`` : tuple[float, float] or None
            Frequency range in GHz covered by the pixel.  ``None`` if
            frequency is not a plot axis.
        ``"n_scatter_samples"`` : int or None
            Number of individual scatter samples in this pixel.
            ``None`` if ``scatter_df`` was not provided.
        """

    # ------------------------------------------------------------------ #
    # Convenience                                                          #
    # ------------------------------------------------------------------ #

    def available_axes(self) -> list[Axis]:
        """Return the subset of ``Axis`` members valid for this reader.

        Excludes ``CALIBRATION`` axes (only valid against cal tables)
        and any axes whose underlying data variables are absent from the
        open dataset.

        Override in subclasses to refine based on actual variable
        availability.
        """
        return [
            ax for ax in Axis
            if ax.axis_type is not AxisType.CALIBRATION
        ]


# ======================================================================
# MSv4 backend (xradio / Zarr)
# ======================================================================

class MSv4Backend(XArrayReader):
    """``XArrayReader`` implementation backed by ``xradio`` (MSv4 Zarr).

    Opens an MSv4 Processing Set (``*.ps.zarr``) via
    ``xradio.read_processing_set()`` which returns a DataTree of lazy,
    Dask-backed xarray Datasets.

    Each partition in the Processing Set corresponds to a unique
    ``(DATA_DESC_ID, OBS_MODE, OBSERVATION_ID)`` tuple and may contain
    multiple scans and fields.  ``SelectionSpec`` constraints on
    ``scan`` and ``field_names`` are applied via
    ``ds.where(ds.scan_name.isin(...))`` boolean masking, not via
    ``.sel()`` (see §4.6 of the design doc).

    Parameters
    ----------
    path:
        Path to the root of the ``*.ps.zarr`` Processing Set directory.
    chunks:
        Dask chunk specification passed to ``xradio``.  ``None`` uses
        the stored Zarr chunk layout, which is usually the right choice.
    """

    def __init__(
        self,
        path: str,
        chunks: Optional[dict] = None,
    ) -> None:
        self._path = path
        self._chunks = chunks
        self._datatree: Optional[object] = None  # xr.DataTree once opened

    # ------------------------------------------------------------------ #

    def open(self) -> None:
        if self._datatree is not None:
            return
        try:
            import xradio  # noqa: F401 — presence check
            from xradio.measurement_set import open_processing_set
        except ImportError as exc:
            raise ImportError(
                "xradio is required for MSv4Backend.  "
                "Install it with: pip install xradio"
            ) from exc

        log.debug("MSv4Backend: opening %s", self._path)
        self._datatree = open_processing_set(
            self._path,
            chunks=self._chunks,
        )
        log.debug("MSv4Backend: opened — %d partition(s)", len(self._datatree))

    def close(self) -> None:
        self._datatree = None

    # ------------------------------------------------------------------ #

    def _require_open(self) -> object:
        if self._datatree is None:
            raise RuntimeError(
                "MSv4Backend is not open.  Call open() or use as a "
                "context manager."
            )
        return self._datatree

    def _iter_partitions(self) -> "Iterator[xr.Dataset]":
        """Yield each leaf Dataset from the DataTree."""
        dt = self._require_open()
        for node in dt.subtree:
            if node.has_data:
                yield node.ds

    def _apply_selection(
        self, ds: xr.Dataset, sel: SelectionSpec
    ) -> xr.Dataset:
        """Apply *sel* constraints to *ds* via xarray operations.

        All boolean masks are applied lazily so that no data is
        materialised.
        """
        mask = xr.ones_like(ds["time"], dtype=bool)

        if sel.scan is not None:
            scan_mask = ds["scan_name"].isin(sel.scan)
            # scan_name is on the time dimension; broadcast over baseline
            mask = mask & scan_mask

        if sel.field_names is not None:
            field_mask = ds["field_name"].isin(sel.field_names)
            mask = mask & field_mask

        if sel.time_range is not None:
            t_min, t_max = sel.time_range
            mask = mask & (ds["time"] >= t_min) & (ds["time"] <= t_max)

        if sel.freq_range is not None:
            f_min, f_max = sel.freq_range
            freq_mask = (ds["frequency"] >= f_min) & (ds["frequency"] <= f_max)
            # freq is a separate dimension; apply via .where() on that dim
            ds = ds.where(freq_mask)

        if sel.correlation is not None:
            pol_mask = ds["polarization"].isin(sel.correlation)
            ds = ds.where(pol_mask)

        if sel.baselines is not None:
            # Build a boolean mask over the baseline_id dimension
            bl_mask = xr.zeros_like(
                ds["baseline_antenna1_name"], dtype=bool
            )
            for ant1, ant2 in sel.baselines:
                bl_mask = bl_mask | (
                    (ds["baseline_antenna1_name"] == ant1)
                    & (ds["baseline_antenna2_name"] == ant2)
                )
            # baseline_id is a separate dimension from time
            ds = ds.where(bl_mask)

        return ds.where(mask)

    # ------------------------------------------------------------------ #

    def metadata(self) -> dict:
        dt = self._require_open()
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
            # Scan / field — non-index string coordinates on time dim
            if "scan_name" in ds:
                scan_names.update(str(s) for s in ds["scan_name"].values.ravel())
            if "field_name" in ds:
                field_names.update(str(f) for f in ds["field_name"].values.ravel())

            # Antenna names
            if "baseline_antenna1_name" in ds:
                ant_names.update(ds["baseline_antenna1_name"].values.ravel())
            if "baseline_antenna2_name" in ds:
                ant_names.update(ds["baseline_antenna2_name"].values.ravel())

            # SPW
            spw_id = ds.attrs.get("spectral_window_id")
            if spw_id is not None:
                spw_ids.add(int(spw_id))

            # Polarization labels
            if "polarization" in ds:
                pol_labels.update(str(p) for p in ds["polarization"].values)

            # Time range
            if "time" in ds:
                t = ds["time"]
                t_min = min(t_min, float(t.min()))
                t_max = max(t_max, float(t.max()))

            # Frequency range
            if "frequency" in ds:
                f = ds["frequency"]
                f_min = min(f_min, float(f.min()))
                f_max = max(f_max, float(f.max()))

            # Baseline count (per partition, not deduplicated across)
            n_baselines = max(n_baselines, ds.sizes.get("baseline_id", 0))

            # Data columns
            for col in ("VISIBILITY", "CORRECTED_DATA", "MODEL_DATA"):
                if col in ds:
                    data_columns.add(
                        {"VISIBILITY": "DATA", "CORRECTED_DATA": "CORRECTED",
                         "MODEL_DATA": "MODEL"}[col]
                    )

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

    def query_columns(
        self,
        xaxis: Axis,
        yaxis: Axis,
        selection: SelectionSpec,
        *,
        color_axis: Optional[Axis] = None,
    ) -> xr.Dataset:
        self._require_open()
        datasets: list[xr.Dataset] = []

        for raw_ds in self._iter_partitions():
            ds = self._apply_selection(raw_ds, selection)

            x_vals = _compute_axis_values(ds, xaxis, selection.data_column)
            y_vals = _compute_axis_values(ds, yaxis, selection.data_column)

            out = xr.Dataset(
                {
                    "x": x_vals,
                    "y": y_vals,
                    "flag": _compute_axis_values(ds, Axis.FLAG, selection.data_column),
                },
                coords={
                    "scan_name": ds.get("scan_name"),
                    "field_name": ds.get("field_name"),
                    "baseline_antenna1_name": ds.get("baseline_antenna1_name"),
                    "baseline_antenna2_name": ds.get("baseline_antenna2_name"),
                },
            )
            if color_axis is not None:
                out["color"] = _compute_axis_values(
                    ds, color_axis, selection.data_column
                )

            datasets.append(out)

        if not datasets:
            log.warning("query_columns: no partitions matched selection")
            return xr.Dataset()

        return xr.concat(datasets, dim="time")

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
        self._require_open()
        datasets: list[xr.DataArray] = []

        for raw_ds in self._iter_partitions():
            ds = self._apply_selection(raw_ds, selection)
            q_vals = _compute_axis_values(ds, quantity, selection.data_column)
            datasets.append(q_vals)

        if not datasets:
            log.warning("query_raster: no partitions matched selection")
            n_rows, n_cols = shape
            return xr.DataArray(
                np.full((n_rows, n_cols), np.nan),
                attrs={"long_name": quantity.label},
            )

        # The caller (VisibilityRasterSource) runs Datashader on this
        # DataArray.  The bounds/shape parameterisation is handled there.
        return xr.concat(datasets, dim="time")
