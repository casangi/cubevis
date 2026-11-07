'''Mask edits in the imaging layer (cubevis.imaging._masks, ImageCube.edit_mask;
design step 2) without CASA.

    pytest tests/manual/iclean-imaging/test_masks.py
'''
import sys
import subprocess
from itertools import product
from math import floor, ceil

import numpy as np
import pytest

from cubevis.imaging import ImageCube, polygon_pixels
from cubevis.imaging import _masks


def polygon_indexes_before(xs, ys, shape):
    '''cubevis.utils.polygon_indexes as it was until 2026-11-07 (the reference)'''
    from matplotlib.path import Path
    if len(xs) == 4 and len(ys) == 4:
        uniqx = sorted(set(xs))
        uniqy = sorted(set(ys))
        if len(uniqx) == 2 and len(uniqy) == 2:
            return product(range(max(floor(uniqx[0]), 0), min(ceil(uniqx[1]), shape[0] - 1)),
                           range(max(floor(uniqy[0]), 0), min(ceil(uniqy[1]), shape[1] - 1)))
    path = Path(list(zip(xs, ys)))
    xmin, xmax = max([0, min(xs) - 1]), max(xs) + 1
    ymin, ymax = max([0, min(ys) - 1]), max(ys) + 1
    return filter(path.contains_point, product(range(max(floor(xmin), 0), min(ceil(xmax), shape[0] - 1)),
                                               range(max(floor(ymin), 0), min(ceil(ymax), shape[1] - 1))))


def as_set(ix, iy):
    return set(zip(map(int, ix), map(int, iy)))


# ---- rasterization ------------------------------------------------------

def test_polygon_pixels_match_the_old_selection():
    rng = np.random.default_rng(20261107)
    shape = (60, 45)
    cases = []
    for _ in range(400):                                      # polygons, some leaving the plane
        n = int(rng.integers(3, 9))
        cases.append((list(rng.uniform(-10, 70, n)), list(rng.uniform(-10, 55, n))))
    for _ in range(200):                                      # boxes, some tiny, some at the edges
        x0, y0 = rng.uniform(-5, 62), rng.uniform(-5, 47)
        w, h = rng.choice([0.3, 1.0, 4.5, 30.0]), rng.choice([0.4, 1.0, 7.25, 50.0])
        cases.append(([x0, x0 + w, x0 + w, x0], [y0, y0, y0 + h, y0 + h]))
    cases += [([0, 60, 60, 0], [0, 0, 45, 45]),                # the whole plane
              ([10, 20, 20], [10, 10, 30]),                    # integer vertices on pixel corners
              ([5.5, 5.5, 5.5, 5.5], [3.2, 3.2, 3.2, 3.2])]    # degenerate
    for xs, ys in cases:
        assert as_set(*polygon_pixels(xs, ys, shape)) == set(polygon_indexes_before(xs, ys, shape)), (xs, ys)


def test_polygon_pixels_edges_kept_as_they_were():
    ''''the last column and row are never selected (the old behaviour, kept)'''
    ix, iy = polygon_pixels([0, 10, 10, 0], [0, 0, 8, 8], (10, 8))
    assert ix.max() == 8 and iy.max() == 6
    assert polygon_pixels([], [], (10, 8))[0].size == 0


def test_utils_polygon_indexes_delegates():
    from cubevis.utils import polygon_indexes
    xs, ys = [3.2, 17.9, 11.1], [2.5, 4.0, 19.3]
    got = list(polygon_indexes(xs, ys, (30, 30)))
    assert set(got) == set(polygon_indexes_before(xs, ys, (30, 30)))
    assert all(type(i) is int and type(j) is int for i, j in got)


def test_region_within_one_pixel_selects_it():
    ix, iy = _masks.region_pixels([40.2, 40.8, 40.8, 40.2], [10.3, 10.3, 10.7, 10.7], (64, 64))
    assert as_set(ix, iy) == {(40, 10)}


# ---- edits ----------------------------------------------------------------

class PlaneStore:
    '''An ImageStore's mask side over numpy, plane by plane only.'''

    def __init__(self, mask):
        self.maskarr = np.array(mask, dtype=np.float64)
        self.image_path = 'img.residual'
        self.mask_path = 'img.mask'
        self.plane_reads = self.plane_writes = 0

    def shape(self):
        return list(self.maskarr.shape)

    def has_mask(self):
        return True

    def read_mask_plane(self, index):
        self.plane_reads += 1
        s, c = index
        return self.maskarr[:, :, s:s + 1, c:c + 1].copy()

    def write_mask_plane(self, index, pixels):
        self.plane_writes += 1
        s, c = index
        self.maskarr[:, :, s, c] = np.asarray(pixels).reshape(self.maskarr.shape[:2])


class RangedStore(PlaneStore):
    '''... with read_mask_chans/write_mask_chans.'''

    def __init__(self, mask):
        super().__init__(mask)
        self.range_calls = []

    def read_mask_chans(self, stokes, start, count):
        self.range_calls.append(('r', stokes, start, count))
        return self.maskarr[:, :, stokes:stokes + 1, start:start + count].copy()

    def write_mask_chans(self, stokes, start, pixels):
        self.range_calls.append(('w', stokes, start, pixels.shape[-1]))
        self.maskarr[:, :, stokes, start:start + pixels.shape[-1]] = pixels.reshape(self.maskarr.shape[0], self.maskarr.shape[1], -1)


def start_mask(shape=(20, 16, 2, 7), seed=3):
    return (np.random.default_rng(seed).random(shape) > 0.6).astype(np.float64)


BOX = {'xs': [4, 9, 9, 4], 'ys': [3, 3, 8, 8]}
TRI = {'xs': [2.2, 15.7, 8.1], 'ys': [1.4, 4.9, 13.6]}


def expected(mask, op, scope, index, polygon=None, src=None):
    '''What the per-channel loops in CubeMask's handler did (numpy, [x, y, stokes, chan])'''
    m = mask.copy()
    s, c = index
    chans = [c] if scope == 'chan' else range(m.shape[3])
    if op in ('add', 'subtract') and polygon is not None:
        pix = tuple(np.array(list(polygon_indexes_before(polygon['xs'], polygon['ys'], m.shape[:2]))).T)
        for ch in chans:
            m[:, :, s, ch][pix] = 1 if op == 'add' else 0
    elif op in ('add', 'subtract'):
        mod = mask[:, :, src[0], src[1]] != 0
        for ch in chans:
            if scope == 'chan' and [s, ch] == list(src):
                if op == 'subtract':
                    m[:, :, s, ch] = 0
                continue
            if scope == 'cube' and s == src[0] and ch == src[1]:
                continue
            cur = m[:, :, s, ch] != 0
            m[:, :, s, ch] = np.logical_or(cur, mod) if op == 'add' else np.logical_and(cur, ~mod)
    elif op == 'clear':
        for ch in chans:
            m[:, :, s, ch] = 0
    elif op == 'fill':
        for ch in chans:
            m[:, :, s, ch] = 1
    elif op == 'invert':
        for ch in chans:
            m[:, :, s, ch] = (m[:, :, s, ch] == 0)
    return m


EDITS = [
    ('add', 'chan', [0, 2], dict(polygon=BOX)),
    ('add', 'cube', [1, 0], dict(polygon=TRI)),
    ('subtract', 'chan', [1, 5], dict(polygon=TRI)),
    ('subtract', 'cube', [0, 3], dict(polygon=BOX)),
    ('add', 'chan', [0, 4], dict(src=[0, 1])),
    ('add', 'chan', [0, 4], dict(src=[1, 6])),
    ('subtract', 'chan', [1, 0], dict(src=[0, 2])),
    ('add', 'cube', [0, 0], dict(src=[0, 3])),
    ('subtract', 'cube', [1, 2], dict(src=[1, 6])),
    ('add', 'cube', [1, 2], dict(src=[0, 6])),
    ('add', 'chan', [0, 3], dict(src=[0, 3])),
    ('subtract', 'chan', [0, 3], dict(src=[0, 3])),
    ('clear', 'chan', [1, 1], {}),
    ('clear', 'cube', [0, 5], {}),
    ('fill', 'cube', [1, 0], {}),
    ('invert', 'chan', [0, 6], {}),
    ('invert', 'cube', [1, 4], {}),
]


@pytest.mark.parametrize('store_type', [PlaneStore, RangedStore])
@pytest.mark.parametrize('op,scope,index,kw', EDITS)
def test_edit_mask_does_what_the_loops_did(store_type, op, scope, index, kw):
    mask = start_mask()
    store = store_type(mask)
    update = ImageCube(store).edit_mask(op, scope, index, **kw)
    np.testing.assert_array_equal(store.maskarr, expected(mask, op, scope, index, **kw))
    assert update == ({'clear_region': True} if (op == 'subtract' and kw.get('src') == index) else {})


def test_edits_in_sequence_and_chunked(monkeypatch):
    mask = start_mask((30, 25, 2, 11), seed=7)
    want = mask.copy()
    whole, chunked = RangedStore(mask), RangedStore(mask)
    for op, scope, index, kw in EDITS:
        want = expected(want, op, scope, index, **kw)
        ImageCube(whole).edit_mask(op, scope, index, **kw)
    np.testing.assert_array_equal(whole.maskarr, want)
    monkeypatch.setattr(_masks, 'CHUNK_BYTES', 30 * 25 * 8 * 3)        # three channels at a time
    for op, scope, index, kw in EDITS:
        ImageCube(chunked).edit_mask(op, scope, index, **kw)
    np.testing.assert_array_equal(chunked.maskarr, want)
    counts = [c[3] for c in chunked.range_calls if c[0] == 'r']
    assert max(counts) == 3 and 2 in counts                          # 11 channels: 3+3+3+2


def test_cube_edit_with_ranges_reads_no_planes():
    store = RangedStore(start_mask())
    ImageCube(store).edit_mask('invert', 'cube', [0, 0])
    assert store.plane_reads == 0 and store.plane_writes == 0
    assert [c[0] for c in store.range_calls] == ['r', 'w']


def test_written_values_are_zero_and_one():
    store = RangedStore(start_mask() * 3.0)                          # any non-zero is "in the mask"
    cube = ImageCube(store)
    cube.edit_mask('add', 'cube', [0, 0], src=[0, 1])
    cube.edit_mask('invert', 'chan', [1, 2])
    edited = [c for c in range(store.maskarr.shape[3]) if c != 1]     # the source channel is not edited
    assert set(np.unique(store.maskarr[:, :, 0, edited])) <= {0.0, 1.0}
    assert set(np.unique(store.maskarr[:, :, 0, 1])) == {0.0, 3.0}
    assert set(np.unique(store.maskarr[:, :, 1, 2])) <= {0.0, 1.0}


@pytest.mark.parametrize('args,kw', [(('bogus', 'chan', [0, 0]), {}),
                                     (('add', 'bogus', [0, 0]), dict(polygon=BOX)),
                                     (('add', 'chan', [0, 0]), {})])
def test_bad_edits_raise(args, kw):
    with pytest.raises(ValueError):
        ImageCube(PlaneStore(start_mask())).edit_mask(*args, **kw)


def test_edit_without_a_mask_raises():
    class NoMask(PlaneStore):
        def has_mask(self):
            return False
    with pytest.raises(RuntimeError):
        ImageCube(NoMask(start_mask())).edit_mask('clear', 'chan', [0, 0])


def test_importing_the_imaging_layer_does_not_load_matplotlib():
    code = ('import sys, cubevis.imaging; '
            'print(sorted(m for m in sys.modules if m.split(".")[0] in ("matplotlib", "bokeh", "casatools")))')
    out = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, check=True).stdout
    assert out.strip() == '[]'
