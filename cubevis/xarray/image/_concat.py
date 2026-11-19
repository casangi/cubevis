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
'''``ConcatImage``: a concatenated CASA image (``ia.imageconcat``,
casacore's ``ImageConcat``), read and written through the images it is
made of.

On disk [measured, casatools 6.7.6, 2026-11-16] it is a directory with one
file, ``imageconcat.json``::

    {"Version": 1, "DataType": "float", "Axis": 3, "TempClose": false,
     "Images": ["./a.im", "./b.im"], "MiscInfo": {}}

``Axis`` is the pixel axis the images are joined along (0-based, CASA
order).  An image name starting ``./`` is beside the concatenated image
(in its parent directory, ``mode="nomove"``); ``././`` is inside its
directory (``mode="copy"`` or ``"move"``); anything else is a path as
given.  (``mode="paged"`` writes an ordinary image, which is a
``CasaImage``.)  A member may itself be a concatenated image.

What casacore makes of the members, followed here and compared with
casatools in the tests:

* pixels: the members' joined along the axis; a write goes to the member
  that holds the pixel;
* the pixel mask: the members' default masks joined, all good for a member
  without one; none when no member has one;
* coordinates: the first member's, except along a spectral axis -- kept
  when the members' channels run on evenly from each other, otherwise a
  tabular axis listing every channel's frequency -- and a Stokes axis,
  whose labels are joined;
* the beam: the members' beam when they all have the same one, a beam per
  plane when they differ, none when any member has none;
* units and image information: the first member's.
'''

import contextlib
import json
import os
from typing import Any, Dict, Iterator, List, Optional, Sequence

import numpy as np

from . import _coords as co
from ._image import CONCAT_FILE, CasaImage, IndexedArray, PIXEL_COLUMN, _Span, _transpose_matrices, is_concat_image

_DATA_TYPES = {'float': np.float32, 'double': np.float64, 'complex': np.complex64, 'dcomplex': np.complex128}


def member_path(name: str, concat_path: str) -> str:
    '''The path of a member named in ``imageconcat.json``.'''
    if name.startswith('././'):
        return os.path.normpath(os.path.join(concat_path, name[4:]))
    if name.startswith('./'):
        return os.path.normpath(os.path.join(os.path.dirname(concat_path), name[2:]))
    if os.path.isabs(name) or os.path.exists(name):
        return os.path.abspath(name)
    return os.path.normpath(os.path.join(os.path.dirname(concat_path), name))


def read_concat(path: str) -> Dict[str, Any]:
    '''``imageconcat.json``: ``axis``, ``images`` (paths), ``miscinfo``,
    ``data_type``.'''
    with open(os.path.join(path, CONCAT_FILE), 'r', encoding='utf-8') as f:
        spec = json.load(f)
    names = spec.get('Images') or []
    if not names:
        raise ValueError(f'{path}: {CONCAT_FILE} names no images')
    return {'axis': int(spec.get('Axis', 0)), 'images': [member_path(str(n), path) for n in names],
            'miscinfo': spec.get('MiscInfo') or {}, 'data_type': str(spec.get('DataType', '')).lower()}


class ConcatArray(IndexedArray):
    '''Arrays joined along one axis, indexed as one.  A part that is None
    reads as ``fill`` (a member with no pixel mask is all good) and cannot
    be written.'''

    def __init__(self, parts: Sequence[Optional[IndexedArray]], axis: int, shapes: Sequence[Sequence[int]],
                 dtype, fill: Any = 0):
        self.parts = list(parts)
        self.axis = int(axis)
        self.lengths = [int(shape[axis]) for shape in shapes]
        self.offsets = [0]
        for n in self.lengths:
            self.offsets.append(self.offsets[-1] + n)
        first = list(shapes[0])
        first[axis] = self.offsets[-1]
        self.shape = tuple(int(n) for n in first)
        self.dtype = np.dtype(dtype)
        self.fill = fill

    def _pieces(self, spans: List[_Span]):
        '''(part, its spans, the run of the block it covers) for each part
        the selection touches.'''
        lo, hi = spans[self.axis].lo, spans[self.axis].hi
        for part, start, n in zip(self.parts, self.offsets, self.lengths):
            a, b = max(lo, start), min(hi, start + n)
            if a < b:
                local = list(spans)
                local[self.axis] = _Span.run(a - start, b - start)
                local = [_Span.run(s.lo, s.hi) if i != self.axis else s for i, s in enumerate(local)]
                yield part, local, (a - lo, b - lo)

    def _read_block(self, spans: List[_Span]) -> np.ndarray:
        blocks = []
        for part, local, _ in self._pieces(spans):
            if part is None:
                blocks.append(np.full([s.hi - s.lo for s in local], self.fill, dtype=self.dtype))
            else:
                blocks.append(np.asarray(part._read_block(local)).astype(self.dtype, copy=False))   # pylint: disable=protected-access
        return blocks[0] if len(blocks) == 1 else np.concatenate(blocks, axis=self.axis)

    def _write_block(self, spans: List[_Span], block: np.ndarray) -> None:
        for part, local, (a, b) in self._pieces(spans):
            if part is None:
                raise PermissionError('this part of the concatenated image has no mask to write to')
            index = [slice(None)] * block.ndim
            index[self.axis] = slice(a, b)
            part._write_block(local, block[tuple(index)].astype(part.dtype, copy=False))             # pylint: disable=protected-access

    def __repr__(self) -> str:
        return f'<ConcatArray of {len(self.parts)} along axis {self.axis} shape={self.shape} dtype={self.dtype}>'


def _beam_quantities(beam: np.ndarray) -> Dict[str, Any]:
    arcsec = 180.0 * 3600.0 / np.pi
    return {'major': {'value': float(beam[0] * arcsec), 'unit': 'arcsec'},
            'minor': {'value': float(beam[1] * arcsec), 'unit': 'arcsec'},
            'positionangle': {'value': float(np.degrees(beam[2])), 'unit': 'deg'}}


class ConcatImage(CasaImage):
    '''A concatenated CASA image, with ``CasaImage``'s surface.  Made by
    ``CasaImage(path)`` when ``path`` is one.'''

    def __init__(self, path: str):                       # pylint: disable=super-init-not-called
        import threading                                 # pylint: disable=import-outside-toplevel
        self.path = os.path.abspath(os.path.expanduser(os.fspath(path)))
        if not is_concat_image(self.path):
            raise ValueError(f'{self.path}: not a concatenated image (no {CONCAT_FILE})')
        self._lock = threading.RLock()
        self._held = None
        self._column = PIXEL_COLUMN
        self.tile_shape = None
        self._load()

    # ---- description ----------------------------------------------------

    def _load(self) -> None:
        spec = read_concat(self.path)
        self.axis = spec['axis']
        members: List[CasaImage] = []
        for member in spec['images']:
            try:
                members.append(CasaImage(member))
            except (ValueError, FileNotFoundError) as ex:
                raise ValueError(f'{self.path}: its part {member} could not be opened: {ex}') from ex
        first = members[0]
        if not 0 <= self.axis < first.ndim:
            raise ValueError(f'{self.path}: joined along axis {self.axis} of images with {first.ndim} axes')
        for member in members[1:]:
            same = [n for i, n in enumerate(member.shape) if i != self.axis]
            if member.ndim != first.ndim or same != [n for i, n in enumerate(first.shape) if i != self.axis]:
                raise ValueError(f'{self.path}: {member.path} has shape {member.shape}, '
                                 f'which does not join {first.shape} along axis {self.axis}')
        self.members = members
        dtype = np.result_type(*[m.dtype for m in members])
        self._pixels = ConcatArray(members, self.axis, [m.shape for m in members], dtype)
        self.shape = self._pixels.shape
        self.dtype = self._pixels.dtype

        record = first.coords_record()
        axes = co.axes(record, first.ndim)
        joined = axes[self.axis]
        if joined.kind == 'spectral' and joined.key is not None:
            record[joined.key] = self._spectral(joined.key, record[joined.key])
        elif joined.kind == 'stokes' and joined.key is not None:
            labels: List[str] = []
            for member in members:
                labels.extend(co.stokes_labels(member.coords_record().get(joined.key, {})))
            record[joined.key] = dict(record[joined.key], stokes=labels)

        imageinfo = {k: v for k, v in first.imageinfo.items() if k not in ('restoringbeam', 'perplanebeams')}
        imageinfo.update(self._beams(axes))
        keywords: Dict[str, Any] = {'coords': _transpose_matrices(record), 'imageinfo': imageinfo}
        if first.units:
            keywords['units'] = first.units
        if spec['miscinfo']:
            keywords['miscinfo'] = spec['miscinfo']
        elif first.miscinfo:
            keywords['miscinfo'] = first.miscinfo
        if any(m.default_mask for m in members):
            keywords['masks'] = {'mask0': {'name': 'LCPagedMask', 'comment': 'the parts\' default masks, joined'}}
            keywords['Image_defaultmask'] = 'mask0'
        self._keywords = keywords

    def _spectral(self, key: str, coordinate: Dict[str, Any]) -> Dict[str, Any]:
        '''The spectral coordinate of the whole: the first part's when every
        part is linear and the channels run on evenly, else tabular.'''
        freqs, linear = [], True
        for member in self.members:
            sc = member.coords_record().get(key, coordinate)
            linear = linear and 'wcs' in sc and not (sc.get('tabular') or {}).get('worldvalues')
            freqs.append(co.spectral_values(sc, member.shape[self.axis]))
        values = np.concatenate(freqs)
        if linear and len(values) > 1:
            step = float(coordinate['wcs'].get('cdelt', 0.0)) * float(coordinate['wcs'].get('pc', 1.0))
            expect = values[0] + step * np.arange(len(values))
            if step != 0 and np.allclose(values, expect, rtol=0, atol=abs(step) * 1e-6):
                return coordinate
        elif linear:
            return coordinate
        out = {k: v for k, v in coordinate.items() if k != 'wcs'}
        step = float(values[-1] - values[0]) / (len(values) - 1) if len(values) > 1 else 0.0
        out['tabular'] = {'axes': ['Frequency'], 'crval': [float(values[0])], 'crpix': [0.0], 'cdelt': [step],
                          'pc': [[1.0]], 'units': ['Hz'],
                          'pixelvalues': [float(i) for i in range(len(values))],
                          'worldvalues': [float(v) for v in values]}
        return out

    def _beams(self, axes) -> Dict[str, Any]:
        chan_axis, stokes_axis = co.find_axis(axes, 'spectral'), co.find_axis(axes, 'stokes')
        arrays = []
        for member in self.members:
            nchan = member.shape[chan_axis] if chan_axis is not None else 1
            nstokes = member.shape[stokes_axis] if stokes_axis is not None else 1
            beam = co.beam_array(member.imageinfo, nchan, nstokes)
            if beam is None:
                return {}                                # one part without a beam: none for the whole
            arrays.append(beam)
        if self.axis == chan_axis:
            beams = np.concatenate(arrays, axis=0)
        elif self.axis == stokes_axis:
            beams = np.concatenate(arrays, axis=1)
        else:
            beams = arrays[0]
        if np.allclose(beams, beams[0, 0], rtol=1e-12, atol=0):
            first = self.members[0].imageinfo
            if first.get('restoringbeam'):
                return {'restoringbeam': first['restoringbeam']}
            return {'restoringbeam': _beam_quantities(beams[0, 0])}
        nchan, nstokes = beams.shape[0:2]
        flat: Dict[str, Any] = {'nChannels': nchan, 'nStokes': nstokes}
        for s in range(nstokes):
            for c in range(nchan):
                flat[f'*{c + nchan * s}'] = _beam_quantities(beams[c, s])
        return {'perplanebeams': flat}

    def refresh(self) -> None:
        self._load()

    def files(self) -> List[str]:
        out = [os.path.join(self.path, CONCAT_FILE)]
        for member in self.members:
            out.extend(member.files())
        return out

    def modified(self) -> float:
        return max([os.path.getctime(os.path.join(self.path, CONCAT_FILE))] + [m.modified() for m in self.members])

    # ---- handles ----------------------------------------------------------

    @contextlib.contextmanager
    def hold(self, readonly: bool = True) -> Iterator['ConcatImage']:
        '''Keep a handle on every part for the ``with`` block.'''
        with contextlib.ExitStack() as stack:
            for member in self.members:
                stack.enter_context(member.hold(readonly))
            yield self

    # ---- pixels and masks ---------------------------------------------------

    def _read_block(self, spans: List[_Span]) -> np.ndarray:
        return self._pixels._read_block(spans)           # pylint: disable=protected-access

    def _write_block(self, spans: List[_Span], block: np.ndarray) -> None:
        self._pixels._write_block(spans, block)          # pylint: disable=protected-access

    def mask(self, name: Optional[str] = None) -> Optional[IndexedArray]:
        '''The parts' default masks joined (True = good; all good for a part
        without one); None when no part has a mask.'''
        if name is None:
            name = self.default_mask
            if name is None:
                return None
        if name not in self.mask_names:
            raise KeyError(f'{self.path}: no mask {name!r} (masks: {self.mask_names})')
        return ConcatArray([m.mask() for m in self.members], self.axis, [m.shape for m in self.members],
                           np.bool_, fill=True)

    def __repr__(self) -> str:
        return f'<ConcatImage {self.path} of {len(self.members)} along axis {self.axis} shape={self.shape}>'
