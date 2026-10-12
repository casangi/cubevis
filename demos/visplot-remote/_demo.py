"""Shared helpers for the visplot remote-execution demos.

Not a test: the demos print what they do and why, show the call each step
stands for (``code``) -- the part to copy into your own scripts -- and with
``--step`` wait for Enter between sections so they can be followed at a
terminal.
"""
from __future__ import annotations

import argparse
import os
import sys
import textwrap
import time

# Run from a cubevis checkout without installing it: the repository root
# (two levels up) goes first on sys.path, as for devel/tools/visplot_headless
# (CUBEVIS_ROOT overrides).  Nothing needs to be linked or copied here.
_ROOT = os.environ.get("CUBEVIS_ROOT") or os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
if os.path.isdir(os.path.join(_ROOT, "cubevis")) and _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_STEP = False


def args(description: str, extra=None, local: bool = True):
    """Common options: data, kernel, field, --step.  *extra* adds more.

    *local*: the demo also opens the data in this process (demo 1 compares
    the two sides), so ``--ms`` must be a path here.  Otherwise only the
    kernel's path is needed: ``--remote-ms``, or ``--ms`` when the kernel
    runs on this machine."""
    global _STEP
    ap = argparse.ArgumentParser(description=description)
    ap.add_argument("--ms", required=local, default=None,
                    help="the measurement set, as a path on THIS machine"
                         + ("" if local else " (not needed with --remote-ms)"))
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
    if not (a.ms or a.remote_ms):
        ap.error("give --remote-ms (the data on the kernel's host), or --ms "
                 "if the kernel runs on this machine")
    a.remote_ms = a.remote_ms or a.ms
    _STEP = a.step
    if local and not os.path.isdir(a.ms):
        sys.exit(f"--ms {a.ms!r} is not a directory")
    import cubevis
    print(f"cubevis from {os.path.dirname(os.path.abspath(cubevis.__file__))}")
    return a


def section(title: str, text: str = "", pause: bool = True) -> None:
    """Print a section heading and its explanation; pause with --step.
    A section that shows its call with :func:`code` passes pause=False:
    code() pauses instead, after the call is shown and before it runs."""
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)
    if text:
        print(textwrap.fill(textwrap.dedent(text).strip(), 78))
    if _STEP and pause:
        input("\n[Enter to run this section] ")
    else:
        print()


def code(text: str, pause: bool = True) -> None:
    """Show the call this step stands for -- what you would write in your
    own script -- before the demo runs it.  Values are the ones this run
    uses."""
    print()
    print("  # in your own script:")
    depth = 0                      # open brackets: inside a call, "..."
    for line in textwrap.dedent(text).strip("\n").splitlines():
        print(("  ... " if depth > 0 else "  >>> ") + line)
        depth += sum(line.count(c) for c in "([{") - sum(line.count(c) for c in ")]}")
    print()
    if _STEP and pause:
        input("[Enter to run it] ")


def call(fn: str, **kw) -> str:
    """``fn(k=v, ...)`` text for :func:`code`, one argument per line when
    it would be long."""
    items = [f"{k}={v!r}" for k, v in kw.items() if v is not None]
    one = f"{fn}(" + ", ".join(items) + ")"
    if len(one) <= 72:
        return one
    return f"{fn}(\n" + "".join(f"    {it},\n" for it in items) + ")"


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
