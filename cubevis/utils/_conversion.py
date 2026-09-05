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
'''Contains for conversion of data passed between Python and JavaScript
via websockets'''

import importlib
import json
from enum import Enum

import numpy as np
from bokeh.util.serialization import transform_array
from bokeh.core.serialization import Serializer, Deserializer
from bokeh.core.json_encoder import serialize_json
from ._static import static_vars

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
# -- not found by inspection. Tuples become lists on the way through
# (bokeh's _encode_tuple -> _encode_list, no wire-level marker to
# reconstruct a tuple; JSON has no tuple type) -- structural, not a bug,
# not fixed here.
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
        cls = getattr(importlib.import_module(module_name), qualname)
        attributes = obj.get("attributes", {})
        decoded = {key: self._decode(val) for key, val in attributes.items()}
        return cls(**decoded)


def _decode_cubevis_enum(obj, deserializer):
    module_name, _, qualname = obj["cls"].rpartition(".")
    cls = getattr(importlib.import_module(module_name), qualname)
    return cls[obj["name"]]

# Deserializer._decoders is a ClassVar dict shared by every Deserializer
# (and subclass) instance in the process -- this registers "cubevis_enum"
# exactly once, at import time of this module, which Python only
# executes once per process regardless of how many times this module is
# imported elsewhere.
Deserializer.register("cubevis_enum", _decode_cubevis_enum)


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
    '''convert python values to a string that can be sent via websockets
    '''
    return serialize_json(serialize.encoder.serialize(val))

@static_vars( decoder=CubevisDeserializer( ) )
def deserialize( val ):
    '''convert an encoded value received from websockets
    '''
    value = json.loads(val)
    return deserialize.decoder.deserialize(value)

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
