########################################################################
#
# Copyright (C) 2021,2023
# Associated Universities, Inc. Washington DC, USA.
#
# This script is free software; you can redistribute it and/or modify it
# under the terms of the GNU Library General Public License as published by
# the Free Software Foundation; either version 2 of the License, or (at your
# option) any later version.
#
# This library is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or
# FITNESS FOR A PARTICULAR PURPOSE.  See the GNU Library General Public
# License for more details.
#
# You should have received a copy of the GNU Library General Public License
# along with this library; if not, write to the Free Software Foundation,
# Inc., 675 Massachusetts Ave, Cambridge, MA 02139, USA.
#
# Correspondence concerning AIPS++ should be adressed as follows:
#        Internet email: casa-feedback@nrao.edu.
#        Postal address: AIPS++ Project Office
#                        National Radio Astronomy Observatory
#                        520 Edgemont Road
#                        Charlottesville, VA 22903-2475 USA
#
########################################################################
'''Wire (de)serialization for cubevis.

CORRECTED (2026-09-08, second pass): confirmed against the actual
transport files (not just a stand-in test) that ``serialize()``/
``deserialize()`` genuinely ARE the functions
``cubevis/bokeh/transport/_low_level_transport.py`` calls for real
browser/JavaScript traffic -- both its ``WebSocketTransport``
(``await self.websocket.send(serialize(...))``) and its anywidget-based
``CommsTransport`` alternative call
``from ...utils import serialize, deserialize`` directly. An earlier
version of this docstring claimed the opposite, based on a test against
a plain, un-subclassed Bokeh Serializer used as a stand-in rather than
the real call path -- that stand-in was reasonable given what was
visible at the time, but wrong once the actual file was available to
check. The SAME two functions are also the entire
P_local<->supervisor<->worker remote-execution wire protocol
(``_worker_transport.py``, ``_kernel_transport.py``) -- so before this
pass, one pair of functions genuinely served both audiences at once,
which is exactly the design tension that caused the 2026-09-08 erratum
below.

Now genuinely split, not just relabelled:

* ``serialize``/``deserialize`` -- browser/JavaScript traffic. No
  tuple-specific handling: JavaScript has no tuple type at all, so a
  Python tuple degrading to a plain JSON array here is not a loss of
  anything the browser could have used. Enum and dataclass support
  (below) still apply -- those are Python-language concepts a
  browser-bound Enum value can still hit.
* ``remote_serialize``/``remote_deserialize`` -- the
  P_local<->supervisor<->worker wire protocol only. Adds exact tuple
  round-tripping (including tuple-as-dict-key) on top of the same
  Enum/dataclass support, safe here because both ends are Python.

See the 2026-09-08 erratum further down for the full incident this
split resolves.
'''

import importlib
import json
import sys
from enum import Enum

import numpy as np
from bokeh.util.serialization import transform_array
from bokeh.core.serialization import Serializer, Deserializer
from bokeh.core.json_encoder import serialize_json
from ._static import static_vars


def _resolve_class(module_name: str, qualname: str):
    """Resolve a class from its module path without acquiring Python's
    import lock in the common case.

    Used on SyncBridge's background thread when decoding a reply -- a
    genuine deadlock risk was observed in practice (2026-09-05): a
    concurrent import elsewhere in the process (e.g. IPython's own
    background completion/introspection activity) holding the import
    lock while this thread's importlib.import_module() call blocked on
    it, hanging the whole call until _DEFAULT_CALL_TIMEOUT gave up --
    confirmed absent when the identical call ran as a plain script with
    no such background activity. sys.modules.get() is a plain dict
    lookup with no locking at all, and covers the overwhelming majority
    of real cases: the class being decoded had to be imported by this
    same process already, to have been passed into the original call in
    the first place. import_module() is the correct fallback only for
    the rare case where the module genuinely isn't loaded yet -- that
    path still pays the same lock-acquisition cost the original code
    always did, just no longer unconditionally.
    """
    module = sys.modules.get(module_name)
    if module is None:
        module = importlib.import_module(module_name)
    return getattr(module, qualname)

# ----------------------------------------------------------------------
# Enum + dataclass wire support (2026-09-04)
#
# Two confirmed gaps in bokeh's stock Serializer/Deserializer (bokeh
# 3.10.0's actual bokeh/core/serialization.py, not assumed):
#
# 1. Serializer has NO Enum handling at all -- _encode falls through
#    every built-in case (bool/str/int/float/tuple/list/set/dict/bytes/
#    slice/ndarray/dataclass) to _encode_other, which only knows
#    datetime/numpy-scalar/pandas types, then raises
#    "can't serialize <enum '...'>". Fixed below by overriding
#    _encode_other in a subclass -- not via Serializer.register(), which
#    keys on the *exact* type(obj) and would need one call per concrete
#    Enum class as new ones are introduced; overriding the fallback
#    hook instead covers every Enum subclass generically, forever.
#
# 2. Serializer DOES encode arbitrary @dataclass instances natively
#    (is_dataclass()/_encode_dataclass() -- confirmed it just checks
#    hasattr(type(obj), "__dataclass_fields__"), so any plain stdlib
#    dataclass qualifies, no bokeh-specific decoration needed), but the
#    matching Deserializer._decode_object() is `raise
#    NotImplementedError()` in bokeh's own base class -- a stub clearly
#    meant to be overridden downstream, never implemented here. Fixed
#    below by overriding _decode_object in a subclass -- deliberately
#    NOT via Deserializer.register("object", ...), which would also
#    intercept bokeh's own Model-reference decoding (real Bokeh widgets
#    also use type="object", distinguished only by an "id" key) and
#    require reimplementing that path to avoid breaking it elsewhere in
#    cubevis. Overriding the one unimplemented method leaves bokeh's own
#    "object"-with-"id" (_decode_object_ref) path completely untouched.
#
# Both were caught by running a real query_raster() round trip through
# a live remote execution context (cubevis.remote) -- see
# cubevis-remote-execution-implementation.md / Chunk 2's own smoke test
# -- not found by inspection.
#
# 3. Tuples become lists on the way through (bokeh's _encode_tuple ->
#    _encode_list, no wire-level marker to reconstruct a tuple; JSON has
#    no tuple type). Originally noted here as "structural, not a bug"
#    when it only affected value equality (e.g. a (min, max) metadata
#    pair) -- confirmed genuinely load-bearing (2026-09-06) once a tuple
#    was used as a dict key downstream (MSv2Backend.query_columns()'s
#    `{key: [] for key in yaxes}`), where a list can never substitute
#    for a tuple no matter what, since lists aren't hashable at all.
#
#    ERRATUM (2026-09-08): first fixed via Serializer.register(tuple,
#    ...)/Deserializer.register("cubevis_tuple", ...) -- broke every
#    ordinary local, browser-displayed VisibilityPlotter session (JS
#    console: "Uncaught (in promise) Error: unable to decode an object
#    of type 'cubevis_tuple'"). Root cause: unlike the Enum fix above
#    (a _encode_other override, correctly scoped to CubevisSerializer
#    instances only -- see that fix's own comment), .register() mutates
#    a ClassVar dict SHARED BY EVERY Serializer/Deserializer instance in
#    the process, confirmed by this very file's own
#    Deserializer.register("cubevis_enum", ...) below (harmless there
#    only because nothing but CubevisSerializer's scoped _encode_other
#    override ever produces that tag for the shared decoder to see).
#    The tuple fix, registered on both the encode AND decode side, had
#    no such out: every Serializer in the process -- including the one
#    inside cubevis.utils.serialize() itself, which
#    _low_level_transport.py's WebSocketTransport/CommsTransport call
#    directly on real browser traffic -- started wrapping every tuple
#    (a hover-probe response's antenna_pairs/freq_range_ghz, always
#    plain tuples, for instance) in a {"type": "cubevis_tuple", ...}
#    envelope no browser has ever been taught to unwrap.
#
#    SECOND PASS, same day: the first re-fix moved tuple handling into
#    serialize()/deserialize() themselves (wrap before Bokeh's encoder,
#    unwrap after its decoder), reasoning that these two functions were
#    the entire P_local<->supervisor<->worker wire protocol and nothing
#    else -- checked against _worker_transport.py/_kernel_transport.py,
#    both true. What wasn't checked at that point (no visibility into
#    the file) was _low_level_transport.py, which turned out to call
#    these exact same two functions for real browser traffic too. So
#    the fix now applied identically to both: browser-bound tuples
#    stopped crashing the JS decoder, but started arriving as a
#    {"__cubevis_tuple__": true, "items": [...]} object instead of a
#    plain array -- no longer a hard crash, but a hover-probe response
#    containing antenna_pairs/freq_range_ghz would still reach the
#    browser as something no JS code was written to expect, quietly
#    wrong rather than loudly broken. Caught before it shipped, by
#    finally obtaining and reading _low_level_transport.py directly
#    rather than continuing to infer its behavior from Bokeh's public
#    Serializer as a stand-in.
#
#    Re-fixed a third time (2026-09-08) by actually splitting the two
#    audiences into two function pairs -- see this module's top-level
#    docstring. serialize()/deserialize() (browser) never wrap a tuple
#    at all now; remote_serialize()/remote_deserialize() (the
#    P_local<->supervisor<->worker protocol) keep the wrap/unwrap pass
#    described below, updated to work on their own dedicated names.
#    Neither pair uses Serializer.register()/Deserializer.register()
#    for tuples, so nothing here can leak into any OTHER
#    Serializer/Deserializer instance in the process regardless of
#    which pair a given caller uses.
#
#    One real limitation worth knowing rather than discovering later:
#    the wrap pass walks plain dict/list/tuple structures reachable
#    from serialize()'s top-level argument -- it does NOT reach inside
#    a @dataclass instance's fields, since dataclass encoding happens
#    natively inside Bokeh's own Serializer.encode(), after this outer
#    pass has already finished. A tuple-valued dataclass field (e.g.
#    ScatterRenderResult.layers, ScatterLayerSpec.cmap) still degrades
#    to a list on the wire. Confirmed harmless for everything currently
#    in the codebase -- every such field is only ever unpacked,
#    iterated, or explicitly re-listed (e.g. `list(lyr.cmap)`), never
#    used as a dict key or isinstance-checked -- but if a future
#    dataclass field genuinely needs exact tuple identity, it will need
#    its own explicit handling, not automatic coverage from this fix.
#
# Deliberately generic, and deliberately the LAST word on genericity.
# Enum/dataclass/tuple are Python-language concepts any consumer of
# serialize()/deserialize() might hit, so the fix belongs here, once,
# for everyone. A DOMAIN-specific type (xr.DataArray, pd.DataFrame, a
# future CASA-image wrapper for iclean, ...) does NOT belong here even
# though the mechanism looks similar -- see
# cubevis/toolbox/visplot/_wire_types.py for why, and for where
# visplot's own array-like wire types are actually registered from
# (Serializer.register()/Deserializer.register(), called from
# visplot's own code, not hardcoded into this shared module).
# ----------------------------------------------------------------------


class CubevisSerializer(Serializer):
    def _encode_other(self, obj):
        if isinstance(obj, Enum):
            cls = type(obj)
            return {
                "type": "cubevis_enum",
                "cls": f"{cls.__module__}.{cls.__qualname__}",
                "name": obj.name,
            }
        return super()._encode_other(obj)


class CubevisDeserializer(Deserializer):
    def _decode_object(self, obj):
        module_name, _, qualname = obj["name"].rpartition(".")
        cls = _resolve_class(module_name, qualname)
        attributes = obj.get("attributes", {})
        decoded = {key: self._decode(val) for key, val in attributes.items()}
        return cls(**decoded)


def _decode_cubevis_enum(obj, deserializer):
    module_name, _, qualname = obj["cls"].rpartition(".")
    cls = _resolve_class(module_name, qualname)
    return cls[obj["name"]]


# Deserializer._decoders is a ClassVar dict shared by every Deserializer
# (and subclass) instance in the process -- this registers "cubevis_enum"
# exactly once, at import time of this module, which Python only
# executes once per process regardless of how many times this module is
# imported elsewhere.
Deserializer.register("cubevis_enum", _decode_cubevis_enum)


# ----------------------------------------------------------------------
# Tuple wire fidelity for the remote protocol ONLY (2026-09-08, third
# pass) -- see the erratum above for the two prior attempts and why
# each fell short.
#
# Applied by remote_serialize()/remote_deserialize() only, entirely
# outside Bokeh's own encode/decode dispatch: _wrap_tuples runs BEFORE
# Bokeh's Serializer ever sees the value, replacing every tuple with a
# plain, JSON-safe sentinel dict; _unwrap_tuples runs AFTER Bokeh's
# Deserializer has finished, reversing the substitution. Bokeh's own
# serializer/deserializer therefore never has any tuple-specific
# behavior registered on it at all -- it only ever sees plain dicts,
# lists, and scalars, which it already handles correctly and always
# has. serialize()/deserialize() (browser-facing) never call either of
# these functions.
#
# A tuple used as a dict key needs its own branch: the sentinel dict
# _wrap_tuples produces for a tuple is itself unhashable, so it cannot
# simply replace a tuple appearing as a key the way it can replace one
# appearing as a value. When any key in a dict is a tuple, the whole
# dict is instead represented as an explicit list of [key, value]
# pairs (both independently wrapped) -- unlike a dict, a list places no
# hashability constraint on what it holds. The common case (no tuple
# keys) keeps the plain per-key pass-through, which is both cheaper and
# leaves Bokeh's own (already-correct) dict encoding for it undisturbed.
# ----------------------------------------------------------------------

_TUPLE_TAG = "__cubevis_tuple__"
_PAIRS_TAG = "__cubevis_dict_pairs__"


def _wrap_tuples(val):
    if isinstance(val, tuple):
        return {_TUPLE_TAG: True, "items": [_wrap_tuples(item) for item in val]}
    if isinstance(val, dict):
        if any(isinstance(k, tuple) for k in val.keys()):
            return {
                _PAIRS_TAG: True,
                "pairs": [[_wrap_tuples(k), _wrap_tuples(v)]
                          for k, v in val.items()],
            }
        return {k: _wrap_tuples(v) for k, v in val.items()}
    if isinstance(val, list):
        return [_wrap_tuples(item) for item in val]
    return val


def _unwrap_tuples(val):
    if isinstance(val, dict):
        if val.get(_TUPLE_TAG) is True and "items" in val:
            return tuple(_unwrap_tuples(item) for item in val["items"])
        if val.get(_PAIRS_TAG) is True and "pairs" in val:
            return {
                _unwrap_tuples(k): _unwrap_tuples(v)
                for k, v in val["pairs"]
            }
        return {k: _unwrap_tuples(v) for k, v in val.items()}
    if isinstance(val, list):
        return [_unwrap_tuples(item) for item in val]
    return val


def strip_arrays( val ):
    '''convert all numpy arrays contained within val to lists
    '''
    if isinstance( val, dict ):
        result = { }
        for k, v in val.items( ):
            result[k] = strip_arrays(v)
        return result
    if isinstance( val, np.ndarray ):
        return val.tolist( )
    if isinstance( val, range ):
        return list(val)
    return val

@static_vars( encoder=CubevisSerializer(deferred=False) )
def serialize( val ):
    '''Encode a Python value for browser/JavaScript traffic.

    Used directly by cubevis/bokeh/transport/_low_level_transport.py's
    WebSocketTransport and CommsTransport, for every message that
    actually reaches a browser tab. No tuple-specific handling: a tuple
    degrades to a plain JSON array, exactly Bokeh's own stock behavior,
    which loses nothing a browser could have used in the first place --
    JavaScript has no tuple type. Enum and dataclass support (see this
    module's Enum + dataclass wire support section, above) still apply.

    For the P_local<->supervisor<->worker remote-execution wire
    protocol, use remote_serialize() instead -- see that function's
    docstring for why the two are no longer the same function.
    '''
    return serialize_json(serialize.encoder.serialize(val))

@static_vars( decoder=CubevisDeserializer( ) )
def deserialize( val ):
    '''Decode a value received from browser/JavaScript traffic.

    See serialize()'s docstring -- same scope note applies here. For
    the remote-execution wire protocol, use remote_deserialize().
    '''
    value = json.loads(val)
    return deserialize.decoder.deserialize(value)


@static_vars( encoder=CubevisSerializer(deferred=False) )
def remote_serialize( val ):
    '''Encode a Python value for the cubevis.remote wire protocol.

    Used directly by _worker_transport.py's and _kernel_transport.py's
    wire protocols -- the entire P_local<->supervisor<->worker
    remote-execution stack, Python at both ends. Unlike serialize()
    (browser-facing), this preserves exact tuple identity end to end,
    including a tuple used as a dict key -- see _wrap_tuples/
    _unwrap_tuples and the 2026-09-08 erratum above for why this needs
    to be a genuinely different function rather than a shared one with
    a flag, and why it's implemented as an explicit pre/post-processing
    pass rather than a Bokeh Serializer.register() call.
    '''
    return serialize_json(remote_serialize.encoder.serialize(_wrap_tuples(val)))

@static_vars( decoder=CubevisDeserializer( ) )
def remote_deserialize( val ):
    '''Decode a value received over the cubevis.remote wire protocol.

    See remote_serialize()'s docstring -- same scope note applies here.
    '''
    value = json.loads(val)
    return _unwrap_tuples(remote_deserialize.decoder.deserialize(value))

def pack_arrays( val ):
    """Convert `numpy` N dimensional arrays stored within a dictionary to
    a format that can be converted into the multi-dimensional arrays that
    are usable for Bokeh data.

    Parameters
    ----------
    val: value

    Returns
    -------
    value
        return value is identical to `val` parameter except that any
        N dimensional `numpy` arrays are converted to Bokeh compatible
        format
    """
    if isinstance( val, dict ):
        result = { }
        for k, v in val.items( ):
            result[k] = pack_arrays(v)
        return result
    if isinstance( val, np.ndarray ):
        if isinstance(val, np.ma.MaskedArray):
            return transform_array(val.filled(0))
        else:
            return transform_array(val)
    if isinstance( val, range ):
        return list(val)
    return val
