"""_wire_types.py
================
visplot-specific wire-type registrations for ``cubevis.utils.serialize``/
``deserialize``.

Deliberately **not** part of ``cubevis.utils._conversion`` — that module
owns generic, Python-language-level wire support (``Enum``, ``dataclass``)
that every consumer of ``serialize``/``deserialize`` might need,
regardless of domain. ``xr.DataArray`` is not that: it's a specific
third-party scientific-computing type needed by exactly one domain
(visplot) today, and the very next domain we've actually looked at
(iclean, via ``ImagePipe``/``casatools.image``) doesn't touch xarray at
all. Putting it in the shared module would mean every consumer of
``cubevis.utils.serialize`` — including code with nothing to do with
visplot or xarray — pulls in an `import xarray` and a chunk of
domain knowledge it never asked for, for the benefit of one caller.

This module registers against the same, already-shared
``bokeh.core.serialization.Serializer``/``Deserializer`` classes that
``cubevis.utils._conversion``'s ``CubevisSerializer``/
``CubevisDeserializer`` subclass, using the plain, public
``Serializer.register(type, encoder)`` / ``Deserializer.register(tag,
decoder)`` API — the same mechanism ``_conversion.py`` uses for its own
``"cubevis_enum"`` tag. ``Serializer._encode``'s dispatch checks
``self._encoders.get(type(obj))`` before any built-in case, including
before ``CubevisSerializer._encode_other``'s own `Enum` check ever
runs — so this composes with `_conversion.py`'s subclass with zero
coordination required in either direction, verified against a real
round trip, not just reasoned about.

Unlike `Enum` (a whole family of concrete subclasses, which is why
``_conversion.py`` overrides the ``_encode_other`` fallback hook rather
than registering one type at a time), `xr.DataArray` is exactly one
concrete type — a plain ``Serializer.register(xr.DataArray, ...)`` call
is the right tool here, not a subclass override.

Must be imported on **both** ends of the wire before any DataArray
crosses it, since registration is required for both directions:
``query_raster()``'s result flows worker -> P_local (worker encodes,
P_local decodes), while ``probe_raster_pixel(raw_grid: xr.DataArray,
...)`` flows P_local -> worker (P_local encodes, worker decodes). The
same applies to ``pd.DataFrame`` below: ``query_columns()``'s result
flows worker -> P_local, while ``probe_scatter_pixel(..., scatter_df:
pd.DataFrame)`` flows P_local -> worker. In practice: imported by
``remote_reduction_context.py`` (P_local side) and by
``remote_registrations.py`` (worker side) — see each file's own import
of this module. Also required by ``_supervisor.py``'s generic relay
process, which sits between the two and must be able to
``deserialize()`` every message passing through it — confirmed by
direct investigation (2026-09-05) that a message containing an
unregistered custom type tag kills that process's relay loop silently.
The supervisor gets this without importing this module directly: its
``create_context()`` call is configured with ``"wire_types":
["cubevis.toolbox.visplot._wire_types"]``, which it dynamically
imports itself, the same way it already does for ``register_function``
— see ``_supervisor.py``'s ``_handle_create_context`` and
``remote_reduction_context.py``'s own ``create_context()`` call.

``pd.DataFrame`` has the identical ``__array__()`` fallback problem as
`xr.DataArray` did, by the same reasoning — confirmed against a real
round trip (2026-09-06), fixed below the same way. Index dtype is not
preserved exactly (an int64 index comes back int32, a Bokeh
ndarray-encoding default) — confirmed harmless for label-based access
(``.loc[]``, boolean indexing, aggregation) but worth knowing about if
anything downstream ever does a strict index-dtype check.
"""

from __future__ import annotations

from typing import Any, Dict

import pandas as pd
import xarray as xr
from bokeh.core.serialization import Serializer, Deserializer

_DATAARRAY_TAG = "cubevis_dataarray"
_DATAFRAME_TAG = "cubevis_dataframe"


def _encode_dataarray(obj: xr.DataArray, serializer: Serializer) -> Dict[str, Any]:
    return {
        "type": _DATAARRAY_TAG,
        "data": serializer.encode(obj.values),
        "dims": list(obj.dims),
        "coords": {
            name: {
                "dims": list(coord.dims),
                "data": serializer.encode(coord.values),
            }
            for name, coord in obj.coords.items()
        },
        "attrs": serializer.encode(dict(obj.attrs)),
        "name": obj.name,
    }


def _decode_dataarray(obj: Dict[str, Any], deserializer: Deserializer) -> xr.DataArray:
    data = deserializer._decode(obj["data"])
    coords = {
        name: (c["dims"], deserializer._decode(c["data"]))
        for name, c in obj["coords"].items()
    }
    attrs = deserializer._decode(obj["attrs"])
    return xr.DataArray(data, dims=obj["dims"], coords=coords, attrs=attrs, name=obj["name"])


def _encode_dataframe(obj: pd.DataFrame, serializer: Serializer) -> Dict[str, Any]:
    return {
        "type": _DATAFRAME_TAG,
        "columns": list(obj.columns),
        "data": {str(col): serializer.encode(obj[col].to_numpy()) for col in obj.columns},
        "index": serializer.encode(obj.index.to_numpy()),
    }


def _decode_dataframe(obj: Dict[str, Any], deserializer: Deserializer) -> pd.DataFrame:
    data = {col: deserializer._decode(val) for col, val in obj["data"].items()}
    index = deserializer._decode(obj["index"])
    return pd.DataFrame(data, columns=obj["columns"], index=index)


def _register() -> None:
    """Idempotent -- Serializer.register()/Deserializer.register() both
    assert against double-registration, and this module may legitimately
    be imported from more than one place on the same side of the wire
    (e.g. both directly and transitively) within a single process."""
    if xr.DataArray not in Serializer._encoders:
        Serializer.register(xr.DataArray, _encode_dataarray)
    if _DATAARRAY_TAG not in Deserializer._decoders:
        Deserializer.register(_DATAARRAY_TAG, _decode_dataarray)
    if pd.DataFrame not in Serializer._encoders:
        Serializer.register(pd.DataFrame, _encode_dataframe)
    if _DATAFRAME_TAG not in Deserializer._decoders:
        Deserializer.register(_DATAFRAME_TAG, _decode_dataframe)


_register()
