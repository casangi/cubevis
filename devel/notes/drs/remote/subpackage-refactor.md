# Chunk 1 — subpackage isolation refactor

Done before testing begins, per request: isolate Chunk 1's implementation
into its own subpackage so (a) it doesn't sit in the middle of files other
`cubevis` work touches, and (b) it's in reasonable shape to be pulled out
as a standalone package later, if the multiplexed-request/response-over-
a-Jupyter-kernel machinery turns out to be useful outside `cubevis`.

All 17 tests still pass (16 from before + 1 new one — see below), same
5-7s runtime, still no Bokeh GUI/browser involved.

## What moved

```
BEFORE                                          AFTER
cubevis/bokeh/transport/_kernel_transport.py -> cubevis/remote/_kernel_transport.py
cubevis/bokeh/transport/_remote_bridge.py    -> cubevis/remote/_bridge.py
cubevis/bokeh/transport/_remote_worker.py    -> cubevis/remote/_worker.py
tests/ (flat, ad hoc loopback_transport.py)  -> cubevis/remote/tests/, cubevis/remote/testing.py
```

`cubevis/bokeh/transport/_comm_mgr.py` is the **only** existing `cubevis`
file still touched — everything else new lives under `cubevis/remote/`.

## What's new (didn't exist in the previous delivery)

- **`cubevis/remote/_link.py`** — `open_remote_kernel_link(kernel_manager,
  target_name=...)`. One call replaces the hand-wiring (`mgr._transport =
  ...`, `mgr.state = ...`, `mgr._initialized = ...`) every P_local-side
  test was doing manually before. Goes through `CommMgr.initialize()`
  rather than touching private attributes.
- **`CommMgr.initialize(transport=None)`** — small, additive extension to
  `_comm_mgr.py`: `initialize()` already had the right state-transition
  logic for "transport constructed and assigned externally, connect it,
  mark running" (that's what its `'colab'`/`'jupyter'` branch already
  did) — it just required the caller to have already set
  `self._transport` by hand first. Now it optionally takes the transport
  directly, so `open_remote_kernel_link()` doesn't need to reach past the
  underscore at all.
- **`transport_type == 'remote_kernel'`** — new value `initialize()`
  recognizes, handled identically to `'colab'`/`'jupyter'`. Separate
  label (rather than reusing `'jupyter'`) so it doesn't collide with
  `transport_type == 'auto'` autodetection used elsewhere for the browser
  leg, and so it's self-documenting at a glance.
- **`CommMgr.ROLE_DEFAULT` / `CommMgr.ROLE_MIRROR`** (also importable as
  module-level `cubevis.bokeh.transport.ROLE_DEFAULT`/`ROLE_MIRROR`) —
  named constants for what were magic strings `'default'`/`'mirror'`
  before. `cubevis.remote` and its tests now write `CommMgr.ROLE_MIRROR`
  instead of `'mirror'`.
- **`cubevis/remote/testing.py`** — the loopback `TransportBase` double
  (previously an ad hoc test helper file) promoted to a small, documented,
  public module. Anyone testing against mirrored-role `CommMgr` wiring —
  Chunk 2/3's own suites, or a future standalone-package user — can
  `from cubevis.remote.testing import wire_loopback_pair` instead of
  reimplementing it.
- **`test_link.py`** — new test covering `open_remote_kernel_link()` and
  `initialize()`'s new branch specifically, against a real kernel subprocess
  (the earlier tests all wire things by hand and don't exercise this path).
- **`ensure_remote_worker(build_worker, ...)` now calls `build_worker(mgr)`**
  instead of `build_worker()`. Found while writing the demo script below:
  the old no-argument signature gave `build_worker` no way to actually
  register any comm handlers (`mgr.open(...)`/`.register(...)`), which is
  the entire reason Chunk 2/3 need a hook here in the first place. Fixed
  now, before anything else depends on the old signature — Chunk 2/3
  should write `def build_worker(mgr): comm = mgr.open(...); comm.register(...); return backend`.
- **`cubevis/remote/examples/demo_local_or_remote_kernel.py`** — a
  hand-run, step-by-step script: start a kernel (local or a real
  sshpyk-provisioned remote one, chosen purely by `--kernel-name`),
  bootstrap a toy worker with two demo commands, run both, shut down
  cleanly. Not part of the pytest suite — meant to be read top to bottom
  and run by hand while getting oriented, and as a template for a real
  first cluster-connected smoke test. Verified working end-to-end against
  a real local kernel subprocess.

## Remaining coupling (by design, not oversight)

`cubevis.remote` depends on three things outside itself:

1. **`cubevis.bokeh.transport`'s public surface** (`CommMgr`, `Comm`,
   `TransportBase`, `AppState`) — permanent and fundamental. This is the
   thing being bridged to a remote kernel; there's no meaningful
   "isolated" version of this package that doesn't depend on it.
2. **`cubevis.utils.serialize`/`deserialize`** — a public (non-underscore)
   module. Kept rather than rolling a separate wire encoding, so the
   kernel leg stays byte-compatible with whatever `WebSocketTransport`/
   `CommsTransport` already assume about numpy-safe JSON encoding.
3. **`cubevis.bokeh.transport._environment.get_ipython_kernel_shell`** —
   the one real seam. It's generic "am I inside a Jupyter kernel"
   detection that happens to live in a *private* module inside
   `bokeh.transport`, despite not being Bokeh-specific. Flagged in both
   `_kernel_transport.py` and `_worker.py` at the exact import sites.
   **If/when standalone extraction actually happens**, this is the one
   thing that needs a decision: promote it to a public location, or
   vendor a small copy into `cubevis.remote`. Deliberately not done
   pre-emptively here — duplicating it now, before there's a second
   consumer, would just be a maintenance burden with no present benefit.

None of these three seams block the isolation goal for concurrent
`cubevis` work: nothing in `cubevis.remote` is *modified* by someone else
touching `bokeh.transport`'s other files, it just still *imports* from
the one file (`_comm_mgr.py`) that was necessarily touched, and from two
narrow, stable-surface modules.

## Layout

```
cubevis/
  bokeh/
    transport/
      _comm_mgr.py          <- only existing file changed (role param,
                                ROLE_DEFAULT/ROLE_MIRROR constants,
                                initialize(transport=) extension)
  remote/                    <- new, isolated subpackage
    __init__.py               public API
    _bridge.py                request(), SyncBridge  (zero cubevis deps)
    _kernel_transport.py      KernelClientTransport, KernelCommTransport
    _link.py                  open_remote_kernel_link()
    _worker.py                ensure_remote_worker(), RemoteWorkerHandle
    testing.py                LoopbackTransport, wire_loopback_pair
    tests/                    17 tests, all passing
    examples/
      demo_local_or_remote_kernel.py   hand-run start/bootstrap/run/shutdown demo
```

## If you'd rather use a different subpackage name

Everything here is written as `cubevis.remote`. If a different name is
preferred (`cubevis.remote_exec`, `cubevis.bridge`, whatever) it's a pure
rename — nothing outside this subpackage imports it by name yet (Chunk
2/3 haven't been written), so there's no external call site to update.
