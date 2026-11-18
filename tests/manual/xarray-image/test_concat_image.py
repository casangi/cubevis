'''Concatenated CASA images (ia.imageconcat) through cubevis.xarray.image
and ImageReader.

    pytest tests/manual/xarray-image/test_concat_image.py

The first part needs no CASA: the parts and the concatenation are written
here.  The second compares with casatools on concatenations casatools made
(every mode), and skips where casatools is not installed.
'''
import os
import pickle
import shutil

import numpy as np
import pytest

pytest.importorskip('arcae')
xr = pytest.importorskip('xarray')

from cubevis.imaging import ImageCube, ImageReader, XarrayImageStore          # noqa: E402
from cubevis.xarray import image as ci                                        # noqa: E402

co = ci.coords
SHAPE = (12, 10, 2)


def _part(path, nchan, f0, *, df=2.0e6, mask=False, beam=(3.0, 2.0, 30.0), seed=0, stokes=None):
    rng = np.random.default_rng(seed)
    shape = SHAPE + (nchan,)
    pixels = rng.normal(size=shape).astype(np.float32)
    coords = ci.default_coords(shape, reference_direction=(1.2, -0.7), reference_frequency=f0, channel_width=df,
                               stokes=stokes)
    ci.write_image(path, pixels, coords=coords, units='Jy/beam', beam=beam,
                   masks={'mask0': pixels > -1.0} if mask else None, overwrite=True)
    return pixels, (pixels > -1.0) if mask else np.ones(shape, bool)


@pytest.fixture()
def parts(tmp_path):
    '''three images whose channels run on from each other; the middle one
    has no mask'''
    made = [_part(str(tmp_path / 'a.im'), 3, 1.400e9, mask=True, seed=1),
            _part(str(tmp_path / 'b.im'), 4, 1.406e9, seed=2),
            _part(str(tmp_path / 'c.im'), 2, 1.414e9, mask=True, seed=3)]
    paths = [str(tmp_path / n) for n in ('a.im', 'b.im', 'c.im')]
    return paths, np.concatenate([m[0] for m in made], axis=3), np.concatenate([m[1] for m in made], axis=3)


# ---- without CASA -------------------------------------------------------------

def test_a_concatenation_is_one_image(parts, tmp_path):
    paths, pixels, good = parts
    cat = ci.concat_images(str(tmp_path / 'cat.im'), paths)
    assert isinstance(cat, ci.ConcatImage) and isinstance(ci.CasaImage(cat.path), ci.ConcatImage)
    assert ci.is_casa_image(cat.path) and ci.is_concat_image(cat.path) and not ci.is_paged_image(cat.path)
    assert cat.shape == SHAPE + (9,) and cat.dtype == np.float32 and cat.axis == 3 and len(cat.members) == 3
    assert cat.units == 'Jy/beam' and cat.mask_names == ['mask0'] and cat.default_mask == 'mask0'
    assert np.array_equal(cat[...], pixels)
    for key in (np.s_[:, :, 0, 4], np.s_[5, 5, 1, :], np.s_[3:9, ::-1, 1, 1:8:2], np.s_[..., 2:4], np.s_[0, 0, 0, 8],
                np.s_[:, :, :, ::-1], np.s_[2:2], np.s_[..., 6:]):
        assert np.array_equal(cat[key], pixels[key]) and cat[key].shape == pixels[key].shape, key
        assert np.array_equal(cat.mask()[key], good[key]), key
    assert cat.mask().dtype == bool
    ### channels run on evenly: the first part's linear axis, for all nine
    spectral = cat.coords_record()['spectral2']
    assert 'wcs' in spectral and 'tabular' not in spectral
    assert np.allclose(co.spectral_values(spectral, 9), 1.4e9 + 2.0e6 * np.arange(9))
    assert co.restoring_beam(cat.imageinfo)['major'] == {'value': 3.0, 'unit': 'arcsec'}
    assert sorted(os.path.basename(os.path.dirname(f)) + '/' + os.path.basename(f) for f in cat.files()) == \
        ['a.im/table.dat', 'b.im/table.dat', 'c.im/table.dat', 'cat.im/imageconcat.json']
    assert cat.modified() >= max(ci.CasaImage(p).modified() for p in paths)
    with cat.hold():
        assert np.array_equal(cat[1, 2, 0, :], pixels[1, 2, 0, :])
    assert np.array_equal(pickle.loads(pickle.dumps(cat))[1, 2, 1, :], pixels[1, 2, 1, :])


def test_writes_go_to_the_parts(parts, tmp_path):
    paths, pixels, _ = parts
    cat = ci.concat_images(str(tmp_path / 'cat.im'), paths)
    expect = pixels.copy()
    cat[:, :, 1, 2:5] = 7.0                                   # the last channel of a, the first two of b
    expect[:, :, 1, 2:5] = 7.0
    plane = np.arange(120, dtype=np.float32).reshape(12, 10)
    cat[:, :, 0, 8] = plane                                   # the last channel of c
    expect[:, :, 0, 8] = plane
    assert np.array_equal(cat[...], expect)
    assert np.array_equal(ci.CasaImage(paths[0])[...], expect[..., 0:3])
    assert np.array_equal(ci.CasaImage(paths[1])[...], expect[..., 3:7])
    assert np.array_equal(ci.CasaImage(paths[2])[...], expect[..., 7:9])
    mask = cat.mask()
    mask[0, 0, 0, 0:3] = False
    assert not ci.CasaImage(paths[0]).mask()[0, 0, 0, :].any()
    with pytest.raises(PermissionError):                      # b has no mask to write to
        mask[0, 0, 0, 3] = False


def test_gaps_differing_beams_and_no_beam(tmp_path):
    a = str(tmp_path / 'a.im')
    g = str(tmp_path / 'g.im')
    n = str(tmp_path / 'n.im')
    _part(a, 3, 1.400e9)
    _part(g, 3, 1.500e9, beam=(5.0, 4.0, 10.0))               # a gap in frequency, and another beam
    _part(n, 2, 1.506e9, beam=None)
    cat = ci.concat_images(str(tmp_path / 'gap.im'), [a, g])
    spectral = cat.coords_record()['spectral2']
    expect = [1.400e9, 1.402e9, 1.404e9, 1.500e9, 1.502e9, 1.504e9]
    assert 'wcs' not in spectral and spectral['tabular']['worldvalues'] == expect
    assert spectral['tabular']['cdelt'] == [(1.504e9 - 1.400e9) / 5] and spectral['tabular']['pixelvalues'] == [0.0, 1.0, 2.0, 3.0, 4.0, 5.0]
    assert np.allclose(co.spectral_values(spectral, 6), expect)
    beam = co.restoring_beam(cat.imageinfo)
    assert beam['nChannels'] == 6 and beam['nStokes'] == 2
    assert np.isclose(beam['beams']['*2']['*1']['major']['value'], 3.0) and np.isclose(beam['beams']['*3']['*0']['major']['value'], 5.0)
    assert cat.mask() is None and cat.mask_names == []
    reader = ImageReader.open(cat.path)
    assert np.allclose(reader.info().frequency, expect) and not reader.info().has_default_mask
    assert np.isclose(reader.beam(0, 1)['major'], 3.0) and np.isclose(reader.beam(1, 4)['major'], 5.0)
    assert co.restoring_beam(ci.concat_images(str(tmp_path / 'nobeam.im'), [g, n]).imageinfo) is None


def test_other_axes_and_nesting(tmp_path):
    i = str(tmp_path / 'i.im')
    q = str(tmp_path / 'q.im')
    rng = np.random.default_rng(4)
    pi, pq = (rng.normal(size=(12, 10, 1, 3)).astype(np.float32) for _ in range(2))
    ci.write_image(i, pi, coords=ci.default_coords(pi.shape, stokes=['I']))
    ci.write_image(q, pq, coords=ci.default_coords(pq.shape, stokes=['Q']))
    cat = ci.concat_images(str(tmp_path / 'iq.im'), [i, q], axis=2)
    assert cat.shape == (12, 10, 2, 3) and cat.coords_record()['stokes1']['stokes'] == ['I', 'Q']
    assert np.array_equal(cat[...], np.concatenate([pi, pq], axis=2))
    assert list(ci.open_casa_image(cat.path).polarization.values) == ['I', 'Q']
    ### a concatenation of concatenations, and parts kept inside the directory
    a, b, c = (str(tmp_path / n) for n in ('a.im', 'b.im', 'c.im'))
    pa, pb, pc = (_part(p, 2, f, seed=s)[0] for p, f, s in ((a, 1.400e9, 5), (b, 1.404e9, 6), (c, 1.408e9, 7)))
    ab = ci.concat_images(str(tmp_path / 'ab.im'), [a, b])
    abc = ci.concat_images(str(tmp_path / 'abc.im'), [ab.path, c])
    assert abc.shape == SHAPE + (6,) and np.array_equal(abc[...], np.concatenate([pa, pb, pc], axis=3))
    assert len(abc.files()) == 5
    inside = str(tmp_path / 'inside.im')
    os.makedirs(inside)
    shutil.copytree(a, os.path.join(inside, 'a.im'))
    shutil.copytree(b, os.path.join(inside, 'b.im'))
    with open(os.path.join(inside, 'imageconcat.json'), 'w', encoding='utf-8') as f:
        f.write('{"Version": 1, "DataType": "float", "Axis": 3, "TempClose": false,\n'
                ' "Images": ["././a.im", "././b.im"], "MiscInfo": {}}')
    shutil.move(inside, str(tmp_path / 'moved.im'))            # it goes where its directory goes
    assert np.array_equal(ci.CasaImage(str(tmp_path / 'moved.im'))[...], np.concatenate([pa, pb], axis=3))


def test_what_does_not_join(parts, tmp_path):
    paths, _, _ = parts
    other = str(tmp_path / 'other.im')
    ci.write_image(other, np.zeros((8, 8, 2, 3), np.float32))
    with pytest.raises(ValueError, match='does not join'):
        ci.concat_images(str(tmp_path / 'bad.im'), [paths[0], other])
    cat = ci.concat_images(str(tmp_path / 'cat.im'), paths)
    with pytest.raises(FileExistsError):
        ci.concat_images(cat.path, paths)
    shutil.rmtree(paths[1])
    with pytest.raises(ValueError, match='could not be opened'):
        ci.CasaImage(cat.path)


def test_dataset_reader_and_cube(parts, tmp_path):
    paths, pixels, good = parts
    cat = ci.concat_images(str(tmp_path / 'cat.residual'), paths)
    ds = xr.open_dataset(cat.path, engine='casa-image')
    assert ds.SKY.shape == (1, 9, 2, 12, 10) and np.array_equal(ds.SKY.values[0], pixels.transpose(3, 2, 0, 1))
    assert np.array_equal(ds.FLAG_SKY.values[0], ~good.transpose(3, 2, 0, 1))
    assert ds.attrs['casa_image']['concatenated'] == paths
    reader = ImageReader.open(cat.path)
    info = reader.info()
    assert info.source_format == 'casa-image' and info.shape == SHAPE + (9,) and info.writable and info.has_default_mask
    assert np.array_equal(reader.plane(1, 5), pixels[:, :, 1, 5]) and np.array_equal(reader.spectrum(3, 4, 0), pixels[3, 4, 0, :])
    assert reader.statistics(0, 1)['npts'][0] == good[:, :, 0, 1].sum() and reader.statistics(0, 4)['npts'][0] == 120
    reader.write_chans(0, 2, np.full((12, 10, 3), 5.0))        # across a and b
    assert (ci.CasaImage(paths[0])[:, :, 0, 2] == 5.0).all() and (ci.CasaImage(paths[1])[:, :, 0, :2] == 5.0).all()
    ### a part rewritten with another beam, as an imager would: the description follows
    assert reader.beam(0, 4)['major'] == pytest.approx(3.0)
    _part(paths[1], 4, 1.406e9, beam=(6.0, 5.0, 0.0), seed=9)
    assert reader.beam(0, 4)['major'] == pytest.approx(6.0) and reader.beam(0, 0)['major'] == pytest.approx(3.0)
    assert reader.modified() >= os.path.getctime(os.path.join(paths[1], 'table.dat'))
    ### interactive clean's surface over a concatenated residual and a paged mask
    mask_path = str(tmp_path / 'cat.mask')
    ci.write_image(mask_path, np.zeros(SHAPE + (9,), np.float32), coords=cat.coords_record())
    cube = ImageCube(XarrayImageStore(cat.path, mask_path), track='ctime')
    assert cube.shape == list(SHAPE + (9,))
    assert np.array_equal(cube.channel([1, 8], np.float64), ci.CasaImage(cat.path)[:, :, 1, 8].astype(np.float64).T)
    cube.edit_mask('add', 'chan', [0, 5], polygon={'xs': [2, 9, 9, 2], 'ys': [3, 3, 8, 8]})
    assert ci.CasaImage(mask_path)[:, :, 0, 5].sum() == 35


# ---- against casatools ------------------------------------------------------------

def _casatools():
    return pytest.importorskip('casatools', reason='casatools (and its data) for the comparison')


def _plain(value):
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items() if k != 'conversion'}
    return value.tolist() if hasattr(value, 'tolist') else value


@pytest.mark.parametrize('mode', ['nomove', 'copy', 'move', 'paged'])
@pytest.mark.parametrize('case', ['even', 'gap'])
def test_what_casatools_concatenated(tmp_path, mode, case):
    ct = _casatools()
    a, b, c = (str(tmp_path / n) for n in ('a.im', 'b.im', 'c.im'))
    _part(a, 3, 1.400e9, mask=True, seed=1)
    _part(b, 4, 1.406e9 if case == 'even' else 1.500e9, seed=2, beam=(3.0, 2.0, 30.0) if case == 'even' else (5.0, 4.0, 10.0))
    _part(c, 2, 1.414e9 if case == 'even' else 1.508e9, mask=True, seed=3, beam=(3.0, 2.0, 30.0) if case == 'even' else (5.0, 4.0, 10.0))
    out = str(tmp_path / 'cat.im')
    ia = ct.image()
    made = ia.imageconcat(outfile=out, infiles=[a, b, c], axis=3, relax=True, tempclose=False, overwrite=True, mode=mode)
    made.done()
    ia.close()
    im = ci.CasaImage(out)
    assert isinstance(im, ci.ConcatImage) == (mode != 'paged')
    ia.open(out)
    try:
        assert list(im.shape) == list(ia.shape())
        assert np.array_equal(im[...], ia.getchunk())
        mask = im.mask()
        assert np.array_equal(np.ones(im.shape, bool) if mask is None else mask[...], ia.getchunk(getmask=True))
        mine, theirs = im.coords_record(), _plain(ia.coordsys().torecord())
        for key in co.coordinate_keys(mine):
            assert _plain(mine[key]) == theirs[key], key
        assert im.units == ia.brightnessunit()
        cs = ia.coordsys()
        freq = co.spectral_values(mine['spectral2'], im.shape[3])
        assert np.allclose(freq, [cs.toworld([0, 0, 0, ch])['numeric'][3] for ch in range(im.shape[3])], rtol=1e-13, atol=0)
        nchan = im.shape[3]
        ours = co.beam_array(im.imageinfo, nchan, 2)
        for chan in (0, 3, nchan - 1):
            beam = ia.restoringbeam(channel=chan, polarization=1)
            assert np.isclose(np.degrees(ours[chan, 1, 0]) * 3600, beam['major']['value']), chan
            assert np.isclose(np.degrees(ours[chan, 1, 2]), beam['positionangle']['value']), chan
        ### a write here is what casatools then reads, and the other way round
        im[:, :, 1, 2:5] = 7.0
    finally:
        ia.close()
    ia.open(out)
    assert (ia.getchunk()[:, :, 1, 2:5] == 7.0).all()
    ia.putchunk(np.full([12, 10, 1, 1], 9.0, np.float32), blc=[0, 0, 0, 6])
    ia.close()
    assert (ci.CasaImage(out)[:, :, 0, 6] == 9.0).all()


def test_casatools_opens_a_concatenation_made_here(parts, tmp_path):
    ct = _casatools()
    paths, pixels, good = parts
    cat = ci.concat_images(str(tmp_path / 'cat.im'), paths)
    ia = ct.image()
    ia.open(cat.path)
    try:
        assert list(ia.shape()) == list(cat.shape)
        assert np.array_equal(ia.getchunk(), pixels) and np.array_equal(ia.getchunk(getmask=True), good)
    finally:
        ia.close()
