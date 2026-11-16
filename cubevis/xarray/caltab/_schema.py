"""Every schema choice of the ``casa-caltab`` backend, in one place.

xradio's calibration schema is a draft (PR 578, branch
``545-calset-schema``, last changed 2026-08-06, commit ``f871ae3``).  It
defines one dataset, ``FringeJonesXds``; the rest of what is here is this
backend's choice in the draft's style.  When the draft moves, this module
is what changes; the reader (``_reader``) takes its names, dimensions and
units from here.

From the draft:
  * the dimensions ``direction, time, antenna_name, frequency,
    receptor_label``, in that order;
  * ``CALPARAM_PHASE`` / ``_DELAY`` / ``_RATE``, ``CALERROR_<name>`` and
    ``SNR`` for a fringe fit;
  * the coordinates ``field_name(time)`` and ``scan_name(time)``;
  * the attributes ``type`` (``"antenna_calibration"``) and
    ``reference_antenna``.

This backend's own (``DEVIATIONS`` says the same to a reader of the data):
  * one node per spectral window (the draft does not say how a calibration
    set is arranged);
  * ``CALPARAM_GAIN``, ``CALPARAM_TSYS`` and ``CALPARAM_DISP_DELAY``, and
    a ``FLAG`` variable beside every dataset's parameters;
  * the receptor label ``all`` for a solution that is for every
    polarization (T Jones);
  * CASA's units as stored (delay in ns; the draft has seconds);
  * ``CALPARAM`` over a ``parameter`` dimension for a type not listed
    below;
  * ``SOURCE_ROW`` and ``INTERVAL``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

SCHEMA_DRAFT = "xradio PR 578 (545-calset-schema) at f871ae3, 2026-08-06"
SCHEMA_VERSION = "0.1.0"          # of this backend's output; changes with it

DATASET_TYPE = "antenna_calibration"
TREE_TYPE = "calibration_set"

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
SOURCE_ROW = "SOURCE_ROW"
INTERVAL = "INTERVAL"
REFERENCE_ANTENNA = "REFERENCE_ANTENNA"

DEVIATIONS = (
    "one node per spectral window",
    "FLAG beside the parameters of every dataset",
    "CASA's units as stored (delay in ns, not s)",
    "CALPARAM_GAIN, CALPARAM_TSYS and CALPARAM_DISP_DELAY are not in the draft",
    "CALPARAM over a 'parameter' dimension for a type the backend does not know",
    "SOURCE_ROW (the table row of each time and antenna; -1: no solution) and INTERVAL",
)


@dataclass(frozen=True)
class Param:
    """One named parameter of a calibration type."""
    name: str                      # CALPARAM_<name>
    long_name: str
    units: Optional[str]           # None: not established (left off the variable)


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

#: keyed by ``table.info`` SubType (the ``VisCal`` keyword says the same)
CAL_TYPES: dict[str, CalType] = {
    "G Jones": CalType("G", "Complex", _GAIN),
    # gaintype="T": one gain for both polarizations, npar = 1
    "T Jones": CalType("T", "Complex", _GAIN, one_for_all=True),
    "B Jones": CalType("B", "Complex", _GAIN),
    "Accor Jones": CalType("Accor", "Complex", _GAIN),
    "K Jones": CalType("K", "Float", (Param("DELAY", "Delay", "ns"),)),
    "B TSYS": CalType("Tsys", "Float", (Param("TSYS", "System temperature", "K"),)),
    "Fringe Jones": CalType("Fringe", "Float", (
        Param("PHASE", "Phase offset", "rad"),
        Param("DELAY", "Delay", "ns"),
        Param("RATE", "Rate", "dimensionless"),            # s/s
        # CASA 6.7 does not solve it in these tables (all zero), and its
        # unit has not been checked against CASA's code: no units attribute
        Param("DISP_DELAY", "Dispersive delay", None),
    )),
}


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


def pol_basis_of(labels) -> str:
    """The ``PolBasis`` keyword for a receptor axis: what ``receptor_labels``
    would read back as the same labels."""
    labels = [str(v) for v in labels]
    if labels and labels == ["X", "Y"][:len(labels)]:
        return "linear"
    if labels and labels == ["R", "L"][:len(labels)]:
        return "circular"
    return "unknown"


def receptor_labels(pol_basis: Optional[str], nreceptor: int,
                    override: Optional[list[str]] = None, one_for_all: bool = False) -> list[str]:
    """Labels for the receptor axis.

    A cal table says what its receptors are only in the ``PolBasis``
    keyword, and CASA 6.7 writes ``unknown`` there.  ``pol_0``, ``pol_1``
    are MSv4's receptor labels and claim nothing; *override* is for a
    caller who knows.  A type whose one receptor is for every
    polarization (*one_for_all*) is labelled ``all``.
    """
    if override is not None:
        labels = [str(v) for v in override]
        if len(labels) != nreceptor:
            raise ValueError(f"receptor_labels has {len(labels)} labels for {nreceptor} receptors")
        return labels
    if one_for_all and nreceptor == 1:
        return [ALL_RECEPTORS]
    basis = (pol_basis or "").strip().lower()
    if basis.startswith("lin"):
        labels = ["X", "Y"]
    elif basis.startswith("circ"):
        labels = ["R", "L"]
    else:
        labels = []
    if len(labels) >= nreceptor:
        return labels[:nreceptor]
    return [f"pol_{i}" for i in range(nreceptor)]
