'''The casa-image xarray engine (cubevis.xarray.image): CASA images read
and written with arcae.

    pytest tests/manual/xarray-image/test_casa_image.py

The first part needs arcae, numpy and xarray only: its images are written
here by the package's own writer.  The second part (``casatools``) compares
with casatools in both directions -- images casatools made, read here; images
made here, read by casatools -- and skips where casatools is not installed.

The package is loaded by path, not through ``import cubevis``: it must work
without Bokeh and the rest of cubevis.
'''
import importlib
import os
import pickle
import sys
import types

import numpy as np
import pytest

pytest.importorskip('arcae')
xr = pytest.importorskip('xarray')

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))


def _load():
    '''cubevis.xarray.image without running cubevis/__init__.py'''
    if 'cubevis' not in sys.modules:
        for name, sub in (('cubevis', 'cubevis'), ('cubevis.xarray', 'cubevis/xarray')):
            module = types.ModuleType(name)
            module.__path__ = [os.path.join(ROOT, sub)]
            sys.modules[name] = module
    return importlib.import_module('cubevis.xarray.image')


ci = _load()
co = ci.coords


@pytest.fixture(scope='module')
def cube(tmp_path_factory):
    '''a 4-axis image with a default mask, a beam and a unit'''
    rng = np.random.default_rng(20261116)
    pixels = rng.normal(size=(24, 18, 2, 7)).astype(np.float32)
    good = pixels > -1.5
    path = str(tmp_path_factory.mktemp('img') / 'cube.image')
    ci.write_image(path, pixels, units='Jy/beam', beam=(3.0, 2.0, 30.0), masks={'mask0': good},
                   object_name='test', coords=ci.default_coords(pixels.shape, reference_direction=(1.0, -0.5)))
    return path, pixels, good


# ---- CasaImage ------------------------------------------------------------

def test_stands_alone():
    '''nothing of cubevis but the package itself, no CASA, no Bokeh'''
    loaded = [m for m in sys.modules if m.startswith('cubevis.') and not m.startswith('cubevis.xarray')]
    assert loaded == [] or 'cubevis.imaging' in sys.modules      # (another test file imported cubevis)
    import ast
    allowed = ('numpy', 'xarray', 'arcae', 'astropy')
    folder = os.path.join(ROOT, 'cubevis', 'xarray', 'image')
    for name in os.listdir(folder):
        if not name.endswith('.py'):
            continue
        with open(os.path.join(folder, name), encoding='utf-8') as f:
            tree = ast.parse(f.read())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert node.level <= 1, (name, node.module)                  # only its own modules
                modules = [] if node.level else [node.module]
            elif isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            else:
                continue
            for module in modules:
                top = module.split('.')[0]
                assert top in sys.stdlib_module_names or top in allowed, (name, module)


def test_describes_the_image(cube):
    path, pixels, _ = cube
    im = ci.CasaImage(path)
    assert im.shape == pixels.shape and im.dtype == np.float32 and im.ndim == 4
    assert im.units == 'Jy/beam' and im.mask_names == ['mask0'] and im.default_mask == 'mask0'
    assert im.imageinfo['objectname'] == 'test'
    assert ci.is_casa_image(path) and not ci.is_casa_image(os.path.join(path, 'mask0'))
    assert not ci.is_casa_image(os.path.dirname(path)) and not ci.is_casa_image(None)
    with pytest.raises(ValueError, match='Paged Array'):
        ci.CasaImage(os.path.join(path, 'mask0'))


@pytest.mark.parametrize('key', [
    np.s_[...], np.s_[:, :, 0, 3], np.s_[5, 6, 1, :], np.s_[5, 6, 1, 2], np.s_[::-2, 3:17:4, -1, 1:7:3],
    np.s_[2:2], np.s_[..., 0], np.s_[3], np.s_[:, :, 1:2, 4:5], np.s_[-3:, :2], np.s_[10:4:-1, ..., ::-1],
])
def test_indexing_is_numpy_indexing(cube, key):
    path, pixels, good = cube
    im = ci.CasaImage(path)
    assert np.array_equal(im[key], pixels[key]) and im[key].shape == pixels[key].shape
    mask = im.mask()
    assert mask.dtype == bool and np.array_equal(mask[key], good[key])


def test_index_errors(cube):
    im = ci.CasaImage(cube[0])
    for bad in (np.s_[24], np.s_[0, 0, 0, 0, 0], np.s_[[1, 2]], np.s_[..., ...]):
        with pytest.raises(IndexError):
            im[bad]


def test_writes_are_partial_and_exact(cube, tmp_path):
    _, pixels, _ = cube
    path = str(tmp_path / 'w.image')
    im = ci.write_image(path, pixels)
    expect = pixels.copy()
    im[2:5, 3, 1, :] = 9.0
    expect[2:5, 3, 1, :] = 9.0
    plane = np.arange(24 * 18, dtype=np.float32).reshape(24, 18)
    im[:, :, 0, 6] = plane
    expect[:, :, 0, 6] = plane
    im[20:, 15:, :, 2:4] = np.float64(-1.25)                    # cast to the image's type
    expect[20:, 15:, :, 2:4] = -1.25
    with im.hold(readonly=False):                               # one handle for a burst
        for c in range(3):
            im[0, 0, 0, c] = c + 100
            expect[0, 0, 0, c] = c + 100
    assert np.array_equal(ci.CasaImage(path)[...], expect)
    with pytest.raises(IndexError):
        im[::2] = 0
    with pytest.raises(TypeError):
        im[0, 0, 0, 0] = 1j


def test_hold_is_reentrant(cube):
    path, pixels, _ = cube
    im = ci.CasaImage(path)
    with im.hold():
        with im.hold():
            assert np.array_equal(im[:, :, 0, 0], pixels[:, :, 0, 0])
        assert np.array_equal(im[1, 2, 1, :], pixels[1, 2, 1, :])
        with pytest.raises(RuntimeError):
            with im.hold(readonly=False):
                pass
    assert im._held is None
    assert np.array_equal(pickle.loads(pickle.dumps(im))[3, 4, 1, :], pixels[3, 4, 1, :])


@pytest.mark.parametrize('dtype', [np.float32, np.float64, np.complex64, np.complex128])
def test_pixel_types(tmp_path, dtype):
    values = (np.arange(6 * 5 * 3).reshape(6, 5, 1, 3) * (1 + (2j if np.dtype(dtype).kind == 'c' else 0))).astype(dtype)
    im = ci.write_image(str(tmp_path / 't.image'), values)
    assert im.dtype == np.dtype(dtype) and np.array_equal(im[...], values)


def test_two_axis_image(tmp_path):
    values = np.arange(30, dtype=np.float32).reshape(6, 5)
    im = ci.write_image(str(tmp_path / 'two.image'), values)
    assert im.shape == (6, 5) and np.array_equal(im[:, 2], values[:, 2])
    ds = ci.open_casa_image(im.path)
    assert ds.SKY.dims == ('time', 'l', 'm') and np.array_equal(ds.SKY.values[0], values)


# ---- the coordinate system record ---------------------------------------------

def test_coordinate_record_round_trip(tmp_path):
    '''matrices (pc) are what arcae cannot write as JSON and reads transposed'''
    shape = (8, 6, 2, 4)
    record = ci.default_coords(shape, frequencies=[1.0e9, 1.1e9, 1.3e9, 1.6e9], stokes=['XX', 'YY'])
    record['direction0']['pc'] = [[0.8, -0.6], [0.6, 0.8]]
    record['direction0']['projection_parameters'] = []
    im = ci.create_image(str(tmp_path / 'r.image'), shape, coords=record)
    back = im.coords_record()
    assert back['direction0']['pc'] == [[0.8, -0.6], [0.6, 0.8]]
    assert back['direction0']['projection_parameters'] == []
    assert back['stokes1']['stokes'] == ['XX', 'YY'] and back['stokes1']['pc'] == [[1.0]]
    assert back == record
    info = co.direction(back['direction0'])
    assert info['pc'] == [[0.8, 0.6], [-0.6, 0.8]]             # FITS PCi_j: the transpose


def test_axes_and_values():
    shape = (8, 6, 2, 4)
    record = ci.default_coords(shape, reference_frequency=1.4e9, channel_width=2.0e6, rest_frequency=1.42e9)
    axes = co.axes(record, 4)
    assert [a.kind for a in axes] == ['direction', 'direction', 'stokes', 'spectral']
    assert [a.name for a in axes] == ['Right Ascension', 'Declination', 'Stokes', 'Frequency']
    assert co.find_axis(axes, 'spectral') == 3 and co.find_axis(axes, 'direction', 1) == 1
    assert co.find_axis(axes, 'linear') is None
    freq = co.spectral_values(record['spectral2'], 4)
    assert np.allclose(freq, [1.4e9, 1.402e9, 1.404e9, 1.406e9])
    vel = co.velocity_values(freq, 1.42e9, 'radio')
    assert np.allclose(vel, co.SPEED_OF_LIGHT * (1 - freq / 1.42e9))
    assert np.allclose(co.velocity_values(freq, 1.42e9, 'optical'), co.SPEED_OF_LIGHT * (1.42e9 / freq - 1))
    ### tabular: the listed values, and a line beyond them
    tab = ci.default_coords(shape, frequencies=[1.0e9, 1.1e9, 1.3e9, 1.6e9])['spectral2']
    assert np.allclose(co.spectral_values(tab, 4), [1.0e9, 1.1e9, 1.3e9, 1.6e9])
    assert np.allclose(co.spectral_values(tab, 6), [1.0e9, 1.1e9, 1.3e9, 1.6e9, 1.9e9, 2.2e9])
    ### axes in another order, and one removed
    swapped = dict(record, pixelmap1=[3], pixelmap2=[2])
    assert [a.kind for a in co.axes(swapped, 4)] == ['direction', 'direction', 'spectral', 'stokes']
    assert [a.kind for a in co.axes(dict(record, pixelmap1=[-1], pixelmap2=[2]), 3)] == ['direction', 'direction', 'spectral']
    info = co.direction(dict(record['direction0'], units=["'", "'"], crval=[60.0, -30.0], cdelt=[-1.0, 1.0]))
    assert np.allclose(info['reference_value'], np.radians([1.0, -0.5]))
    assert np.allclose(info['increment'], np.radians([-1 / 60, 1 / 60]))


def test_beams():
    one = {'restoringbeam': ci.beam_record(3.0, 2.0, 30.0)}
    assert co.restoring_beam(one)['major'] == {'value': 3.0, 'unit': 'arcsec'}
    arr = co.beam_array(one, 4, 2)
    assert arr.shape == (4, 2, 3) and np.allclose(arr[2, 1], np.radians([3 / 3600, 2 / 3600, 30.0]))
    flat = {'perplanebeams': {'nChannels': 3, 'nStokes': 2,
                              **{f'*{i}': ci.beam_record(10 + i, 1.0, 0.0) for i in range(6)}}}
    beam = co.restoring_beam(flat)
    assert beam['beams']['*2']['*0']['major']['value'] == 12.0        # channel varies fastest
    assert beam['beams']['*0']['*1']['major']['value'] == 13.0
    assert co.restoring_beam({}) is None and co.beam_array({'imagetype': 'Intensity'}, 1, 1) is None
    area = co.beam_area_pixels(np.radians([3 / 3600, 2 / 3600, 0]), np.radians([-1 / 3600, 1 / 3600]))
    assert np.isclose(area, np.pi / (4 * np.log(2)) * 6.0)


# ---- the xarray engine ----------------------------------------------------------

def test_dataset_layout(cube):
    path, pixels, good = cube
    ds = xr.open_dataset(path, engine=ci.CasaImageBackendEntrypoint)
    assert ds.SKY.dims == ('time', 'frequency', 'polarization', 'l', 'm')
    assert dict(ds.sizes) == {'time': 1, 'frequency': 7, 'polarization': 2, 'l': 24, 'm': 18, 'beam_params_label': 3}
    assert list(ds.data_vars) == ['SKY', 'FLAG_SKY', 'BEAM_FIT_PARAMS_SKY']
    assert ds.SKY.dtype == np.float32 and ds.FLAG_SKY.dtype == bool
    assert ds.SKY.attrs['units'] == 'Jy/beam' and ds.SKY.attrs['flag'] == 'FLAG_SKY'
    assert ds.SKY.attrs['beam_fit_params'] == 'BEAM_FIT_PARAMS_SKY' and ds.SKY.attrs['object_name'] == 'test'
    assert list(ds.polarization.values) == ['I', 'Q']
    assert np.allclose(ds.frequency.values, 1.415e9 + 1.0e3 * np.arange(7)) and ds.frequency.attrs['frame'] == 'LSRK'
    rest = ds.frequency.attrs['rest_frequency']
    assert rest['attrs'] == {'units': 'Hz', 'type': 'quantity'}                  # as xradio writes quantities
    assert np.allclose(ds.velocity.values, co.SPEED_OF_LIGHT * (1 - ds.frequency.values / rest['data']))
    info = ds.attrs['coordinate_system_info']
    assert info['reference_direction']['data'] == [1.0, -0.5] and info['projection'] == 'SIN'
    assert info['reference_direction']['attrs'] == {'units': 'rad', 'frame': 'fk5', 'type': 'sky_coord', 'equinox': 'j2000.0'}
    assert ds.attrs['data_groups'] == {'base': {'sky': 'SKY', 'beam_fit_params_sky': 'BEAM_FIT_PARAMS_SKY', 'flag': 'FLAG_SKY'}}
    cell = np.radians(1 / 3600)
    assert np.allclose(ds.l.values, -(np.arange(24) - 12) * cell) and np.allclose(ds.m.values, (np.arange(18) - 9) * cell)
    assert np.allclose(ds.BEAM_FIT_PARAMS_SKY.values[0, 3, 1], np.radians([3 / 3600, 2 / 3600, 30.0]))
    assert ds.time.values[0] == 51544.0 and ds.time.attrs['scale'] == 'utc'
    assert ds.attrs['coordinate_system'] == ci.CasaImage(path).coords_record()
    assert ds.attrs['casa_image']['pixel_axis_of_dim'] == {'frequency': 3, 'polarization': 2, 'l': 0, 'm': 1}
    assert ds.attrs['schema']['deviations']
    ### values, in the dataset's order
    expect = pixels.transpose(3, 2, 0, 1)[None]
    assert np.array_equal(ds.SKY.values, expect)
    assert np.array_equal(ds.FLAG_SKY.values, ~good.transpose(3, 2, 0, 1)[None])       # True = not valid
    assert np.array_equal(ds.SKY.where(~ds.FLAG_SKY).count().values, good.sum())


def test_dataset_reads_only_what_is_asked(cube, monkeypatch):
    path, pixels, _ = cube
    reads = []
    original = ci.PagedArray.__getitem__

    def spy(self, key):
        out = original(self, key)
        reads.append(out.size)
        return out

    monkeypatch.setattr(ci.PagedArray, '__getitem__', spy)
    ds = ci.open_casa_image(path)
    assert reads == []                                                  # opening reads no pixels
    plane = ds.SKY.isel(time=0, frequency=3, polarization=1).values
    assert reads == [24 * 18] and np.array_equal(plane, pixels[:, :, 1, 3])
    spectrum = ds.SKY.isel(time=0, polarization=0, l=5, m=6).values
    assert reads[-1] == 7 and np.array_equal(spectrum, pixels[5, 6, 0, :])
    sub = ds.SKY[0, ::-1, 1, 3:9, ::4].values
    assert np.array_equal(sub, pixels.transpose(3, 2, 0, 1)[::-1, 1, 3:9, ::4])
    assert np.array_equal(ds.SKY.isel(l=[1, 5, 2], frequency=[6, 0]).values,
                          pixels.transpose(3, 2, 0, 1)[None][:, [6, 0]][:, :, :, [1, 5, 2]])
    nearest = ds.SKY.sel(l=0.0, m=0.0, method='nearest').isel(time=0, polarization=0).values
    assert np.array_equal(nearest, pixels[12, 9, 0, :])


def test_engine_options(cube):
    path, _, _ = cube
    entry = ci.CasaImageBackendEntrypoint()
    assert entry.guess_can_open(path) is False                          # never by guessing (xradio has an engine too)
    ds = xr.open_dataset(path, engine=ci.CasaImageBackendEntrypoint, variable='SKY_RESIDUAL', masks=False,
                         drop_variables=['BEAM_FIT_PARAMS_SKY_RESIDUAL'])
    assert list(ds.data_vars) == ['SKY_RESIDUAL']
    with pytest.raises(ValueError):
        xr.open_dataset(os.path.dirname(path), engine=ci.CasaImageBackendEntrypoint)
    with pytest.raises(TypeError):
        entry.open_dataset(path, nonsense=1)


def test_engine_by_name_from_a_checkout(cube):
    """importing the package registers the engine where no entry point is
    installed (_register.py); xradio's engine, if there, is left as it was"""
    path, pixels, _ = cube
    before = set(xr.backends.list_engines())
    assert 'casa-image' in before
    ds = xr.open_dataset(path, engine='casa-image')
    assert np.array_equal(ds.SKY.isel(time=0, frequency=2, polarization=1).values, pixels[:, :, 1, 2])
    assert ci.register() is False                                       # already there: nothing to do
    assert set(xr.backends.list_engines()) == before
    from xarray.backends import plugins
    assert xr.backends.list_engines()['casa-image'].guess_can_open(path) is False
    ### an engine table without it (xarray used before the package was imported)
    saved = plugins.BACKEND_ENTRYPOINTS.pop('casa-image', None)
    plugins.list_engines.cache_clear()
    try:
        if not ci._register.entry_point_installed():
            assert 'casa-image' not in xr.backends.list_engines()
            assert ci.register() is True
        assert 'casa-image' in xr.backends.list_engines()
    finally:
        if saved is not None:
            plugins.BACKEND_ENTRYPOINTS.setdefault('casa-image', saved)
        plugins.list_engines.cache_clear()


def test_entry_point_is_declared():
    with open(os.path.join(ROOT, 'pyproject.toml'), encoding='utf-8') as f:
        text = f.read()
    assert 'casa-image = "cubevis.xarray.image._backend:CasaImageBackendEntrypoint"' in text


def test_other_masks_keep_their_names(tmp_path):
    values = np.zeros((6, 5, 1, 2), np.float32)
    good = np.ones(values.shape, bool)
    good[0, 0] = False
    ci.write_image(str(tmp_path / 'm.image'), values, masks={'mask0': good, 'mask1': ~good, 'edge': good},
                   default_mask='mask1')
    ds = ci.open_casa_image(str(tmp_path / 'm.image'))
    assert sorted(v for v in ds.data_vars if v != 'SKY') == ['FLAG_SKY', 'MASK_0', 'MASK_EDGE']
    assert ds.FLAG_SKY.attrs['casa_mask'] == 'mask1' and np.array_equal(ds.FLAG_SKY.values[0], good.transpose(3, 2, 0, 1))
    copy = ci.to_casa_image(ds, str(tmp_path / 'copy.image'))
    assert sorted(copy.mask_names) == ['edge', 'mask0', 'mask1'] and copy.default_mask == 'mask1'


def test_sky_coordinates(cube):
    pytest.importorskip('astropy')
    ds = ci.open_casa_image(cube[0], sky_coords=True)
    assert ds.right_ascension.dims == ('l', 'm')
    assert np.isclose(ds.right_ascension.values[12, 9], 1.0) and np.isclose(ds.declination.values[12, 9], -0.5)
    ### one pixel north is one cell of declination
    assert np.isclose(ds.declination.values[12, 10] - ds.declination.values[12, 9], np.radians(1 / 3600))


def test_dask_chunks(cube):
    pytest.importorskip('dask')
    path, pixels, _ = cube
    ds = xr.open_dataset(path, engine=ci.CasaImageBackendEntrypoint, chunks={})
    assert ds.SKY.chunks[1] == (1,) * 7 and ds.SKY.chunks[2] == (1, 1) and ds.SKY.chunks[3] == (24,)
    assert np.allclose(ds.SKY.mean(('time', 'l', 'm')).compute().values, pixels.mean((0, 1)).T, atol=1e-6)


def test_dataset_back_to_an_image(cube, tmp_path):
    path, pixels, good = cube
    ds = ci.open_casa_image(path)
    copy = ci.to_casa_image(ds, str(tmp_path / 'copy.image'))
    assert np.array_equal(copy[...], pixels) and np.array_equal(copy.mask()[...], good)
    assert copy.coords_record() == ci.CasaImage(path).coords_record()
    assert copy.imageinfo == ci.CasaImage(path).imageinfo and copy.units == 'Jy/beam'
    ### a changed dataset: what an imager without CASA would write
    doubled = ds.assign(SKY=ds.SKY * 2).drop_vars('FLAG_SKY')
    doubled.SKY.attrs = {k: v for k, v in ds.SKY.attrs.items() if k != 'flag'}
    twice = ci.to_casa_image(doubled, str(tmp_path / 'twice.image'))
    assert np.array_equal(twice[...], pixels * 2) and twice.mask_names == []
    with pytest.raises(FileExistsError):
        ci.to_casa_image(ds, str(tmp_path / 'copy.image'))


def test_to_zarr_and_back(cube, tmp_path):
    pytest.importorskip('zarr')
    ds = ci.open_casa_image(cube[0])
    ds.to_zarr(str(tmp_path / 'img.zarr'))
    back = xr.open_zarr(str(tmp_path / 'img.zarr'))
    assert np.array_equal(back.SKY.values, ds.SKY.values) and np.array_equal(back.FLAG_SKY.values, ds.FLAG_SKY.values)
    assert back.attrs['coordinate_system'] == ds.attrs['coordinate_system']
    again = ci.to_casa_image(back, str(tmp_path / 'again.image'))
    assert np.array_equal(again[...], cube[1]) and np.array_equal(again.mask()[...], cube[2])


# ---- against casatools ------------------------------------------------------------

def _casatools():
    return pytest.importorskip('casatools', reason='casatools (and its data) for the comparison')


def _same(a, b, where=''):
    '''records equal, numpy arrays and lists alike'''
    if isinstance(a, dict) or isinstance(b, dict):
        assert isinstance(a, dict) and isinstance(b, dict) and sorted(a) == sorted(b), (where, a, b)
        for k in a:
            _same(a[k], b[k], f'{where}.{k}')
    elif isinstance(a, (list, np.ndarray)) or isinstance(b, (list, np.ndarray)):
        assert np.asarray(a).shape == np.asarray(b).shape and np.all(np.asarray(a) == np.asarray(b)), (where, a, b)
    else:
        assert a == b, (where, a, b)


def _within(part, whole, where=''):
    '''everything in ``part`` is in ``whole`` with the same value (casatools
    adds what a record leaves to its defaults)'''
    if isinstance(part, dict):
        assert isinstance(whole, dict), where
        for k in part:
            assert k in whole, f'{where}.{k}'
            _within(part[k], whole[k], f'{where}.{k}')
    else:
        _same(part, whole, where)


@pytest.fixture(scope='module')
def casa_images(tmp_path_factory):
    ct = _casatools()
    ia = ct.image()
    d = tmp_path_factory.mktemp('casa')
    rng = np.random.default_rng(7)
    made = {}
    ### rotated, a beam per plane (2 Stokes x 3 channels), a mask, off-centre
    shape = [16, 12, 2, 3]
    path = str(d / 'rot.image')
    ia.fromshape(path, shape, overwrite=True)
    pixels = rng.normal(size=shape).astype(np.float32)
    ia.putchunk(pixels)
    cs = ia.coordsys()
    cs.setlineartransform('direction', np.array([[0.8, -0.6], [0.6, 0.8]]))
    cs.setreferencevalue([1.2, -0.7, 1.0, 1.4e9])
    cs.setreferencepixel([3.0, 7.5, 0.0, 1.0])
    ia.setcoordsys(cs.torecord())
    for c in range(3):
        for s in range(2):
            ia.setrestoringbeam(major=f'{3 + c + 10 * s}arcsec', minor='2arcsec', pa='10deg', channel=c, polarization=s)
    ia.setbrightnessunit('Jy/beam')
    ia.calcmask(f'"{path}" > -1.0', name='mask0')
    ia.close()
    made['rot'] = (path, pixels)
    ### a tabular spectral axis, optical velocities, GHz
    path = str(d / 'tab.image')
    ia.fromshape(path, [8, 8, 1, 4], overwrite=True)
    cs = ct.coordsys().newcoordsys(direction=True, spectral=True, stokes=['I'])
    cs.setspectral(refcode='LSRK', restfreq='1.42GHz', frequencies={'value': np.array([1.0, 1.1, 1.3, 1.6]), 'unit': 'GHz'},
                   doppler='optical')
    ia.setcoordsys(cs.torecord())
    pixels = rng.normal(size=[8, 8, 1, 4]).astype(np.float32)
    ia.putchunk(pixels)
    ia.close()
    made['tab'] = (path, pixels)
    ### complex, and galactic
    path = str(d / 'cx.image')
    ia.fromshape(path, [6, 5, 1, 3], type='c', overwrite=True)
    pixels = (rng.normal(size=[6, 5, 1, 3]) + 1j * rng.normal(size=[6, 5, 1, 3])).astype(np.complex64)
    ia.putchunk(pixels)
    cs = ia.coordsys()
    cs.setconversiontype(direction='GALACTIC')
    cs.setreferencecode('GALACTIC', 'direction')
    ia.setcoordsys(cs.torecord())
    ia.close()
    made['cx'] = (path, pixels)
    return made


@pytest.mark.parametrize('name', ['rot', 'tab', 'cx'])
def test_reads_what_casatools_wrote(casa_images, name):
    ct = _casatools()
    path, pixels = casa_images[name]
    im = ci.CasaImage(path)
    ia = ct.image()
    ia.open(path)
    try:
        assert list(im.shape) == list(ia.shape()) and np.array_equal(im[...], pixels)
        assert np.array_equal(im[...], ia.getchunk())
        _same(im.coords_record(), ia.coordsys().torecord(), 'coords')
        assert im.units == ia.brightnessunit()
        if name == 'rot':
            assert im.mask_names == list(ia.maskhandler('get')) == ['mask0'] and im.default_mask == 'mask0'
        mask = im.mask()
        got = np.ones(im.shape, bool) if mask is None else mask[...]
        assert np.array_equal(got, ia.getchunk(getmask=True))
        beam = ia.restoringbeam()
        _same(co.restoring_beam(im.imageinfo) or {}, beam, 'beam')
        ### the frequency of every channel, and the sky position of pixels
        record = im.coords_record()
        axes = co.axes(record, im.ndim)
        cs = ia.coordsys()
        chan_axis = co.find_axis(axes, 'spectral')
        freq = co.spectral_values(record[axes[chan_axis].key], im.shape[chan_axis])
        for c in range(im.shape[chan_axis]):
            world = cs.toworld([0, 0, 0, c])['numeric'][chan_axis]
            unit = cs.units()[chan_axis]
            assert np.isclose(freq[c], world * co.FREQUENCY_TO_HZ[unit], rtol=1e-12, atol=0), (c, freq[c], world, unit)
        pytest.importorskip('astropy')
        info = co.direction(record[axes[0].key])
        lon, lat = co.sky_coordinates(info, im.shape[0], im.shape[1])
        to_rad = [co.ANGLE_TO_RAD[u] for u in cs.units()[:2]]
        for x, y in ((0, 0), (im.shape[0] - 1, 0), (2, im.shape[1] - 1), (4, 3)):
            world = cs.toworld([x, y, 0, 0])['numeric']
            dlon = (lon[x, y] - world[0] * to_rad[0] + np.pi) % (2 * np.pi) - np.pi
            assert abs(dlon) < 1e-11 and abs(lat[x, y] - world[1] * to_rad[1]) < 1e-11, (x, y)
    finally:
        ia.close()


def test_casatools_reads_what_was_written_here(cube, tmp_path):
    ct = _casatools()
    path, pixels, good = cube
    ia = ct.image()
    ia.open(path)
    try:
        assert list(ia.shape()) == list(pixels.shape)
        assert np.array_equal(ia.getchunk(), pixels) and np.array_equal(ia.getchunk(getmask=True), good)
        assert ia.brightnessunit() == 'Jy/beam' and list(ia.maskhandler('get')) == ['mask0']
        assert ia.restoringbeam()['major'] == {'unit': 'arcsec', 'value': 3.0}
        _within(ci.CasaImage(path).coords_record(), ia.coordsys().torecord(), 'coords')
        assert ia.statistics()['npts'][0] == good.sum()
        assert ia.summary()['axisnames'].tolist() == ['Right Ascension', 'Declination', 'Stokes', 'Frequency']
    finally:
        ia.close()
    ### pixels written here into an image casatools made, and the other way round
    other = str(tmp_path / 'ct.image')
    ia.fromshape(other, list(pixels.shape), overwrite=True)
    ia.putchunk(np.zeros(pixels.shape, np.float32))
    ia.close()
    im = ci.CasaImage(other)
    im[3:9, 2:5, 1, 4] = pixels[3:9, 2:5, 1, 4]
    ia.open(other)
    got = ia.getchunk()
    ia.putchunk(np.full([24, 18, 1, 1], 5.0, np.float32), blc=[0, 0, 0, 2])
    ia.close()
    expect = np.zeros(pixels.shape, np.float32)
    expect[3:9, 2:5, 1, 4] = pixels[3:9, 2:5, 1, 4]
    assert np.array_equal(got, expect)
    assert (im[:, :, 0, 2] == 5.0).all()                               # a new handle each read: not stale


def test_a_held_handle_is_stale_after_casatools_writes(tmp_path):
    '''why CasaImage opens for each call (module docstring of _image.py): if
    this starts failing, arcae or casacore learned to notice, and holding
    handles could be reconsidered'''
    ct = _casatools()
    path = str(tmp_path / 's.image')
    im = ci.write_image(path, np.zeros((8, 6, 1, 2), np.float32))
    ia = ct.image()
    with im.hold():
        assert im[0, 0, 0, 0] == 0.0
        ia.open(path)
        ia.putchunk(np.full([8, 6, 1, 1], 5.0, np.float32))
        ia.close()
        held = float(im[0, 0, 0, 0])
    assert float(im[0, 0, 0, 0]) == 5.0
    assert held == 0.0


def test_the_mask_tools_helpers_need_no_casatools(cube, tmp_path, monkeypatch):
    """cubevis.data.casaimage.new and .shape (createmask, createregion)"""
    pytest.importorskip('PIL')
    import cubevis.data.casaimage as old
    path, pixels, _ = cube
    monkeypatch.setattr(old, 'casatools_available', lambda: False)
    monkeypatch.setattr(old, 'imagetool', lambda: (_ for _ in ()).throw(AssertionError('casatools was used')))
    assert list(old.shape(path)) == list(pixels.shape) and (old.shape(path) == old.shape(path)).all()
    made = old.new(path, str(tmp_path / 'new.mask'))
    assert made == str(tmp_path / 'new.mask')
    mask = ci.CasaImage(made)
    assert mask.shape == pixels.shape and mask.dtype == np.float32 and not mask[...].any()
    assert mask.coords_record() == ci.CasaImage(path).coords_record() and mask.mask_names == []
    with pytest.raises(RuntimeError, match='already exists'):
        old.new(path, made)
    old.new(path, made, overwrite=True)
