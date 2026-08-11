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

from ..axes import Axis, AxisType
from ..selection import SelectionSpec

log = logging.getLogger(__name__)


# ======================================================================
# Probe geometry helpers
# ======================================================================
#
# Shared by MSv2Backend and MSv4Backend so the two probe implementations
# cannot drift apart again.  Both previously carried copy-pasted cell
# geometry with the same defects; see the 2026-08 probe-miss notes in
# VisibilityScatter._handle_probe.

def _cell_bounds(coords: np.ndarray, idx: int) -> tuple[float, float]:
    """Data-space ``(lo, hi)`` bounds of cell *idx* in *coords*.

    Uses **local** neighbour spacing rather than a global
    ``(c[-1] - c[0]) / (N - 1)`` average.

    The global-average form is exact for a Datashader canvas agg, whose
    bins are uniform by construction, but it is wrong for a raw MS
    coordinate axis, which routinely is not:

    * ``time`` has large gaps between scans — an average half-width
      derived across those gaps makes every cell's window many times
      wider than the actual integration spacing, so the field/scan/
      antenna metadata lookup in ``probe_raster_pixel`` sweeps in rows
      belonging to *neighbouring scans* and reports them as though they
      were under the cursor.
    * ``frequency`` is non-uniform across concatenated spectral windows
      for the same reason.

    Local spacing degrades gracefully: on a uniform axis it reproduces
    the global answer exactly, and on a gapped axis it keeps each cell's
    window tied to its own neighbours.  Handles descending coordinate
    arrays and the degenerate single-element case.
    """
    n = len(coords)
    if n == 0:
        raise IndexError("empty coordinate array")
    if not (0 <= idx < n):
        raise IndexError(f"index {idx} out of range for {n} coordinates")

    centre = float(coords[idx])
    if n == 1:
        return centre, centre

    if idx > 0:
        half_lo = abs(centre - float(coords[idx - 1])) / 2.0
    else:
        half_lo = abs(float(coords[1]) - centre) / 2.0
    if idx < n - 1:
        half_hi = abs(float(coords[idx + 1]) - centre) / 2.0
    else:
        half_hi = abs(centre - float(coords[n - 2])) / 2.0

    return centre - half_lo, centre + half_hi


def _widen_if_degenerate(
    bounds: tuple[float, float], coords: np.ndarray
) -> tuple[float, float]:
    """Give a zero-width bin window a usable width.

    ``_cell_bounds`` returns ``(c, c)`` for a single-element coordinate
    axis, which the adaptive scatter canvas can produce at extreme zoom
    on sparse data.  A closed test against a zero-width window requires
    exact float equality, so the sample count comes back 0 even when the
    bin plainly contains points.

    The pad is sized to absorb float32 round-trip error rather than to
    guess a bin width: MS columns are frequently float32 while the agg
    coordinates are float64, so a sample and its bin centre can differ
    in the seventh significant figure while being the same number.  It
    deliberately does *not* widen far enough to capture genuinely
    different values — with a single-bin canvas there is no bin width to
    recover, and ``_compute_canvas_size`` clamps the canvas to at least
    10x10 anyway, so this is a guard against an unreachable state rather
    than a routine code path.
    """
    lo, hi = bounds
    if hi > lo:
        return bounds
    if coords.size > 1:
        pad = abs(float(coords[-1]) - float(coords[0])) / 2.0
    else:
        pad = abs(lo) * 1e-6 or 1e-9
    return lo - pad, hi + pad


def _bin_membership(
    series: "pd.Series",
    bounds: tuple[float, float],
    idx: int,
    n_bins: int,
):
    """Half-open ``[lo, hi)`` membership, closed on the final bin.

    Matches Datashader's own binning rule
    (``floor((v - v0) / (v1 - v0) * N)``).  The previous
    closed-on-both-ends test double-counted samples lying exactly on the
    edge shared by two adjacent bins.
    """
    lo, hi = bounds
    if idx >= n_bins - 1:
        return (series >= lo) & (series <= hi)
    return (series >= lo) & (series < hi)


def _agg_value(values: np.ndarray, iy: int, ix: int) -> Optional[float]:
    """Value at ``values[iy, ix]``, or ``None`` when that bin is empty.

    Empty-bin sentinels differ by reduction: ``mean``/``max``/``min``
    leave NaN, but ``count`` leaves integer ``0`` and ``any`` leaves
    ``False``.  A bare ``np.isnan`` test therefore reports every bin of
    an integer agg as populated, which would make a switch of reduction
    silently break the "is there data here?" readout.
    """
    raw = values[iy, ix]
    if np.issubdtype(values.dtype, np.floating):
        return None if np.isnan(raw) else float(raw)
    if np.issubdtype(values.dtype, np.bool_):
        return 1.0 if bool(raw) else None
    return None if raw == 0 else float(raw)


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
        ``field_ids`` : list[Optional[int]], optional
            Real MS/PS FIELD_ID for each entry in ``field_names``,
            aligned by position. Concrete backends SHOULD populate this
            from an authoritative source (e.g. the MS's ``FIELD``
            subtable row order for ``MSv2Backend``) rather than
            omitting it -- without it, ``ObservationMetadata`` falls
            back to a bare positional index, which silently gives wrong
            results whenever the real FIELD_IDs are non-contiguous
            (confirmed on a real MS: alphabetically-sorted field names
            do not line up with FIELD_ID order). Omit this key entirely
            (rather than returning wrong values) if no authoritative
            source is available yet.
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
        y_dim: Axis,
        x_dim: Axis,
        quantity: Axis,
        selection: SelectionSpec,
        polarization: Optional[str] = None,
        max_cells: int = 2_000_000,
    ) -> tuple[xr.DataArray, tuple[float, float], tuple[float, float], bool]:
        """Return a computed 2D DataArray suitable for ``Canvas.raster()``.

        The backend reduces the selected data to a 2D float64 array by
        averaging over all dimensions not in ``(y_dim, x_dim)``, then
        decimates the result to at most ``max_cells`` cells by applying a
        uniform stride in each dimension.  The caller (``VisibilityRaster``)
        is responsible for Datashader resampling, colour mapping, and RGBA
        conversion.

        Two-level rendering contract
        ----------------------------
        The ``is_decimated`` return value drives ``VisibilityRaster``'s
        pan/zoom strategy:

        * ``is_decimated=False`` — the agg contains every data point that
          matched the selection.  Datashader resamples it for all zoom
          levels; no backend re-query is ever needed on zoom-in.
        * ``is_decimated=True`` — the agg was strided to fit within
          ``max_cells``.  Detail exists in the MS that is not in the agg.
          When the viewer zooms in past one agg cell (viewport pixel size <
          agg cell size in data units), ``VisibilityRaster`` should call
          ``query_raster`` again with a tightened ``SelectionSpec`` and a
          higher ``max_cells`` to fetch the sub-window at full resolution.

        Separating the data reduction (backend) from the canvas sizing and
        colour mapping (``VisibilityRaster``) keeps the backend free of
        canvas-size knowledge and lets the caller re-colour without
        re-reading data.

        Parameters
        ----------
        y_dim :
            Native axis for the y (row) dimension.  Must be one of
            ``Axis.TIME``, ``Axis.BASELINE``, ``Axis.FREQUENCY``,
            ``Axis.CHANNEL``.
        x_dim :
            Native axis for the x (column) dimension — same vocabulary.
        quantity :
            Derived axis to render as colour.  Must be one of
            ``Axis.AMPLITUDE``, ``Axis.PHASE``, ``Axis.REAL``,
            ``Axis.IMAGINARY``, ``Axis.FLAG``.
        selection :
            Data selection constraints (field, scan, SPW, time range,
            baselines, channel range, …).
        polarization :
            Polarization product label (e.g. ``"XX"``).  Required for
            visibility-derived quantities; ignored for ``Axis.FLAG``.
            If ``None`` and required, the backend logs a warning and
            uses the first available correlation.
        max_cells : int
            Maximum number of cells (rows × columns) in the returned agg.
            Defaults to 2,000,000 (≈16 MB at float64), which comfortably
            covers a 1000×600 canvas with ~3× oversampling.  For very
            large MSes the backend strides the reduced 2D grid to fit
            within this budget before calling ``.compute()``, so that
            only the strided rows/columns are read from disk via Dask.

        Returns
        -------
        agg : xr.DataArray
            Computed (not lazy) 2D float64 DataArray with named
            coordinates on both dimensions.  Shape is at most
            ``(n_y_cells, n_x_cells)`` where ``n_y_cells * n_x_cells
            <= max_cells``.  Passed directly to
            ``datashader.Canvas.raster()``.
        x_range : tuple[float, float]
            ``(x_min, x_max)`` — the full extent of the x coordinate in
            the *original unreduced* data (not the strided agg).  Used to
            set the Bokeh figure x_range so the axis reflects real data
            bounds even when the agg is decimated.
        y_range : tuple[float, float]
            ``(y_min, y_max)`` — the full extent of the y coordinate.
        is_decimated : bool
            ``True`` if a stride > 1 was applied in either dimension,
            meaning the agg does not contain every data point.  ``False``
            when the full reduced grid fit within ``max_cells``.
        """

    # ------------------------------------------------------------------ #
    # Pixel hover probe                                                    #
    # ------------------------------------------------------------------ #

    @abc.abstractmethod
    def probe_raster_pixel(
        self,
        raw_grid: xr.DataArray,
        gx: int,
        gy: int,
        selection: SelectionSpec,
    ) -> dict:
        """Return metadata for a raster pixel at raw grid indices (gx, gy).

        Operates on the **raw backend grid** returned by ``query_raster()``
        — the 2D float64 DataArray with named MS dimension coordinates
        (e.g. ``"time"``, ``"baseline_id"``, ``"frequency"``).  It does
        NOT accept a Datashader canvas agg with generic ``"x"``/``"y"``
        dims.

        ``VisibilityRaster`` converts the hover data-space ``{x, y}``
        coordinates to raw grid indices via ``_data_to_pixel()`` before
        calling this method.

        Index semantics (important for flagging)
        -----------------------------------------
        ``(gx, gy)`` are **raw grid indices**, not canvas pixel indices.
        The raw grid has shape ``(n_y_cells, n_x_cells)`` from the data,
        which differs from the canvas shape ``(PLOT_H, PLOT_W)``.
        ``VisibilityRaster._data_to_pixel()`` performs the conversion from
        hover data-space ``{x, y}`` to ``(gx, gy)`` via argmin on the
        grid's named coordinate arrays.

        Flagging contract
        -----------------
        The returned ``"x_range"`` and ``"y_range"`` tuples contain the
        data-space extents of the cell in native MS coordinate units
        (MJD seconds for TIME, integer ID for BASELINE_ID, Hz for
        FREQUENCY).  These can be used directly to construct a
        ``SelectionSpec`` for a flag operation without any further
        coordinate conversion.  Flagging should only be enabled when
        ``VisibilityRaster._is_decimated`` is ``False`` — when the agg
        was decimated, each cell covers multiple native data points and
        the cell range is ambiguous.

        Layer 1 — value from raw grid
            ``raw_grid.values[gy, gx]`` gives the pre-computed aggregated
            quantity.  Named coordinate arrays provide the data-space range
            for the cell.

        Layer 2 — metadata from partition coordinate lookup
            Field names, scan names, antenna pairs, and frequency in GHz
            are retrieved by scanning partition coordinate arrays within the
            cell's data-space range.  No VISIBILITY read occurs.

        Parameters
        ----------
        raw_grid :
            The 2D float64 DataArray returned by the most recent
            ``query_raster()`` call.  Must have named MS coordinate arrays
            on both dimensions (e.g. ``dims=("time", "baseline_id")``).
        gx, gy :
            Zero-based indices into ``raw_grid``.  ``gy`` indexes rows
            (y-axis, dim 0); ``gx`` indexes columns (x-axis, dim 1).
            Derived from hover ``{x, y}`` via ``_data_to_pixel()``.
        selection :
            The ``SelectionSpec`` active when ``raw_grid`` was produced.

        Returns
        -------
        dict with keys:

        ``"value"`` : float or None
            Aggregated quantity at this cell.  ``None`` if NaN.
        ``"x_range"`` : tuple[float, float]
            Data-space (min, max) of the x-axis for this cell.
            Use directly in ``SelectionSpec`` for flagging.
        ``"y_range"`` : tuple[float, float]
            Data-space (min, max) of the y-axis for this cell.
        ``"x_centre"`` : float
            Data-space centre on the x-axis.
        ``"y_centre"`` : float
            Data-space centre on the y-axis.
        ``"field_names"`` : list[str]
        ``"scan_names"`` : list[str]
        ``"antenna_pairs"`` : list[tuple[str, str]]
        ``"freq_range_ghz"`` : tuple[float, float] or None
        """

    @abc.abstractmethod
    def probe_scatter_pixel(
        self,
        canvas_agg: xr.DataArray,
        px: int,
        py: int,
        selection: SelectionSpec,
        scatter_df: "pd.DataFrame",
    ) -> dict:
        """Return metadata for a scatter pixel at canvas indices (px, py).

        Operates on the **Datashader canvas agg** returned by
        ``cvs.points()``, which has generic ``"x"``/``"y"`` dimensions
        and canvas-resolution coordinates.  Canvas pixel indices are valid
        directly against this array without any coordinate conversion.

        Index semantics (contrast with probe_raster_pixel)
        ---------------------------------------------------
        ``(px, py)`` are **canvas pixel indices** in the range
        ``[0, PLOT_W)`` × ``[0, PLOT_H)``, not raw data grid indices.
        This is the opposite convention from ``probe_raster_pixel`` which
        takes raw grid indices ``(gx, gy)``.  The canvas agg from
        ``cvs.points()`` has shape ``(PLOT_H, PLOT_W)`` so canvas indices
        are valid directly.

        Flagging note
        -------------
        Scatter probe is less directly usable for flagging than raster
        probe because the ``canvas_agg`` aggregates multiple data points
        into each pixel bin — ``n_scatter_samples`` tells you how many,
        but not which specific MS rows they correspond to.  For flagging
        individual visibility samples, zoom to a resolution where each
        canvas pixel contains approximately one sample (adaptive canvas
        is active), then use the ``scatter_df`` boolean index with the
        returned ``x_range``/``y_range`` to identify the rows.

        Parameters
        ----------
        canvas_agg :
            Float64 Datashader aggregation DataArray from ``cvs.points()``,
            shape ``(PLOT_H, PLOT_W)`` or smaller (adaptive canvas),
            dims ``("y", "x")``.
        px, py :
            Zero-based canvas pixel indices.  ``py`` indexes rows (y-axis,
            dim 0); ``px`` indexes columns (x-axis, dim 1).
        selection :
            The ``SelectionSpec`` active when ``scatter_df`` was produced.
        scatter_df :
            Flat DataFrame from ``query_columns()`` (columns ``"x"``,
            ``"y"``).  Rows within the pixel bin are found by a boolean
            index — no MS re-read.

        Returns
        -------
        dict with keys:

        ``"value"`` : float or None
            Aggregated quantity at this pixel.  ``None`` if NaN.
        ``"x_range"`` : tuple[float, float]
        ``"y_range"`` : tuple[float, float]
        ``"x_centre"`` : float
        ``"y_centre"`` : float
        ``"n_scatter_samples"`` : int
            Number of scatter samples in this pixel bin.
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
