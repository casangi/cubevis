# Flaky failure: `test_persistence_manifest.py`, full-suite runs only

**Status:** Observed, not reproducible on demand, not blocking. No code
change made or recommended at this time. Logged for future reference in
case it recurs or worsens.

**Where:** `cubevis/tests/manual/remote/test_persistence_manifest.py::
test_record_survives_a_simulated_p_local_restart_and_forget_removes_it`
— Chunk 1c, Task 6's definition-of-done test (per that file's own
docstring). Unrelated to Chunk 2c/the scatter `InfoTool` work; this is
the older, already-proven remote-execution foundation layer.

---

## What was observed

Running the full remote-execution suite (`pytest test_*`, 39 tests
across 15 files) intermittently produces a failure in
`test_persistence_manifest.py`, always in the same place: the
kernel-reattachment liveness check near the end of the test.

- Running `test_persistence_manifest.py` in isolation: **never fails.**
- Running the full suite: **fails intermittently** — roughly 1 run in
  4 in observed testing, with **no consistent failure mode**. The
  first observed failure differed from the one captured below; neither
  has been reproduced a third time. Rerunning the full suite
  immediately after a failure typically passes cleanly.

### Captured failure

```
FAILED test_persistence_manifest.py::test_record_survives_a_simulated_p_local_restart_and_forget_removes_it
RuntimeError: Kernel died before replying to kernel_info
```

Raised from `jupyter_client`'s `_async_wait_for_ready`, specifically
this branch:

```python
if not await self._async_is_alive():
    msg = "Kernel died before replying to kernel_info"
    raise RuntimeError(msg)
```

i.e. the heartbeat liveness check (`_async_is_alive()`) returned
`False` mid-poll — **not** a plain 30-second timeout expiring. Stderr
only showed the routine unencrypted-TCP warning `ipykernel` always
prints; nothing indicating a real crash.

---

## What the failing step actually tests

The test has three phases:

1. Record a persistent-kernel entry via `KernelPersistenceManifest`,
   confirm a **fresh** manifest instance (simulating a `P_local`
   restart) sees it.
2. **The failing step:** construct a second, independent
   `AsyncKernelManager`, load the recorded connection file, and
   complete a live `kernel_info` round trip against the still-running
   kernel — proving the recorded file really does lead back to the
   same live kernel.
3. `forget()` the entry, confirm a third fresh manifest instance no
   longer sees it.

Per the test file's own module docstring, step 2 is explicitly a
sanity check of **generic `jupyter_client`/`ipykernel` reattachment
mechanics** — not of any `cubevis` code. `_persistence.py` is
documented as deliberately layering on top of that mechanism rather
than reimplementing it.

**Both times this was observed, phases 1 and 3 — the actual
`cubevis`-specific logic under test — passed without issue.** Only the
downstream jupyter_client sanity check failed.

---

## Investigation done

Checked every kernel-spinning test file available for whether it
properly cleans up its real kernel subprocess (`try/finally: await
km.shutdown_kernel()`), since a leaked kernel from an earlier test
would be the most obvious explanation for something that only shows up
in a full-suite run:

| File | Cleanup pattern present? |
|---|---|
| `test_persistence_manifest.py` | Yes |
| `test_kernel_transport_spike.py` | Yes |
| `test_object_registry_e2e.py` | Yes |
| `test_eval_exec.py` | Yes (3 kernels started, all cleaned up) |
| `test_remote_app_link.py` | Yes (2 kernels started, both cleaned up) |

**Not checked** — not available at the time of this writing:
`test_bridge.py`, `test_worker_process_transport.py`,
`test_worker_start_reattach_real_kernel.py`. These are exactly the
files most likely to also spin up real kernels, given their names and
item counts, and are worth a look if this recurs.

---

## Working diagnosis

Most likely explanation: **transient OS-level scheduling contention
from running many real kernel subprocess start/stop cycles back to
back in one pytest session**, not a logic bug in `cubevis` or in
`jupyter_client`. The full suite starts and tears down roughly 8–10+
real `ipykernel` subprocesses across the files that run before
`test_persistence_manifest.py`. A moment of genuine CPU contention
while several kernels are mid-teardown/startup is enough for a single
heartbeat reply to arrive late enough to read as "dead," even though
nothing is actually wrong — consistent with:

- Never failing standalone (no such contention exists).
- Failing only under full-suite load.
- Failing differently each time (a load-dependent race, not a
  deterministic defect).
- Clean cleanup discipline everywhere it's been checked so far.

This is a working theory, not a confirmed root cause — it hasn't been
directly observed (e.g. via process monitoring during a failing run).

---

## Recommendations if this recurs or worsens

In rough order of effort:

1. **Check the three unreviewed files** (`test_bridge.py`,
   `test_worker_process_transport.py`,
   `test_worker_start_reattach_real_kernel.py`) for the same
   `try/finally: shutdown_kernel()` discipline confirmed elsewhere.
2. **Confirm no lingering `ipykernel` process** after a full run:
   `ps aux | grep ipykernel` immediately after `pytest test_*`
   completes, on both a passing and a failing run, to see whether a
   failing run correlates with something still alive that shouldn't
   be.
3. **If the working diagnosis holds** (a load-sensitive false
   liveness read, not a real crash): a targeted retry-once around
   *just* the reattachment liveness check would be a better fix than
   further extending the 30-second budget, since the observed failure
   is a discrete "declared dead" event mid-poll, not the timeout
   simply running out.
4. **If it starts happening often enough to be disruptive**, consider
   whether the full suite needs a brief settle delay between
   kernel-heavy test files, though this hasn't been justified by the
   evidence so far and shouldn't be added speculatively.

## What NOT to do

Don't chase this by modifying `KernelPersistenceManifest` /
`_persistence.py` itself — nothing about the manifest bookkeeping
(record/outstanding/forget) has failed in either observed instance;
only the unrelated downstream jupyter_client liveness sanity check
has. A fix here, if one ever proves necessary, belongs in the test's
reattachment step or in how the suite sequences kernel-heavy tests,
not in the code the test exists to cover.
