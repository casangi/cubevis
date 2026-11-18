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
'''``CasaImage``: a CASA image on disk, read and written with arcae.

A CASA (paged) image is a casacore table of one row and one column,
``map``, whose single cell is the whole pixel array, stored in tiles
(``TiledCellStMan``).  Everything else is in the table's keywords:
``coords`` (the coordinate system record -- what casatools gives as
``coordsys().torecord()``), ``imageinfo`` (restoring beam, object name,
image type), ``units``, ``miscinfo``, and ``masks`` / ``Image_defaultmask``
(pixel masks: subtables of one boolean cell, column ``PagedArray``, True
where a pixel is good).  So arcae, which has no image interface, reads an
image with table calls alone.

Axis order.  Pixels are indexed as CASA does: ``image[x, y, stokes, chan]``
for the usual four axes (the order of ``image.shape``, which is
``casatools.image.shape()``).  arcae hands the cell over in C order, the
reverse; the reversal here is a view, not a copy.

Reads and writes are partial: only the tiles a selection touches are read.

Handles.  Each read or write opens the table, does its work and closes it
(about 2 ms).  A handle kept open does **not** see what another casacore
in the same process writes afterwards -- casatools has its own casacore,
and the file locks that keep two *processes* in step do not exist within
one (measured 2026-11-16: an arcae handle held across a casatools
``putchunk`` kept returning the old pixels, even after casatools closed
the image; a new handle saw the new ones).  ``hold()`` keeps one handle
for a burst of reads where nothing else writes.

A *concatenated* image (``ia.imageconcat``, casacore's ``ImageConcat``) is
not a table: it is a directory holding ``imageconcat.json``, which names
the images that make it up.  ``CasaImage(path)`` gives a ``ConcatImage``
for one (``_concat.py``), with the same surface.

Nothing here imports CASA, Bokeh or xarray; arcae is imported when a table
is first opened.
'''

import contextlib
import copy
import os
import threading
from typing import Any, Dict, Iterator, List, Optional, Tuple

import numpy as np

#: casacore value type (``getcoldesc()['valueType']``) -> numpy dtype
VALUE_DTYPES = {
    'boolean': np.dtype(np.bool_),
    'uchar': np.dtype(np.uint8),
    'short': np.dtype(np.int16),
    'int': np.dtype(np.int32),
    'float': np.dtype(np.float32),
    'double': np.dtype(np.float64),
    'complex': np.dtype(np.complex64),
    'dcomplex': np.dtype(np.complex128),
}

PIXEL_COLUMN = 'map'
MASK_COLUMN = 'PagedArray'


def table_info(path: str) -> Dict[str, str]:
    '''``Type`` and ``SubType`` from a table's ``table.info`` ({} when there
    is none).'''
    info: Dict[str, str] = {}
    try:
        with open(os.path.join(path, 'table.info'), 'r', encoding='utf-8', errors='replace') as f:
            for line in f:
                key, sep, value = line.partition('=')
                if sep and key.strip().lower() in ('type', 'subtype'):
                    info['Type' if key.strip().lower() == 'type' else 'SubType'] = value.strip()
    except OSError:
        pass
    return info


CONCAT_FILE = 'imageconcat.json'


def _as_path(path: Any) -> Optional[str]:
    try:
        path = os.fspath(path)
    except TypeError:
        return None
    return os.fsdecode(path) if isinstance(path, bytes) else path


def is_paged_image(path: Any) -> bool:
    '''Is ``path`` a CASA paged image: a casacore table whose ``table.info``
    says ``Type = Image``?'''
    path = _as_path(path)
    return (path is not None and os.path.isdir(path) and os.path.isfile(os.path.join(path, 'table.dat'))
            and table_info(path).get('Type') == 'Image')


def is_concat_image(path: Any) -> bool:
    '''Is ``path`` a concatenated image: a directory with
    ``imageconcat.json`` (and not itself a table)?'''
    path = _as_path(path)
    return (path is not None and os.path.isdir(path) and os.path.isfile(os.path.join(path, CONCAT_FILE))
            and not os.path.isfile(os.path.join(path, 'table.dat')))


def is_casa_image(path: Any) -> bool:
    '''Is ``path`` a CASA image this package reads: a paged image or a
    concatenation of images?  (Not a FITS or MIRIAD file, an expression
    image or a component list -- casacore opens those through its image
    classes, which arcae does not have.)'''
    return is_paged_image(path) or is_concat_image(path)


def _open(path: str, readonly: bool):
    from arcae.lib.arrow_tables import Table        # pylint: disable=import-outside-toplevel
    return Table.from_filename(path, ninstances=1, readonly=readonly)


class _Span:
    '''One axis of a selection: the contiguous run ``lo:hi`` read from (or
    written to) disk, how to index that run afterwards (``post``), and
    whether the axis was an integer (dropped from the result).'''
    __slots__ = ('lo', 'hi', 'post', 'drop', 'count')

    def __init__(self, key, n: int, axis: int):
        if isinstance(key, (int, np.integer)) and not isinstance(key, (bool, np.bool_)):
            k = int(key)
            if k < -n or k >= n:
                raise IndexError(f'index {k} is out of bounds for axis {axis} with size {n}')
            k %= n
            self.lo, self.hi, self.post, self.drop, self.count = k, k + 1, slice(None), True, 1
        elif isinstance(key, slice):
            start, stop, step = key.indices(n)
            picked = range(start, stop, step)
            self.count = len(picked)
            self.drop = False
            if self.count == 0:
                self.lo = self.hi = 0
                self.post = slice(0, 0)
            elif step > 0:
                self.lo, self.hi = picked[0], picked[-1] + 1
                self.post = slice(None, None, step)
            else:
                self.lo, self.hi = picked[-1], picked[0] + 1
                self.post = slice(None, None, step)
        else:
            raise IndexError(f'axis {axis}: only integers and slices index an image, not {type(key).__name__}')

    @classmethod
    def run(cls, lo: int, hi: int) -> '_Span':
        '''The contiguous run ``lo:hi``, kept as an axis.'''
        span = object.__new__(cls)
        span.lo, span.hi, span.post, span.drop, span.count = lo, hi, slice(None), False, hi - lo
        return span

    @property
    def contiguous(self) -> bool:
        return self.post == slice(None) or self.post == slice(None, None, 1)


class IndexedArray:
    '''Indexing as numpy's (integers and slices, CASA axis order) for
    anything that has ``shape`` and ``dtype`` and can read and write a
    block: ``_read_block(spans)`` gives the run ``lo:hi`` of every axis,
    all axes kept; ``_write_block(spans, block)`` takes the same.'''

    shape: Tuple[int, ...] = ()
    dtype = np.dtype(np.float32)

    @property
    def ndim(self) -> int:
        return len(self.shape)

    @property
    def size(self) -> int:
        return int(np.prod(self.shape, dtype=np.int64))

    def _spans(self, key) -> List[_Span]:
        if not isinstance(key, tuple):
            key = (key,)
        if any(k is Ellipsis for k in key):
            at = [i for i, k in enumerate(key) if k is Ellipsis]
            if len(at) > 1:
                raise IndexError('an index can only have a single ellipsis')
            fill = self.ndim - (len(key) - 1)
            key = key[:at[0]] + (slice(None),) * fill + key[at[0] + 1:]
        if len(key) > self.ndim:
            raise IndexError(f'too many indices: {len(key)} for an image of {self.ndim} axes')
        key = key + (slice(None),) * (self.ndim - len(key))
        return [_Span(k, n, axis) for axis, (k, n) in enumerate(zip(key, self.shape))]

    def _read_block(self, spans: List[_Span]) -> np.ndarray:
        raise NotImplementedError

    def _write_block(self, spans: List[_Span], block: np.ndarray) -> None:
        raise NotImplementedError

    def __getitem__(self, key) -> np.ndarray:
        spans = self._spans(key)
        out_shape = tuple(s.count for s in spans if not s.drop)
        if any(s.count == 0 for s in spans):
            return np.empty(out_shape, dtype=self.dtype)
        block = self._read_block(spans)
        if block.dtype != self.dtype:
            block = block.astype(self.dtype)              # arcae: Bool cells are uint8
        if not all(s.contiguous for s in spans):
            block = block[tuple(s.post for s in spans)]
        return block.reshape(out_shape)

    def __setitem__(self, key, values) -> None:
        spans = self._spans(key)
        if not all(s.contiguous for s in spans):
            raise IndexError('only contiguous selections (step 1) can be written')
        if any(s.count == 0 for s in spans):
            return
        values = np.asarray(values)
        if values.dtype.kind == 'c' and self.dtype.kind != 'c':
            raise TypeError(f'complex values cannot be written to {self.dtype} pixels')
        sel_shape = tuple(s.count for s in spans if not s.drop)
        full_shape = tuple(s.count for s in spans)
        self._write_block(spans, np.broadcast_to(values.astype(self.dtype, copy=False), sel_shape).reshape(full_shape))

    def read(self) -> np.ndarray:
        '''The whole array.'''
        return self[...]


class PagedArray(IndexedArray):
    '''The one tiled array cell of a casacore table: an image's pixels
    (column ``map``) or a pixel mask's (column ``PagedArray``).

    ``arr[x0:x1, y0:y1, s, c]`` reads, ``arr[...] = values`` writes; indices
    are integers and slices, in CASA's axis order.  Boolean cells come back
    as ``bool`` (arcae gives ``uint8``).
    '''

    def __init__(self, path: str, column: Optional[str] = None):
        self.path = os.path.abspath(os.path.expanduser(os.fspath(path)))
        if not os.path.isfile(os.path.join(self.path, 'table.dat')):
            raise FileNotFoundError(f'not a casacore table: {self.path}')
        self._lock = threading.RLock()
        self._held = None               # (table, readonly, depth)
        self._column = column
        self._keywords: Dict[str, Any] = {}
        with self._table(True) as table:
            self._read_description(table)

    def __getstate__(self):
        ### for dask workers: the lock and any held handle stay behind
        state = self.__dict__.copy()
        state['_lock'] = None
        state['_held'] = None
        return state

    def __setstate__(self, state):
        self.__dict__.update(state)
        self._lock = threading.RLock()

    # ---- description ----------------------------------------------------

    def _read_description(self, table) -> None:
        columns = list(table.columns())
        if self._column is None:
            if len(columns) != 1:
                raise ValueError(f'{self.path}: expected one column, found {columns}')
            self._column = columns[0]
        elif self._column not in columns:
            raise ValueError(f'{self.path}: no column {self._column!r} (columns: {columns})')
        if table.nrow() != 1:
            raise ValueError(f'{self.path}: expected one row, found {table.nrow()}')
        desc = table.getcoldesc(self._column)
        value_type = str(desc.get('valueType', '')).lower()
        if value_type not in VALUE_DTYPES:
            raise TypeError(f'{self.path}: pixels of type {value_type!r} are not supported')
        self.dtype = VALUE_DTYPES[value_type]
        c_shape = [int(n) for n in table.row_shapes(self._column)[0]]
        self.shape: Tuple[int, ...] = tuple(reversed(c_shape))
        self.tile_shape: Optional[Tuple[int, ...]] = None
        try:
            for manager in table.getdminfo().values():
                if self._column in manager.get('COLUMNS', []):
                    cubes = manager.get('SPEC', {}).get('HYPERCUBES', {})
                    for cube in cubes.values():
                        self.tile_shape = tuple(int(n) for n in cube['TileShape'])
        except Exception:               # informational: the chunking a reader may prefer
            self.tile_shape = None
        self._keywords = table.getkeywords()

    @property
    def column(self) -> str:
        return self._column

    @property
    def keywords(self) -> Dict[str, Any]:
        '''The table's keywords as arcae gives them, read when the object was
        made (``refresh()`` reads them again).  2-D arrays are transposed
        relative to casatools' records: see ``CasaImage.coords_record``.'''
        return self._keywords

    def refresh(self) -> None:
        '''Read shape and keywords again (another program rewrote the table).'''
        with self._table(True) as table:
            self._read_description(table)

    def modified(self) -> float:
        '''Newest change time of the table's files.'''
        return max(os.path.getctime(os.path.join(self.path, f)) for f in os.listdir(self.path))

    # ---- handles ----------------------------------------------------------

    @contextlib.contextmanager
    def _table(self, readonly: bool) -> Iterator[Any]:
        with self._lock:
            held = self._held
            if held is not None and (readonly or not held[1]):
                yield held[0]
                return
            table = _open(self.path, readonly)
            try:
                yield table
            finally:
                table.close()

    @contextlib.contextmanager
    def hold(self, readonly: bool = True) -> Iterator['PagedArray']:
        '''Keep one handle open for the ``with`` block (a burst of reads, or
        of writes with ``readonly=False``).  Do not hold a handle across
        another program's write to the table: see the module's note.'''
        with self._lock:
            if self._held is not None and (readonly or not self._held[1]):
                self._held = (self._held[0], self._held[1], self._held[2] + 1)
                opened = False
            elif self._held is not None:
                raise RuntimeError(f'{self.path}: held read-only; a write needs hold(readonly=False)')
            else:
                self._held = (_open(self.path, readonly), readonly, 1)
                opened = True
        try:
            yield self
        finally:
            with self._lock:
                table, ro, depth = self._held
                if depth > 1 and not opened:
                    self._held = (table, ro, depth - 1)
                else:
                    self._held = None
                    table.close()

    # ---- pixels -----------------------------------------------------------

    @staticmethod
    def _arcae_index(spans: List[_Span]) -> tuple:
        return (slice(0, 1),) + tuple(slice(s.lo, s.hi) for s in reversed(spans))

    def _read_block(self, spans: List[_Span]) -> np.ndarray:
        with self._table(True) as table:
            block = table.getcol(self._column, index=self._arcae_index(spans))
        return np.asarray(block)[0].T                     # CASA axis order: a view

    def _write_block(self, spans: List[_Span], block: np.ndarray) -> None:
        block = np.ascontiguousarray(block.T)[None, ...]
        with self._table(False) as table:
            table.putcol(self._column, block, index=self._arcae_index(spans))

    def __repr__(self) -> str:
        return f'<{type(self).__name__} {self.path} shape={self.shape} dtype={self.dtype}>'


def _transpose_matrices(record: Any) -> Any:
    '''arcae writes a keyword's 2-D array row by row as casacore stores it,
    which is column by column; casatools gives the same array as numpy
    ``[row][column]``.  So every 2-D array (the ``pc`` matrices) is the
    transpose of casatools' (measured on a rotated image, 2026-11-16).
    Applied twice it is the identity.'''
    if isinstance(record, dict):
        return {k: _transpose_matrices(v) for k, v in record.items()}
    if isinstance(record, list) and record and all(isinstance(r, list) for r in record):
        if all(not isinstance(v, (list, dict)) for r in record for v in r) and len({len(r) for r in record}) == 1:
            return [list(col) for col in zip(*record)]
    if isinstance(record, str):
        return record.rstrip('\x00')                      # FITS ctype strings are padded with NULs
    return record


class CasaImage(PagedArray):
    '''A CASA paged image: pixels (index it), keywords, coordinate system
    record, pixel masks.

    >>> im = CasaImage('ngc5921.residual')
    >>> im.shape                                # (nx, ny, nstokes, nchan)
    >>> plane = im[:, :, 0, 12]                 # one channel, [x, y]
    >>> spectrum = im[128, 128, 0, :]
    >>> im.coords_record()['direction0']        # as coordsys().torecord()
    >>> good = im.mask()[:, :, 0, 12]           # default pixel mask, True = good
    '''

    def __new__(cls, path: Any = None):
        ### a concatenated image is a ConcatImage, whoever asks for a CasaImage
        if cls is CasaImage and path is not None and is_concat_image(os.path.expanduser(_as_path(path) or '')):
            from ._concat import ConcatImage              # pylint: disable=import-outside-toplevel
            return object.__new__(ConcatImage)
        return object.__new__(cls)

    def __init__(self, path: str):
        path = os.path.abspath(os.path.expanduser(os.fspath(path)))
        if not is_paged_image(path):
            kind = table_info(path).get('Type')
            what = f'a casacore table of type {kind!r}' if kind else 'not a CASA image'
            raise ValueError(f'{path}: {what}; paged images (table.info "Type = Image") and concatenated '
                             'images (imageconcat.json) are read, not FITS, MIRIAD or expression images')
        super().__init__(path, PIXEL_COLUMN)

    def files(self) -> List[str]:
        '''The files whose change means the image's description (shape,
        keywords) may have changed.'''
        return [os.path.join(self.path, 'table.dat')]

    # ---- keywords ---------------------------------------------------------

    @property
    def units(self) -> str:
        '''Brightness unit (``ia.brightnessunit()``), '' when not set.'''
        return str(self._keywords.get('units', '') or '')

    @property
    def imageinfo(self) -> Dict[str, Any]:
        return self._keywords.get('imageinfo', {}) or {}

    @property
    def miscinfo(self) -> Dict[str, Any]:
        return self._keywords.get('miscinfo', {}) or {}

    def coords_record(self) -> Dict[str, Any]:
        '''The coordinate system record laid out as casatools'
        ``coordsys().torecord()``: the ``coords`` keyword with its matrices
        transposed back and NUL padding removed.  A copy.'''
        return _transpose_matrices(copy.deepcopy(self._keywords.get('coords', {}) or {}))

    # ---- pixel masks ------------------------------------------------------

    @property
    def mask_names(self) -> List[str]:
        '''Names of the image's pixel masks (``ia.maskhandler('get')``).'''
        return list((self._keywords.get('masks') or {}).keys())

    @property
    def default_mask(self) -> Optional[str]:
        '''The mask applied by default, None when there is none.'''
        name = self._keywords.get('Image_defaultmask')
        return str(name) if name and name in self.mask_names else None

    def mask(self, name: Optional[str] = None) -> Optional[IndexedArray]:
        '''The pixel mask ``name`` (the default mask when None) as a boolean
        ``PagedArray``, True where a pixel is good; None when the image has
        no default mask.  (An ``IndexedArray``: a ``PagedArray``, or a
        ``ConcatArray`` for a concatenated image.)'''
        if name is None:
            name = self.default_mask
            if name is None:
                return None
        masks = self._keywords.get('masks') or {}
        if name not in masks:
            raise KeyError(f'{self.path}: no mask {name!r} (masks: {list(masks)})')
        record = masks[name]
        if record.get('name') != 'LCPagedMask':
            raise NotImplementedError(f'{self.path}: mask {name!r} is a {record.get("name")!r} region, '
                                      'not a paged mask')
        ### the keyword holds the path the table had when written; the mask is
        ### a subtable of the image, wherever the image is now
        ref = str(record.get('mask', ''))
        sub = os.path.basename(ref[len('Table: '):].rstrip('/')) if ref.startswith('Table: ') else name
        return PagedArray(os.path.join(self.path, sub or name), MASK_COLUMN)
