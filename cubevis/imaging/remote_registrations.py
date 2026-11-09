'''remote_registrations.py
========================
The worker side of a remote interactive clean (iclean design §3, step 3):
``register`` makes ``ImagingSessionBackend`` available in a
``cubevis.remote`` worker; ``RemoteImagingSession``
(``_remote_session.py``) creates one and calls it.

This module runs **inside the worker process** on the kernel's host (the
data's host): it must import cold, with nothing but ``cubevis`` on the
path, and it is a root of the kernel fingerprint (``cubevis._fingerprint``).
``ImagingSessionBackend`` holds a plain ``LocalImagingSession`` and does not
know it is driven remotely; the methods below are what crosses the wire.

Cycles do not block the worker
------------------------------
A worker answers one call at a time.  A major cycle takes minutes, and while
it runs the GUI still asks for channels, spectra and statistics (it only
stops mask edits).  So a cycle runs in a thread, as ``arun_cycle`` does
locally (gclean's ``__anext__``): ``start_cycle`` begins it and returns,
``cycle_status`` reports it, with the cycle's progress (tclean's log, read
here: ``LocalImagingSession.progress``); the laptop polls.

Values on the wire
------------------
``remote_serialize`` keeps dicts (with int keys), lists, tuples and numpy
arrays; gclean's ``StopCodes`` namedtuple arrives as a plain tuple (the GUI
indexes it).  ``CycleResult`` is sent as a list and rebuilt on the laptop.
'''

import threading
import time
from typing import Any, Dict, List, Optional

#: ImageCube methods the laptop may call (``cube_call``); everything the GUI
#: (ImagePipe, CubeMask) uses.  ``coorddesc`` is served without its casatools
#: coordsys, which cannot cross the wire (see ``cube_call``).
CUBE_METHODS = frozenset({
    'channel', 'channel_reply', 'histogram', 'spectrum', 'statistics', 'statistics_config',
    'pixel_value', 'mask_value', 'stokes_labels', 'have_mask', 'have_mask0', 'mask', 'mask0',
    'put_mask', 'edit_mask', 'set_mask_name', 'coordinates', 'coorddesc', 'fits_header',
})


#: How a refused argument begins (``ImagingSessionBackend(check=True)``):
#: ``RemoteImagingSession`` raises it as a ValueError, without the worker's
#: traceback.
PARAMS_ERROR = "iclean's parameters"


def _load(dotted: str):
    '''``"module:name"`` -> the object'''
    import importlib
    module, _, name = dotted.partition(':')
    obj = importlib.import_module(module)
    for part in name.split('.'):
        obj = getattr(obj, part)
    return obj


class ImagingSessionBackend:
    '''``LocalImagingSession`` in the worker.

    Parameters
    ----------
    args : dict
        tclean's (iclean's) arguments, paths resolved on this host
    iclean_backend : str
        "PROD" (casatasks' gclean) or NAME (a module ``_gclean_NAME``)
    deconvolver : str
        ``"module:callable"`` making the deconvolver from ``**args`` instead
        (tests: ``cubevis.imaging.testing:ScriptedDeconvolver``)
    store : str
        ``"module:callable"`` making a field's ``ImageStore`` from
        ``(image_path, mask_path)``; casatools' by default
    check : bool
        check and coerce *args* here first, as tclean's task wrapper does
        (``_tclean_params.check``: paths must exist on this host, relative
        to this working directory, and are made absolute), and log the task
        in this host's CASA log -- what a local ``iclean`` does in the
        user's Python.  ``args()`` gives the checked arguments back.
    '''

    def __init__(self, args: Dict[str, Any], iclean_backend: str = 'PROD',
                 deconvolver: Optional[str] = None, store: Optional[str] = None,
                 check: bool = False) -> None:
        from cubevis.imaging import LocalImagingSession
        self._task_log = None
        if check:
            from cubevis.imaging._tclean_params import check as check_params
            try:
                args = check_params(args)
            except AssertionError as exc:
                import os
                import socket
                raise ValueError(f"{PARAMS_ERROR} (checked on {socket.gethostname()}, "
                                 f"in {os.getcwd()}): {exc}") from None
            try:
                from casatasks.private.task_logging import start_log
                self._task_log = start_log('iclean', [f'{k}={v!r}' for k, v in args.items()])
            except Exception:                                   # logging is not the task
                self._task_log = None
        self._args = args
        kw = {} if store is None else {'store_factory': _load(store)}
        if deconvolver is not None:
            self._session = LocalImagingSession(_load(deconvolver)(**args), **kw)
        else:
            self._session = LocalImagingSession.from_args(args, iclean_backend, **kw)
        self._lock = threading.Lock()
        self._cycle: Optional[dict] = None

    # ---- the imager ------------------------------------------------------

    def start_cycle(self, kind: str = 'run') -> int:
        '''Begin ``start()`` (the dirty image; ``kind='start'``) or
        ``run_cycle()`` in a thread; returns its number.  One at a time.'''
        if kind not in ('start', 'run'):
            raise ValueError(f"start_cycle: kind must be 'start' or 'run', not {kind!r}")
        with self._lock:
            if self._cycle is not None and not self._cycle['done']:
                raise RuntimeError('a cycle is already running')
            number = 1 if self._cycle is None else self._cycle['number'] + 1
            cycle = {'number': number, 'kind': kind, 'done': False, 'result': None,
                     'error': None, 'traceback': None, 'started': time.monotonic(), 'seconds': None}
            self._cycle = cycle

        def run():
            try:
                result = self._session.start() if kind == 'start' else self._session.run_cycle()
                cycle['result'] = list(result)
            except BaseException as exc:                      # reported, not raised: a thread
                import traceback
                cycle['error'] = f'{type(exc).__name__}: {exc}'
                cycle['traceback'] = traceback.format_exc()
            finally:
                cycle['seconds'] = time.monotonic() - cycle['started']
                cycle['done'] = True

        threading.Thread(target=run, name=f'iclean-cycle-{number}', daemon=True).start()
        return number

    def cycle_status(self, number: Optional[int] = None) -> dict:
        '''``{'number', 'kind', 'done', 'seconds'}``, and when done ``result``
        (``CycleResult`` as a list) or ``error`` and ``traceback``.'''
        cycle = self._cycle
        if cycle is None or (number is not None and cycle['number'] != number):
            return {'number': number, 'done': True, 'error': f'no cycle {number}', 'traceback': ''}
        out = {k: cycle[k] for k in ('number', 'kind', 'done')}
        out['seconds'] = cycle['seconds'] if cycle['done'] else time.monotonic() - cycle['started']
        out['progress'] = self._session.progress()
        if cycle['done']:
            for k in ('result', 'error', 'traceback'):
                out[k] = cycle[k]
        return out

    def progress(self) -> dict:
        '''the running (or last) cycle's progress (``LocalImagingSession.progress``)
        -- also for a GUI that was not polling, e.g. one reconnecting'''
        return self._session.progress()

    def run_cycle(self) -> list:
        '''One cycle, blocking the worker (tests; the GUI uses start_cycle).'''
        return list(self._session.run_cycle())

    def update(self, params: dict):
        return list(self._session.update(params))

    def has_next(self) -> bool:
        return bool(self._session.has_next())

    def log(self, history: bool = False) -> List[str]:
        return list(self._session.log(history))

    def restore(self) -> dict:
        return _wire_safe(self._session.restore())

    def stop_descriptions(self) -> dict:
        return {int(k): str(v) for k, v in self._session.stop_descriptions().items()}

    # ---- the images ------------------------------------------------------

    def image_products(self) -> list:
        return _wire_safe(self._session.image_products())

    def fields(self) -> list:
        return _wire_safe(self._session.fields())

    def cube_info(self, field: str, image: bool = False) -> dict:
        '''What the laptop's proxy keeps: the paths (informational), and with
        ``image`` (which opens the image: not before ``start`` has made it)
        the shape, the coordinate description and the stokes labels.'''
        cube = self._session.cube(field)
        info = {'image_path': cube.image_path, 'mask_path': cube.mask_path}
        if image:
            info.update(shape=[int(n) for n in cube.shape], coordinates=cube.coordinates(),
                        stokes_labels=list(cube.stokes_labels()))
        return info

    def cube_call(self, field: str, method: str, args: Optional[list] = None,
                  kwargs: Optional[dict] = None):
        '''``ImageCube.<method>(*args, **kwargs)`` on the field's cube.'''
        if method not in CUBE_METHODS:
            raise AttributeError(f'ImageCube.{method} is not available remotely')
        cube = self._session.cube(field)
        if method == 'coorddesc':
            ### the casatools coordsys stays here; the description replaces it
            desc = cube.coordinates()
            return {'csys': None, 'shape': list(desc['shape']), 'coordinates': desc}
        args = list(args or [])
        if method == 'channel' and len(args) > 1 and isinstance(args[1], str):
            import numpy as np
            args[1] = np.dtype(args[1])                           # sent by name
        return getattr(cube, method)(*args, **(kwargs or {}))

    def args(self) -> dict:
        '''iclean's arguments as the session got them (checked and coerced
        here when it was made with ``check``): what the GUI shows.'''
        return _wire_safe(self._args)

    def close(self) -> None:
        try:
            self._session.close()
        finally:
            if self._task_log is not None:
                state, self._task_log = self._task_log, None
                try:
                    from casatasks.private.task_logging import end_log
                    end_log(state, 'iclean', None)
                except Exception:
                    pass

    # ---- diagnostics -----------------------------------------------------

    def runtime_info(self) -> dict:
        import os
        import sys
        import cubevis
        return {'python': sys.version.split()[0], 'executable': sys.executable,
                'cubevis_path': os.path.dirname(cubevis.__file__), 'pid': os.getpid(),
                'cwd': os.getcwd()}


def _wire_safe(obj):
    '''numpy scalars and arrays to plain values, tuples kept; anything else
    the wire cannot carry (a casatools object) becomes its ``repr``.'''
    import numpy as np
    if isinstance(obj, dict):
        return {k if isinstance(k, (str, int, float, bool)) else str(k): _wire_safe(v)
                for k, v in obj.items()}
    if isinstance(obj, tuple):
        return tuple(_wire_safe(v) for v in obj)
    if isinstance(obj, list):
        return [_wire_safe(v) for v in obj]
    if isinstance(obj, np.ndarray):
        return obj
    if isinstance(obj, np.generic):
        return obj.item()
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    return repr(obj)


def register(comm, registry, **kwargs: Any) -> None:
    '''``register_function`` (``worker_main.handle_configure``).'''
    registry.register_class('ImagingSessionBackend', ImagingSessionBackend)
