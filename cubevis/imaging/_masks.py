'''Mask edits as whole operations (design §2.2, step 2).

The browser's mask edits (add or subtract a drawn region or another
channel's mask, clear, invert; one channel or the whole cube) used to be
loops in ``CubeMask``'s ``mask-mod`` handler that read and wrote the mask one
channel at a time through ``ImagePipe``.  ``ImageCube.edit_mask`` now does the
whole edit where the mask is stored: one call per edit, the region sent as its
vertices.  Remotely that is one round trip instead of two per channel.

``polygon_pixels`` is ``cubevis.utils.polygon_indexes`` as arrays, with the
same selection (kept as it was; see the notes on its edges below), computed
in one vectorized pass.  It is here because ``cubevis.imaging`` must not
import ``cubevis.utils`` (Bokeh); ``polygon_indexes`` now calls it.

Pixel layout is the store's: planes are ``[x, y]`` (not the transposed
display layout), indices are 0-based.
'''
from math import floor, ceil
from typing import Optional, Sequence, Tuple

import numpy as np

OPERATIONS = ('add', 'subtract', 'clear', 'fill', 'invert')
SCOPES = ('chan', 'cube')

#: cube-scope edits read and write this many bytes of mask at a time at most
#: (at least one channel); the per-channel loops this replaces never held
#: more than one plane, and a cube's mask can be much larger than memory
CHUNK_BYTES = 64 * 1024 * 1024


def polygon_pixels(xs: Sequence[float], ys: Sequence[float], shape: Sequence[int]) -> Tuple[np.ndarray, np.ndarray]:
    '''``(ix, iy)``: the pixels of a ``shape`` (``[nx, ny]``) plane that the
    polygon with vertices ``xs``, ``ys`` (Bokeh data coordinates) selects.

    The same pixels as ``polygon_indexes`` has always given (checked against
    it): an axis-aligned box (four vertices, two distinct x and two distinct
    y) selects ``floor(x0) <= i < ceil(x1)``; any other polygon selects the
    pixels whose *corner* ``(i, j)`` is inside it (matplotlib's
    ``Path.contains_points``).  In both cases the last column and row
    (``nx - 1``, ``ny - 1``) are never selected.
    '''
    assert len(shape) == 2, 'contains only works for 2D shapes, so "shape" should have length equal to two'
    assert len(xs) == len(ys), 'to specify a polygon the number of X values must equal the number of Y values'
    empty = (np.zeros(0, dtype=np.intp), np.zeros(0, dtype=np.intp))
    if len(xs) == 0:
        return empty

    def grid(x0, x1, y0, y1):
        ix = np.arange(max(floor(x0), 0), min(ceil(x1), shape[0] - 1), dtype=np.intp)
        iy = np.arange(max(floor(y0), 0), min(ceil(y1), shape[1] - 1), dtype=np.intp)
        gx, gy = np.meshgrid(ix, iy, indexing='ij')
        return gx.ravel(), gy.ravel()

    if len(xs) == 4:
        uniqx = sorted(set(xs))
        uniqy = sorted(set(ys))
        if len(uniqx) == 2 and len(uniqy) == 2:
            ### a proper box (Path.contains_point errs slightly with very small regions)
            return grid(uniqx[0], uniqx[1], uniqy[0], uniqy[1])

    gx, gy = grid(max(0, min(xs) - 1), max(xs) + 1, max(0, min(ys) - 1), max(ys) + 1)
    if gx.size == 0:
        return empty
    from matplotlib.path import Path        # not at import: cubevis.imaging stays light
    inside = Path(list(zip(xs, ys))).contains_points(np.column_stack((gx, gy)))
    return gx[inside], gy[inside]


def region_pixels(xs: Sequence[float], ys: Sequence[float], shape: Sequence[int]) -> Tuple[np.ndarray, np.ndarray]:
    '''The pixels a drawn region edits: ``polygon_pixels``, or, when that is
    empty because the whole region lies within one pixel, that pixel (as
    ``CubeMask``'s handler did).'''
    ix, iy = polygon_pixels(xs, ys, shape)
    if ix.size == 0 and len(xs) > 0 and len(xs) == len(ys):
        ux = set(map(int, xs))
        uy = set(map(int, ys))
        if len(ux) == len(uy) == 1:
            return np.array([ux.pop()], dtype=np.intp), np.array([uy.pop()], dtype=np.intp)
    return ix, iy


def apply(op: str, planes: np.ndarray, pixels=None, modifier=None) -> np.ndarray:
    '''One edit on mask planes ``[x, y]`` or ``[x, y, n]`` (modified in place
    where possible; the result is returned).  ``pixels`` is ``(ix, iy)`` for a
    region, ``modifier`` a ``[x, y]`` mask for another channel's mask.'''
    if op == 'clear':
        planes[...] = 0
    elif op == 'fill':
        planes[...] = 1
    elif op == 'invert':
        planes = (planes == 0).astype(planes.dtype)
    elif pixels is not None:
        planes[pixels[0], pixels[1], ...] = 1 if op == 'add' else 0
    else:
        mod = np.asarray(modifier) != 0
        if planes.ndim == 3:
            mod = mod[:, :, np.newaxis]
        if op == 'add':
            planes = np.logical_or(planes != 0, mod).astype(planes.dtype)
        else:
            planes = np.logical_and(planes != 0, np.logical_not(mod)).astype(planes.dtype)
    return planes


class MaskEditor:
    '''``ImageCube.edit_mask``: reads and writes through the cube's store,
    whole channel ranges at a time when the store can
    (``read_mask_chans``/``write_mask_chans``), else plane by plane.'''

    def __init__(self, store, shape):
        self._store = store
        self._shape = list(shape)

    # ---- store access -------------------------------------------------

    def _plane(self, stokes, chan):
        p = np.asarray(self._store.read_mask_plane([stokes, chan]))
        return p.reshape(p.shape[0], p.shape[1])

    def _write(self, stokes, chan, plane):
        self._store.write_mask_plane([stokes, chan], self._writable(plane))

    @staticmethod
    def _writable(pixels):
        ### casatools' putchunk does not take bools
        return pixels.astype(np.uint8) if pixels.dtype == bool else pixels

    def _chunks(self, nchan):
        nx, ny = self._shape[0], self._shape[1]
        step = max(1, CHUNK_BYTES // max(1, nx * ny * 8))
        for start in range(0, nchan, step):
            yield start, min(step, nchan - start)

    def _cube(self, stokes, edit, skip=None):
        '''``edit`` every channel of ``stokes`` but ``skip``.'''
        nchan = self._shape[3]
        ranged = hasattr(self._store, 'read_mask_chans') and hasattr(self._store, 'write_mask_chans')
        for start, count in self._chunks(nchan):
            if ranged:
                planes = np.asarray(self._store.read_mask_chans(stokes, start, count))
                planes = planes.reshape(planes.shape[0], planes.shape[1], count)
                keep = None
                if skip is not None and start <= skip < start + count:
                    keep = planes[:, :, skip - start].copy()
                planes = edit(planes)
                if keep is not None:
                    planes[:, :, skip - start] = keep
                self._store.write_mask_chans(stokes, start, self._writable(planes))
            else:
                for c in range(start, start + count):
                    if c != skip:
                        self._write(stokes, c, edit(self._plane(stokes, c)))

    # ---- the edit -----------------------------------------------------

    def edit(self, op: str, scope: str, index: Sequence[int], *,
             polygon: Optional[dict] = None, src: Optional[Sequence[int]] = None) -> dict:
        if op not in OPERATIONS:
            raise ValueError(f'mask edit: unknown operation {op!r}')
        if scope not in SCOPES:
            raise ValueError(f'mask edit: unknown scope {scope!r}')
        stokes, chan = int(index[0]), int(index[1])
        update = {}

        if op in ('add', 'subtract'):
            if polygon is not None:
                pixels = region_pixels(polygon['xs'], polygon['ys'], self._shape[:2])
                edit = lambda planes: apply(op, planes, pixels=pixels)
                skip = None
            elif src is not None:
                src = [int(src[0]), int(src[1])]
                if scope == 'chan' and src == [stokes, chan]:
                    ### a channel with itself: subtract clears it (and the browser's
                    ### copied region no longer matches the mask); add changes nothing
                    if op == 'subtract':
                        self._write(stokes, chan, apply('clear', self._plane(stokes, chan)))
                        update['clear_region'] = True
                    return update
                modifier = self._plane(src[0], src[1])
                edit = lambda planes: apply(op, planes, modifier=modifier)
                ### never add/subtract the modifier mask with itself
                skip = src[1] if src[0] == stokes else None
            else:
                raise ValueError('mask edit: add/subtract needs a polygon or a source channel')
        else:
            edit = lambda planes: apply(op, planes)
            skip = None

        if scope == 'chan':
            self._write(stokes, chan, edit(self._plane(stokes, chan)))
        else:
            self._cube(stokes, edit, skip)
        return update
