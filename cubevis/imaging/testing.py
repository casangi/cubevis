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
    '''gclean's surface without tclean.  Keywords as tclean's; uses
    ``imagename``, ``imsize`` (int or [nx, ny]), ``nchan`` and ``niter``,
    and two of its own: ``cycle_seconds`` (how long a cycle takes) and
    ``fail_at`` (the cycle that raises).  Each cycle halves the residual
    and uses ``cycleniter`` iterations (default 10).'''

    def __init__(self, imagename='scripted', imsize=16, nchan=4, niter=40, cycleniter=10,
                 cycle_seconds=0.0, fail_at=None, **ignored):
        nx, ny = (imsize, imsize) if isinstance(imsize, int) else tuple(imsize)
        nchan = max(1, int(nchan))
        self._name = imagename
        self._iterleft = int(niter)
        self._cycleniter = int(cycleniter)
        self._cycle_seconds = float(cycle_seconds)
        self._fail_at = fail_at
        self._major = 0
        self._lines: List[str] = []
        self._params = {}
        seed = zlib.crc32(imagename.encode())
        image = np.random.default_rng(seed).normal(size=(nx, ny, 1, nchan))
        self.residual_path = f'/scripted/{imagename}.residual'
        self.mask_path = f'/scripted/{imagename}.mask'
        STORES[self.residual_path] = NumpyImageStore(image, np.zeros_like(image),
                                                     self.residual_path, self.mask_path)

    # ---- Deconvolver --------------------------------------------------------

    def image_products(self):
        return [{'name': self._name, 'imagename': self._name, 'imagepath': '/scripted',
                 'residualname': f'{self._name}.residual', 'maskname': f'{self._name}.mask'}]

    def update(self, params):
        self._params.update(params)
        if 'niter' in params:
            try:
                self._iterleft = int(params['niter'])
            except (TypeError, ValueError):
                return (-1, f'niter must be an integer, not {params["niter"]!r}')
        return (0, '')

    def has_next(self):
        return self._iterleft > 0

    def _step(self):
        if self._fail_at is not None and self._major == int(self._fail_at):
            raise RuntimeError(f'scripted failure in cycle {self._major}')
        if self._cycle_seconds:
            time.sleep(self._cycle_seconds)
        store = STORES[self.residual_path]
        if self._major > 0:
            store.image *= 0.5
            store.mtime += 1.0
            done = min(self._cycleniter, self._iterleft)
            self._iterleft -= done
        self._lines.append(f'cycle {self._major}: peak {float(np.abs(store.image).max()):.6f}')
        nchan = store.image.shape[3]
        convergence = {self._name: {c: {0: {'iterDone': [self._major * self._cycleniter],
                                            'peakRes': [float(np.abs(store.image[:, :, 0, c]).max())],
                                            'modelFlux': [0.5 * self._major],
                                            'stopCode': [0]}}
                                    for c in range(nchan)}}
        stop = (1, 0) if self._iterleft <= 0 else (0, 0)
        result = ('iteration limit' if stop[0] else '', stop, self._major, 0 if stop[0] else 1,
                  self._iterleft, convergence)
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
        return {'stopcode': 1, 'iterdone': self._major * self._cycleniter,
                'peak': np.float64(np.abs(STORES[self.residual_path].image).max())}

    def stop_descriptions(self):
        return {0: 'no stop', 1: 'iteration limit', 2: 'threshold'}
