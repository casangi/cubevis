# Chunk 1 deliverable — isolated `cubevis.remote` subpackage

## Where these files go

```
cubevis/bokeh/transport/_comm_mgr.py   REPLACES your existing file
                                        (role param, ROLE_DEFAULT/ROLE_MIRROR
                                        constants, initialize(transport=)
                                        extension -- see chunk1-addendum.md §2-3
                                        and subpackage-refactor.md)

cubevis/remote/                        NEW subpackage, drop in whole
  __init__.py
  _bridge.py
  _kernel_transport.py
  _link.py
  _worker.py
  testing.py
  tests/
```

This is the **only** existing `cubevis` file touched. Everything else new
lives under `cubevis/remote/` — see `subpackage-refactor.md` for why this
layout, and what it costs/buys for future standalone extraction.

Then add to `cubevis/bokeh/transport/__init__.py`'s existing exports (this
sandbox never had that file's real contents, so nothing here overwrites
it):

```python
from ._comm_mgr import ROLE_DEFAULT, ROLE_MIRROR  # new in this delivery
```

(`KernelClientTransport`, `request`, `SyncBridge`, `ensure_remote_worker`,
etc. are no longer exported from `bokeh.transport` at all — they live in
`cubevis.remote` now, so nothing to add there for them.)

## Try it by hand

`cubevis/remote/examples/demo_local_or_remote_kernel.py` is a step-by-step
script, not a pytest test — start a kernel, bootstrap a toy worker, run
two commands against it, shut down. Same code path either way; only the
kernel name changes:

```
# Fast local iteration, no SSH/cluster needed:
python cubevis/remote/examples/demo_local_or_remote_kernel.py --kernel-name python3

# The real target, once you're ready -- literally the only flag that changes:
python cubevis/remote/examples/demo_local_or_remote_kernel.py --kernel-name zuul06_python312
```

Run `jupyter kernelspec list` to see what names are available. The script
prints the remote process's own PID/hostname back to you, so you can
visually confirm the "add" command actually executed on the kernel side
(and, against a real sshpyk target, on a different host entirely) rather
than in the script's own process.

## Running the tests

No Bokeh GUI, no websocket, no browser. Two ways to run against your real
repo:

**Option A — your repo already has `cubevis` importable** (editable
install, or running from the repo root): drop `cubevis/remote/` in and
run `pytest cubevis/remote/tests/`. `conftest.py`'s `sys.path.insert`
fallback (three levels up from `tests/`) is a no-op if that path doesn't
resolve to anything in your layout.

**Option B — running standalone**: `PYTHONPATH=/path/to/repo pytest cubevis/remote/tests/`
(pointing at whatever directory `cubevis/` sits directly under).

Dependencies beyond your existing ones: `pytest`, `pytest-asyncio`
(`asyncio_mode = auto`), and for the real-kernel tests specifically:
`jupyter_client`, `ipykernel`, `comm` (things your production code needs
anyway once Chunk 2/3 land) — those files `pytest.importorskip` their way
past gracefully if absent.

`cubevis/remote/tests/pytest.ini` sets `asyncio_mode = auto` for
convenience, scoped only to this directory (pytest picks it up when test
paths point inside `cubevis/remote/tests/`; it won't affect or collide
with any repo-wide `pytest.ini`/`pyproject.toml` you already have).
Actually optional — every async test here is explicitly
`@pytest.mark.asyncio`-marked, so the suite passes under
pytest-asyncio's stricter default with no config at all; this file was
verified absent in a from-scratch clean-room run before being added back
purely for convenience.

```
pytest cubevis/remote/tests/                                    # everything (17 tests)
pytest cubevis/remote/tests/ -k "not spike and not real_kernel and not open_remote_kernel_link"
                                                                  # fast subset, no subprocess kernel (~1s)
```

## Documents

- **`chunk1-addendum.md`** — the substantive write-up: what each kickoff
  task resolved to and why, task by task. Written to slot into
  `cubevis-remote-execution-design.md` directly.
- **`subpackage-refactor.md`** — what moved where in this refactor, what's
  genuinely new (`open_remote_kernel_link()`, `initialize(transport=)`,
  the `ROLE_*` constants), and the three remaining coupling seams between
  `cubevis.remote` and `cubevis.bokeh.transport` (one of them worth
  revisiting if/when standalone extraction actually happens).

## What's genuinely new vs. what's a sandbox artifact

Everything under `cubevis/bokeh/transport/_comm_mgr.py`'s diff and all of
`cubevis/remote/` in this delivery is real, intended-for-your-repo code.
The sandbox this was built and tested in also vendored copies of your
`_low_level_transport.py`/`_environment.py` and stubbed the surrounding
`cubevis.utils`/`cubevis.bokeh`/`cubevis.exe` plumbing just enough to
import and construct real `CommMgr`/`Comm` instances outside the full
app — that scaffolding isn't included here since you already have the
real versions of all of it.
