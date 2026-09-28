"""Diagnostic conftest for the "Too many open files" investigation.

Not meant to stay -- drop this named "configtest.py" into the same
directory as the test_*.py files for ONE full run, look at the trace
afterward, then remove it. It logs this process's open file-descriptor
count after every test to a CSV, so instead of only finding out once
ulimit is finally exceeded, you get the whole growth curve and can see
exactly which test(s) push it up.

Usage
-----
Run your normal command line unchanged, e.g.:

    ulimit -n 8096 && PS=... MS=... CUBEVIS_TEST_KERNEL=... \\
        CUBEVIS_TEST_KERNEL_PS=... CUBEVIS_TEST_KERNEL_MS=... \\
        pytest test_*.py -v

pytest auto-discovers any ``conftest.py`` in the test directory, so
just rename this file to ``conftest.py`` (or symlink it) before the
run -- don't have another ``conftest.py`` already there, or merge
this into it instead of replacing it.

Afterward, look at /tmp/cubevis_fd_trace.csv (override the path with
the CUBEVIS_FD_TRACE env var). Each row is
(timestamp, test_nodeid, fd_count_after_this_test, delta_from_previous).

What to look for
-----------------
* A steady, roughly-linear climb across many tests -> a genuine
  per-test leak somewhere; sort/filter the CSV by test name (msv2 vs
  msv4, raster vs scatter vs remote) to see which side of the matrix
  it correlates with.
* A flat count for most tests with one or two big jumps -> a single
  culprit test/fixture, not a general leak -- look at exactly what
  that test does.
* fd_count staying flat overall but still eventually hitting the
  ulimit -> something outside pytest's own test loop entirely
  (module import time, session-scoped fixture teardown, interpreter
  shutdown) -- check the last few rows against the total ulimit.
"""
from __future__ import annotations

import csv
import os
import time

_LOG_PATH = os.environ.get("CUBEVIS_FD_TRACE", "/tmp/cubevis_fd_trace.csv")

_prev_fd_count = None  # set in pytest_configure


def _open_fd_count() -> int:
    """Portable-enough open-fd count for this process.

    /proc/self/fd (Linux) or /dev/fd (macOS/BSD) cover both platforms
    this suite runs on without needing psutil; psutil is tried last in
    case neither virtual directory is available for some reason.
    Returns -1 if nothing works, so a broken counter is visible in the
    CSV rather than silently missing.
    """
    for path in ("/proc/self/fd", "/dev/fd"):
        try:
            return len(os.listdir(path))
        except OSError:
            continue
    try:
        import psutil
        return psutil.Process().num_fds()
    except Exception:
        return -1


def pytest_configure(config) -> None:
    global _prev_fd_count
    with open(_LOG_PATH, "w", newline="") as f:
        csv.writer(f).writerow(
            ["timestamp", "test_nodeid", "fd_count", "fd_delta"]
        )
    _prev_fd_count = _open_fd_count()
    print(f"\n[cubevis fd trace] logging to {_LOG_PATH} "
          f"(starting fd count: {_prev_fd_count})")


def pytest_runtest_teardown(item, nextitem) -> None:
    global _prev_fd_count
    fd_now = _open_fd_count()
    prev = _prev_fd_count if _prev_fd_count is not None else fd_now
    delta = fd_now - prev
    _prev_fd_count = fd_now
    with open(_LOG_PATH, "a", newline="") as f:
        csv.writer(f).writerow([time.time(), item.nodeid, fd_now, delta])
