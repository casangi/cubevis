'''ImageReader (cubevis.imaging.reader): one way to read an image cube
whether it is a CASA image, an xradio image in Zarr or a dataset in hand.

    pytest tests/manual/xarray-image/test_image_reader.py

No CASA.  The xradio part writes its Zarr store with xradio's own
``write_image`` and skips where xradio is not installed.
'''
import os

import numpy as np
import pytest

pytest.importorskip('arcae')
xr = pytest.importorskip('xarray')
pytest.importorskip('zarr')

from cubevis.imaging import ImageCube, ImageInfo, ImageReader, XarrayImageStore      # noqa: E402
from cubevis.xarray import image as ci                                               # noqa: E402

SHAPE = (24, 18, 2, 7)


@pytest.fixture(scope='module')
def pixels():
    rng = np.random.default_rng(20261116)
    values = rng.normal(size=SHAPE).astype(np.float32)
    return values, values > -1.5


@pytest.fixture()
def sources(tmp_path, pixels):
    '''the same image three ways: CASA, Zarr (the dataset saved), in memory'''
    values, good = pixels
    casa = str(tmp_path / 'cube.image')
    ci.write_image(casa, values, units='Jy/beam', beam=(3.0, 2.0, 30.0), masks={'mask0': good},
                   coords=ci.default_coords(SHAPE, reference_direction=(1.0, -0.5), frequencies=None))
    ds = ci.open_casa_image(casa)
    zarr_path = str(tmp_path / 'cube.zarr')
    ds.to_zarr(zarr_path)
    return {'casa-image': casa, 'zarr': zarr_path, 'dataset': ds.load()}


def _same(a, b, where=''):
    if isinstance(a, dict):
        assert isinstance(b, dict) and sorted(a) == sorted(b), (where, a, b)
        for k in a:
            _same(a[k], b[k], f'{where}.{k}')
    elif isinstance(a, str) or a is None or b is None or (isinstance(a, list) and a and isinstance(a[0], str)):
        assert a == b, (where, a, b)
    else:
        assert np.asarray(a).shape == np.asarray(b).shape, where
        assert np.allclose(np.asarray(a, dtype=float), np.asarray(b, dtype=float), rtol=1e-12, atol=1e-12), (where, a, b)


@pytest.mark.parametrize('kind', ['casa-image', 'zarr', 'dataset'])
def test_the_same_answers_from_every_source(sources, pixels, kind):
    values, good = pixels
    reader = ImageReader.open(sources[kind])
    info = reader.info()
    assert isinstance(info, ImageInfo) and info.source_format == kind and info.variable == 'SKY'
    assert info.shape == SHAPE and reader.shape == SHAPE and info.dtype == 'float32'
    assert info.stokes == ('I', 'Q') and info.units == 'Jy/beam' and info.spectral_frame == 'LSRK'
    assert np.allclose(info.frequency, 1.415e9 + 1.0e3 * np.arange(7)) and info.rest_frequency == 1420405751.786
    assert info.has_direction and info.has_beam and info.has_default_mask and info.writable
    assert np.array_equal(reader.plane(1, 3), values[:, :, 1, 3])
    assert np.array_equal(reader.spectrum(5, 6, 1), values[5, 6, 1, :])
    assert np.array_equal(reader.chans(0, 2, 3), values[:, :, 0, 2:5])
    assert reader.pixel(4, 5, 1, 6) == values[4, 5, 1, 6]
    assert np.array_equal(reader.default_mask(), good)
    assert np.array_equal(reader.default_mask_plane(1, 3), good[:, :, 1, 3])
    beam = reader.beam(1, 3)
    assert np.allclose([beam['major'], beam['minor'], beam['pa']], [3.0, 2.0, 30.0])
    stats = reader.statistics(1, 3)
    v = values[:, :, 1, 3][good[:, :, 1, 3]].astype(np.float64)
    assert stats['npts'][0] == v.size and np.isclose(stats['sum'][0], v.sum())
    assert np.isclose(stats['flux'][0], v.sum() / (np.pi / (4 * np.log(2)) * 6.0))
    extra = np.zeros(SHAPE[:2], bool)
    extra[:5] = True
    assert reader.statistics(1, 3, extra)['npts'][0] == (good[:, :, 1, 3] & extra).sum()
    ### the coordinate description is the CASA image's, whatever the source
    _same(reader.coordinates(), ImageReader.open(sources['casa-image']).coordinates())
    assert stats['maxposf'] == ImageReader.open(sources['casa-image']).statistics(1, 3)['maxposf']


@pytest.mark.parametrize('kind', ['casa-image', 'zarr', 'dataset'])
def test_writes(sources, pixels, kind):
    values, _ = pixels
    reader = ImageReader.open(sources[kind])
    expect = values.copy()
    plane = np.arange(24 * 18, dtype=np.float32).reshape(24, 18)
    reader.write_plane(1, 4, plane)
    expect[:, :, 1, 4] = plane
    block = np.full((24, 18, 3), 7.0, np.float32)
    block[2, 3, 1] = -1.0
    reader.write_chans(0, 2, block)
    expect[:, :, 0, 2:5] = block
    again = ImageReader.open(sources[kind])                       # as another reader finds it
    for s in range(2):
        for c in range(7):
            assert np.array_equal(again.plane(s, c), expect[:, :, s, c]), (s, c)
            assert np.array_equal(reader.plane(s, c), expect[:, :, s, c]), (s, c)   # and no stale pixels here


def test_what_another_program_wrote_is_seen(sources, pixels):
    values, _ = pixels
    reader = ImageReader.open(sources['casa-image'])
    assert reader.plane(0, 0)[0, 0] == values[0, 0, 0, 0] and reader.beam()['major'] == 3.0
    ci.CasaImage(sources['casa-image'])[:, :, 0, 0] = 42.0
    assert (reader.plane(0, 0) == 42.0).all()
    ci.write_image(sources['casa-image'], values[:, :, :1, :], units='Jy/beam', beam=(5.0, 4.0, 0.0), overwrite=True)
    assert reader.shape == (24, 18, 1, 7) and reader.beam()['major'] == 5.0 and not reader.info().has_default_mask


def test_images_with_fewer_axes(tmp_path):
    plane = np.arange(30, dtype=np.float32).reshape(6, 5)
    ci.write_image(str(tmp_path / 'two.image'), plane)
    reader = ImageReader.open(str(tmp_path / 'two.image'))
    info = reader.info()
    assert info.shape == (6, 5, 1, 1) and info.stokes == () and info.frequency is None and not info.has_beam
    assert np.array_equal(reader.plane(0, 0), plane) and reader.spectrum(1, 2, 0).shape == (1,)
    assert reader.chans(0, 0, 1).shape == (6, 5, 1) and reader.default_mask() is None
    reader.write_plane(0, 0, plane * 2)
    assert np.array_equal(reader.plane(0, 0), plane * 2)
    with pytest.raises(IndexError):
        reader.plane(1, 0)
    assert reader.statistics(0, 0)['max'][0] == 58.0 and 'flux' not in reader.statistics(0, 0)


def test_what_is_not_an_image(tmp_path, sources):
    with pytest.raises(ValueError):
        ImageReader.open(str(tmp_path))
    with pytest.raises(ValueError):
        ImageReader.open(os.path.join(sources['casa-image'], 'mask0'))
    with pytest.raises(ValueError, match='not a sky image'):
        ImageReader.open(xr.Dataset({'SKY': (('a', 'b'), np.zeros((2, 2)))}))
    with pytest.raises(KeyError):
        ImageReader.open(sources['dataset'], variable='SKY_MODEL')
    lazy = ci.open_casa_image(sources['casa-image'])               # a dataset not in memory
    reader = ImageReader.open(lazy)
    assert not reader.info().writable
    with pytest.raises(PermissionError):
        reader.write_plane(0, 0, np.zeros(SHAPE[:2]))


def test_the_store_and_cube_over_a_zarr_image(sources, pixels, tmp_path):
    '''interactive clean's surface over an image that is not a CASA image'''
    values, good = pixels
    mask_zarr = str(tmp_path / 'mask.zarr')
    ci.open_casa_image(sources['casa-image'], masks=False).pipe(lambda d: d.assign(SKY=d.SKY * 0)).to_zarr(mask_zarr)
    cube = ImageCube(XarrayImageStore(sources['zarr'], mask_zarr), track='ctime')
    assert cube.shape == list(SHAPE) and cube.stokes_labels() == ['I', 'Q']
    assert np.array_equal(cube.channel([1, 3], np.float64), values[:, :, 1, 3].astype(np.float64).T)
    assert np.array_equal(np.squeeze(cube.mask0([1, 3])), good[:, :, 1, 3])
    cube.edit_mask('add', 'chan', [0, 5], polygon={'xs': [2, 9, 9, 2], 'ys': [3, 3, 8, 8]})
    mask = ImageReader.open(mask_zarr)
    assert mask.plane(0, 5)[2:9, 3:8].all() and mask.plane(0, 5).sum() == 35 and mask.plane(0, 4).sum() == 0
    cube.statistics_config(use_mask=True)
    assert cube.statistics([0, 5])['npts'] == (good[:, :, 0, 5][2:9, 3:8]).sum()
    assert cube.coorddesc()['coordinates']['direction']['native_frame'] == 'J2000'


# ---- xradio's own images ------------------------------------------------------------

@pytest.fixture()
def xradio_zarr(tmp_path, pixels):
    '''an image made and written by xradio, with no CASA image behind it'''
    xi = pytest.importorskip('xradio.image', reason='xradio, to write one of its images')
    values, good = pixels
    cell = np.radians(1 / 3600)
    xds = xi.make_empty_sky_image(phase_center=[1.0, -0.5], image_size=[24, 18], cell_size=[cell, cell],
                                  frequency_coords=1.4e9 + 2.0e6 * np.arange(7), pol_coords=['I', 'Q'],
                                  time_coords=[59000.0])
    dims = ('time', 'frequency', 'polarization', 'l', 'm')
    xds['SKY'] = xr.DataArray(values.transpose(3, 2, 0, 1)[None], dims=dims, attrs={'units': 'Jy/beam', 'type': 'sky'})
    xds['FLAG_SKY'] = xr.DataArray(~good.transpose(3, 2, 0, 1)[None], dims=dims, attrs={'type': 'flag'})
    beam = np.empty((1, 7, 2, 3))
    beam[...] = [3 * cell, 2 * cell, np.radians(30.0)]
    xds['BEAM_FIT_PARAMS_SKY'] = xr.DataArray(beam, dims=('time', 'frequency', 'polarization', 'beam_params_label'),
                                              attrs={'units': 'rad'})
    xds.attrs['data_groups'] = {'base': {'sky': 'SKY', 'flag': 'FLAG_SKY', 'beam_fit_params_sky': 'BEAM_FIT_PARAMS_SKY'}}
    written = xi.write_image(xds, str(tmp_path / 'sky.zarr'), out_format='zarr')
    path = written[0] if isinstance(written, (list, tuple)) and written else None
    if not path or not os.path.isdir(path):
        path = next(os.path.join(tmp_path, f) for f in os.listdir(tmp_path) if f.endswith('.zarr'))
    return path


def test_an_xradio_image(xradio_zarr, pixels):
    values, good = pixels
    reader = ImageReader.open(xradio_zarr)
    info = reader.info()
    assert info.source_format == 'zarr' and info.shape == SHAPE and info.stokes == ('I', 'Q')
    assert info.units == 'Jy/beam' and info.has_direction and info.has_beam and info.has_default_mask and info.writable
    assert np.allclose(info.frequency, 1.4e9 + 2.0e6 * np.arange(7))
    assert np.array_equal(reader.plane(1, 3), values[:, :, 1, 3])
    assert np.array_equal(reader.spectrum(5, 6, 1), values[5, 6, 1, :])
    assert np.array_equal(reader.default_mask(), good)
    desc = reader.coordinates()
    d = desc['direction']
    assert d['frame'] == 'FK5' and d['equinox'] == 2000.0 and d['native_frame'] == 'J2000' and d['projection'] == 'SIN'
    assert np.allclose(d['reference_value'], np.degrees([1.0, -0.5]))
    assert np.allclose(d['increment'], [-1 / 3600, 1 / 3600]) and np.allclose(d['reference_pixel'], [12.0, 9.0])
    assert desc['shape'] == list(SHAPE) and desc['stokes'] == ['I', 'Q'] and desc['brightness_unit'] == 'Jy/beam'
    assert np.allclose([desc['beam']['major'], desc['beam']['minor'], desc['beam']['pa']], [3.0, 2.0, 30.0])
    stats = reader.statistics(1, 3)
    v = values[:, :, 1, 3][good[:, :, 1, 3]].astype(np.float64)
    assert stats['npts'][0] == v.size and np.isclose(stats['flux'][0], v.sum() / (np.pi / (4 * np.log(2)) * 6.0))
    assert stats['blcf'].endswith(', Q, 1.406e+09Hz')
    plane = np.ones(SHAPE[:2], np.float32)
    reader.write_plane(0, 2, plane)
    assert np.array_equal(ImageReader.open(xradio_zarr).plane(0, 2), plane)
    cube = ImageCube(XarrayImageStore(xradio_zarr), track='ctime')
    assert np.array_equal(cube.channel([1, 3], np.float64), values[:, :, 1, 3].astype(np.float64).T)
    assert cube.channel_reply([1, 3], stats=True, hist_bins=8)['stats']['npts'] == v.size


def test_xradio_and_this_package_describe_a_casa_image_alike(sources):
    '''xradio's reading of a CASA image and the casa-image engine's: the same
    dataset as far as ImageReader is concerned'''
    xi = pytest.importorskip('xradio.image', reason='xradio, to read the same CASA image')
    try:
        theirs = ImageReader.open(xi.open_image(sources['casa-image']))
    except Exception as ex:                                        # its CASA reader needs its own extras
        pytest.skip(f'xradio could not open the CASA image: {ex}')
    ours = ImageReader.open(sources['casa-image'])
    a, b = ours.info(), theirs.info()
    for field in ('shape', 'stokes', 'units', 'spectral_frame', 'rest_frequency', 'has_beam', 'has_default_mask'):
        assert getattr(a, field) == getattr(b, field), field
    assert np.allclose(a.frequency, b.frequency)
    assert np.array_equal(ours.plane(1, 3), theirs.plane(1, 3)) and np.array_equal(ours.default_mask(), theirs.default_mask())
    da, db = ours.coordinates()['direction'], theirs.coordinates()['direction']
    for key in ('frame', 'equinox', 'projection', 'native_frame'):
        assert da[key] == db[key], key
    for key in ('reference_value', 'reference_pixel', 'increment', 'pc'):
        assert np.allclose(da[key], db[key], rtol=1e-9, atol=1e-9), key
