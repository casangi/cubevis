'''The imaging layer (cubevis.imaging) without CASA: an ImageCube over a
numpy store, a LocalImagingSession over a scripted deconvolver.

    pytest tests/manual/iclean-imaging/test_imaging_layer.py
'''
import sys
import asyncio
import subprocess
import textwrap

import numpy as np
import pytest

from cubevis.imaging import (ImageCube, LocalImagingSession, CycleResult, quantize,
                             as_imaging_session, resolve_deconvolver,
                             ImagingSession, ImageStore, Deconvolver, ImageCubeProtocol)


class NumpyStore:
    '''An ImageStore over numpy arrays shaped [x, y, stokes, chan].'''

    def __init__(self, image, mask=None, image_path='img.residual', mask_path='img.mask'):
        self.image = np.array(image, dtype=np.float64)
        self.maskarr = None if mask is None else np.array(mask, dtype=np.float64)
        self.image_path = image_path
        self.mask_path = mask_path if mask is not None else None
        self.reads = 0
        self.mtime = 1.0
        self.stats_calls = []
        self.closed = 0

    def shape(self):
        return list(self.image.shape)

    def modified(self):
        return self.mtime

    def read_plane(self, index):
        self.reads += 1
        s, c = index
        return self.image[:, :, s:s + 1, c:c + 1].copy()

    def read_spectrum(self, index):
        x, y, s = index
        return self.image[x:x + 1, y:y + 1, s:s + 1, :].copy()

    def has_mask(self):
        return self.maskarr is not None

    def open_mask(self, path):
        self.mask_path = path

    def close_mask(self):
        pass

    def read_mask_plane(self, index):
        s, c = index
        return self.maskarr[:, :, s:s + 1, c:c + 1].copy()

    def write_mask_plane(self, index, pixels):
        s, c = index
        self.maskarr[:, :, s, c] = pixels

    def read_mask_spectrum(self, index):
        x, y, s = index
        return self.maskarr[x:x + 1, y:y + 1, s:s + 1, :].copy()

    def mask_pixel(self, chan, index):
        return int(self.maskarr[index[0], index[1], chan[0], chan[1]])

    def has_default_mask(self):
        return False

    def read_default_mask(self):
        return None

    def statistics(self, index, mask_path=None):
        self.stats_calls.append((list(index), mask_path))
        plane = self.image[:, :, index[0], index[1]]
        return {'max': np.array([plane.max()]), 'min': np.array([plane.min()]),
                'blc': np.array([0, 0, index[0], index[1]]), 'npts': np.array([plane.size])}

    def stokes_labels(self):
        return ['I']

    def fits_header(self):
        return ({'NAXIS': 4}, 'NAXIS = 4')

    def coordinates(self):
        return {'version': 1, 'shape': self.shape(), 'pixel_origin': 0, 'direction': None,
                'spectral': None, 'stokes': ['I'], 'beam': None, 'brightness_unit': '', 'extra': {}}

    def close(self):
        self.closed += 1


def cube_data(nx=8, ny=6, nchan=3, seed=1):
    rng = np.random.default_rng(seed)
    return rng.normal(size=(nx, ny, 1, nchan))


# ---------------------------------------------------------------------------

def _modules_after(statement):
    code = textwrap.dedent(f'''
        import sys, json
        {statement}
        print(json.dumps(sorted(sys.modules)))
    ''')
    out = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, check=True)
    import json
    return set(json.loads(out.stdout.strip().splitlines()[-1]))


def test_dependency_rule_imaging_imports_no_bokeh_no_casa():
    '''design §6.2: the imaging layer imports neither Bokeh nor CASA (nor the
    rest of cubevis.utils, which brings Bokeh, astropy and regions), so the
    remote worker, an RADPS implementation or a test can use it without the GUI.'''
    mods = _modules_after("import cubevis.imaging")
    heavy = {m.split('.')[0] for m in mods} & {"bokeh", "casatools", "casatasks", "astropy", "regions"}
    assert not heavy, heavy
    assert not [m for m in mods if m.startswith("cubevis.") and not m.startswith("cubevis.imaging")]


def test_toolbox_iclean_is_lazy():
    '''importing the application package loads none of its modules (nor CASA)
    until a name is used'''
    mods = _modules_after("import cubevis.toolbox.iclean")
    assert not {"cubevis.toolbox.iclean._app", "cubevis.toolbox.iclean._ui", "casatools", "casatasks"} & mods


def test_protocols():
    store = NumpyStore(cube_data())
    assert isinstance(store, ImageStore)
    assert isinstance(ImageCube(store), ImageCubeProtocol)
    assert isinstance(LocalImagingSession(FakeGclean([])), ImagingSession)
    assert isinstance(FakeGclean([]), Deconvolver)


def test_channel_quantized_matches_quantize():
    data = cube_data()
    cube = ImageCube(NumpyStore(data))
    plane = np.squeeze(data[:, :, 0, 1])
    linear = {'bounds': [[], []], 'transfer': {'scaling': 'linear'}}
    assert np.array_equal(cube.channel([0, 1], np.uint8), quantize(np.uint8, plane, linear).transpose())
    quant = {'bounds': [[-0.5], [1.0]], 'transfer': {'scaling': 'log', 'args': {'alpha': 100}}}
    assert np.array_equal(cube.channel([0, 1], np.uint8, quant), quantize(np.uint8, plane, quant).transpose())
    assert np.allclose(cube.channel([0, 1], np.float64), plane.transpose())


def test_generation_tracking_rereads_only_after_touch():
    store = NumpyStore(cube_data())
    cube = ImageCube(store, track='generation')
    cube.channel([0, 0], np.float64)
    cube.channel([0, 0], np.float64)
    assert store.reads == 1
    store.mtime += 10                       # ignored in generation mode
    cube.channel([0, 0], np.float64)
    assert store.reads == 1
    cube.touch()
    cube.channel([0, 0], np.float64)
    assert store.reads == 2
    cube.channel([0, 1], np.float64)        # another plane
    assert store.reads == 3


def test_ctime_tracking_rereads_on_change():
    store = NumpyStore(cube_data())
    cube = ImageCube(store)                 # default: ctime
    cube.channel([0, 0], np.float64)
    cube.channel([0, 0], np.float64)
    assert store.reads == 1
    store.mtime += 1
    cube.channel([0, 0], np.float64)
    assert store.reads == 2


def test_mask_planes():
    data = cube_data()
    mask = np.zeros_like(data)
    store = NumpyStore(data, mask)
    cube = ImageCube(store)
    m = cube.mask([0, 1], True)
    assert m.shape == (8, 6)
    m[2:4, 1:3] = True
    cube.put_mask([0, 1], m.astype(bool))
    assert store.maskarr[:, :, 0, 1].sum() == 4
    shown = cube.mask([0, 1])
    assert shown.dtype == np.bool_ and shown.shape == (6, 8)          # transposed for display
    assert cube.mask_value([0, 1], [2, 1]) == 1
    assert cube.mask_value([0, 1], [99, 99]) == -1                    # errors -> -1, as before


def test_spectrum_and_single_channel():
    data = cube_data(nchan=3)
    mask = np.ones_like(data)
    cube = ImageCube(NumpyStore(data, mask))
    spec, msk = cube.spectrum([99, 1, 0], True)                       # x clipped to the shape
    assert spec['chan'] == [0, 1, 2]
    assert np.allclose(spec['pixel'], data[7, 1, 0, :])
    assert msk == [True, True, True]
    one = ImageCube(NumpyStore(cube_data(nchan=1)))
    assert one.spectrum([0, 0, 0])['chan'] == [0]


def test_statistics_mask_config():
    data = cube_data()
    store = NumpyStore(data, np.ones_like(data))
    cube = ImageCube(store)
    stats = cube.statistics([0, 2])
    assert list(stats.keys())[-1] == 'blc'                            # blc/trc sorted last
    assert stats['max'] == pytest.approx(data[:, :, 0, 2].max())     # singletons unwrapped
    cube.statistics_config(use_mask=True)
    cube.statistics([0, 2])
    assert store.stats_calls[-1][1] == 'img.mask'
    nomask = ImageCube(NumpyStore(data))
    nomask.statistics_config(use_mask=True)                           # no mask: stays off
    nomask.statistics([0, 0])
    assert nomask.store.stats_calls[-1][1] is None


def test_channel_reply():
    data = cube_data()
    cube = ImageCube(NumpyStore(data, np.zeros_like(data)))
    r = cube.channel_reply([0, 1], None, stats=True, hist_bins=16)
    assert set(r) == {'img', 'msk', 'hist', 'stats'}
    assert r['img'].dtype == np.uint8
    assert len(r['hist']['top']) == 16
    r = cube.channel_reply([0, 1])
    assert r['hist'] == {} and 'stats' not in r


# ---------------------------------------------------------------------------

class FakeGclean:
    '''gclean's surface, scripted.  Each step rewrites the residual.'''

    def __init__(self, steps, store=None, own_stopdesc=True):
        self.steps = list(steps)
        self.store = store
        self.updates = []
        self.restored = False
        if own_stopdesc:
            self.stop_descriptions = lambda: {0: 'zero', 1: 'cycleniter'}

    def _step(self):
        if not self.steps:
            raise StopIteration
        if self.store is not None:
            self.store.image *= 0.5
        return self.steps.pop(0)

    def __next__(self):
        return self._step()

    async def __anext__(self):
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._step)

    def update(self, msg):
        self.updates.append(msg)
        return (0, '')

    def image_products(self):
        return [dict(name='f1', imagename='f1.image', imagepath='/data', maskname='f1.mask',
                     residualname='f1.residual')]

    def _log(self, history=False):
        return ['tclean(...)'] + (['history'] if history else [])

    def restore(self):
        self.restored = True
        return {'stopcode': 1}

    def has_next(self):
        return bool(self.steps)


def step(n):
    return ('desc', (0, n), n, -1, 50 - n, {'f1': {'chan': [n], 'major': {'cyclethreshold': [0.1]}}})


def test_session_fields_and_cubes():
    made = []

    def factory(image, mask):
        made.append((image, mask))
        return NumpyStore(cube_data(), np.zeros_like(cube_data()), image, mask)

    s = LocalImagingSession(FakeGclean([step(0)]), store_factory=factory)
    assert s.fields() == [dict(id='f1', imagename='f1.image', imagepath='/data',
                               residual='/data/f1.residual', mask='/data/f1.mask')]
    c = s.cube('f1')
    assert s.cube('f1') is c and made == [('/data/f1.residual', '/data/f1.mask')]
    assert c.image_path == '/data/f1.residual' and c.mask_path == '/data/f1.mask'
    with pytest.raises(KeyError):
        s.cube('nope')


def test_a_failed_cycles_bare_stop_code_becomes_a_pair():
    """gclean gives ``-1`` (not a pair) when its cycle raised, with the
    exception's text as the description; the GUI indexes the code
    (notes §72)."""
    failed = ("the imagename parameter must be a path that exists ('br.residual' does not exist)",
              -1, 1, -1, 50, {'f1': {'chan': [], 'major': {'cyclethreshold': []}}})
    s = LocalImagingSession(FakeGclean([step(0), failed]),
                            store_factory=lambda i, m: NumpyStore(cube_data(), np.zeros_like(cube_data()), i, m))
    assert s.start().stopcode == (0, 0)
    r = s.run_cycle()
    assert r.stopcode == (-1, 0) and r.stopcode[0] == -1 and "does not exist" in r.stopdesc


def test_session_cycles_bump_generation_and_reread():
    data = cube_data()
    store = NumpyStore(data)
    dec = FakeGclean([step(0), step(1), step(2)], store=store)
    s = LocalImagingSession(dec, store_factory=lambda i, m: store)
    cube = s.cube('f1')
    r = s.start()
    assert isinstance(r, CycleResult)
    stopdesc, stopcode, majordone, majorleft, iterleft, conv = r       # unpacks like gclean's tuple
    assert majordone == 0 and conv['f1']['chan'] == [0]
    first = cube.channel([0, 0], np.float64).copy()
    assert s.update({'niter': '5'}) == (0, '') and dec.updates == [{'niter': '5'}]
    r = asyncio.run(s.arun_cycle())
    assert r.majordone == 1 and cube.generation == 2
    assert np.allclose(cube.channel([0, 0], np.float64), first * 0.5)  # reread after the cycle
    r = s.run_cycle()
    assert r.majordone == 2 and cube.generation == 3
    assert not s.has_next()
    assert s.log() == ['tclean(...)'] and s.log(True)[-1] == 'history'
    assert s.restore() == {'stopcode': 1} and dec.restored
    assert s.stop_descriptions() == {0: 'zero', 1: 'cycleniter'}
    s.close()
    assert store.closed == 1


def test_as_imaging_session():
    dec = FakeGclean([])
    s = as_imaging_session(dec)
    assert isinstance(s, LocalImagingSession) and s.deconvolver is dec
    assert as_imaging_session(s) is s


def test_resolve_and_from_args(tmp_path, monkeypatch):
    (tmp_path / '_gclean_layertest.py').write_text(textwrap.dedent('''
        class gclean:
            def __init__(self, **kw):
                self.kw = kw
    '''))
    monkeypatch.syspath_prepend(str(tmp_path))
    mod = resolve_deconvolver('layertest')
    assert mod.gclean(a=1).kw == {'a': 1}
    s = LocalImagingSession.from_args({'vis': 'x.ms'}, 'layertest')
    assert s.deconvolver.kw == {'vis': 'x.ms'}
    with pytest.raises(ImportError):
        resolve_deconvolver('no_such_backend_here')
