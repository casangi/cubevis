'''XarrayImageStore (cubevis.imaging): interactive clean's image access
without casatools, over ImageReader.

    pytest tests/manual/xarray-image/test_xarray_store.py

The first part needs no CASA: the images are written with cubevis.xarray.image's
writer.  The second compares XarrayImageStore with
CasatoolsImageStore call by call, and an ImageCube over each reply by reply,
on images casatools made; it skips where casatools is not installed.
'''
import shutil

import numpy as np
import pytest

pytest.importorskip('arcae')

from cubevis.imaging import (XarrayImageStore, CasatoolsImageStore, ImageCube, ImageStore, image_store,   # noqa: E402
                             image_store_name, open_image_cube)
from cubevis.xarray import image as ci                                                                # noqa: E402

SHAPE = (24, 18, 2, 7)


def _pixels():
    rng = np.random.default_rng(20261116)
    pixels = rng.normal(size=SHAPE).astype(np.float32)
    return pixels, pixels > -1.5, (pixels > 1.0).astype(np.float32)


@pytest.fixture()
def images(tmp_path):
    '''residual (with mask0, beam, unit) and a mask image, written here'''
    pixels, good, mask = _pixels()
    coords = ci.default_coords(SHAPE, reference_direction=(1.0, -0.5))
    image = str(tmp_path / 'f.residual')
    ci.write_image(image, pixels, units='Jy/beam', beam=(3.0, 2.0, 30.0), masks={'mask0': good}, coords=coords)
    mask_path = str(tmp_path / 'f.mask')
    ci.write_image(mask_path, mask, coords=coords)
    return image, mask_path, pixels, good, mask


# ---- without CASA -------------------------------------------------------------

def test_is_an_image_store(images):
    image, mask_path, pixels, good, mask = images
    store = XarrayImageStore(image, mask_path)
    assert isinstance(store, ImageStore)
    assert image_store('xarray') is XarrayImageStore and image_store('arcae') is XarrayImageStore
    assert image_store('casatools') is CasatoolsImageStore
    assert image_store() is XarrayImageStore                         # the default
    with pytest.raises(ValueError):
        image_store('fits')
    assert store.shape() == list(SHAPE) and store.stokes_labels() == ['I', 'Q']
    plane = store.read_plane([1, 3])
    assert plane.shape == (24, 18, 1, 1) and plane.dtype == np.float64
    assert np.array_equal(plane[:, :, 0, 0], pixels[:, :, 1, 3])
    assert np.array_equal(store.read_spectrum([5, 6, 1]).ravel(), pixels[5, 6, 1, :])
    assert store.has_mask() and store.has_default_mask()
    assert np.array_equal(store.read_default_mask(), good)
    assert np.array_equal(store.read_mask_plane([0, 2])[:, :, 0, 0], mask[:, :, 0, 2])
    assert np.array_equal(store.read_mask_chans(1, 2, 3)[:, :, 0, :], mask[:, :, 1, 2:5])
    assert np.array_equal(store.read_mask_spectrum([5, 6, 0]).ravel(), mask[5, 6, 0, :])
    assert store.mask_pixel([1, 3], [4, 5]) == int(mask[4, 5, 1, 3])
    assert store.modified() > 0
    store.close()
    assert store.shape() == list(SHAPE) and store.has_mask()         # opens again


def test_environment_chooses_the_store(images, monkeypatch):
    image, mask_path = images[0], images[1]
    assert isinstance(open_image_cube(image, mask_path).store, XarrayImageStore)
    monkeypatch.setenv('CUBEVIS_IMAGE_STORE', 'casatools')           # the way back
    assert image_store() is CasatoolsImageStore
    assert isinstance(open_image_cube(image, mask_path).store, CasatoolsImageStore)
    assert isinstance(open_image_cube(image, mask_path, store='xarray').store, XarrayImageStore)
    monkeypatch.delenv('CUBEVIS_IMAGE_STORE')
    import cubevis.imaging._xarray_store as xs
    monkeypatch.setattr(xs, 'missing_modules', lambda: ['arcae'])     # no arcae: casatools, as before
    assert image_store() is CasatoolsImageStore


def test_the_log_says_which_store(monkeypatch, caplog):
    import logging
    import cubevis.imaging._session as session
    import cubevis.imaging._xarray_store as xs
    logger = logging.getLogger('cubevis')
    monkeypatch.setattr(logger, 'propagate', True)                   # so caplog hears it
    caplog.set_level(logging.DEBUG, logger='cubevis')
    monkeypatch.setattr(session, '_told', set())
    assert image_store_name() == 'xarray'
    assert 'XarrayImageStore' in caplog.text and 'the default' in caplog.text and 'arcae' in caplog.text
    count = len(caplog.records)
    image_store()
    assert len(caplog.records) == count                              # said once
    monkeypatch.setenv('CUBEVIS_IMAGE_STORE', 'casatools')
    assert image_store_name() == 'casatools'
    assert 'CasatoolsImageStore' in caplog.records[-1].getMessage() and caplog.records[-1].levelno == logging.DEBUG
    assert 'CUBEVIS_IMAGE_STORE=casatools' in caplog.records[-1].getMessage()
    ### arcae missing: casatools stands in, and that is said at INFO, without CUBEVIS_DEBUG
    monkeypatch.delenv('CUBEVIS_IMAGE_STORE')
    monkeypatch.setattr(xs, 'missing_modules', lambda: ['arcae'])
    assert image_store_name() == 'casatools'
    last = caplog.records[-1]
    assert last.levelno == logging.INFO and 'arcae not installed' in last.getMessage() and 'cubevis[image]' in last.getMessage()


def test_statistics(images):
    image, mask_path, pixels, good, mask = images
    store = XarrayImageStore(image, mask_path)
    for use_mask in (False, True):
        s = store.statistics([1, 3], mask_path if use_mask else None)
        sel = good[:, :, 1, 3] & ((mask[:, :, 1, 3] >= 0.5) if use_mask else True)
        v = pixels[:, :, 1, 3][sel].astype(np.float64)
        assert s['npts'][0] == v.size and np.isclose(s['sum'][0], v.sum()) and np.isclose(s['mean'][0], v.mean())
        assert np.isclose(s['rms'][0], np.sqrt((v ** 2).mean())) and np.isclose(s['sigma'][0], v.std(ddof=1))
        assert s['min'][0] == v.min() and s['max'][0] == v.max()
        assert pixels[s['maxpos'][0], s['maxpos'][1], 1, 3] == v.max() and list(s['maxpos'][2:]) == [1, 3]
        assert list(s['blc']) == [0, 0, 1, 3] and list(s['trc']) == [23, 17, 1, 3]
        assert np.isclose(s['flux'][0], v.sum() / (np.pi / (4 * np.log(2)) * 6.0))     # 3" x 2" beam, 1" pixels


def test_mask_writes(images):
    image, mask_path, pixels, good, mask = images
    store = XarrayImageStore(image, mask_path)
    expect = mask.copy()
    plane = np.zeros((24, 18))
    plane[3:9, 2:5] = 1
    store.write_mask_plane([1, 4], plane)
    expect[:, :, 1, 4] = plane
    chans = np.ones((24, 18, 3))
    store.write_mask_chans(0, 2, chans)
    expect[:, :, 0, 2:5] = 1
    assert np.array_equal(ci.CasaImage(mask_path)[...], expect)
    assert np.array_equal(XarrayImageStore(image, mask_path).read_mask_plane([1, 4])[:, :, 0, 0], plane)


def test_no_mask_writes_while_a_cycle_runs(images):
    """the imager has the mask during a cycle, and within one process arcae
    and casatools cannot see each other's table locks"""
    from cubevis.imaging import LocalImagingSession, testing as T
    image, mask_path, _, _, mask = images
    dec = T.ScriptedDeconvolver(imagename='guard', imsize=[24, 18], nchan=7, niter=20, cycleniter=10)
    session = LocalImagingSession(dec, store_factory=lambda *_: XarrayImageStore(image, mask_path))
    field = session.fields()[0]['id']
    store = session.cube(field).store
    assert store.write_guard == session.cycle_running and not session.cycle_running()
    plane = np.ones((24, 18))
    seen = {}
    step = dec._step

    def stepping():
        seen['running'] = session.cycle_running()
        for name, call in (('plane', lambda: store.write_mask_plane([0, 1], plane)),
                           ('chans', lambda: store.write_mask_chans(0, 1, np.ones((24, 18, 2)))),
                           ('edit', lambda: session.cube(field).edit_mask('fill', 'chan', [0, 1]))):
            try:
                call()
                seen[name] = 'written'
            except RuntimeError as ex:
                seen[name] = str(ex)
        seen['read'] = store.read_mask_plane([0, 1]).shape           # reading goes on
        return step()

    dec._step = stepping
    session.start()
    assert seen['running'] is True and seen['read'] == (24, 18, 1, 1)
    for name in ('plane', 'chans', 'edit'):
        assert 'a clean cycle is running' in seen[name], (name, seen[name])
    assert np.array_equal(ci.CasaImage(mask_path)[...], mask)        # nothing was written
    assert not session.cycle_running()
    store.write_mask_plane([0, 1], plane)                            # and afterwards it is
    assert (ci.CasaImage(mask_path)[:, :, 0, 1] == 1).all()
    ### a store on its own (createmask, a script) has no guard
    assert XarrayImageStore(image, mask_path).write_guard is None


def test_mask_shape_and_missing_images(images, tmp_path):
    image, mask_path = images[0], images[1]
    other = str(tmp_path / 'other.mask')
    ci.write_image(other, np.zeros((8, 8, 1, 2), np.float32))
    store = XarrayImageStore(image, other)
    with pytest.raises(RuntimeError, match='mismatch'):
        store.shape()
    with pytest.raises(RuntimeError, match='could not open image'):
        XarrayImageStore(str(tmp_path / 'nothing.residual')).shape()
    ### a store made before the imager wrote the residual (iclean makes its cubes first)
    late = XarrayImageStore(str(tmp_path / 'late.residual'), None)
    shutil.copytree(image, str(tmp_path / 'late.residual'))
    assert late.shape() == list(SHAPE) and not late.has_mask()
    late.open_mask(mask_path)
    assert late.has_mask()


def test_the_image_rewritten_between_calls(images):
    '''what tclean does each cycle: new pixels, then a restoring beam'''
    image, mask_path, pixels, _, _ = images
    store = XarrayImageStore(image, mask_path)
    assert store.read_plane([0, 0])[0, 0, 0, 0] == pixels[0, 0, 0, 0]
    assert store.coordinates()['beam']['major'] == 3.0
    ci.CasaImage(image)[:, :, 0, 0] = 42.0
    assert (store.read_plane([0, 0]) == 42.0).all()
    ### the image made again with another beam: keywords are read again
    shutil.rmtree(image)
    ci.write_image(image, pixels * 2, units='Jy/beam', beam=(5.0, 4.0, 0.0))
    assert store.coordinates()['beam']['major'] == 5.0 and not store.has_default_mask()
    assert np.array_equal(store.read_plane([1, 2])[:, :, 0, 0], (pixels * 2)[:, :, 1, 2])


def test_image_cube_over_the_store(images):
    image, mask_path, pixels, good, mask = images
    cube = ImageCube(XarrayImageStore(image, mask_path), track='ctime')
    assert cube.shape == list(SHAPE) and cube.stokes_labels() == ['I', 'Q']
    assert np.array_equal(cube.channel([1, 3], np.float64), pixels[:, :, 1, 3].astype(np.float64).T)      # [y, x], for Bokeh
    reply = cube.channel_reply([1, 3], stats=True, hist_bins=16)
    assert reply['stats'] and reply['hist']
    assert cube.have_mask() and cube.have_mask0()
    assert np.array_equal(np.squeeze(cube.mask0([1, 3])).astype(bool), good[:, :, 1, 3])
    box = {'xs': [2, 9, 9, 2], 'ys': [3, 3, 8, 8]}
    cube.edit_mask('clear', 'cube', [0, 0])
    cube.edit_mask('add', 'chan', [0, 5], polygon=box)
    after = ci.CasaImage(mask_path)[...]
    assert after[:, :, 0, :5].sum() == 0 and after[2:9, 3:8, 0, 5].all() and after[:, :, 0, 5].sum() == 7 * 5
    assert np.array_equal(after[:, :, 1, :], mask[:, :, 1, :])       # the other Stokes plane untouched
    desc = cube.coorddesc()
    assert isinstance(desc['csys'], dict) and 'direction0' in desc['csys'] and desc['shape'] == SHAPE
    header, text = cube.fits_header()
    assert header['NAXIS1'] == 24 and header['BUNIT'] == 'Jy/beam' and 'CRVAL1' in text


# ---- against CasatoolsImageStore ---------------------------------------------------

def _casatools():
    return pytest.importorskip('casatools', reason='casatools (and its data) for the comparison')


@pytest.fixture()
def casa_pair(tmp_path):
    '''an image and a mask made by casatools as tclean lays them out (radians,
    a beam, mask0), twice: one copy for each store'''
    ct = _casatools()
    ia = ct.image()
    pixels, _, mask = _pixels()
    made = []
    for who in ('ct', 'ar'):
        image, mask_path = str(tmp_path / f'{who}.residual'), str(tmp_path / f'{who}.mask')
        ia.fromshape(image, list(SHAPE), overwrite=True)
        ia.putchunk(pixels)
        cs = ia.coordsys()
        cs.setunits(['rad', 'rad', '', 'Hz'])
        cs.setreferencevalue([1.2, -0.7, 1.0, 1.4e9])
        cs.setincrement([-4.8e-6, 4.8e-6, 1.0, 2.0e6])
        cs.setreferencepixel([12.0, 9.0, 0.0, 0.0])
        ia.setcoordsys(cs.torecord())
        ia.setbrightnessunit('Jy/beam')
        ia.setrestoringbeam(major='3arcsec', minor='2arcsec', pa='30deg')
        ia.calcmask(f'"{image}" > -1.5', name='mask0')
        record = ia.coordsys().torecord()
        ia.close()
        ia.fromshape(mask_path, list(SHAPE), csys=record, overwrite=True)
        ia.putchunk(mask)
        ia.close()
        made.append((image, mask_path))
    return made


def _equal(a, b, where=''):
    if isinstance(a, dict):
        assert isinstance(b, dict) and sorted(a) == sorted(b), (where, sorted(a), sorted(b) if isinstance(b, dict) else b)
        for k in a:
            _equal(a[k], b[k], f'{where}.{k}')
    elif isinstance(a, str) or isinstance(b, str) or a is None or b is None:
        assert a == b, (where, a, b)
    elif isinstance(a, (list, tuple)) and any(isinstance(v, (dict, str)) for v in a):
        assert len(a) == len(b), where
        for i, (x, y) in enumerate(zip(a, b)):
            _equal(x, y, f'{where}[{i}]')
    else:
        x, y = np.asarray(a), np.asarray(b)
        assert x.shape == y.shape, (where, x.shape, y.shape)
        if x.dtype.kind in 'fc':
            ### 1e-7: casacore sums the squares of float32 pixels differently (rms, sumsq at 1e-9)
            assert np.allclose(x, y, rtol=1e-7, atol=0, equal_nan=True), (where, a, b)
        else:
            assert np.array_equal(x, y), (where, a, b)


def test_the_two_stores_agree(casa_pair):
    (ct_image, ct_mask), (ar_image, ar_mask) = casa_pair
    ct, ar = CasatoolsImageStore(ct_image, ct_mask), XarrayImageStore(ar_image, ar_mask)
    try:
        assert ct.shape() == ar.shape() and ct.stokes_labels() == ar.stokes_labels()
        assert ct.has_mask() == ar.has_mask() and ct.has_default_mask() == ar.has_default_mask()
        for index in ([0, 0], [1, 3], [0, 6]):
            a, b = ct.read_plane(index), ar.read_plane(index)
            assert a.shape == b.shape and a.dtype == b.dtype and np.array_equal(a, b)
            a, b = ct.read_mask_plane(index), ar.read_mask_plane(index)
            assert a.shape == b.shape and a.dtype == b.dtype and np.array_equal(a, b)
            assert ct.mask_pixel(index, [4, 5]) == ar.mask_pixel(index, [4, 5])
            for mp_ct, mp_ar in ((None, None), (ct_mask, ar_mask)):
                _equal(ct.statistics(index, mp_ct), ar.statistics(index, mp_ar), f'statistics{index}')
        for index in ([0, 0, 0], [5, 6, 1], [23, 17, 1]):
            for name in ('read_spectrum', 'read_mask_spectrum'):
                a, b = getattr(ct, name)(index), getattr(ar, name)(index)
                assert a.shape == b.shape and a.dtype == b.dtype and np.array_equal(a, b), name
        a, b = ct.read_mask_chans(1, 2, 4), ar.read_mask_chans(1, 2, 4)
        assert a.shape == b.shape and a.dtype == b.dtype and np.array_equal(a, b)
        a, b = ct.read_default_mask(), ar.read_default_mask()
        assert a.shape == b.shape and a.dtype == b.dtype and np.array_equal(a, b)
        _equal(ct.coordinates(), ar.coordinates(), 'coordinates')
        _equal(ct.coordsys().torecord(), ar.coordsys(), 'coordsys')
        ### writes: the same calls, then each mask read with the other library
        plane = np.zeros((24, 18))
        plane[3:9, 2:5] = 1
        for store in (ct, ar):
            store.write_mask_plane([1, 4], plane)
            store.write_mask_chans(0, 1, np.ones((24, 18, 2)))
    finally:
        ct.close()
        ar.close()
    by_casatools = CasatoolsImageStore(ar_image, ar_mask)
    try:
        by_casatools.shape()                                         # (it needs its image opened first)
        theirs = by_casatools.read_mask_chans(0, 0, 7), by_casatools.read_mask_chans(1, 0, 7)
    finally:
        by_casatools.close()
    by_arcae = XarrayImageStore(ct_image, ct_mask)
    ours = by_arcae.read_mask_chans(0, 0, 7), by_arcae.read_mask_chans(1, 0, 7)
    assert np.array_equal(theirs[0], ours[0]) and np.array_equal(theirs[1], ours[1])
    assert ours[0][:, :, 0, 1:3].all() and np.array_equal(ours[1][:, :, 0, 4], plane)


def test_image_cubes_agree(casa_pair):
    (ct_image, ct_mask), (ar_image, ar_mask) = casa_pair
    ct = ImageCube(CasatoolsImageStore(ct_image, ct_mask), track='ctime')
    ar = ImageCube(XarrayImageStore(ar_image, ar_mask), track='ctime')
    try:
        box = {'xs': [2, 9, 9, 2], 'ys': [3, 3, 8, 8]}
        steps = [('add', 'chan', [0, 5], {'polygon': box}), ('invert', 'chan', [1, 2], {}),
                 ('subtract', 'cube', [0, 0], {'polygon': {'xs': [4, 6, 5], 'ys': [4, 4, 7]}}),
                 ('add', 'chan', [1, 3], {'src': [1, 2]}), ('fill', 'chan', [0, 1], {}), ('clear', 'cube', [1, 0], {})]
        for step in [None] + steps:
            if step is not None:
                op, scope, index, kw = step
                _equal(ct.edit_mask(op, scope, index, **kw), ar.edit_mask(op, scope, index, **kw), f'edit {step}')
            for index in ([0, 0], [1, 3], [0, 5], [1, 2]):
                for cube in (ct, ar):
                    cube.statistics_config(use_mask=(step is not None))
                _equal(ct.channel_reply(index, stats=True, hist_bins=16), ar.channel_reply(index, stats=True, hist_bins=16),
                       f'channel_reply{index} after {step}')
                _equal(ct.mask(index), ar.mask(index), f'mask{index} after {step}')
                _equal(ct.mask0(index), ar.mask0(index), f'mask0{index}')
            _equal(ct.spectrum([5, 6, 1]), ar.spectrum([5, 6, 1]), 'spectrum')
            _equal(ct.spectrum([5, 6, 0], mask=True), ar.spectrum([5, 6, 0], mask=True), 'mask spectrum')
        _equal(ct.coordinates(), ar.coordinates(), 'coordinates')
    finally:
        ct.close()
        ar.close()


def test_casatools_rewrites_the_residual_in_this_process(casa_pair):
    '''the imager and the store share a process: the store must see each
    cycle's pixels (a held arcae handle would not: test_casa_image.py)'''
    ct = _casatools()
    _, (image, mask_path) = casa_pair
    cube = ImageCube(XarrayImageStore(image, mask_path), track='generation')
    before = np.squeeze(cube.channel([0, 0], np.float64)).copy()
    ia = ct.image()
    for cycle in range(3):
        ia.open(image)
        ia.putchunk(np.full(SHAPE, float(cycle), np.float32))
        ia.close()
        cube.touch()
        assert (np.squeeze(cube.channel([0, 0], np.float64)) == float(cycle)).all()
        assert cube.channel_reply([1, 6], stats=True)['stats']['max'] == float(cycle)
    assert not np.array_equal(before, np.squeeze(cube.channel([0, 0], np.float64)))
