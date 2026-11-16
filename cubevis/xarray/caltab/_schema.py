"""Every schema choice of the ``casa-caltab`` backend, in one place.

xradio's calibration schema is a draft: PR 578, branch
``545-calset-schema``, whose code was last changed on 2026-08-06 (commit
``f871ae3``) and whose discussion has gone further (to 2026-11-09).  The
code defines one dataset, ``FringeJonesXds``.  This backend follows the
code, then the discussion where that is settled, and xradio's shared
conventions (its Conventions page, PR 690).  When the draft moves, this
module is what changes; the reader (``_reader``) and the builder
(``_build``) take their names, dimensions and units from here.

From the draft's code:
  * the dimensions ``direction, time, antenna_name, frequency,
    receptor_label``, in that order;
  * ``CALPARAM_PHASE`` / ``_DELAY`` / ``_RATE`` as float64, in rad, s and
    dimensionless; ``CALERROR_<name>`` and ``SNR``;
  * the attributes ``schema_version``, ``creator``, ``creation_date`` and
    ``type_version``;
  * from its commented-out baseline dataset: ``field_name(time)``,
    ``scan_name(time)``, ``type`` and ``reference_antenna``.

From the discussion on the PR:
  * ``receptor_label`` is positional (``pol_0``, ``pol_1``, as xradio's
    converters write for MSv4), and what each receptor is goes in
    ``polarization_type(antenna_name, receptor_label)``, as in MSv4's
    antenna dataset (meeting of 2026-10-28);
  * a physical parameter is an array of its own, with its own units.

From xradio's conventions:
  * ``time`` and ``frequency`` increase (a window CASA stores in
    decreasing frequency is reversed, and says so);
  * units are from xradio's list, so a delay is in ``s``, where CASA
    stores ns;
  * data variables are UPPER_SNAKE, coordinates lower_snake; a flag that
    is True means invalid;
  * ``frequency`` carries ``type``, ``long_name``, ``units`` and
    ``channel_width`` (a quantity) as attributes, as MSv4's does and as
    PR 690's shared ``FrequencyCoordArrayBase`` has them.  A table with
    one channel per window (G, K, a fringe fit) gives there the width of
    the band it was solved over, which is what evaluating a delay across
    it needs.

This backend's own (``DEVIATIONS`` says the same to a reader of the data):
  * one node per spectral window (nothing says yet how a calibration set
    is arranged);
  * ``CALPARAM_GAIN``, ``CALPARAM_TSYS`` and ``CALPARAM_DISP_DELAY``, and
    a ``FLAG`` variable beside every dataset's parameters;
  * the receptor label ``all`` for a solution that is for every
    polarization (T Jones);
  * ``CALPARAM`` over a ``parameter`` dimension for a type not listed
    below;
  * ``SOURCE_ROW`` and ``INTERVAL``.

Known to be behind the discussion, and waiting for it to settle:
  * gains (G, B, T, Accor) have no ``parameter`` axis after the receptor
    (George Moellenbrock's text of 2026-11-09: 1 for a diagonal gain, 2
    for a full Jones);
  * the gain curve is read as an unknown type, not as receptor by
    polynomial coefficient;
  * the dispersive delay, and an unknown type's parameters, have no units.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

SCHEMA_DRAFT = ("xradio PR 578 (545-calset-schema): code at f871ae3 (2026-08-06), "
                "discussion to 2026-11-09; conventions of PR 690")
SCHEMA_VERSION = "0.2.0"          # of this backend's output; changes with it
TYPE_VERSION = "0.1.0"            # the draft's per-type version

DATASET_TYPE = "antenna_calibration"
TREE_TYPE = "calibration_set"
CREATOR = "cubevis.xarray.caltab"

DIRECTION = "direction"
TIME = "time"
ANTENNA = "antenna_name"
FREQUENCY = "frequency"
RECEPTOR = "receptor_label"
PARAMETER = "parameter"

#: a CASA cal table is solved for one direction per field and applied to
#: the whole field of view: one label, saying so
DIRECTION_LABEL = "direction_independent"

PARAM_PREFIX = "CALPARAM"
ERROR_PREFIX = "CALERROR"
SNR = "SNR"
FLAG = "FLAG"
CHANNEL_WIDTH = "channel_width"          # an attribute of frequency: a quantity, Hz
POLARIZATION_TYPE = "polarization_type"  # a coordinate over (antenna_name, receptor_label)
SOURCE_ROW = "SOURCE_ROW"
INTERVAL = "INTERVAL"
REFERENCE_ANTENNA = "REFERENCE_ANTENNA"

#: a node's attribute when its channels are in the opposite order to the table's
CHANNEL_ORDER = "casa_channel_order"
REVERSED = "reversed"

DEVIATIONS = (
    "one node per spectral window",
    "FLAG beside the parameters of every dataset",
    "CALPARAM_GAIN, CALPARAM_TSYS and CALPARAM_DISP_DELAY are not in the draft",
    "gains have no 'parameter' axis after the receptor (proposed in the PR's discussion)",
    "CALPARAM over a 'parameter' dimension for a type the backend does not know, without units",
    "receptor label 'all' for a solution that is for every polarization",
    "polarization_type is left out when the table does not say what its receptors are",
    "SOURCE_ROW (the table row of each time and antenna; -1: no solution) and INTERVAL",
    "casa_channel_order = 'reversed' on a window stored in decreasing frequency",
)


@dataclass(frozen=True)
class Param:
    """One named parameter of a calibration type."""
    name: str                      # CALPARAM_<name>
    long_name: str
    units: Optional[str]           # None: not established (left off the variable)
    scale: float = 1.0             # CASA's stored value x scale = the value in ``units``
    casa_units: Optional[str] = None   # what CASA stores, when not ``units``


@dataclass(frozen=True)
class CalType:
    """How one CASA calibration type is packed.

    A row's parameter cell is (npar, nchan).  For a known type
    ``npar = nreceptor * len(params)``, receptor-major: all of the first
    receptor's parameters, then the second's.
    """
    caltype: str                   # short name: the node's ``caltype`` attribute
    partype: str                   # "Complex" (CPARAM) or "Float" (FPARAM)
    params: tuple[Param, ...]
    one_for_all: bool = False      # its one receptor is a solution for every polarization


_GAIN = (Param("GAIN", "Complex gain", "dimensionless"),)
_DELAY = Param("DELAY", "Delay", "s", scale=1e-9, casa_units="ns")

#: keyed by ``table.info`` SubType (the ``VisCal`` keyword says the same)
CAL_TYPES: dict[str, CalType] = {
    "G Jones": CalType("G", "Complex", _GAIN),
    # gaintype="T": one gain for both polarizations, npar = 1
    "T Jones": CalType("T", "Complex", _GAIN, one_for_all=True),
    "B Jones": CalType("B", "Complex", _GAIN),
    "Accor Jones": CalType("Accor", "Complex", _GAIN),
    "K Jones": CalType("K", "Float", (_DELAY,)),
    "B TSYS": CalType("Tsys", "Float", (Param("TSYS", "System temperature", "K"),)),
    "Fringe Jones": CalType("Fringe", "Float", (
        Param("PHASE", "Phase offset", "rad"),
        _DELAY,
        Param("RATE", "Rate", "dimensionless"),            # s/s
        # CASA 6.7 does not solve it in these tables (all zero), and its
        # unit has not been checked against CASA's code: no units attribute
        Param("DISP_DELAY", "Dispersive delay", None),
    )),
}


def quantity(value: float, units: str) -> dict:
    """A quantity as xradio writes one in an attribute
    (``xradio._utils.dict_helpers.make_quantity``)."""
    return {"data": float(value), "dims": [], "attrs": {"units": units, "type": "quantity"}}


def quantity_value(q) -> Optional[float]:
    """The number of a quantity attribute, or None when it is not one."""
    try:
        return float(q["data"])
    except Exception:
        return None


def lookup(subtype: Optional[str], partype: Optional[str], npar: int) -> Optional[CalType]:
    """The packing of a table, or None for the generic form.

    None is also the answer when a table says it is a known type but does
    not have that type's layout: its parameters are then kept as they
    are, not guessed at.
    """
    ct = CAL_TYPES.get(subtype or "")
    if ct is None:
        return None
    if partype and partype != ct.partype:
        return None
    if npar <= 0 or npar % len(ct.params):
        return None
    return ct


#: the receptor label of a solution that is for every polarization (T Jones)
ALL_RECEPTORS = "all"


def receptor_labels(nreceptor: int, one_for_all: bool = False) -> list[str]:
    """The labels of the receptor axis: positional, as MSv4's antenna
    dataset has them (``pol_0``, ``pol_1``).  They say which receptor, not
    what it is; that is ``polarization_type``.  A type whose one receptor
    is for every polarization (*one_for_all*) is labelled ``all``."""
    if one_for_all and nreceptor == 1:
        return [ALL_RECEPTORS]
    return [f"pol_{i}" for i in range(nreceptor)]


def polarization_types(pol_basis: Optional[str], nreceptor: int,
                       override: Optional[Sequence[str]] = None,
                       one_for_all: bool = False) -> Optional[list[str]]:
    """What each receptor is (``["R", "L"]``, ``["X", "Y"]``), or None when
    nothing says.

    A cal table says it only in the ``PolBasis`` keyword, and CASA 6.7
    writes ``unknown`` there; *override* is for a caller who knows (from
    the MS).  A solution for every polarization has no type.
    """
    if one_for_all and nreceptor == 1:
        return None
    if override is not None:
        types = [str(v) for v in override]
        if len(types) != nreceptor:
            raise ValueError(f"polarization_type has {len(types)} values for {nreceptor} receptors")
        return types
    basis = (pol_basis or "").strip().lower()
    types = ["X", "Y"] if basis.startswith("lin") else ["R", "L"] if basis.startswith("circ") else []
    return types[:nreceptor] if len(types) >= nreceptor else None


def pol_basis_of(types: Optional[Sequence[str]]) -> str:
    """The ``PolBasis`` keyword for receptors of these types: what
    ``polarization_types`` would read back as the same."""
    types = [str(v) for v in (types if types is not None else [])]
    if types and types == ["X", "Y"][:len(types)]:
        return "linear"
    if types and types == ["R", "L"][:len(types)]:
        return "circular"
    return "unknown"
