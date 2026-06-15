"""Axis and AxisType enumerations for visibility data plotting.

This module defines the complete axis vocabulary used throughout the
visibility plotter.  Every layer — ``VisibilityPlotter``, the source
classes, ``XArrayReader``, and ``FlagDB`` — refers to ``Axis`` members
rather than to bare strings.

``AxisType`` classifies each axis for three independent purposes:

* **Mode selection** — ``Axis.triggers_raster`` decides whether
  ``VisibilityPlotter`` enters raster or scatter/line mode.
* **Flagging validity** — ``Axis.is_native`` determines whether a flag
  operation can be expressed as a coordinate-range specification.
* **Reader dispatch** — ``XArrayReader`` uses ``axis_type`` to choose
  the correct xarray operation for each requested axis.

References
----------
msvis_design.md §2.1, §4.2, Appendix A
"""

from enum import Enum, auto


class AxisType(Enum):
    """Classification of a plot axis.

    Attributes
    ----------
    NATIVE_CONTINUOUS
        Axis that exists as a real coordinate or data variable in the
        MSv4 DataTree and is continuous-valued.  Does **not** trigger
        raster mode.  Examples: ``TIME``, ``FREQUENCY``, ``U``, ``V``.
    NATIVE_DISCRETE
        Axis that exists as a real coordinate or data variable in the
        MSv4 DataTree and is discrete-valued.  **Does** trigger raster
        mode when used as a plot axis.  Note that ``SCAN`` and ``FIELD``
        are ``NATIVE_DISCRETE`` for mode-selection purposes but are
        internally implemented via ``ds.where(ds.scan_name.isin(...))``
        rather than ``.sel()`` indexing.
    DERIVED
        Axis computed from data variables; not directly accessible as a
        DataTree coordinate.  Display-only for flagging (``is_native``
        returns ``False``).  Examples: ``AMPLITUDE``, ``PHASE``.
    CALIBRATION
        Valid only when the data source is a calibration table.
        Examples: ``GAIN_AMPLITUDE``, ``TSYS``.
    """

    NATIVE_CONTINUOUS = auto()
    NATIVE_DISCRETE = auto()
    DERIVED = auto()
    CALIBRATION = auto()


class Axis(Enum):
    """All supported plot axes.

    Each member carries a human-readable ``label``, a ``unit`` string
    for axis tick formatting, and an ``AxisType`` classification.

    Usage — mode selection in ``VisibilityPlotter``::

        mode = ('raster'
                if xaxis.triggers_raster or yaxis.triggers_raster
                else 'scatter')

    Usage — flagging validity::

        can_flag = xaxis.is_native or yaxis.is_native

    Usage — reader dispatch (inside ``XArrayReader``)::

        if axis.axis_type is AxisType.DERIVED:
            # compute from data variable, not from a coordinate
            ...
    """

    # ------------------------------------------------------------------ #
    # Native continuous                                                    #
    # ------------------------------------------------------------------ #

    TIME = ("Time", "s", AxisType.NATIVE_CONTINUOUS)
    """Integration mid-point time, MJD seconds."""

    FREQUENCY = ("Frequency", "Hz", AxisType.NATIVE_CONTINUOUS)
    """Channel centre frequency in Hz."""

    CHANNEL = ("Channel", "", AxisType.NATIVE_CONTINUOUS)
    """Integer channel index within an SPW."""

    VELOCITY = ("Velocity", "m/s", AxisType.NATIVE_CONTINUOUS)
    """Radio velocity derived from frequency and rest frequency."""

    UVDIST = ("UV Distance", "m", AxisType.NATIVE_CONTINUOUS)
    """Projected baseline length in metres, sqrt(u²+v²).

    Classified ``NATIVE_CONTINUOUS`` because u, v, w are stored
    directly in the MSv4 DataTree as the ``UVW`` data variable; the
    distance in metres is a simple computation from native data.
    Compare ``UVDIST_LAMBDA``, which requires dividing by a
    frequency-derived wavelength and is therefore ``DERIVED``.
    """

    U = ("U", "m", AxisType.NATIVE_CONTINUOUS)
    V = ("V", "m", AxisType.NATIVE_CONTINUOUS)
    W = ("W", "m", AxisType.NATIVE_CONTINUOUS)

    INTERVAL = ("Interval", "s", AxisType.NATIVE_CONTINUOUS)
    """Integration interval in seconds."""

    ROW = ("Row", "", AxisType.NATIVE_CONTINUOUS)
    """MSv2 row index (not meaningful for MSv4 Zarr)."""

    # ------------------------------------------------------------------ #
    # Native discrete                                                      #
    # ------------------------------------------------------------------ #

    BASELINE = ("Baseline", "", AxisType.NATIVE_DISCRETE)
    """Antenna-pair baseline, identified by (ant1_name, ant2_name)."""

    ANTENNA1 = ("Antenna 1", "", AxisType.NATIVE_DISCRETE)
    ANTENNA2 = ("Antenna 2", "", AxisType.NATIVE_DISCRETE)

    CORRELATION = ("Correlation", "", AxisType.NATIVE_DISCRETE)
    """Polarization product label, e.g. 'XX', 'YY', 'RR', 'LL'."""

    SCAN = ("Scan", "", AxisType.NATIVE_DISCRETE)
    """Scan name string.

    Classified ``NATIVE_DISCRETE`` for mode-selection purposes.
    Internally implemented in ``XArrayReader`` via
    ``ds.where(ds.scan_name.isin(selection.scan))`` — not via
    ``.sel()`` — because ``scan_name`` is a non-index coordinate on
    the time dimension in the MSv4 schema.
    """

    FIELD = ("Field", "", AxisType.NATIVE_DISCRETE)
    """Field name string; analogous to ``SCAN`` in the DataTree model."""

    SPW = ("SPW", "", AxisType.NATIVE_DISCRETE)
    """Spectral window index."""

    OBSERVATION = ("Observation", "", AxisType.NATIVE_DISCRETE)
    INTENT = ("Intent", "", AxisType.NATIVE_DISCRETE)

    # ------------------------------------------------------------------ #
    # Derived                                                              #
    # ------------------------------------------------------------------ #

    AMPLITUDE = ("Amplitude", "", AxisType.DERIVED)
    """abs(VISIBILITY) — computed from the complex visibility column."""

    PHASE = ("Phase", "rad", AxisType.DERIVED)
    """angle(VISIBILITY) in radians."""

    REAL = ("Real", "", AxisType.DERIVED)
    """Real part of the complex visibility."""

    IMAGINARY = ("Imaginary", "", AxisType.DERIVED)
    """Imaginary part of the complex visibility."""

    WEIGHT = ("Weight", "", AxisType.DERIVED)
    WEIGHT_SPECTRUM = ("Weight Spectrum", "", AxisType.DERIVED)

    FLAG = ("Flag", "", AxisType.DERIVED)
    """Boolean FLAG array — rendered via the three-colour overlay, not
    as a conventional plot axis."""

    UVDIST_LAMBDA = ("UV Distance", "λ", AxisType.DERIVED)
    """Projected baseline length in wavelengths.

    Requires dividing the metre-domain UV distance by the wavelength
    derived from frequency, making this a fully derived, display-only
    quantity that cannot contribute to a ``FlagOperation`` specification.
    """

    AZIMUTH = ("Azimuth", "deg", AxisType.DERIVED)
    ELEVATION = ("Elevation", "deg", AxisType.DERIVED)
    HOUR_ANGLE = ("Hour Angle", "h", AxisType.DERIVED)
    PARALLACTIC_ANGLE = ("Parallactic Angle", "deg", AxisType.DERIVED)

    # ------------------------------------------------------------------ #
    # Calibration table axes                                               #
    # ------------------------------------------------------------------ #

    GAIN_AMPLITUDE = ("Gain Amplitude", "", AxisType.CALIBRATION)
    GAIN_PHASE = ("Gain Phase", "rad", AxisType.CALIBRATION)
    DELAY = ("Delay", "s", AxisType.CALIBRATION)
    TSYS = ("Tsys", "K", AxisType.CALIBRATION)
    SNR = ("SNR", "", AxisType.CALIBRATION)
    OPACITY = ("Opacity", "", AxisType.CALIBRATION)

    # ------------------------------------------------------------------ #
    # Constructor and derived properties                                   #
    # ------------------------------------------------------------------ #

    def __init__(self, label: str, unit: str, axis_type: AxisType) -> None:
        self.label = label          # human-readable display label
        self.unit = unit            # unit string for axis tick labels
        self.axis_type = axis_type  # classification

    # ------------------------------------------------------------------

    @property
    def is_native(self) -> bool:
        """``True`` if this axis exists as a real coordinate or column in
        the MSv4 DataTree and can therefore contribute to a
        ``FlagOperation`` coordinate-range specification.
        """
        return self.axis_type in (
            AxisType.NATIVE_CONTINUOUS,
            AxisType.NATIVE_DISCRETE,
        )

    @property
    def is_derived(self) -> bool:
        """``True`` if this axis is computed from data variables and is
        display-only for flagging purposes.
        """
        return self.axis_type is AxisType.DERIVED

    @property
    def triggers_raster(self) -> bool:
        """``True`` if choosing this axis causes ``VisibilityPlotter`` to
        enter raster mode rather than scatter/line mode.
        """
        return self.axis_type is AxisType.NATIVE_DISCRETE

    # ------------------------------------------------------------------

    def __repr__(self) -> str:  # pragma: no cover
        return f"Axis.{self.name}"
