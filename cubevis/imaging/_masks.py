'''Mask edits as whole operations (design §2.2, step 2).

The browser's mask edits (add or subtract a drawn region or another
channel's mask, clear, invert; one channel or the whole cube) used to be
loops in ``CubeMask``'s ``mask-mod`` handler that read and wrote the mask one
channel at a time through ``ImagePipe``.  ``ImageCube.edit_mask`` now does the
whole edit where the mask is stored: one call per edit, the region sent as its
vertices.  Remotely that is one round trip instead of two per channel.

``polygon_pixels`` selects a region's pixels by their centres, in one
vectorized pass.  It is here because ``cubevis.imaging`` must not import
``cubevis.utils`` (Bokeh); ``cubevis.utils.polygon_indexes`` calls it.

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
    '''``(ix, iy)``: the pixels of a ``shape`` (``[nx, ny]``) plane whose
    **centre** lies inside the polygon with vertices ``xs``, ``ys`` (Bokeh data
    coordinates).

    The image glyph is drawn at ``x=0, y=0`` with ``dw, dh`` equal to the shape,
    so pixel ``i`` covers ``[i, i+1]`` and its centre is ``i + 0.5`` (the same
    convention as the browser's world coordinates, design §2.5.2).  An
    axis-aligned box selects ``x0 <= i + 0.5 < x1`` (and the same in y),
    computed exactly; any other polygon uses matplotlib's
    ``Path.contains_points`` at the centres.  Every pixel of the plane can be
    selected, the last column and row included.

    Until 2026-11-07 (notes §66) non-box polygons tested the pixel's corner
    ``(i, j)``, which moved the selection half a pixel down and to the left of
    the drawn region, boxes took every pixel they overlapped, and the last
    column and row could never be selected.
    '''
    assert len(shape) == 2, 'contains only works for 2D shapes, so "shape" should have length equal to two'
    assert len(xs) == len(ys), 'to specify a polygon the number of X values must equal the number of Y values'
    empty = (np.zeros(0, dtype=np.intp), np.zeros(0, dtype=np.intp))
    if len(xs) == 0:
        return empty

    def centres(lo, hi, n):
        '''the indices i, 0 <= i < n, with lo <= i + 0.5 < hi'''
        return np.arange(max(ceil(lo - 0.5), 0), min(ceil(hi - 0.5), n), dtype=np.intp)

    def grid(ix, iy):
        gx, gy = np.meshgrid(ix, iy, indexing='ij')
        return gx.ravel(), gy.ravel()

    if len(xs) == 4:
        uniqx = sorted(set(xs))
        uniqy = sorted(set(ys))
        if len(uniqx) == 2 and len(uniqy) == 2:
            ### a proper box: exact, no point-in-polygon test
            return grid(centres(uniqx[0], uniqx[1], shape[0]), centres(uniqy[0], uniqy[1], shape[1]))

    gx, gy = grid(centres(min(xs), max(xs) + 1, shape[0]), centres(min(ys), max(ys) + 1, shape[1]))
    if gx.size == 0:
        return empty
    from matplotlib.path import Path        # not at import: cubevis.imaging stays light
    inside = Path(list(zip(xs, ys))).contains_points(np.column_stack((gx + 0.5, gy + 0.5)))
    return gx[inside], gy[inside]


def region_pixels(xs: Sequence[float], ys: Sequence[float], shape: Sequence[int]) -> Tuple[np.ndarray, np.ndarray]:
    '''The pixels a drawn region edits: ``polygon_pixels``, or, when the region
    holds no pixel centre (it is smaller than a pixel, or a thin sliver), the
    pixel under the middle of its bounding box, if that is on the plane.'''
    ix, iy = polygon_pixels(xs, ys, shape)
    if ix.size == 0 and len(xs) > 0 and len(xs) == len(ys):
        x = floor((min(xs) + max(xs)) / 2)
        y = floor((min(ys) + max(ys)) / 2)
        if 0 <= x < shape[0] and 0 <= y < shape[1]:
            return np.array([x], dtype=np.intp), np.array([y], dtype=np.intp)
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
