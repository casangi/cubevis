'''Interactive clean's clean controls (continue, finish, stop) through the
handlers the browser uses, without CASA (iclean design step 4): the real
InteractiveCleanUI on the scripted deconvolver of ``cubevis.imaging.testing``,
in this process and through a Jupyter kernel on this computer ("python3").

    pytest tests/manual/iclean-imaging/test_clean_controls.py
'''
import asyncio
import inspect
import time

import numpy as np
import pytest

from cubevis.imaging import LocalImagingSession
from cubevis.imaging import testing as T

KERNEL = 'python3'
DEC = 'cubevis.imaging.testing:ScriptedDeconvolver'
STORE = 'cubevis.imaging.testing:numpy_store'


def make_ui(where, monkeypatch, **extra):
    '''InteractiveClean with the scripted imager, here ('local') or in a kernel
    ('remote'); returns (app, ui) with the GUI built as for a browser'''
    import cubevis.toolbox.iclean._app as app
    from cubevis.private.apps import InteractiveClean

    class Sessions:
        @staticmethod
        def from_args(args, iclean_backend='PROD'):
            args = dict(args, **extra)
            if where == 'local':
                return LocalImagingSession(T.ScriptedDeconvolver(**args), store_factory=T.numpy_store)
            from cubevis.imaging import RemoteImagingSession
            return RemoteImagingSession(args, KERNEL, deconvolver=DEC, store=STORE)

    monkeypatch.setattr(app, 'LocalImagingSession', Sessions)
    ic = InteractiveClean(vis='scripted.ms', imagename=extra.pop('imagename', 'ctl'), imsize=[20, 14],
                          nchan=4, niter=30, cycleniter=10, threshold='0.0Jy')
    ui = ic._ui
    ui._setup()
    ui._build_bokeh()
    return ic, ui


def handlers(ui):
    out = {}
    for _, hs in ui._comm_mgr._handlers.items():
        out.update(hs)
    return out


async def call(h, msg):
    r = h(msg, context=None) if 'context' in inspect.signature(h).parameters else h(msg)
    return await r if asyncio.iscoroutine(r) else r


def button(ui, action, **iteration):
    it = {'niter': '30', 'cycleniter': '10', 'nmajor': '-1', 'threshold': '0.0Jy',
          'cyclefactor': '3', 'gain': '0.1', 'nsigma': '0.0'}
    it.update({k: str(v) for k, v in iteration.items()})
    h = handlers(ui)[ui._clean_ids['continue' if action == 'next' else action]]
    return call(h, {'action': action, 'value': {'iteration': it, 'automask': {},
                                                'masks': {}, 'breadcrumbs': {}}})


def close(ic):
    session = ic._ui._clean['session']
    if hasattr(session, 'connection'):
        session.close()


@pytest.fixture(autouse=True)
def no_browser(monkeypatch):
    '''building the GUI asks ``webbrowser`` for one; "true" is a browser that
    does nothing (CI and headless machines have none)'''
    import webbrowser
    monkeypatch.setenv('BROWSER', 'true')
    monkeypatch.setattr(webbrowser, '_tryorder', None)
    monkeypatch.setattr(webbrowser, '_browsers', {})


@pytest.fixture(params=['local', 'remote'])
def where(request):
    if request.param == 'remote':
        pytest.importorskip('ipykernel')
    return request.param


def test_continue_runs_one_cycle_and_reports_convergence(where, monkeypatch):
    ic, ui = make_ui(where, monkeypatch, imagename=f'cont-{where}')
    try:
        r = asyncio.run(button(ui, 'next'))
        assert r['result'] == 'update', r
        assert r['majordone'] == 1 and r['iterleft'] == 20 and r['iterdone'] == 10
        field = list(r['convergence'])[0]
        assert len(r['convergence'][field][0][0]['peakRes']) == 2         # dirty + one cycle
        assert r['cyclethreshold'][field][-1] > 0
        assert any('cycle 1' in line for line in r['cmd'])
    finally:
        close(ic)


def test_finish_repeats_until_a_stopping_criterion(where, monkeypatch):
    '''the browser sends 'finish' again after each cycle until one reports
    convergence; here the loop the browser runs'''
    ic, ui = make_ui(where, monkeypatch, imagename=f'fin-{where}')
    try:
        async def finish():
            replies = []
            while True:
                r = await button(ui, 'finish', niter=replies[-1]['iterleft'] if replies else 30)
                replies.append(r)
                if r['result'] != 'update' or len(replies) > 10:
                    return replies
        replies = asyncio.run(finish())
        assert [r['result'] for r in replies] == ['update', 'update', 'converged'], replies
        assert replies[-1]['iterleft'] == 0 and replies[-1]['stopcode'][0] == 1
    finally:
        close(ic)


def test_a_refused_value_is_an_error_and_runs_no_cycle(where, monkeypatch):
    '''gclean's update() refusing a value (here the threshold) is shown, and
    no cycle runs with the old values (until 2026-11-07 it was ignored)'''
    ic, ui = make_ui(where, monkeypatch, imagename=f'bad-{where}')
    try:
        r = asyncio.run(button(ui, 'next', threshold='abc'))
        assert r['result'] == 'error' and 'threshold must be a number' in r['stopdesc']
        assert r['stopcode'] == [-1, -1]
        r = asyncio.run(button(ui, 'next'))
        assert r['result'] == 'update' and r['majordone'] == 1               # nothing ran before
    finally:
        close(ic)


def test_a_failing_cycle_is_an_error_the_gui_shows(where, monkeypatch):
    '''the imager raising during a cycle: an 'error' reply with the message
    (until 2026-11-07 the handler raised and the browser waited for ever)'''
    ic, ui = make_ui(where, monkeypatch, imagename=f'fail-{where}', fail_at=2)
    try:
        assert asyncio.run(button(ui, 'next'))['result'] == 'update'
        r = asyncio.run(button(ui, 'next'))
        assert r['result'] == 'error', r
        assert 'scripted failure in cycle 2' in r['stopdesc'] and 'RuntimeError' in r['stopdesc']
        assert '--- remote traceback ---' not in r['stopdesc']
        assert any('cycle 1' in line for line in r['cmd'])                   # the log so far
    finally:
        close(ic)


def test_stop_during_finish_waits_for_the_cycle(where, monkeypatch):
    '''the orange stop: the browser sends 'status' while a 'finish' cycle runs
    and stops sending 'finish'; a cycle is not interrupted (gclean cannot), so
    tclean stops after it.  The GUI's loop stays free meanwhile.'''
    ic, ui = make_ui(where, monkeypatch, imagename=f'stop-{where}', cycle_seconds=1.5)
    try:
        async def stop_during():
            t0 = time.perf_counter()
            cycle = asyncio.ensure_future(button(ui, 'finish'))
            await asyncio.sleep(0.3)
            status = await call(handlers(ui)[ui._clean_ids['stop']], {'action': 'status', 'value': {}})
            ticks = 0
            while not cycle.done():                      # Bokeh's loop keeps running
                await asyncio.sleep(0.05)
                ticks += 1
            return status, await cycle, time.perf_counter() - t0, ticks
        status, r, seconds, ticks = asyncio.run(stop_during())
        assert status['result'] == 'ok'
        assert r['result'] == 'update' and r['majordone'] == 1 and seconds >= 1.5
        assert ticks >= 10
    finally:
        close(ic)


def test_a_dead_worker_is_an_error_the_gui_shows(monkeypatch):
    pytest.importorskip('ipykernel')
    ic, ui = make_ui('remote', monkeypatch, imagename='dead')
    try:
        session = ui._clean['session']
        try:
            session.connection.exec_code('import os; os._exit(3)', timeout=10)
        except Exception:
            pass
        time.sleep(1.0)
        r = asyncio.run(button(ui, 'next'))
        assert r['result'] == 'error', r
        assert 'worker' in r['stopdesc'] and 'gone' in r['stopdesc'], r['stopdesc']
        r = asyncio.run(button(ui, 'next'))                                  # and stays so
        assert r['result'] == 'error' and 'gone' in r['stopdesc']
    finally:
        close(ic)
