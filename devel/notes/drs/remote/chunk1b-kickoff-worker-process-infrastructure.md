# Chunk 1b Kickoff — Compute Worker Process Infrastructure

**Read first:** `cubevis-remote-execution-design.md` §2c (why a third
tier exists) and §3 (chunk breakdown), and
`cubevis-remote-execution-implementation.md`'s "Chunk 1b" section (the
full design this doc turns into tasks) and "Chunk 1" section (what's
already built and how — you'll be reusing `CommMgr`, `TransportBase`,
`request()`, `SyncBridge`, and the mirrored-role pattern directly). If
these aren't showing up automatically, search project knowledge for
"compute worker process" before starting.

## Goal of this chat

Give the supervisor kernel (Chunk 1's remote-kernel side) a way to
delegate long-running, potentially GIL-holding work to a separate OS
process it spawns and supervises — without ever blocking its own
responsiveness to `P_local` — and a home for that worker's lifecycle to
attach to a specific `cubevis` app instance via `BokehAppContext`. This is
the foundation both Chunk 2's `RemoteReductionContext` and Chunk 3's
`gclean` proxy dispatch their actual compute work through; nothing in
those chunks should require touching this layer again once it's done.

## Out of scope for this chat

Don't touch `reduction_context.py`, `visibility_plot.py`,
`visibility_raster.py`, `visibility_scatter.py`,
`_interactive_clean_ui.py`, or backend files (`msv2_backend.py`/
`msv4_backend.py`). Those are Chunk 2/3's job, building on what this chat
produces — no application-specific wiring here, purely reusable
infrastructure. Do touch `cubevis/bokeh/models/_bokeh_app_context.py`
(read-only investigation first, see Task 4) and add to `cubevis/remote/`.

## Tasks, roughly in order

1. **`WorkerProcessTransport` (supervisor side) / `WorkerCommTransport`
   (worker side).** New `TransportBase` pair in `cubevis/remote/`
   (e.g. `_worker_transport.py`, alongside `_kernel_transport.py`).
   Spawn via `asyncio.create_subprocess_exec(sys.executable, "-m",
   "cubevis.remote.worker_main", ...)` — **not** `multiprocessing`'s
   default `fork()` (see the implementation doc's Chunk 1b section for
   why: fork-without-exec in a multi-threaded parent is a documented
   hazard, and an ipykernel process is multi-threaded). Length-prefixed
   framing (4-byte big-endian length + JSON payload) using
   `cubevis.utils.serialize`/`deserialize` for consistency with the other
   two transports. Both sides run plain `CommMgr` with mirrored roles
   (worker: `ROLE_DEFAULT`; supervisor's worker-facing side:
   `ROLE_MIRROR`) — identical pattern to Chunk 1's P_local↔supervisor
   hop, one level deeper.

2. **`worker_main` entry point.** A small, minimal `python -m
   cubevis.remote.worker_main` module: constructs `CommMgr(role=
   CommMgr.ROLE_DEFAULT)` + `WorkerCommTransport`, reads/writes its own
   `sys.stdin.buffer`/`sys.stdout.buffer`. For *this chat's* tests, a toy
   worker registering a couple of trivial handlers is enough (mirror
   Chunk 1's `demo_local_or_remote_kernel.py` toy commands) — don't wire
   real backend objects here, that's Chunk 2/3's job.

3. **Stderr relay.** The supervisor's `WorkerProcessTransport.connect()`
   must capture the worker's stderr and relay it to the supervisor's own
   logger, not discard it — this is Chunk 1's own hard-learned lesson
   (see the implementation doc's "Real-environment issues found": a
   plain script's logging defaults swallowed `sshpyk`'s diagnostics
   entirely, turning a real startup failure into an opaque error).
   Verify concretely: spawn a worker that deliberately raises on
   startup, confirm the supervisor's log shows *why*, not just *that* it
   died.

4. **Status vocabulary.** Concrete message schema for
   died/working/stuck/completed, combining (a) IPC status pushes from
   the worker when it can send them, and (b) `proc.returncode is None`
   (or equivalent) as the supervisor-side fallback when it can't.
   Demonstrate "died" (worker process exits) and "completed" (worker
   sends a final message, or exits cleanly) end-to-end against a real
   subprocess. Document "stuck" as a heuristic (time-since-last-update
   while the process is still alive) rather than trying to prove it in a
   fast test — it genuinely isn't provable without cooperation from
   whatever's running inside the worker.

5. **`RemoteAppLink`.** New class in `cubevis/remote/`, owning `(mgr,
   transport, sync_bridge, worker_supervisor)` as one unit. Suggested
   shape, matching Chunk 1's `open_remote_kernel_link()` naming
   convention for async constructors:
   ```python
   link = await RemoteAppLink.open(kernel_manager, worker_target_name=...)
   ...
   await link.close()
   ```
   `close()` must confirm the worker subprocess actually exits (not just
   that Python-side references are dropped) — test this explicitly.

6. **`BokehAppContext` integration.** First, read
   `cubevis/bokeh/models/_bokeh_app_context.py` and `BokehInit`'s actual
   registry code (`set_app_context`/`get_app_context`/
   `clear_app_context`) closely enough to answer the open question flagged
   in the implementation doc: `show()` appears to call
   `clear_app_context()` right after HTML generation, which reads like it
   happens well before "the app is actually closed" — confirm what
   `clear_app_context()` actually signals before deciding where the
   teardown hook lives. Attach `RemoteAppLink` as a **plain Python
   attribute** on `BokehAppContext` (e.g. `app_context.remote_link`) —
   **not** a new `Instance(...)` Bokeh Property; it holds a transport,
   an event loop, and a subprocess handle, none of which are
   JS-serializable or have any business being JS-visible. Wire teardown
   so the browser-facing `comm_mgr`'s `on_shutdown` cascades into
   `remote_link.close()`, once Task 6's investigation confirms the right
   hook point.

## Definition of done

- `WorkerProcessTransport`/`WorkerCommTransport` complete a
  request/response round trip and a push round trip against a **real**
  subprocess (not a loopback double, not mocked) — matching Chunk 1's own
  bar for `KernelClientTransport`.
- A deliberately-crashing toy worker's failure is diagnosable from the
  supervisor's log output in a test, not just "it died."
- `RemoteAppLink.open()`/`.close()` tested; the worker subprocess is
  confirmed gone (not just unreferenced) after `close()`.
- Status vocabulary: "died" and "completed" demonstrated end-to-end
  against a real worker subprocess; "stuck" documented as a heuristic,
  with the reasoning for why it can't be more than that written down,
  not silently glossed over.
- `BokehAppContext` integration tested without requiring a full Bokeh
  GUI/browser (construct `BokehAppContext` instances directly, the same
  "no GUI needed" standard Chunk 1 held itself to for `CommMgr`) —
  confirms `remote_link` is per-instance, not shared globally, and that
  the teardown cascade actually runs.
- Everything above runs as plain `pytest`, no Bokeh GUI required,
  consistent with Chunk 1.

## When this chat wraps up

Update `cubevis-remote-execution-implementation.md`'s Chunk 1b section
with what got resolved (especially Task 6's `BokehAppContext`/`BokehInit`
lifecycle findings, and the final status-message schema) — mark it
**Status: implemented and tested** with the same level of honesty about
what was verified vs. assumed that Chunk 1's record has, so Chunk 2's
kickoff can build on something concrete.
