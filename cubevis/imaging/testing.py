'''Stand-ins for testing the imaging layer without CASA, importable by name
in a ``cubevis.remote`` worker (``RemoteImagingSession(...,
deconvolver='cubevis.imaging.testing:ScriptedDeconvolver',
store='cubevis.imaging.testing:numpy_store')``), like
``cubevis.remote.testing``.

``ScriptedDeconvolver`` has gclean's surface (``Deconvolver``): each cycle
halves its fields' residuals and reports a convergence record; ``numpy_store``
gives the in-memory ``ImageStore`` for a path, the same one the deconvolver
changes (one registry per process).  Not imported by ``cubevis.imaging``.
'''

import os
import time
import zlib
from typing import Dict, List, Optional

import numpy as np

#: path -> NumpyImageStore, shared by the deconvolver and the store factory
STORES: Dict[str, 'NumpyImageStore'] = {}


class NumpyImageStore:
    '''An ``ImageStore`` over numpy arrays ``[x, y, stokes, chan]``, with the
    optional ranged mask primitives.'''

    def __init__(self, image, mask=None, image_path='img.residual', mask_path='img.mask'):
        self.image = np.array(image, dtype=np.float64)
        self.maskarr = None if mask is None else np.array(mask, dtype=np.float64)
        self.image_path = image_path
        self.mask_path = mask_path if mask is not None else None
        self.mtime = 1.0
        self.reads = 0

    def shape(self) -> List[int]:
        return list(self.image.shape)

    def modified(self) -> float:
        return self.mtime

    def read_plane(self, index):
        self.reads += 1
        s, c = index
        return self.image[:, :, s:s + 1, c:c + 1].copy()

    def read_spectrum(self, index):
        x, y, s = index
        return self.image[x:x + 1, y:y + 1, s:s + 1, :].copy()

    def has_mask(self) -> bool:
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
        self.maskarr[:, :, s, c] = np.asarray(pixels).reshape(self.maskarr.shape[:2])

    def read_mask_chans(self, stokes, start, count):
        return self.maskarr[:, :, stokes:stokes + 1, start:start + count].copy()

    def write_mask_chans(self, stokes, start, pixels):
        n = pixels.shape[-1]
        self.maskarr[:, :, stokes, start:start + n] = pixels.reshape(self.maskarr.shape[0],
                                                                      self.maskarr.shape[1], n)

    def read_mask_spectrum(self, index):
        x, y, s = index
        return self.maskarr[x:x + 1, y:y + 1, s:s + 1, :].copy()

    def mask_pixel(self, chan, index):
        return int(self.maskarr[index[0], index[1], chan[0], chan[1]])

    def has_default_mask(self) -> bool:
        return False

    def read_default_mask(self):
        return None

    def statistics(self, index, mask_path=None):
        plane = self.image[:, :, index[0], index[1]]
        if mask_path is not None and self.maskarr is not None:
            sel = self.maskarr[:, :, index[0], index[1]] != 0
            plane = plane[sel] if sel.any() else np.zeros(1)
        return {'max': np.array([plane.max()]), 'min': np.array([plane.min()]),
                'npts': np.array([float(plane.size)]), 'sum': np.array([plane.sum()]),
                'mean': np.array([plane.mean()])}

    def stokes_labels(self) -> List[str]:
        return ['I', 'Q', 'U', 'V'][:self.image.shape[2]]

    def coordinates(self) -> dict:
        nx, ny, ns, nc = self.image.shape
        return {'version': 1, 'shape': [nx, ny, ns, nc], 'pixel_origin': 0,
                'direction': {'frame': 'FK5', 'equinox': 2000.0, 'projection': 'SIN',
                              'projection_parameters': [0.0, 0.0],
                              'reference_value': [180.0, 40.0], 'reference_pixel': [nx // 2, ny // 2],
                              'increment': [-1.0 / 3600, 1.0 / 3600], 'pc': [[1.0, 0.0], [0.0, 1.0]],
                              'lonpole': None, 'latpole': None},
                'spectral': {'frame': 'LSRK', 'unit': 'Hz', 'values': [1.0e9 + 1.0e6 * c for c in range(nc)],
                             'rest_frequency': None, 'velocity_convention': 'radio'},
                'stokes': self.stokes_labels(), 'beam': None, 'brightness_unit': 'Jy/beam', 'extra': {}}

    def fits_header(self):
        return ({'NAXIS': 4}, 'NAXIS = 4')

    def close(self):
        pass


def numpy_store(image_path: str, mask_path: Optional[str] = None) -> NumpyImageStore:
    '''the registry's store for ``image_path`` (``store_factory``)'''
    if image_path not in STORES:
        raise KeyError(f'no scripted image {image_path!r} (made by ScriptedDeconvolver)')
    return STORES[image_path]


class ScriptedDeconvolver:
    '''gclean's surface without tclean, with gclean's results: the six values
    (stop description, ``(major, minor)`` stop code, majors done and left,
    iterations left) and its convergence record per field -- ``{'major':
    {'cyclethreshold': [...]}, 'chan': {chan: {stokes: {'modelFlux',
    'iterations' (an array), 'peakRes', 'stopCode', 'cycleThresh'}}}}``, each
    list one entry longer per major cycle -- so ``InteractiveCleanUI`` runs on
    it.  Keywords as tclean's; uses ``imagename``, ``imsize`` (int or [nx, ny]),
    ``nchan``, ``niter``, ``cycleniter`` and ``threshold``, and two of its own:
    ``cycle_seconds`` (how long a cycle takes) and ``fail_at`` (the cycle that
    raises).  Each cycle halves the residual.  ``update`` checks its values as
    gclean does (``(-1, message)`` for a bad one).'''

    def __init__(self, imagename='scripted', imsize=16, nchan=4, niter=40, cycleniter=10,
                 threshold='0.0Jy', cycle_seconds=0.0, fail_at=None, **ignored):
        nx, ny = (imsize, imsize) if isinstance(imsize, int) else tuple(imsize)
        nchan = max(1, int(nchan))
        self._name = os.path.basename(os.path.abspath(imagename))   # as gclean names a field
        imagename = self._name
        self._niter = int(niter)
        self._iterleft = int(niter)
        self._cycleniter = int(cycleniter) if int(cycleniter) > 0 else int(niter)
        self._threshold = _jy(threshold)
        self._cycle_seconds = float(cycle_seconds)
        self._fail_at = fail_at
        self._major = 0
        self._lines: List[str] = []
        seed = zlib.crc32(imagename.encode())
        image = np.random.default_rng(seed).normal(size=(nx, ny, 1, nchan))
        self.residual_path = f'/scripted/{imagename}.residual'
        self.mask_path = f'/scripted/{imagename}.mask'
        STORES[self.residual_path] = NumpyImageStore(image, np.zeros_like(image),
                                                     self.residual_path, self.mask_path)
        import tempfile
        fd, self._log_file = tempfile.mkstemp(prefix=f'scripted-{imagename}-', suffix='.log')
        os.close(fd)
        self._cyclethreshold: List[float] = []
        self._chan = {c: {0: {'modelFlux': [], 'iterations': np.zeros(0), 'peakRes': [],
                              'stopCode': [], 'cycleThresh': []}} for c in range(nchan)}

    # ---- Deconvolver --------------------------------------------------------

    def image_products(self):
        return [{'name': self._name, 'imagename': self._name, 'imagepath': '/scripted',
                 'residualname': f'{self._name}.residual', 'maskname': f'{self._name}.mask'}]

    def update(self, params):
        '''gclean's checks for the values the GUI sends (the rest ignored)'''
        for key in ('niter', 'cycleniter', 'nmajor'):
            if key in params:
                try:
                    value = int(params[key])
                except (TypeError, ValueError):
                    return (-1, f'{key} must be an integer')
                if value < -1:
                    return (-1, f'{key} must be >= -1')
        if 'threshold' in params:
            try:
                if _jy(params['threshold']) < 0:
                    return (-1, 'threshold must be >= 0')
            except ValueError:
                return (-1, 'threshold must be a number, or a number with units (Jy/mJy/uJy)')
        for key in ('gain', 'cyclefactor'):
            if key in params:
                try:
                    if float(params[key]) <= 0:
                        return (-1, f'{key} must be > 0')
                except (TypeError, ValueError):
                    return (-1, f'{key} must be a number')
        if 'niter' in params:
            self._iterleft = int(params['niter'])
        if 'cycleniter' in params and int(params['cycleniter']) > 0:
            self._cycleniter = int(params['cycleniter'])
        if 'threshold' in params:
            self._threshold = _jy(params['threshold'])
        return (0, '')

    def has_next(self):
        return self._iterleft > 0

    def log_path(self):
        '''the log this stand-in writes, in casalog's format (``CycleProgress``)'''
        return self._log_file

    def _write(self, origin, message):
        stamp = time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())
        with open(self._log_file, 'a') as f:
            f.write(f'{stamp}\tINFO\t{origin}\t{message}\n')

    def _logged_work(self, done):
        '''what tclean logs during a cycle, spread over ``cycle_seconds``'''
        store = STORES[self.residual_path]
        nchan = store.image.shape[3]
        if self._major == 0:
            self._write('tclean::::casa', '##### Begin Task: tclean             #####')
            self._write('task_tclean::SynthesisImager::executeMajorCycle', '----- Run Major Cycle 1 -----')
            if self._cycle_seconds:
                time.sleep(self._cycle_seconds)
            return
        self._write('deconvolve::::casa', '##### Begin Task: deconvolve         #####')
        self._write('SDAlgorithmBase::deconvolve', f'[{self._name}] Run Hogbom minor-cycle on {nchan} chans | '
                                                   f'CycleNiter={self._cycleniter}')
        per = done // nchan
        for c in range(nchan):
            if self._cycle_seconds:
                time.sleep(self._cycle_seconds / (nchan + 1))
            peak = float(np.abs(store.image[:, :, 0, c]).max())
            self._write('SDAlgorithmBase::deconvolve',
                        f'[{self._name}:C{c}] iters={c * per}->{(c + 1) * per} [{per}], model=0->1, '
                        f'peakres={2 * peak:.6g}->{peak:.6g}, Reached cyclethreshold')
        self._write('grpcInteractiveCleanManager::mergeIterDone', f'Completed {done} iterations.')
        self._write('tclean::::casa', '##### Begin Task: tclean             #####')
        self._write('task_tclean::SynthesisImager::executeMajorCycle', f'----- Run Major Cycle {self._major + 1} -----')
        if self._cycle_seconds:
            time.sleep(self._cycle_seconds / (nchan + 1))

    def _step(self):
        if self._fail_at is not None and self._major == int(self._fail_at):
            raise RuntimeError(f'scripted failure in cycle {self._major}')
        store = STORES[self.residual_path]
        done = 0
        if self._major > 0:
            store.image *= 0.5
            store.mtime += 1.0
            done = max(0, min(self._cycleniter, self._iterleft))
            self._iterleft -= done
        self._logged_work(done)
        peak = float(np.abs(store.image).max())
        reached = self._major > 0 and (self._iterleft <= 0 or peak <= self._threshold)
        self._cyclethreshold.append(max(self._threshold, 0.1 * peak) if self._major else self._threshold)
        for c, by_stokes in self._chan.items():
            rec = by_stokes[0]
            before = rec['iterations'][-1] if len(rec['iterations']) else 0.0
            rec['iterations'] = np.append(rec['iterations'], before + done)
            rec['modelFlux'].append(0.5 * self._major)
            rec['peakRes'].append(float(np.abs(store.image[:, :, 0, c]).max()))
            rec['stopCode'].append(3 if self._major == 0 else 2)
            rec['cycleThresh'].append(self._cyclethreshold[-1] if self._major else 0.0)
        self._lines.append(f'cycle {self._major}: peak {peak:.6f}, {self._iterleft} iterations left')
        stop = (1 if self._iterleft <= 0 else 2, 0) if reached else (0, 0)
        stopdesc = ('Reached iteration limit' if stop[0] == 1 else
                    'Reached global stopping criterion : threshold' if stop[0] == 2 else '')
        convergence = {self._name: {'major': {'cyclethreshold': list(self._cyclethreshold)},
                                    'chan': {c: {s: {k: (v.copy() if isinstance(v, np.ndarray) else list(v))
                                                     for k, v in d.items()} for s, d in by_stokes.items()}
                                             for c, by_stokes in self._chan.items()}}}
        result = (stopdesc, stop, self._major, -1, self._iterleft, convergence)
        self._major += 1
        return result

    def __next__(self):
        return self._step()

    async def __anext__(self):
        import asyncio
        return await asyncio.get_running_loop().run_in_executor(None, self._step)

    def _log(self, history=False):
        return list(self._lines)

    def restore(self):
        return {'stopcode': 1, 'iterdone': self._niter - self._iterleft,
                'peak': np.float64(np.abs(STORES[self.residual_path].image).max())}

    def stop_descriptions(self):
        return {0: 'no stop', 1: 'iteration limit', 2: 'threshold', 3: 'zero iterations'}


def _jy(value) -> float:
    '''"0.01Jy", "5mJy", "3uJy" or a number, in Jy (ValueError otherwise)'''
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    for unit, scale in (('uJy', 1e-6), ('mJy', 1e-3), ('Jy', 1.0)):
        if text.endswith(unit):
            return float(text[:-len(unit)]) * scale
    return float(text)
