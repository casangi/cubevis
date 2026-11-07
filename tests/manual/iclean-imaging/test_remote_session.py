'''RemoteImagingSession against a Jupyter kernel on this computer (kernelspec
"python3"; iclean design step 3), without CASA: the scripted deconvolver and
the numpy store of ``cubevis.imaging.testing`` run in the kernel's worker and,
for comparison, in this process (``LocalImagingSession``).

    pytest tests/manual/iclean-imaging/test_remote_session.py

The real thing (tclean, CASA images) is compared by
``record_iclean_handlers.py --kernel python3``.
'''
import asyncio
import time

import numpy as np
import pytest

pytest.importorskip('jupyter_client')
pytest.importorskip('ipykernel')

from cubevis.imaging import (RemoteImagingSession, LocalImagingSession, CycleResult,
                             as_imaging_session, ImagingSession)
from cubevis.imaging import testing as T
from cubevis.remote import RemoteCallError, RemoteWorkerDiedError

KERNEL = 'python3'
DEC = 'cubevis.imaging.testing:ScriptedDeconvolver'
STORE = 'cubevis.imaging.testing:numpy_store'
ARGS = dict(imagename='rt', imsize=[20, 14], nchan=4, niter=30, cycleniter=10)


def local_session(args=ARGS):
    return LocalImagingSession(T.ScriptedDeconvolver(**args), store_factory=T.numpy_store)


@pytest.fixture(scope='module')
def pair():
    '''(local, remote): the same scripted clean in this process and in the kernel'''
    remote = RemoteImagingSession(ARGS, KERNEL, deconvolver=DEC, store=STORE)
    local = local_session()
    yield local, remote
    remote.close()


def same(a, b, path='value'):
    '''equal, numpy arrays and nested containers included'''
    if isinstance(a, np.ndarray) or isinstance(b, np.ndarray):
        assert np.array_equal(np.asarray(a), np.asarray(b)), path
        assert np.asarray(a).dtype == np.asarray(b).dtype, f'{path}: dtype'
    elif isinstance(a, dict):
        assert isinstance(b, dict) and set(a) == set(b), f'{path}: keys {set(a)} != {set(b)}'
        for k in a:
            same(a[k], b[k], f'{path}[{k!r}]')
    elif isinstance(a, (list, tuple)):
        assert isinstance(b, (list, tuple)) and len(a) == len(b), path
        for i, (x, y) in enumerate(zip(a, b)):
            same(x, y, f'{path}[{i}]')
    else:
        assert a == b, f'{path}: {a!r} != {b!r}'


def test_is_an_imaging_session(pair):
    _, remote = pair
    assert isinstance(remote, ImagingSession)
    assert as_imaging_session(remote) is remote
    assert remote.deconvolver is None


def test_session_facts_match(pair):
    local, remote = pair
    same(local.fields(), remote.fields())
    same(local.image_products(), remote.image_products())
    same(local.stop_descriptions(), remote.stop_descriptions())
    assert all(isinstance(k, int) for k in remote.stop_descriptions())


def test_cycles_cubes_and_mask_edits_match(pair):
    '''start, cycles, and every cube call the GUI makes give the same answer'''
    local, remote = pair
    a, b = local.start(), remote.start()
    assert isinstance(b, CycleResult)
    same(tuple(a), tuple(b), 'start')
    lc, rc = local.cube('rt'), remote.cube('rt')
    assert rc.shape == lc.shape and rc.image_path == lc.image_path and rc.mask_path == lc.mask_path
    same(lc.coordinates(), rc.coordinates(), 'coordinates')
    same(list(lc.stokes_labels()), rc.stokes_labels())

    def views(tag):
        quant = {'bounds': [[-1.0], [2.0]], 'transfer': {'scaling': 'log', 'args': {'alpha': 100}}}
        for c in range(4):
            same(lc.channel_reply([0, c], None, stats=True, hist_bins=16),
                 rc.channel_reply([0, c], None, stats=True, hist_bins=16), f'{tag} channel_reply {c}')
        same(lc.channel_reply([0, 2], quant), rc.channel_reply([0, 2], quant), f'{tag} quantized')
        same(lc.channel([0, 1], np.float64), rc.channel([0, 1], np.float64), f'{tag} channel float')
        same(lc.channel([0, 1], np.uint8), rc.channel([0, 1], np.uint8), f'{tag} channel uint8')
        same(lc.spectrum([5, 7, 0], True), rc.spectrum([5, 7, 0], True), f'{tag} spectrum')
        same(lc.histogram([0, 3], 10), rc.histogram([0, 3], 10), f'{tag} histogram')
        same(lc.pixel_value([0, 2], [3, 4]), rc.pixel_value([0, 2], [3, 4]), f'{tag} pixel')
        same(lc.mask([0, 1]), rc.mask([0, 1]), f'{tag} mask')
        same(lc.mask([0, 1], True), rc.mask([0, 1], True), f'{tag} mask raw')
        same(lc.mask_value([0, 1], [4, 4]), rc.mask_value([0, 1], [4, 4]), f'{tag} mask value')
        for use_mask in (False, True):
            lc.statistics_config(use_mask)
            rc.statistics_config(use_mask)
            same(lc.statistics([0, 2]), rc.statistics([0, 2]), f'{tag} statistics mask={use_mask}')
        same(lc.have_mask(), rc.have_mask())
        same(lc.have_mask0(), rc.have_mask0())

    views('dirty')
    edits = [('add', 'cube', [0, 0], dict(polygon={'xs': [2.2, 9.7, 6.1], 'ys': [1.4, 3.9, 11.6]})),
             ('subtract', 'chan', [0, 2], dict(polygon={'xs': [4, 8, 8, 4], 'ys': [2, 2, 6, 6]})),
             ('add', 'chan', [0, 3], dict(src=[0, 0])),
             ('subtract', 'chan', [0, 3], dict(src=[0, 3])),
             ('invert', 'cube', [0, 1], {}),
             ('clear', 'chan', [0, 1], {})]
    for op, scope, index, kw in edits:
        same(lc.edit_mask(op, scope, index, **kw), rc.edit_mask(op, scope, index, **kw), f'edit {op}')
    m = lc.mask([0, 2], True)
    m[0:3, 0:3] = 1
    lc.put_mask([0, 2], m)
    rc.put_mask([0, 2], m)
    views('edited')

    for n in range(2):
        a, b = local.run_cycle(), asyncio.run(remote.arun_cycle())
        same(tuple(a), tuple(b), f'cycle {n}')
        views(f'after cycle {n}')                       # the residual is read again (generation)
    same(local.update({'niter': 5}), remote.update({'niter': 5}), 'update')
    same(local.update({'niter': 'x'}), remote.update({'niter': 'x'}), 'update refused')
    same(local.has_next(), remote.has_next())
    same(local.log(), remote.log(), 'log')
    same(local.restore(), remote.restore(), 'restore')


def test_the_worker_answers_during_a_cycle():
    '''a cycle runs in a thread in the worker: the GUI's calls are answered
    meanwhile, and arun_cycle does not block the caller's loop'''
    remote = RemoteImagingSession(dict(ARGS, imagename='slow', cycle_seconds=2.0), KERNEL,
                                  deconvolver=DEC, store=STORE)
    try:
        remote.start()
        cube = remote.cube('slow')

        async def during():
            t0 = time.perf_counter()
            task = asyncio.ensure_future(remote.arun_cycle())
            answered = []
            while not task.done():
                await asyncio.sleep(0.05)               # this loop keeps running
                t = time.perf_counter()
                cube.statistics([0, 0])
                answered.append(time.perf_counter() - t)
            return await task, time.perf_counter() - t0, answered

        result, seconds, answered = asyncio.run(during())
        assert result.majordone == 1 and seconds >= 2.0
        assert len(answered) >= 10 and max(answered) < 1.0, (len(answered), max(answered))
    finally:
        remote.close()


def test_a_failing_cycle_raises_with_the_remote_traceback():
    remote = RemoteImagingSession(dict(ARGS, imagename='fails', fail_at=1), KERNEL,
                                  deconvolver=DEC, store=STORE)
    try:
        remote.start()
        with pytest.raises(RemoteCallError) as info:
            asyncio.run(remote.arun_cycle())
        assert 'scripted failure in cycle 1' in str(info.value)
        assert 'Traceback' in info.value.remote_traceback
        assert remote.fields()[0]['id'] == 'fails'          # the session is still usable
    finally:
        remote.close()


def test_only_the_cube_methods_the_gui_uses_are_served(pair):
    _, remote = pair
    with pytest.raises(RemoteCallError, match='not available remotely'):
        remote.connection.call('cube_call', field='rt', method='close')
    with pytest.raises(RemoteCallError):
        remote.cube('nonesuch')
    desc = remote.cube('rt').coorddesc()
    assert desc['csys'] is None and desc['coordinates']['shape'] == [20, 14, 1, 4]


def test_a_dead_worker_is_reported_and_not_replaced():
    remote = RemoteImagingSession(dict(ARGS, imagename='dies'), KERNEL, deconvolver=DEC, store=STORE)
    try:
        remote.start()
        try:
            remote.connection.exec_code('import os; os._exit(3)', timeout=10)
        except Exception:
            pass
        with pytest.raises((RemoteWorkerDiedError, RemoteCallError)):
            for _ in range(20):                         # the supervisor notices the exit
                remote.cube('dies').statistics([0, 0])
                time.sleep(0.2)
        with pytest.raises(RemoteWorkerDiedError):
            remote.has_next()
    finally:
        remote.close()
        remote.close()                                  # idempotent
