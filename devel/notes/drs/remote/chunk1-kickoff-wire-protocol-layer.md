# Chunk 1 Kickoff — Shared Wire-Protocol Layer

**Read first:** `cubevis-remote-execution-design.md` in this Project's
knowledge, particularly §3 (this chunk) and §2 (overall architecture). This
doc assumes that context; it's a work order, not a re-explanation. If it's
not showing up automatically, search project knowledge for "remote
execution design" before starting.

## Goal of this chat

Give `P_local` a multiplexed request/response + push channel to a remote
Jupyter kernel, with the same reliability properties `CommMgr` already has
on the browser leg. This is the foundation both `visplot`'s
`RemoteReductionContext` (Chunk 2) and `iclean`'s `gclean` proxy (Chunk 3)
get built on — nothing in those chunks should require touching this layer
again once it's done.

## Out of scope for this chat

Don't touch `visibility_plot.py`, `visibility_raster.py`,
`visibility_scatter.py`, `reduction_context.py`, `_interactive_clean_ui.py`,
or backend files (`msv2_backend.py`/`msv4_backend.py`). Those are Chunks 2
and 3. This chat should produce something those chunks can build on without
knowing anything about visplot or iclean specifically.

## Tasks, roughly in order

1. **Reproduce the bug before fixing it.** Write a small test that
   instantiates two `CommMgr`s pointed at each other (or one `CommMgr`
   exercised against itself via a loopback `TransportBase` stub) and shows
   that a push originating from the "wrong" side gets misrouted — arrives
   tagged `p2j`, is treated as a response to a nonexistent pending
   request, and is dropped. Confirms the exact failure mode described in
   the design doc before touching working code.

2. **Parameterize the direction/role tag.** In `_comm_mgr.py`: `Comm.send()`,
   `CommMgr.send()`/`_send_immediate()` (both currently hardcode
   `'direction': 'p2j'`), `_route_message()` (dispatches on that literal
   string), and `_handle_request()`'s auto-reply (hardcodes `'j2p'` on the
   way out). Default to today's literal strings everywhere so the browser
   leg's behavior is provably unchanged — the test suite for the existing
   browser-facing path should pass without modification.

3. **Decide: reuse `CommMgr` as-is on the kernel side, or factor out a
   lighter base class?** Flagged open in the design doc, not resolved.
   Reusing `CommMgr` directly is the fastest path but carries
   `bokeh.model.Model`/`init_scripts` baggage that's meaningless with no
   browser attached. Make a call here and note the reasoning in code
   comments or a short note back to the design doc — don't leave it
   silently unresolved a second time.

4. **Write the two calling-convention primitives** (sketched in the design
   doc §3, not yet placed in a real module — pick a home, e.g. a new
   `_remote_bridge.py` alongside `_comm_mgr.py`, or wherever feels least
   awkward once you're in the code):
   - `request(comm, message_id, payload)` — async, `Future`-based, wraps
     `Comm.send()`'s existing callback. For call sites with a running loop.
   - A sync-bridge — dedicated background thread with its own event loop,
     `asyncio.run_coroutine_threadsafe(...).result()`. For call sites with
     no running loop (the `next(gclean)`-shaped case). Model this on
     `_context.py`'s existing `Mode.THREAD` handling rather than
     inventing a new pattern — that code already solves the same problem.

5. **`KernelClientTransport`: a `TransportBase` implementation for the
   frontend role, backed by `jupyter_client`/`sshpyk`.** This is where the
   design doc was most explicitly speculative — the actual
   `jupyter_client`/`sshpyk` API calls for `connect()`, `send_message()`,
   and the `run()` read-loop against a kernel's iopub channel need to be
   checked against the real API, not assumed. Worth spiking this early in
   the chat rather than late, since it's the piece most likely to reshape
   the primitives above once real constraints show up.

6. **Start-vs-reattach.** First launch must bootstrap the remote worker;
   reattachment must not re-run that and blow away live state. Needs an
   idempotent check on the kernel side. Check what `sshpyk` already
   exposes for kernel discovery/liveness before designing something new —
   this was flagged as unresolved and worth spending real time on, not a
   quick afterthought.

## Definition of done

- Two `CommMgr`-derived multiplexers (mirrored roles) complete a
  request/response round trip and an unsolicited push round trip
  correctly, over a test double transport — doesn't require a real
  `sshpyk`/cluster connection to validate the multiplexing core.
- Existing browser-facing `CommMgr` tests pass unmodified.
- `request()` and the sync-bridge both have at least one test exercising
  the "no running loop" call path, not just the async one — that's the
  path most likely to have subtle bugs (thread lifecycle, shutdown
  ordering) and the one existing tests won't naturally cover.
- `KernelClientTransport` connects to and exchanges at least one message
  with a real (or realistically mocked) remote kernel — a genuine spike,
  not just a design sketch.
- Start-vs-reattach has a concrete, working mechanism, even if minimal.

## When this chat wraps up

Update `cubevis-remote-execution-design.md` (or add a short addendum) with
whatever got decided on the open questions above, so Chunk 2's kickoff can
reference something concrete instead of the same "unverified" caveats.
