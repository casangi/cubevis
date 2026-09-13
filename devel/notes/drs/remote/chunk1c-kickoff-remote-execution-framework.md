# Chunk 1c Kickoff — Remote Execution and Object Framework

**Read first:** `cubevis-remote-execution-design.md` §2f (the
execution-context/object model this chunk builds) and §2e (the
reconnection split this chunk's persistence manifest depends on), and
`cubevis-remote-execution-implementation.md`'s "Chunk 1c" section (the
full design this doc turns into tasks) and "Chunk 1b" section (what's
already built and how — you'll be reusing `WorkerProcessTransport`,
`JobRegistry`, `request()`, and `ensure_remote_worker()`'s namespace
marker directly, and *restructuring* `RemoteAppLink`/`_supervisor.py`
rather than starting over). If these aren't showing up automatically,
search project knowledge for "execution context pool" before starting.

## Goal of this chat

Give the supervisor's one Chunk-1/1b worker a **pool of execution
contexts** instead of a single 1:1 worker subprocess, plus what makes
that pool actually useful: object creation and method invocation against
objects living in a specific context, an eval/exec escape hatch, and a
way for an application to configure what a context has available at
creation time. Also give `cubevis` a small, real answer to "did I leave
a kernel dangling on the cluster" — a persistence manifest layered on top
of `sshpyk`'s own (already-real, already-verified) reattachment
mechanism. This is the foundation Chunk 2 and Chunk 3 are committed to
building on as applications of this framework, not as their own
hand-rolled wire protocols.

## Out of scope for this chat

Don't touch `reduction_context.py`, `visibility_plot.py`,
`visibility_raster.py`, `visibility_scatter.py`, `_interactive_clean_ui.py`,
or backend files (`msv2_backend.py`/`msv4_backend.py`). Those are Chunk
2/3's job, building on what this chat produces — no application-specific
class registration here, purely reusable infrastructure. The one
exception, matching Chunk 1b's own precedent: `worker_main.py` needs a
*generic* configuration hook (Task 3 below), not any specific
application's classes wired into it.

Also out of scope: the harder reconnection scenario named explicitly as
future work in the design doc (`P_local`'s own process restarting from
scratch and rediscovering an execution context it never persisted a
label for). Build the simple, scenario-driven version — a generated id,
remembered by `P_local` for the life of its own process — not a
speculative label/discovery mechanism for a scenario nobody has asked
for yet.

## Tasks, roughly in order

**Prerequisite already done, before this chat starts:** `ensure_remote_worker`'s
`target_name` parameter no longer defaults to a fixed constant — it
generates one from the newly-constructed `mgr.comm_mgr_id` when not
explicitly supplied, per the design doc's §2e correction. `_worker.py`
already reflects this; nothing to do here.

1. **Restructure `_supervisor.py` around a context pool.** Replace the
   current 1:1 `build_worker_process_delegate()`/`_spawn_and_wire_worker()`
   (which builds exactly one `WorkerProcessTransport`-backed subprocess
   per `ensure_remote_worker()` bootstrap) with a pool object — a
   `Dict[execution_context_id, WorkerDelegate]` — owned by whatever
   `build_worker(mgr)` returns from `ensure_remote_worker()`. Register
   new P_local-facing handlers on the Layer-1 `mgr`:
   - `create_context(worker_module=..., config=...) -> {"context_id": ...}` —
     spawns a fresh `WorkerProcessTransport`, generates an id, adds it to
     the pool. `config` (an arbitrary JSON-safe payload) is *not*
     interpreted by this generic layer — it's handed to the spawned
     worker as its opening configuration message (Task 3), unopened.
   - `dispatch_fast`/`dispatch_async`/`job_status`/`shutdown_context` —
     Chunk 1b's existing four operations, each now taking a
     `context_id` and routing to the matching pool entry instead of a
     single fixed worker. Reuse `JobRegistry` per context (one instance
     per pool entry, not shared).
   - `list_contexts()` — for introspection/debugging; not required for
     any reconnection scenario in scope (see "Out of scope"), but cheap
     given the pool already exists, and useful for the demo/tests.
   Keep `ensure_remote_worker()` itself otherwise untouched — this task only changes what `build_worker`
   constructs, not Chunk 1's bootstrap mechanism.

2. **`RemoteAppLink` becomes pool-aware.** `RemoteAppLink.open()` no
   longer bootstraps-and-spawns-one-subprocess in a single call; it
   connects to the kernel's one Layer-1 worker (bootstrapping it via
   `ensure_remote_worker`, `target_name` generated on first bootstrap if
   not already up — unchanged from today) and stops
   there. A new method,
   `link.create_context(worker_module=..., config=...)`, wraps the wire
   operation from Task 1 and returns a light P_local-side handle (holding
   `context_id` plus a reference back to `link` for issuing further
   calls) — this is the object application code actually interacts with.
   `link.close()` should tear down *all* contexts it created, not just
   one — confirm every one of them actually exits, matching Chunk 1b's
   existing standard for `shutdown_worker`, now applied per-context.

3. **Worker configuration via an opening wire message, not argv.**
   `worker_main.py` currently hardcodes `_register_toy_handlers` at
   import time. Replace with: the worker starts up generic (no handlers
   registered beyond whatever bootstrapping is needed to receive one
   message), and the *first* message it receives after connecting is a
   configuration payload — carrying, at minimum, a dotted import path to
   a registration function (mirroring `build_worker`'s own shape: receive
   the worker's own `Comm`/handle table, register whatever classes/
   handlers it wants) — sourced from `create_context`'s `config` payload
   in Task 1. Keep the existing toy handlers (`ping`/`add`/`slow_echo`/
   `crash`/`trigger_push`) available as the *default* registration when no
   config is given, so Chunk 1b's existing tests and demo keep working
   unmodified.

4. **Object registry and handle table, generic (no application classes).**
   Inside a worker process: a name→class registry (populated by
   whatever registration function Task 3 loaded) and a handle→instance
   table. New worker-side wire handlers, registered generically by
   `worker_main.py` itself (not per-application):
   - `create_object(class_name, args, kwargs) -> {"handle": ...}`
   - `call_method(handle, method, args, kwargs) -> <result>`
   - `dispose_object(handle)`
   - `eval_code(code) -> <value of a well-known result variable>` /
     `exec_code(code)` — pick and document one explicit convention for
     "what a multi-statement snippet returns" (a designated variable name
     the handler reads back is the safer default over trying to mirror a
     notebook cell's last-expression display hook; state the choice and
     the reasoning in the implementation doc, don't leave it implicit).
   These are supervisor→worker (or, via `dispatch_fast`/`dispatch_async`,
   P_local→supervisor→worker) message *ids* like any other — no new
   transport or dispatch mechanism, per Chunk 1b's "no new mechanism
   needed" precedent.

5. **Real serialization, swapped in for real.** `WorkerProcessTransport`'s
   wire framing currently calls `cubevis.utils.serialize`/`deserialize` —
   confirm this chunk uses the *real* implementation (Bokeh's
   `Serializer(deferred=False)`/`Deserializer`, per `_conversion.py`), not
   a placeholder. Verify round-trip correctness for at least one
   non-trivial payload (a numpy array, given `create_object`/`call_method`
   results will carry real data, not just toy dicts) end-to-end through a
   real subprocess.

6. **`cubevis`-level kernel-persistence manifest.** A small, `cubevis`-
   owned record — JSON, one entry per caller-chosen label — mapping a
   label to the `sshpyk` `persistent_file`/kernel name last used for it,
   written when a kernel meant to survive `P_local` exiting is created,
   and checked at `cubevis.remote` startup. On finding an outstanding
   entry: surface it (label, when it was created, whatever liveness can
   be cheaply checked) and let the caller decide reuse vs. shutdown —
   don't automate the decision either way. Build this against `sshpyk`'s
   actual `persistent`/`persistent_file`/`existing` provisioner config and
   `get_persistent_info()`'s real fields (confirmed in this project's
   source set — see the implementation doc for the exact fields) rather
   than an assumed shape.

## Definition of done

- A real subprocess-backed pool: at least two `execution_context_id`s
  live concurrently under one supervisor kernel, each independently
  reachable, each confirmed to be a genuinely separate OS process (by
  pid, matching Chunk 1b's own verification style).
- `create_object`/`call_method`/`dispose_object` tested end-to-end
  against a real subprocess, including a non-trivial (numpy-array-typed)
  argument or return value, through the real serializer.
- `eval_code`/`exec_code` tested end-to-end, including the documented
  multi-statement-result convention.
- Worker configuration via the opening message tested: two contexts under
  the same kernel, configured with two *different* registration
  functions, each only having access to what it was configured with.
- `RemoteAppLink.create_context()`/multi-context `close()` tested exactly
  like Chunk 1b's own `RemoteAppLink` tests — no GUI required, real
  subprocess exit confirmed per context.
- The persistence manifest tested against a real (or realistically
  faked) `sshpyk` persistent-file lifecycle: write on creation, found and
  surfaced on a simulated fresh-process restart, not silently reused or
  silently discarded.
- Everything above runs as plain `pytest`, no Bokeh GUI required,
  consistent with Chunk 1/1b.

## When this chat wraps up

Update `cubevis-remote-execution-implementation.md`'s Chunk 1c section
with what got resolved (the exact wire schema for each new operation,
the eval/exec result convention actually chosen, the manifest's exact
file format and location) and mark it **Status: implemented and
tested**, with the same standard of honesty about what was verified vs.
assumed that Chunk 1/1b's records hold themselves to, so Chunk 2's
kickoff can build on something concrete.
