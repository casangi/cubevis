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
crosses it, since registration is required for both directions --
though as of the hover-probe redesign piece 3 / Chunk 2c cleanup, only
one direction is actually exercised any more: ``query_raster()``'s
result flows worker -> P_local (worker encodes, P_local decodes).
``probe_raster_pixel(raw_grid: xr.DataArray, ...)`` used to be the
P_local -> worker direction, but that method (along with
``probe_scatter_pixel(..., scatter_df: pd.DataFrame)`` below) was
removed as dead code -- see ``XArrayReader``'s "Pixel hover probe"
section in ``data/reader.py`` for why. Both encoders/decoders stay
registered regardless (registration is unconditional, not tied to
which directions happen to be in use today), and ``pd.DataFrame``'s
registration below is consequently unused at the moment -- left in
place rather than removed, since it is harmless and a future method
may reasonably need it again. In practice: imported by
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
    # Temporary diagnostic (2026-09-06) -- same technique as the
    # DataArray investigation, this time for a genuinely different
    # hang: wire_types registration on the supervisor is confirmed
    # correct this time (checked directly), so this isn't a repeat of
    # that bug. Remove once root-caused.
    def _dbg(msg):
        with open("/tmp/cubevis_dataframe_debug.log", "a") as f:
            f.write(msg + "\n")
            f.flush()

    _dbg(f"START columns={list(obj.columns)!r} dtypes={dict(obj.dtypes.astype(str))!r} "
          f"index_dtype={obj.index.dtype!r} shape={obj.shape!r}")
    data = {}
    for col in obj.columns:
        _dbg(f"encoding column {col!r} (dtype={obj[col].dtype!r}) ...")
        data[str(col)] = serializer.encode(obj[col].to_numpy())
        _dbg(f"column {col!r} encoded OK")
    _dbg("encoding index ...")
    index = serializer.encode(obj.index.to_numpy())
    _dbg("DONE")
    return {
        "type": _DATAFRAME_TAG,
        "columns": list(obj.columns),
        "data": data,
        "index": index,
    }


def _decode_dataframe(obj: Dict[str, Any], deserializer: Deserializer) -> pd.DataFrame:
    def _dbg(msg):
        with open("/tmp/cubevis_dataframe_debug.log", "a") as f:
            f.write(msg + "\n")
            f.flush()

    _dbg(f"DECODE START columns={obj.get('columns')!r}")
    data = {}
    for col, val in obj["data"].items():
        _dbg(f"decoding column {col!r} ...")
        data[col] = deserializer._decode(val)
        _dbg(f"column {col!r} decoded OK")
    _dbg("decoding index ...")
    index = deserializer._decode(obj["index"])
    _dbg("DECODE DONE")
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
