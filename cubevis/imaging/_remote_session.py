'''``RemoteImagingSession``: the imaging layer on a (remote or local)
Jupyter kernel, for the GUI on this computer (iclean design §3, step 3).

It has ``LocalImagingSession``'s surface, so ``InteractiveCleanUI`` and
``CubeMask``/``ImagePipe`` use it unchanged; every call goes to an
``ImagingSessionBackend`` (``remote_registrations.py``) in a worker on the
kernel, through one ``cubevis.remote.RemoteObjectConnection`` (one kernel,
one worker, one object per interactive clean).  ``cube(field)`` is a
``RemoteImageCube``: a proxy that forwards each ``ImageCube`` method as
``cube_call(field, method, ...)`` on that same object.

* Paths in ``args`` (``vis``, ``imagename``) are the kernel host's; relative
  ones are relative to the kernel's working directory.
* ``start()`` and ``arun_cycle()`` run in a thread in the worker and are
  polled (``CYCLE_POLL`` seconds), so the worker keeps answering the GUI
  during a cycle; ``arun_cycle`` awaits on the caller's event loop without
  blocking it.  No budget applies to a cycle as a whole; the connection
  notices a kernel that stops or goes quiet.
* A worker that dies is not replaced: tclean's state was in it
  (``RemoteWorkerDiedError``).  The images it wrote stay on disk.

Importing this module imports nothing remote; the connection is made by the
constructor.
'''

import asyncio
from typing import Any, Dict, List, Optional

from ._protocols import CycleResult

REGISTER_FUNCTION = 'cubevis.imaging.remote_registrations:register'
BACKEND_CLASS = 'ImagingSessionBackend'
#: seconds between asking the worker whether a cycle is done
CYCLE_POLL = 0.25
#: calls that read image data (the longer budget, ``CUBEVIS_REMOTE_TIMEOUT``)
DATA_METHODS = ('cube_call', 'cube_info', 'restore', 'run_cycle')


class RemoteImagingSession:
    '''An ``ImagingSession`` whose deconvolver and images are on the kernel
    ``kernel_name``.

    Parameters
    ----------
    args : dict
        tclean's (iclean's) arguments, paths as seen on the kernel's host
    kernel_name : str
        a kernelspec name (an sshpyk kernel, or ``"python3"`` here)
    iclean_backend : str
        "PROD" or NAME (``_gclean_NAME``), resolved on the kernel's host
    deconvolver, store : str
        ``"module:callable"`` alternatives for the deconvolver and the image
        store, resolved in the worker (tests, other imagers)
    connection_kwargs :
        passed to ``RemoteObjectConnection`` (timeouts, ...)
    '''

    def __init__(self, args: Dict[str, Any], kernel_name: str, *, iclean_backend: str = 'PROD',
                 deconvolver: Optional[str] = None, store: Optional[str] = None,
                 **connection_kwargs) -> None:
        from cubevis.remote import RemoteObjectConnection
        kwargs = {'args': dict(args), 'iclean_backend': iclean_backend}
        if deconvolver is not None:
            kwargs['deconvolver'] = deconvolver
        if store is not None:
            kwargs['store'] = store
        self._conn = RemoteObjectConnection(
            kernel_name, BACKEND_CLASS, kwargs, register_function=REGISTER_FUNCTION,
            app='iclean', data=args.get('vis'), data_methods=DATA_METHODS, **connection_kwargs)
        self._cubes: Dict[str, 'RemoteImageCube'] = {}
        self._fields = None
        self._products = None

    @property
    def connection(self):
        '''the ``RemoteObjectConnection`` (diagnostics, tests)'''
        return self._conn

    @property
    def deconvolver(self):
        '''None: the deconvolver is in the worker, on the kernel's host.'''
        return None

    # ---- the imager -------------------------------------------------------

    async def _acycle(self, kind: str) -> CycleResult:
        conn = self._conn
        number = await conn.acall('start_cycle', kind=kind)
        while True:
            status = await conn.acall('cycle_status', number=number)
            if status.get('done'):
                break
            await asyncio.sleep(CYCLE_POLL)
        if status.get('error'):
            from cubevis.remote import RemoteCallError
            raise RemoteCallError('run_cycle' if kind == 'run' else 'start',
                                  {'error': status['error'], 'traceback': status.get('traceback', '')})
        return CycleResult(*status['result'])

    def start(self) -> CycleResult:
        '''The dirty image (blocks this thread; the worker stays responsive).'''
        return self._conn.run(self._acycle('start'))

    def run_cycle(self) -> CycleResult:
        '''One major cycle and its minor cycles (blocks this thread).'''
        return self._conn.run(self._acycle('run'))

    async def arun_cycle(self) -> CycleResult:
        '''``run_cycle`` awaited on the caller's loop without blocking it.'''
        return await asyncio.wrap_future(self._conn.submit(self._acycle('run')))

    def update(self, params: dict):
        return tuple(self._conn.call('update', params=params))

    def has_next(self) -> bool:
        return self._conn.call('has_next')

    def log(self, history: bool = False) -> List[str]:
        return self._conn.call('log', history=history)

    def restore(self) -> dict:
        return self._conn.call('restore')

    def stop_descriptions(self) -> Dict[int, str]:
        return self._conn.call('stop_descriptions')

    # ---- the images -------------------------------------------------------

    def image_products(self) -> List[dict]:
        if self._products is None:
            self._products = self._conn.call('image_products')
        return self._products

    def fields(self) -> List[dict]:
        if self._fields is None:
            self._fields = self._conn.call('fields')
        return [dict(f) for f in self._fields]

    def cube(self, field: str) -> 'RemoteImageCube':
        if field not in self._cubes:
            self._cubes[field] = RemoteImageCube(self._conn, field, self._conn.call('cube_info', field=field))
        return self._cubes[field]

    def close(self) -> None:
        if self._conn.closed:
            return
        try:
            self._conn.call('close')
        except Exception:
            pass
        self._conn.close()


class RemoteImageCube:
    '''One field's ``ImageCube`` in the worker, for ``ImagePipe``/``CubeMask``
    here.  The paths, and on first use the shape, coordinate description and
    stokes labels, do not change and are kept (``cube_info``); everything else
    is a call.

    ``coorddesc()['csys']`` is None: the casatools coordsys stays on the
    kernel's host (``convert_masks(format='region')`` needs it; world
    coordinates come from ``coordinates``).'''

    def __init__(self, conn, field: str, info: dict) -> None:
        self._conn = conn
        self._field = field
        self._image_path = info.get('image_path')
        self._mask_path = info.get('mask_path')
        self._image = None              # shape, coordinates, stokes labels: on first use

    def _image_info(self) -> dict:
        '''shape, coordinates and stokes labels, asked for once -- not at
        construction: the GUI makes its cubes before ``start()`` has written
        the images, and a local ``ImageCube`` too opens its image on first use'''
        if self._image is None:
            self._image = self._conn.call('cube_info', field=self._field, image=True)
        return self._image

    def _call(self, method: str, *args, **kwargs):
        return self._conn.call('cube_call', field=self._field, method=method,
                               args=list(args), kwargs=kwargs)

    @property
    def field(self) -> str:
        return self._field

    @property
    def shape(self) -> List[int]:
        return list(self._image_info()['shape'])

    @property
    def image_path(self) -> Optional[str]:
        '''on the kernel's host (informational)'''
        return self._image_path

    @property
    def mask_path(self) -> Optional[str]:
        '''on the kernel's host (informational)'''
        return self._mask_path

    def coordinates(self) -> dict:
        return self._image_info()['coordinates']

    def stokes_labels(self):
        return list(self._image_info()['stokes_labels'])

    def coorddesc(self) -> dict:
        info = self._image_info()
        return {'csys': None, 'shape': list(info['shape']), 'coordinates': info['coordinates']}

    # ---- forwarded -------------------------------------------------------

    def channel(self, index, pixel_type, quant=None):
        import numpy as np
        return self._call('channel', index, np.dtype(pixel_type).name, quant)     # a dtype by name

    def channel_reply(self, index, quant=None, *, stats=False, hist_bins=None):
        return self._call('channel_reply', index, quant, stats=stats, hist_bins=hist_bins)

    def histogram(self, index, nbins):
        return self._call('histogram', index, nbins)

    def spectrum(self, index, mask=False):
        return self._call('spectrum', index, mask)

    def statistics_config(self, use_mask=None):
        return self._call('statistics_config', use_mask)

    def statistics(self, index):
        return self._call('statistics', index)

    def pixel_value(self, chan, index):
        return self._call('pixel_value', chan, index)

    def mask_value(self, chan, index):
        return self._call('mask_value', chan, index)

    def have_mask(self):
        return self._call('have_mask')

    def have_mask0(self):
        return self._call('have_mask0')

    def mask(self, index, modify=False):
        return self._call('mask', index, modify)

    def mask0(self, index):
        return self._call('mask0', index)

    def put_mask(self, index, mask):
        return self._call('put_mask', index, mask)

    def edit_mask(self, op, scope, index, *, polygon=None, src=None):
        return self._call('edit_mask', op, scope, index, polygon=polygon, src=src)

    def set_mask_name(self, new_mask_path):
        self._mask_path = new_mask_path
        return self._call('set_mask_name', new_mask_path)

    def fits_header(self):
        return self._call('fits_header')

    def close(self):
        '''nothing here: the session closes the worker's cubes'''
