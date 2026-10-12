"""Shared helpers for the visplot remote-execution demos.

Not a test: the demos print what they do and why, and with ``--step``
wait for Enter between sections so they can be followed at a terminal.
"""
from __future__ import annotations

import argparse
import os
import sys
import textwrap
import time

_STEP = False


def args(description: str, extra=None):
    """Common options: data, kernel, field, --step.  *extra* adds more."""
    global _STEP
    ap = argparse.ArgumentParser(description=description)
    ap.add_argument("--ms", required=True,
                    help="the measurement set, as a path on THIS machine")
    ap.add_argument("--remote-ms", default=None,
                    help="the same data as a path on the kernel's host "
                         "(default: --ms; they differ when the kernel runs "
                         "on another machine)")
    ap.add_argument("--kernel", default="python3",
                    help="Jupyter kernel to run the data side in (default "
                         "python3: a kernel on this machine, which shows the "
                         "remote path without a second host)")
    ap.add_argument("--field", default=None, help="field to plot, e.g. 3c279")
    ap.add_argument("--step", action="store_true",
                    help="wait for Enter between sections")
    if extra:
        extra(ap)
    a = ap.parse_args()
    a.remote_ms = a.remote_ms or a.ms
    _STEP = a.step
    if not os.path.isdir(a.ms):
        sys.exit(f"--ms {a.ms!r} is not a directory")
    return a


def section(title: str, text: str = "") -> None:
    """Print a section heading and its explanation; pause with --step."""
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)
    if text:
        print(textwrap.fill(textwrap.dedent(text).strip(), 78))
    if _STEP:
        input("\n[Enter to run this section] ")


def note(text: str) -> None:
    print("  " + textwrap.fill(textwrap.dedent(text).strip(), 76,
                               subsequent_indent="  "))


class timed:
    """``with timed("open"): ...`` prints how long the block took."""

    def __init__(self, what: str):
        self.what = what

    def __enter__(self):
        self.t0 = time.perf_counter()
        return self

    def __exit__(self, *exc):
        self.dt = time.perf_counter() - self.t0
        print(f"  {self.what}: {self.dt:.2f} s")
        return False
