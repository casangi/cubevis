"""
test_relay.py
==============
Functional check of LocalVisibilityReader's probe_scatter_region relay
-- confirms it forwards every argument through unchanged, in the right
positions, to the backend.

Imports the real LocalVisibilityReader class directly -- no AST
extraction, no bespoke env var, no path that could point at a stale or
sandbox-only location: uses the same cubevis_test_paths.py mechanism
every other script in this set uses.

    python test_relay.py
"""
from __future__ import annotations

import sys

from cubevis_test_paths import ensure_cubevis_importable

ensure_cubevis_importable()

from cubevis.toolbox.visplot.local_visibility_reader import LocalVisibilityReader


def run_checks():
    calls = []

    class FakeBackend:
        def probe_scatter_region(self, *args, **kwargs):
            calls.append((args, kwargs))
            return {"sentinel": True}

    class FakeSelf:
        _backend = FakeBackend()

    result = LocalVisibilityReader.probe_scatter_region(
        FakeSelf(), "XAXIS", ["Y1", "Y2"], "SEL",
        (1.0, 2.0), (3.0, 4.0), max_samples=42,
    )

    assert result == {"sentinel": True}, result
    assert len(calls) == 1, calls
    args, kwargs = calls[0]
    assert args == ("XAXIS", ["Y1", "Y2"], "SEL", (1.0, 2.0), (3.0, 4.0)), args
    assert kwargs == {"max_samples": 42}, kwargs
    print("[PASS] LocalVisibilityReader.probe_scatter_region forwards all "
          "arguments through to the backend, unchanged and in order.")


if __name__ == "__main__":
    run_checks()
