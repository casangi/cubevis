'''Progress of a running cycle (cubevis.imaging._progress; iclean design step
4b), without CASA: tclean's log lines (as casalog writes them, from a real
run) followed by CycleProgress, and the status line describe() makes.

    pytest tests/manual/iclean-imaging/test_progress.py
'''
import threading
import time

import pytest

from cubevis.imaging import CycleProgress, describe_progress, LocalImagingSession
from cubevis.imaging import testing as T

#: from a real hogbom cycle on a 5-channel cube (casalog, 2026-11-07)
REAL = [
    "2026-11-07 19:59:25\tINFO\tdeconvolve::::casa\t##### Begin Task: deconvolve         #####",
    "2026-11-07 19:59:25\tINFO\tSynthesisDeconvolver::setupDeconvolution\tSet Deconvolution Options for [bl] : hogbom",
    "2026-11-07 19:59:25\tINFO\tSynthesisDeconvolver::executeCubeMinorCycle\t---------------------------------------------------- Run Minor Cycle Iterations  ---------------------------------------------",
    "2026-11-07 19:59:26\tINFO\tCubeMinorCycleAlgorithm::task\tProcessing channels in range [0, 4]",
    "2026-11-07 19:59:26\tINFO\tSDAlgorithmBase::deconvolve\t[bl] Run Hogbom minor-cycle on 5 chans | CycleThreshold=0.427844, CycleNiter=5000, Gain=0.1",
    "2026-11-07 19:59:26\tINFO\tSDAlgorithmBase::deconvolve\t[bl:C0] iters=0->9 [9], model=0->0.612647, peakres=1.00011->0.387463, Reached cyclethreshold.",
    "2026-11-07 19:59:26\tINFO\tSDAlgorithmBase::deconvolve\t[bl:C1] iters=9->18 [9], model=0->0.617985, peakres=1.0088->0.390964, Reached cyclethreshold.",
    "2026-11-07 19:59:26\tINFO\tSDAlgorithmBase::deconvolve\t[bl:C2] iters=18->33 [15], model=0->0.994927, peakres=1.00951->0.42671, Reached cyclethreshold.",
]
REST = [
    "2026-11-07 19:59:26\tINFO\tSDAlgorithmBase::deconvolve\t[bl:C3] iters=33->42 [9], model=0->0.611973, peakres=0.999009->0.387037, Reached cyclethreshold.",
    "2026-11-07 19:59:26\tINFO\tSDAlgorithmBase::deconvolve\t[bl:C4] iters=42->51 [9], model=0->0.612669, peakres=1.00015->0.387477, Reached cyclethreshold.",
    "2026-11-07 19:59:26\tINFO\tgrpcInteractiveCleanManager::mergeIterDone\tCompleted 51 iterations.",
    "2026-11-07 19:59:26\tINFO\ttclean::::casa\t##### Begin Task: tclean             #####",
    "2026-11-07 19:59:27\tINFO\ttask_tclean::SynthesisImager::executeMajorCycle\t----------------------------------------------------------- Run Major Cycle 2 -------------------------------------",
]


def write(path, lines, partial=''):
    with open(path, 'a') as f:
        f.write(''.join(line + '\n' for line in lines) + partial)


def test_a_real_cycle_read_as_it_is_written(tmp_path):
    log = tmp_path / 'casa.log'
    write(log, ['2026-11-07 19:59:20\tINFO\ttclean::::casa\tsomething before the cycle'])
    p = CycleProgress(lambda: str(log))
    assert p.begin('run') == 1
    s = p.snapshot()
    assert s['running'] and s['activity'] == 0 and s['phase'] is None     # what came before is skipped
    write(log, REAL)
    s = p.snapshot()
    assert (s['phase'], s['algorithm'], s['channels'], s['channels_done']) == ('minor cycles', 'Hogbom', 5, 3)
    assert s['iterations'] == 33 and s['peak_residual'] == pytest.approx(0.42671)
    assert s['activity'] == len(REAL) and s['quiet_seconds'] is not None
    assert describe_progress(s, 'Running') == \
        'Running · 0:00 · minor cycles (Hogbom): 3 of 5 channels, 33 iterations, peak residual 0.4267'
    write(log, REST)
    s = p.snapshot()
    assert (s['phase'], s['channels_done'], s['iterations'], s['major_cycle']) == ('major cycle', 5, 51, 2)
    assert describe_progress(s, 'Running').endswith('major cycle (51 iterations so far)')
    p.end(True)
    done = p.snapshot()
    assert not done['running'] and done['ok'] and done['iterations'] == 51
    assert p.begin('run') == 2 and p.snapshot()['iterations'] is None        # a new cycle starts afresh


def test_a_line_written_in_two_pieces_is_read_once(tmp_path):
    log = tmp_path / 'casa.log'
    log.write_text('')
    p = CycleProgress(lambda: str(log))
    p.begin('run')
    write(log, REAL[:5], partial=REAL[5][:40])
    assert p.snapshot()['channels_done'] == 0
    write(log, [REAL[5][40:]])
    s = p.snapshot()
    assert s['channels_done'] == 1 and s['iterations'] == 9 and s['activity'] == 6


def test_a_log_that_starts_again_is_read_from_its_start(tmp_path):
    log = tmp_path / 'casa.log'
    write(log, ['x\tINFO\ty\t' + 'z' * 500])
    p = CycleProgress(lambda: str(log))
    p.begin('run')
    log.write_text('')                                  # rotated or truncated
    write(log, REAL[4:6])
    assert p.snapshot()['channels_done'] == 1


def test_without_a_log_only_the_time_is_known(tmp_path):
    p = CycleProgress(lambda: None)
    p.begin('start')
    time.sleep(0.05)
    s = p.snapshot()
    assert s['running'] and not s['logged'] and s['seconds'] >= 0.05 and s['phase'] is None
    assert describe_progress(s, 'Running') == 'Running · 0:00'
    p2 = CycleProgress(lambda: (_ for _ in ()).throw(RuntimeError('no casalog')))
    p2.begin('run')                                     # a failing source is no log
    assert not p2.snapshot()['logged']


def test_describe_says_when_tclean_has_been_quiet():
    s = {'seconds': 125, 'phase': 'minor cycles', 'channels': 1, 'channels_done': 0, 'quiet_seconds': 42}
    assert describe_progress(s, 'Running') == 'Running · 2:05 · minor cycles: 0 of 1 channel · tclean quiet for 42 s'
    assert describe_progress(None, 'Running') == 'Running'


def test_the_session_follows_its_cycles():
    '''LocalImagingSession.progress during a cycle (the scripted imager writes
    tclean's lines into its own log while it works)'''
    s = LocalImagingSession(T.ScriptedDeconvolver(imagename='prog', nchan=4, niter=40, cycleniter=8,
                                                  cycle_seconds=1.0), store_factory=T.numpy_store)
    s.start()
    assert s.progress()['kind'] == 'start' and not s.progress()['running']
    seen = []
    th = threading.Thread(target=s.run_cycle)
    th.start()
    while th.is_alive():
        time.sleep(0.1)
        seen.append(s.progress())
    running = [p for p in seen if p['running']]
    assert len({p['channels_done'] for p in running}) >= 3             # it moved while we watched
    assert running[-1]['seconds'] > running[0]['seconds']
    last = s.progress()
    assert last['number'] == 2 and last['ok'] and last['channels_done'] == 4 and last['iterations'] == 8


def test_a_failed_cycle_is_marked():
    s = LocalImagingSession(T.ScriptedDeconvolver(imagename='pfail', fail_at=1), store_factory=T.numpy_store)
    s.start()
    with pytest.raises(RuntimeError):
        s.run_cycle()
    p = s.progress()
    assert not p['running'] and p['ok'] is False and p['kind'] == 'run'
