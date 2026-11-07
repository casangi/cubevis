'''Progress of a running cycle (iclean design step 4b, notes §69).

A cycle -- gclean's ``next()``: deconvolve's minor cycles, then tclean's major
cycle -- returns only at its end.  While it runs, tclean writes to its log
(casalog): which task began, the minor-cycle algorithm and how many channels
it works on, one line per channel as each finishes (iterations, peak
residual), the iterations completed, the major cycle.  ``CycleProgress``
follows that log from where it was when the cycle began and keeps what it
read as **state**: a ``snapshot()`` says where the cycle stands, whoever asks
and whenever -- the GUI while it waits, a remote poll, later a GUI that
reconnects to a session still running (design §5, reattach).

Every value is optional: without a log only the elapsed time is known.
``activity`` (log lines read so far) and ``quiet_seconds`` (since the last
one) say that tclean is alive and working; ``seconds`` that the cycle is
being followed.

No CASA at import: the log's path comes from a callable (by default
casatasks' ``casalog.logfile()``, imported when first needed).
'''

import os
import re
import threading
import time
from typing import Callable, Optional

#: at most this much of the log is read per snapshot (the rest is skipped: a
#: snapshot is cheap whatever tclean wrote meanwhile)
READ_LIMIT = 256 * 1024

_TASK = re.compile(r'##### Begin Task: (\w+)')
_MAJOR = re.compile(r'Run Major Cycle (\d+)')
_MINOR = re.compile(r'Run (\w+) minor-cycle on (\d+) chans')
_CHANNEL = re.compile(r'\[([^\]]*?):C(\d+)\] iters=(\d+)->(\d+) \[(\d+)\].*?peakres=([-+0-9.eE]+)->([-+0-9.eE]+)')
_COMPLETED = re.compile(r'Completed (\d+) iterations')


def casalog_path() -> Optional[str]:
    '''casatasks' log file, or None (no casatasks, or logging to nowhere)'''
    try:
        from casatasks import casalog              # pylint: disable=import-outside-toplevel
        path = casalog.logfile()
    except Exception:                              # pylint: disable=broad-except
        return None
    return path if path and os.path.isfile(path) else None


class CycleProgress:
    '''The state of the cycle that runs, or ran last.  ``begin``/``end``
    are called by the session around a cycle (any thread); ``snapshot``
    reads what the log gained since and returns the state (thread-safe).

    Parameters
    ----------
    log_path : callable
        returns the log file to follow, or None; ``casalog_path`` by default
    '''

    def __init__(self, log_path: Optional[Callable[[], Optional[str]]] = None) -> None:
        self._log_path = log_path or casalog_path
        self._lock = threading.Lock()
        self._state = {'running': False, 'number': 0, 'kind': None}
        self._path = None
        self._pos = 0
        self._started = None
        self._last_line = None

    # ---- around a cycle ------------------------------------------------------

    def begin(self, kind: str) -> int:
        '''a cycle of ``kind`` ('start': the dirty image; 'run': a major cycle
        and its minor cycles) begins; returns its number'''
        try:
            path = self._log_path()
        except Exception:                          # pylint: disable=broad-except
            path = None
        with self._lock:
            number = self._state['number'] + 1
            self._path = path
            self._pos = os.path.getsize(path) if path and os.path.isfile(path) else 0
            self._started = time.monotonic()
            self._last_line = None
            self._state = {'running': True, 'number': number, 'kind': kind, 'seconds': 0.0,
                           'phase': None, 'algorithm': None, 'channels': None, 'channels_done': 0,
                           'iterations': None, 'peak_residual': None, 'major_cycle': None,
                           'activity': 0, 'quiet_seconds': None, 'logged': path is not None,
                           'ok': None, 'detail': None}
            return number

    def end(self, ok: bool = True) -> None:
        with self._lock:
            self._read()
            self._state['running'] = False
            self._state['ok'] = bool(ok)
            if self._started is not None:
                self._state['seconds'] = time.monotonic() - self._started

    # ---- the state -----------------------------------------------------------

    def snapshot(self) -> dict:
        '''where the cycle stands (a copy): ``running``, ``number``, ``kind``,
        ``seconds``, and from the log ``phase`` ('minor cycles', 'major cycle'),
        ``algorithm``, ``channels`` / ``channels_done`` (of the minor cycle),
        ``iterations`` (the minor cycles' total so far), ``peak_residual`` (the
        last channel's), ``major_cycle``, ``activity``, ``quiet_seconds``,
        ``detail`` (the last line that said something)'''
        with self._lock:
            if self._state['running']:
                self._read()
                now = time.monotonic()
                self._state['seconds'] = now - self._started
                self._state['quiet_seconds'] = (None if self._last_line is None
                                                else now - self._last_line)
            return dict(self._state)

    def _read(self) -> None:
        if not self._path:
            return
        try:
            size = os.path.getsize(self._path)
            if size < self._pos:                   # a new log (rotated): from its start
                self._pos = 0
            if size == self._pos:
                return
            start = max(self._pos, size - READ_LIMIT)
            with open(self._path, 'rb') as f:
                f.seek(start)
                data = f.read(size - start)
        except OSError:
            return
        ### whole lines only: a partial last line is read again next time
        end = data.rfind(b'\n')
        if end < 0:
            return
        self._pos = start + end + 1
        for raw in data[:end].split(b'\n'):
            self._line(raw.decode('utf-8', 'replace'))

    def _line(self, line: str) -> None:
        st = self._state
        st['activity'] += 1
        self._last_line = time.monotonic()
        message = line.rsplit('\t', 1)[-1]
        m = _TASK.search(message)
        if m:
            task = m.group(1)
            if task == 'deconvolve':
                st['phase'] = 'minor cycles'
            elif task == 'tclean':
                st['phase'] = 'major cycle'
            return
        m = _MAJOR.search(message)
        if m:
            st['phase'] = 'major cycle'
            st['major_cycle'] = int(m.group(1))
            return
        m = _MINOR.search(message)
        if m:
            st['phase'] = 'minor cycles'
            st['algorithm'] = m.group(1)
            st['channels'] = int(m.group(2))
            st['channels_done'] = 0
            return
        m = _CHANNEL.search(message)
        if m:
            st['phase'] = 'minor cycles'
            st['channels_done'] += 1
            st['iterations'] = max(st['iterations'] or 0, int(m.group(4)))
            try:
                st['peak_residual'] = float(m.group(7))
            except ValueError:
                pass
            st['detail'] = message.strip()
            return
        m = _COMPLETED.search(message)
        if m:
            st['iterations'] = int(m.group(1))


def describe(progress: Optional[dict], label: str = 'Running') -> str:
    '''the status line for a snapshot: "<label> · 0:42 · minor cycles
    (hogbom): 3 of 5 channels, 1520 iterations, peak residual 0.0213"'''
    if not progress:
        return label
    seconds = int(progress.get('seconds') or 0)
    text = f'{label} · {seconds // 60}:{seconds % 60:02d}'
    phase = progress.get('phase')
    if phase == 'minor cycles':
        bits = []
        if progress.get('channels'):
            bits.append(f"{progress.get('channels_done', 0)} of {progress['channels']} "
                        f"channel{'s' if progress['channels'] != 1 else ''}")
        if progress.get('iterations') is not None:
            bits.append(f"{progress['iterations']} iterations")
        if progress.get('peak_residual') is not None:
            bits.append(f"peak residual {progress['peak_residual']:.4g}")
        algorithm = f" ({progress['algorithm']})" if progress.get('algorithm') else ''
        text += f' · minor cycles{algorithm}' + (f": {', '.join(bits)}" if bits else '')
    elif phase == 'major cycle':
        text += ' · major cycle' + (f" ({progress['iterations']} iterations so far)"
                                    if progress.get('iterations') else '')
    quiet = progress.get('quiet_seconds')
    if quiet is not None and quiet >= 30:
        text += f' · tclean quiet for {int(quiet)} s'
    return text
