########################################################################
#
# Copyright (C) 2026
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
'''Making a CASA image with arcae alone: ``create_image`` (an empty image
with its coordinate system, masks and log table), ``write_image`` (from
arrays) and ``default_coords`` (a coordinate system record from a few
numbers).

casatools opens what these write: pixels, masks, coordinates, beam, units
(checked in the tests where casatools is installed).  What casacore needs of
an image table (found 2026-11-16):

* one row, one column ``map`` in a ``TiledCellStMan`` -- made with TaQL
  ``CREATE TABLE``, since arcae's table factory makes Measurement Sets;
* ``table.info`` saying ``Type = Image``, written after the last handle is
  closed (casacore rewrites the file on close);
* the ``coords`` keyword; a ``logtable`` keyword naming a log subtable --
  casatools refuses an image without one;
* a mask: a subtable with a boolean column ``PagedArray``, named in the
  ``masks`` keyword with its box.

arcae's ``putkeywords`` goes through casacore's JSON reader, which takes
nested records, scalars and vectors but not matrices or empty arrays.
Those (the ``pc`` matrices) are set afterwards with TaQL ``ALTER TABLE ...
SET KEYWORD``.
'''

import os
import shutil
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import numpy as np

import json

from ._image import CONCAT_FILE, CasaImage, MASK_COLUMN, PIXEL_COLUMN, PagedArray, _transpose_matrices

_TAQL_TYPES = {np.dtype(np.float32): 'R4', np.dtype(np.float64): 'R8',
               np.dtype(np.complex64): 'C4', np.dtype(np.complex128): 'C8',
               np.dtype(np.bool_): 'B'}

#: MJD of 2000-01-01, casacore's date for an image made without one
DEFAULT_OBSDATE_MJD = 51544.0

HI_REST_FREQUENCY = 1420405751.786


def _taql(command: str) -> None:
    from arcae.lib.arrow_tables import Table        # pylint: disable=import-outside-toplevel
    Table.from_taql(command).close()


def _quote(path: str) -> str:
    if '"' in path or '\n' in path:
        raise ValueError(f'a table path cannot hold a double quote or a newline: {path!r}')
    return f'"{path}"'


def _ints(values: Sequence[int]) -> str:
    return '[' + ','.join(str(int(v)) for v in values) + ']'


def _write_info(path: str, kind: str, readme: str = '') -> None:
    with open(os.path.join(path, 'table.info'), 'w', encoding='utf-8') as f:
        f.write(f'Type = {kind}\nSubType = \n\n{readme}')


def default_tile_shape(shape: Sequence[int], dtype=np.float32) -> Tuple[int, ...]:
    '''Tiles of at most 256 x 256 pixels of one plane each: a plane and a
    spectrum both read few tiles.'''
    return tuple(int(min(n, 256)) if axis < 2 else 1 for axis, n in enumerate(shape))


def _create_tiled(path: str, column: str, dtype, shape: Sequence[int], tile: Sequence[int]) -> None:
    taql_type = _TAQL_TYPES.get(np.dtype(dtype))
    if taql_type is None:
        raise TypeError(f'pixels of type {np.dtype(dtype)} cannot be written '
                        f'(one of {", ".join(str(d) for d in _TAQL_TYPES)})')
    _taql(f'CREATE TABLE {_quote(path)} '
          f"({column} {taql_type} [NDIM={len(shape)}, SHAPE={_ints(shape)}, COMMENT='version 4.0']) "
          f'LIMIT 1 '
          f"DMINFO [TYPE='TiledCellStMan', NAME='{column}', COLUMNS=['{column}'], "
          f'SPEC=[DEFAULTTILESHAPE={_ints(tile)}]]')


def _is_matrix_or_empty(value: Any) -> bool:
    return isinstance(value, list) and (len(value) == 0 or all(isinstance(v, list) for v in value))


def _split_keywords(record: Dict[str, Any], path: Tuple[str, ...], deferred: List[Tuple[Tuple[str, ...], Any]]):
    '''``record`` without the values casacore's JSON reader refuses, which
    go to ``deferred`` with their path.'''
    out: Dict[str, Any] = {}
    for key, value in record.items():
        if isinstance(value, np.ndarray):
            value = value.tolist()
        elif isinstance(value, np.generic):
            value = value.item()
        elif isinstance(value, tuple):
            value = list(value)
        if isinstance(value, Mapping):
            out[key] = _split_keywords(dict(value), path + (key,), deferred)
        elif _is_matrix_or_empty(value):
            deferred.append((path + (key,), value))
        else:
            out[key] = value
    return out


def _literal(value: Any) -> str:
    if isinstance(value, list):
        return '[' + ','.join(_literal(v) for v in value) + ']'
    if isinstance(value, bool):
        return 'T' if value else 'F'
    return repr(float(value))


def put_keywords(path: str, keywords: Mapping[str, Any]) -> None:
    '''Set table keywords from a record in arcae's layout (matrices as
    arcae reads them): what ``putkeywords`` takes directly, then the
    matrices and empty arrays through TaQL.'''
    from arcae.lib.arrow_tables import Table        # pylint: disable=import-outside-toplevel
    deferred: List[Tuple[Tuple[str, ...], Any]] = []
    plain = _split_keywords(dict(keywords), (), deferred)
    table = Table.from_filename(path, ninstances=1, readonly=False)
    try:
        table.putkeywords(plain)
    finally:
        table.close()
    for where, value in deferred:
        for name in where:
            if not name.replace('_', 'a').isalnum() or name[0].isdigit():
                raise ValueError(f'keyword {".".join(where)}: a matrix under a field named {name!r} cannot be written')
        _taql(f'ALTER TABLE {_quote(path)} SET KEYWORD {".".join(where)}={_literal(value)} AS R8')


def _mask_record(path: str, shape: Sequence[int]) -> Dict[str, Any]:
    return {'isRegion': 1, 'name': 'LCPagedMask', 'comment': '', 'mask': f'Table: {path}',
            'box': {'isRegion': 1, 'name': 'LCBox', 'comment': '', 'oneRel': True,
                    'blc': [1.0] * len(shape), 'trc': [float(n) for n in shape],
                    'shape': [int(n) for n in shape]}}


def default_coords(shape: Sequence[int], *,
                   reference_direction: Sequence[float] = (0.0, 0.0),
                   cell: Sequence[float] = (-4.84813681109536e-06, 4.84813681109536e-06),
                   frame: str = 'J2000', projection: str = 'SIN',
                   stokes: Optional[Sequence[str]] = None,
                   frequencies: Optional[Sequence[float]] = None,
                   reference_frequency: float = 1.415e9, channel_width: float = 1.0e3,
                   spectral_frame: str = 'LSRK', rest_frequency: float = HI_REST_FREQUENCY,
                   telescope: str = 'ALMA', observer: str = '',
                   obsdate_mjd: float = DEFAULT_OBSDATE_MJD) -> Dict[str, Any]:
    '''A coordinate system record (casatools' layout) for an image of
    ``shape``: two direction axes (radians; ``cell`` defaults to 1 arcsec,
    RA decreasing; the reference pixel is the centre), then a Stokes axis
    when ``shape`` has a third axis and a spectral axis when it has a
    fourth.  ``frequencies`` (Hz, one per channel) gives a tabular spectral
    axis; without it the axis is linear from ``reference_frequency`` at
    channel 0 in steps of ``channel_width``.'''
    shape = [int(n) for n in shape]
    if not 2 <= len(shape) <= 4:
        raise ValueError(f'default_coords makes 2 to 4 axes, not {len(shape)}')
    record: Dict[str, Any] = {
        'telescope': telescope, 'observer': observer,
        'obsdate': {'type': 'epoch', 'refer': 'UTC', 'm0': {'value': float(obsdate_mjd), 'unit': 'd'}},
        'pointingcenter': {'value': [0.0, 0.0], 'initial': True},
        'direction0': {
            'system': frame, 'projection': projection, 'projection_parameters': [0.0, 0.0],
            'crval': [float(reference_direction[0]), float(reference_direction[1])],
            'crpix': [float(shape[0] // 2), float(shape[1] // 2)],
            'cdelt': [float(cell[0]), float(cell[1])],
            'pc': [[1.0, 0.0], [0.0, 1.0]],
            'axes': ['Right Ascension', 'Declination'] if frame.upper() not in ('GALACTIC', 'SUPERGAL', 'ECLIPTIC')
                    else ['Longitude', 'Latitude'],
            'units': ['rad', 'rad'], 'conversionSystem': frame, 'longpole': 180.0,
            'latpole': float(np.degrees(reference_direction[1])),
        },
        'worldmap0': [0, 1], 'worldreplace0': [float(reference_direction[0]), float(reference_direction[1])],
        'pixelmap0': [0, 1], 'pixelreplace0': [float(shape[0] // 2), float(shape[1] // 2)],
    }
    if len(shape) >= 3:
        labels = list(stokes) if stokes is not None else ['I', 'Q', 'U', 'V'][:shape[2]]
        if len(labels) != shape[2]:
            raise ValueError(f'{len(labels)} Stokes labels for an axis of {shape[2]}')
        record.update({
            'stokes1': {'axes': ['Stokes'], 'stokes': labels, 'crval': [1.0], 'crpix': [0.0],
                        'cdelt': [1.0], 'pc': [[1.0]]},
            'worldmap1': [2], 'worldreplace1': [1.0], 'pixelmap1': [2], 'pixelreplace1': [0.0],
        })
    if len(shape) == 4:
        spectral: Dict[str, Any] = {
            'version': 2, 'system': spectral_frame, 'restfreq': float(rest_frequency),
            'restfreqs': [float(rest_frequency)], 'velType': 0, 'nativeType': 0, 'velUnit': 'km/s',
            'waveUnit': 'mm', 'formatUnit': '', 'unit': 'Hz', 'name': 'Frequency',
        }
        if frequencies is not None:
            values = [float(f) for f in frequencies]
            if len(values) != shape[3]:
                raise ValueError(f'{len(values)} frequencies for {shape[3]} channels')
            step = values[1] - values[0] if len(values) > 1 else float(channel_width)
            spectral['tabular'] = {'crval': [values[0]], 'crpix': [0.0], 'cdelt': [step], 'pc': [[1.0]],
                                   'axes': ['Frequency'], 'units': ['Hz'],
                                   'pixelvalues': [float(c) for c in range(len(values))],
                                   'worldvalues': values}
            first = values[0]
        else:
            spectral['wcs'] = {'crval': float(reference_frequency), 'crpix': 0.0,
                               'cdelt': float(channel_width), 'pc': 1.0, 'ctype': 'FREQ'}
            first = float(reference_frequency)
        record.update({'spectral2': spectral, 'worldmap2': [3], 'worldreplace2': [first],
                       'pixelmap2': [3], 'pixelreplace2': [0.0]})
    return record


def create_image(path: str, shape: Sequence[int], *, dtype=np.float32,
                 coords: Optional[Mapping[str, Any]] = None, units: str = '',
                 imageinfo: Optional[Mapping[str, Any]] = None,
                 miscinfo: Optional[Mapping[str, Any]] = None,
                 masks: Sequence[str] = (), default_mask: Optional[str] = None,
                 tile_shape: Optional[Sequence[int]] = None, overwrite: bool = False) -> CasaImage:
    '''Make a CASA image of ``shape`` (CASA axis order) and return it; its
    pixels are not initialized.

    ``coords`` is a coordinate system record in casatools' layout
    (``CasaImage.coords_record()``, ``coordsys().torecord()``); by default
    ``default_coords(shape)``.  ``masks`` names pixel masks to make (each
    all good); ``default_mask`` picks the one applied by default (the first
    when not given).'''
    path = os.path.abspath(os.path.expanduser(os.fspath(path)))
    shape = [int(n) for n in shape]
    if os.path.exists(path):
        if not overwrite:
            raise FileExistsError(path)
        shutil.rmtree(path) if os.path.isdir(path) else os.remove(path)
    tile = list(tile_shape) if tile_shape is not None else list(default_tile_shape(shape, dtype))
    _create_tiled(path, PIXEL_COLUMN, dtype, shape, tile)

    log_path = os.path.join(path, 'logtable')
    _taql(f'CREATE TABLE {_quote(log_path)} (TIME R8, PRIORITY S, MESSAGE S, LOCATION S, OBJECT_ID S)')
    _write_info(log_path, 'Log message', 'Repository for software-generated logging messages\n')

    keywords: Dict[str, Any] = {
        'logtable': f'Table: {log_path}',
        'coords': _transpose_matrices(dict(coords) if coords is not None else default_coords(shape)),
        'imageinfo': dict(imageinfo) if imageinfo else {'imagetype': 'Intensity', 'objectname': ''},
    }
    if units:
        keywords['units'] = str(units)
    if miscinfo:
        keywords['miscinfo'] = dict(miscinfo)
    masks = list(masks)
    if masks:
        records = {}
        for name in masks:
            mask_path = os.path.join(path, name)
            _create_tiled(mask_path, MASK_COLUMN, np.bool_, shape, tile)
            PagedArray(mask_path, MASK_COLUMN)[...] = True
            _write_info(mask_path, 'Paged Array')
            records[name] = _mask_record(mask_path, shape)
        keywords['masks'] = records
        keywords['Image_defaultmask'] = default_mask or masks[0]
    put_keywords(path, keywords)
    _write_info(path, 'Image')
    return CasaImage(path)


def beam_record(major: float, minor: float, pa: float) -> Dict[str, Any]:
    '''An ``imageinfo`` restoring beam from arcseconds and degrees.'''
    return {'major': {'value': float(major), 'unit': 'arcsec'},
            'minor': {'value': float(minor), 'unit': 'arcsec'},
            'positionangle': {'value': float(pa), 'unit': 'deg'}}


def write_image(path: str, pixels, *, coords: Optional[Mapping[str, Any]] = None, units: str = '',
                beam: Optional[Sequence[float]] = None, masks: Optional[Mapping[str, Any]] = None,
                default_mask: Optional[str] = None, object_name: str = '',
                tile_shape: Optional[Sequence[int]] = None, overwrite: bool = False) -> CasaImage:
    '''Write ``pixels`` (CASA axis order, ``[x, y, stokes, chan]``) as a
    CASA image.  ``beam`` is ``(major arcsec, minor arcsec, pa deg)``;
    ``masks`` maps a mask name to a boolean array of the image's shape, True
    where a pixel is good.'''
    pixels = np.asarray(pixels)
    if pixels.dtype not in _TAQL_TYPES or pixels.dtype == np.bool_:
        pixels = pixels.astype(np.complex64 if pixels.dtype.kind == 'c' else np.float32)
    imageinfo: Dict[str, Any] = {'imagetype': 'Intensity', 'objectname': str(object_name)}
    if beam is not None:
        imageinfo['restoringbeam'] = beam_record(*beam)
    masks = dict(masks or {})
    image = create_image(path, pixels.shape, dtype=pixels.dtype, coords=coords, units=units,
                         imageinfo=imageinfo, masks=list(masks), default_mask=default_mask,
                         tile_shape=tile_shape, overwrite=overwrite)
    image[...] = pixels
    for name, good in masks.items():
        image.mask(name)[...] = np.asarray(good, dtype=bool)
    return image


def concat_images(path: str, images: Sequence[str], axis: int = -1, *, overwrite: bool = False) -> CasaImage:
    '''Make a concatenated image at ``path`` from existing ``images``,
    joined along pixel ``axis`` (the last by default: channels, for the
    usual four axes), as ``ia.imageconcat(mode="nomove")`` does: a directory
    holding ``imageconcat.json`` that names the images, which stay where
    they are.  casatools opens the result.  Returns it (a ``ConcatImage``).'''
    path = os.path.abspath(os.path.expanduser(os.fspath(path)))
    members = [os.path.abspath(os.path.expanduser(os.fspath(p))) for p in images]
    if not members:
        raise ValueError('no images to concatenate')
    first = CasaImage(members[0])
    axis = axis % first.ndim
    if os.path.exists(path):
        if not overwrite:
            raise FileExistsError(path)
        shutil.rmtree(path) if os.path.isdir(path) else os.remove(path)
    parent = os.path.dirname(path)
    names = []
    for member in members:
        if os.path.dirname(member) == parent:
            names.append('./' + os.path.basename(member))            # beside the concatenated image
        elif member.startswith(path + os.sep):
            names.append('././' + os.path.relpath(member, path))     # inside it
        else:
            names.append(member)
    data_type = {'f4': 'float', 'f8': 'double', 'c8': 'complex', 'c16': 'dcomplex'}.get(first.dtype.str.lstrip('<>=|'), 'float')
    os.makedirs(path)
    with open(os.path.join(path, CONCAT_FILE), 'w', encoding='utf-8') as f:
        json.dump({'Version': 1, 'DataType': data_type, 'Axis': axis, 'TempClose': False,
                   'Images': names, 'MiscInfo': {}}, f, indent=1)
    return CasaImage(path)
