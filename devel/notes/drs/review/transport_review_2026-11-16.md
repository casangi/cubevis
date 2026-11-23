# cubevis transport review — 2026-11-16

**Status: first pass complete for the four legs; second checkpoint. See "Coverage" and §8 for what has
not been read or confirmed.**

Base: `casangi/cubevis` `main` @ `91affeaf` (2026-11-10, "Merge pull request #8 from casangi/network-change").

Purpose: a working list for future sessions that fix problems or inefficiencies in the
transport layers. Nothing here has been changed in the code, and **nothing was executed** —
every finding is from reading the source. Each finding carries a confidence:

- **[C]** certain from the code as written
- **[L]** likely, but depends on runtime behaviour of a library/browser/Colab; confirm before fixing
- **[?]** a question to settle, not yet a finding

Severity: **S1** can end or wedge a session / lose or duplicate work · **S2** wrong behaviour in an
edge case, or a notable cost · **S3** cleanup, dead code, small inefficiency.

File shorthand:

| Short | Path |
|---|---|
| `llt.py` | `cubevis/bokeh/transport/_low_level_transport.py` |
| `cm.py` | `cubevis/bokeh/transport/_comm_mgr.py` |
| `llt.ts` | `cubevisjs/src/bokeh/transport/low_level_transport.ts` |
| `cm.ts` | `cubevisjs/src/bokeh/transport/comm_mgr.ts` |

## Coverage

| Area | State |
|---|---|
| `llt.py` (WebSocketTransport, CommsTransport, bridge ESM) | read in full |
| `cm.py` | read in full |
| `llt.ts` | read in full |
| `cm.ts` | lines 150–1069 read; 1–149 only grepped for fields/constants |
| `remote/_kernel_transport.py`, `_worker_transport.py`, `_link.py`, `_supervisor.py`, `_bridge.py`, `_reconnect.py`, `_async_dispatch.py` | read in full |
| `remote/_worker.py`, `worker_main.py` | code read; long comment blocks skipped |
| `remote/_remote_object.py` | lines 484–793 (liveness watch, reconnect, `acall`) read; rest outlined only |
| `remote/_persistence.py`, `_object_registry.py`, `_bokeh_integration.py`, `testing.py` | **not read** |
| callers (`visibility_plotter.py`, `iclean/_ui.py`, `_cube.py`, `remote_reduction_context.py`) | only grepped |
| `cubevis/utils/_conversion.py` (serialize) | only grepped |
| `cubevis/setup` (sshpyk kernel setup, sessions) | **not read** |
| existing tests under `tests/manual/remote`, `tests/manual/visplot/test_reconnection.py` etc. | **not read** — check which findings they already cover |

---

## 1. Top items (read these first)

| ID | Sev | One line |
|---|---|---|
| WS-1 ✅ | S1 | Any failure during the handshake of *any* incoming WebSocket connection shuts the whole application down |
| WS-2 ✅ | S1 | A new connection retires the live transport *before* the newcomer has proved itself (handshake) — a stray or second-tab connection kills the working one |
| WS-3 ✅ | S1 | Retiring a half-open socket can take `close_timeout` (10 s default) while the JS handshake deadline is 5 s → guaranteed first-attempt failure, which then hits WS-1 |
| RP-1 | S1 | In-flight requests are replayed after reconnect with no de-duplication; a handler can run twice (JS reuses the request id, so a Python-side fix is cheap) |
| HB-1 | S1/S2 | A synchronous handler longer than 10 s makes the browser declare a healthy socket dead → reconnect + replay (RP-1) → the long handler runs a second time |
| JS-1 | S1/S2 | `cm.ts` reconnect is not single-flight and reads `this.transport` across awaits (the bug Python fixed with a local reference is still present in TypeScript) |
| JC-1 | S1 | Jupyter comm path has no close detection and no reset/replay: after the frontend comm is replaced, a comm with a request in flight stays pending forever |
| CO-1 | S2 | Colab: `_colab_inflight` accounting drifts, so which delivery path a message takes (poll + `eval_js` vs direct `bridge.send`) depends on history, and the poll loop never stops |
| CO-2 | S2 | Colab: chunk arriving before its `init` is dropped → the message never assembles → that comm wedges |
| CO-3 | S2 | Colab: unused `js_code` string re-serialises every outgoing envelope (pure waste, large for images) |
| RK-1 | S1 | Remote: a timed-out request leaves the link's single comm pending; a reply lost on iopub wedges every later call |
| RK-2 | S2 | Remote: one `"worker"` comm serialises all calls — status polls and other contexts wait behind a long call |
| RK-3 | S2 | Remote: one undecodable frame kills the supervisor's relay loop silently; callers then wait forever |
| OPT-1 | — | Remote: arrays cross three hops as base64-in-JSON; raw segments already exist in both wire formats |

Deadlock / wedge conditions, collected: WS-2 (silent client holds the transport slot), JS-2, JC-1, JC-2,
CO-2, CO-7 (comm left pending with nothing to clear it); RK-1 (link wedge); RK-3 (`relay` waits forever);
RK-8 (latent pipe deadlock and inline-callback re-entry); JC-4 last bullet (awaiting a browser reply
inside a cell); RK-9 `[?]` (blocking `conn.call` on a loop thread). The common root for most of the
"pending forever" cases: **no per-request timeout exists in either `CommMgr`**, so any lost message is
permanent. A default per-request deadline that clears `pending` and calls the callback with an error
would turn each of these from a hang into a visible failure.

> **Design decided 2026-11-23 (issue #35): no wall-clock deadline.** A Plot, a flag commit or an
> iclean "finish" can legitimately run for minutes or hours, and a planned use is starting iclean on
> site, unplugging the laptop, going home and resuming -- a request must never fail because no
> browser is connected. Instead: (1) one primitive in both `CommMgr`s, `forget(request_id)` (clear
> pending, move the comm's queue on, call the callback with an error); (2) a *re-ask*: a request that
> has gone quiet *while a transport is connected* is sent again with the same `request_id`, and RP-1's
> per-comm record on the other side drops the copy (still running) or resends the stored reply; only
> an unanswered re-ask is forgotten. Re-asking happens only on a live transport, so a disconnect of
> any length just waits. JC-1's reopen hook and RK-1 use the same primitive.

---

## 2. WebSocket leg (Python ↔ browser tab)

### WS-1 [C] S1 — handshake failure is fatal to the application

> **Fixed in `debug-0003` (2026-11-20).** Hit in visplot user testing: a browser attempt closed while
> still CONNECTING (`ConnectionClosedOK` 1001) shut the session down, and the window, which carried on
> with its next attempt, then got "MSv2Backend is not open" for every request. `connect()` no longer
> calls `abort`; `process_messages` closes a failed attempt, logs it and returns without touching the
> live connection. Regression tests in `tests/manual/visplot/test_reconnection.py`.
`llt.py:320-324` (`WebSocketTransport.connect`): every exception calls `self.abort(e)` and re-raises.
`abort` is `transport_abort` (`cm.py:895-898`) → `report_error(fatal=True)` → state `ERROR`.
The re-raise lands in `process_messages`' outer `except Exception` (`cm.py:995-1002`) →
`should_shutdown = True` → `finally` calls `self.shutdown()` → `_on_shutdown` fires, handlers and comms cleared.

So these all end the session, even while another connection is healthy:
- a client connects and disconnects before/while the ack is sent (`recv()`/`send()` raise `ConnectionClosed`);
- first frame is not the `initialize` message (`RuntimeError`, `llt.py:316`);
- any non-cubevis client touching the port (the server binds an address from `find_ws_address()`).

The run-phase carefully treats `ConnectionClosed*` as non-fatal; the connect-phase does not.

**Fix direction:** in `process_messages`, treat a failure in `transport.connect()` as "this connection
attempt failed": close that websocket, do not touch `self._transport`/state belonging to the live
connection, return. Only escalate to fatal for errors that are about the application, not the peer.
Remove the `abort` call from `connect()` (or make `transport_abort` non-fatal for connect-phase errors).

### WS-2 [C] S1 — the old transport is retired before the new one is validated

> **Remainder fixed on `35-transport-bugs` (2026-11-23):** a page for another session is refused, not
> warned about. `connect()` compares the page's `comm_mgr_id` with its own (each server serves one
> CommMgr; the id is a uuid per CommMgr), and its `backend_id` with the app context's when the page
> sends one -- pages send `null` today, as `find.context()` does not reach the context from the CommMgr.
> A missing value is accepted (older pages). The refused page gets `__refused__` and close code
> `REFUSED_CLOSE_CODE` (4002); `cm.ts` `onRefused` stops reconnecting for good and shows a "Session
> ended" notice (the superseded notice, generalised: `showNotice`, no button). The case it covers: a
> tab left from an earlier run wakes and reconnects to a port a newer process was given; accepted,
> it took the session from the live page (newest page wins) and replayed its requests there.
> Tests: `test_page_for_another_session_is_refused[comm_mgr_id|backend_id]`,
> `test_page_without_ids_is_still_accepted`.

> **Fixed in `debug-0003` (2026-11-20), except the `backend_id` mismatch, which still only warns.**
> The handshake runs first, on the attempt's own transport, with a 10 s timeout
> (`CommMgr._handshake_timeout`); only a completed handshake retires the live transport.
> Second-tab policy: **the newest page wins.** The page it replaces gets `__superseded__` and a close
> with `SUPERSEDED_CLOSE_CODE` (4001), stops reconnecting (including on focus/wake), and shows a
> full-window notice with a *Use this tab* button (`CommMgr.onSuperseded` / `reclaim` in `cm.ts`).
> Python remembers superseded pages (`_superseded_pages`) and turns away their automatic reconnects
> while the other page is connected; only the button's handshake (`claim: true`) takes the session
> back, so two tabs cannot trade it on every wake.
> Found on the way: **`frontend_id` was always `null`.** `find.context(comm_mgr)` does not reach the
> `BokehAppContext` from the CommMgr, so Python never could tell a second tab from a reconnect, and
> two open tabs retired each other in a loop (seen live). `cm.ts` now has a per-page-load `pageId`,
> sent when the context's id is not found.
`cm.py:878-886`: on *any* new connection, `self._transport.close()` and `_reset_for_reconnect()` run
before `WebSocketTransport.connect()` has even read the first frame. Consequences:
- A second browser tab (or anything else that connects) closes the first tab's working socket. The
  "Multiple tabs detected" warning (`llt.py:281-296`) is sent, but the takeover has already happened.
  The first tab then auto-reconnects and takes it back → the two tabs fight, each takeover replaying
  in-flight requests (RP-1).
- `connect()` has **no timeout on `websocket.recv()`** (`llt.py:250`): a client that connects and says
  nothing leaves the session with `self._transport` pointing at a transport that is not connected
  (sends are queued, nothing flows) until something else connects.
- `backend_id` mismatch only warns (`llt.py:272-278`); a frontend for a different backend is accepted.

**Fix direction:** handshake first (with a few-second timeout) on a local transport object; only after a
valid `initialize` for *this* `comm_mgr_id`/`backend_id` retire the old transport and install the new one.
Decide a policy for a genuine second tab: reject the newcomer (send an error and close) or take over
deliberately — but not both tabs oscillating. `frontend_id` already distinguishes "same page reconnecting"
from "another tab".

### WS-3 [L] S1 — slow close of a dead socket outlasts the browser's handshake deadline

> **Fixed in `debug-0003` (2026-11-20):** the retired transport is closed in a separate task
> (`CommMgr._close_retired`), off the new connection's path.
`cm.py:883` awaits `WebSocketTransport.close()` → `websocket.close()` (`llt.py:497`). For a half-open
peer the `websockets` library waits up to `close_timeout` before giving up. visplot passes
`close_timeout=5, ping_interval=20, ping_timeout=10` (`visibility_plotter.py:4624-4631`; iclean's call at
`iclean/_ui.py:1148` not yet checked). The browser's handshake deadline is also 5 s (`llt.ts:432-439`), so
the two race at the margin: if the close wait wins, the JS attempt times out, JS closes that socket and
retries; when Python proceeds, `connect()` runs against a socket the client already abandoned →
exception → **WS-1** → application shutdown. The server binds 127.0.0.1 by default
(`find_ws_address`), so a truly half-open peer needs a forwarded port; on one machine the more likely
trigger is the event loop being busy (HB-1) while the close handshake is pending.

**Fix direction:** do not await the old transport's close on the new connection's critical path
(`asyncio.create_task(old.close())`, or `websocket.transport.abort()` when the close is known to be
abrupt), and fix WS-1. Consider passing an explicit small `close_timeout` to `websockets.serve`.

### WS-4 [C] S2 — `__goodbye__` latches for the life of the transport

> **Fixed on `35-transport-bugs` (2026-11-23).** `llt.ts` sends goodbye from `pagehide` only (not
> cancellable, and after `beforeunload`, so it is the last frame a departing page sends); the
> `goodbyeSent` latch is gone. `llt.py` forgets a goodbye when any later frame arrives (a ping
> within 15 s at most), which also covers bundles that still send it from `beforeunload`. Besides
> "Leave site?" -> Stay, a `beforeunload` that does not end the page includes a click on a link that
> turns out to be a download. Tests: `test_goodbye_followed_by_traffic_is_forgotten`,
> `test_goodbye_then_silence_still_ends_the_session`.
> **Same pattern outside the transport, not changed:** iclean (`iclean/_ui.py`, the `initialize`
> script) sends `stop` from `beforeunload`, and `_createmask.py` / `_createregion.py` call
> `cube_done` there; a cancelled unload ends those sessions outright. Moving them to `pagehide` is
> a separate, app-level change.
`llt.py:370-375` sets `_goodbye_received = True` and it is never cleared. `llt.ts:328-329` sends it on
`pagehide` **and** `beforeunload`. `beforeunload` can be cancelled (another handler prompts "Leave site?"
and the user stays). After that, a later *abrupt* drop (sleep/network) is classified clean
(`llt.py:222-224`) → 3 s grace period (`cm.py:314`) → the session shuts down instead of waiting.
JS `goodbyeSent` is also never reset.
**Fix:** send goodbye on `pagehide` only, and/or clear `_goodbye_received` on any subsequent inbound frame.

### WS-5 [C] S3 — per-message details in `run()`

> **First two bullets fixed on `35-transport-bugs` (2026-11-23):** the failure log names
> `comm_id.message_id (request_id)` only; the goodbye and ping comments are each above their own
> branch. The last two bullets are notes, not changes.
- `llt.py:436`: the failure log interpolates the whole message (`{_msg}`), which can be megabytes. Log
  `comm_id`/`message_id`/`request_id` only.
- `llt.py:359-382`: the keepalive and goodbye comment blocks are interleaved (the ping comment sits above
  the goodbye branch). Cosmetic, but misleading.
- Dispatch is one task per inbound frame with no bound. In practice the JS per-comm throttle bounds it to
  "number of comms", so this is fine today; note it if a peer other than `cm.ts` ever connects.
- Ordering is preserved: tasks start in creation order and `_handle_request` reaches its per-comm
  `asyncio.Lock` (FIFO) without an earlier `await`. Keep it that way — an `await` added before
  `async with lock` in `_handle_request`/`_route_message` would silently break per-comm ordering.

### WS-6 [C] S3 — `ws://` is hard-coded (`llt.ts:133`); a page served over https cannot connect (mixed content). Fine for `file://`/localhost; note for any hosted deployment.

---

## 3. Reconnect, replay and liveness (both sides)

### RP-1 [C] S1 — replay without de-duplication

> **Fixed in `debug-0003` (2026-11-20).** Python: `CommMgr._last_request` (one record per comm --
> each side has one request in flight per comm, so a replay is always that comm's last request):
> running → the copy is dropped; answered → the reply is sent again, handler not called. The reply is
> recorded even when no transport is connected at that moment (that is the case a replay needs), and a
> handler that ends without a reply (cancelled) leaves no record. Python → JS replays keep their
> `request_id` (queue entries carry it), and `cm.ts` keeps the same per-comm record and resends.
Both sides re-queue in-flight requests at the front of the comm's queue when a connection ends
(`cm.py:1153-1160`, `cm.ts:397-418`). The request may already have been *received and executed* by the
peer — only the reply was lost, or is still being computed. Nothing detects that:
- JS → Python replays keep the **same** `requestId` (`cm.ts:402-407`). Python runs the handler again.
- Python → JS replays get a **new** `request_id` (`cm.py:764`), so JS cannot detect them at all.

The docstring at `cm.py:376-391` argues GUI updates are idempotent. That holds for Python→JS pushes; it
does not obviously hold for JS→Python requests (anything that advances state: iclean iteration/stop
controls, flag apply/undo, "next/previous" stepping).

**Fix direction (small):** in `CommMgr._handle_request` keep `request_id → state` for recent requests
(bounded LRU): *in progress* → drop the duplicate (the original's reply will go out on the current
transport, see below); *done* → resend the cached reply without calling the handler. For Python→JS, keep
the original `request_id` on replay (carry it in the queue tuple) and add the same check in `cm.ts`.

Supporting fact [C]: replies are sent on `self._transport` at reply time (`cm.py:1445-1458`), i.e. the
*current* connection, so a reply computed across a reconnect does reach the new socket — which is exactly
why the replayed copy is redundant.

### HB-1 [C] S1/S2 — the browser heartbeat cannot tell "busy" from "dead"
`llt.ts:112-114`: ping every 15 s, 10 s pong deadline (3 s after wake/focus). Python answers pings in the
socket read loop (`llt.py:377-382`), which needs the event loop. Any handler that holds the loop
synchronously for longer than the deadline (the comment at `llt.py:384-417` says so itself) gets the
socket declared dead with code 4000. Then: JS reconnects → Python retires the "old" transport → JS replays
the request (RP-1) → Python runs the long handler again → the loop is blocked again → possibly declared
dead again. Net: ≥2× the work plus reconnect churn, for a connection that was never broken.
Focus/visibility probes use the 3 s deadline, so merely clicking back into the tab during a 4 s
synchronous handler triggers it.

Server-side counterpart [L]: `websockets.serve(ping_interval=20, ping_timeout=10)` runs its keepalive on
the same loop. After the loop has been blocked past the timeout, the timeout can be processed before the
already-received pong and the *server* closes the connection (1011). Same cure: keep handlers off the loop.

`asyncio.to_thread` is used in visplot's scatter/raster handlers; `grep` finds none in `iclean/` or
`_cube.py`, so those handlers are synchronous on the loop [C by grep; individual handler durations not
assessed].

> **Correction (2026-11-23):** iclean's expensive handler, `clean_handler`, awaits
> `session.arun_cycle()`, which runs the imager in a thread (`gclean.__anext__`); the grep missed it.
> What remains to audit is the smaller handlers in `_cube.py` (mask edits, statistics, spectrum,
> palette, colormap) and the synchronous `session.update()` / `log()` around the cycle.

**Fix directions:** (a) RP-1 de-dup removes the double execution; (b) move remaining long handlers to
threads; (c) make the deadline tolerant: in `llt.ts` do not `declareDead` on a missed pong while the
socket's `bufferedAmount` is 0 and a request is in flight *unless* a second consecutive ping is also
missed, or lengthen the routine deadline; (d) optionally answer pings from Python even when busy by
having `_handle_request` send an interim `__busy__` frame before invoking a handler known to be slow.

### JS-1 [C] S1/S2 — `cm.ts` connection setup is not single-flight

> **Fixed in `debug-0003` (2026-11-20):** `connecting` holds the attempt under way;
> `scheduleReconnect`, `onWake` and `send` wait for it (`startAttempt`). `connectWebSocket`,
> `setupTransport` and `runTransportWithReconnection` hold their transport locally and give up when
> `this.transport` has moved on. Live check: socket declared dead, then a burst of focus/online/
> visibility events → one reconnect handshake on the Python side, none failed.
`scheduleReconnect()` is reachable from the end of `runTransportWithReconnection`, from `onWake`
(focus + visibilitychange + pageshow + online — a tab switch fires three of them), and from `send()`.
`reconnectTimer` is cleared at the top of the timer callback (`cm.ts:478`), *before*
`await this.connectWebSocket()`. While that await is pending, `transport.isConnected()` is false, so an
`onWake` or a `send()` schedules another attempt, whose callback closes the transport that is
mid-handshake and starts another. On the Python side each abandoned attempt is a WS-1 candidate.

Also `setupTransport()` (`cm.ts:328-346`) reads `this.transport` again after `await this.transport.connect()`:
if a newer attempt replaced it meanwhile, `flushAllQueues()` marks comms pending against a transport that
is not connected (`sendImmediate` sets `pending` then only warns, `cm.ts:676-691` → wedged until the next
reset) and `run()` is started on the wrong transport (two `run()` calls on one transport overwrite
`runResolve`, `llt.ts:459`).

**Fix:** a `connecting` flag (or a stored in-flight promise) checked by `scheduleReconnect`/`onWake`/`send`;
hold the transport in a local variable through `setupTransport` and bail out if `this.transport !== local`
after each await — the same pattern as `cm.py:841-863`.

### JS-2 [C] S2 — `sendImmediate` marks a comm pending even when nothing was sent

> **Fixed on `35-transport-bugs` (2026-11-23):** with no connected transport, `sendImmediate` puts the
> message back at the head of its comm's queue, unsent and not pending, and schedules a reconnect;
> `flushAllQueues` sends it after the handshake.
`cm.ts:676-691` (pending set, then "Transport not ready, cannot send" and return) and
`llt.ts:515-521` (`WebSocketTransport.send` drops with a warning if not OPEN). Recovery depends on a later
`resetConnectionState()`. Prefer: do not mark pending unless the write happened; queue instead (as
`cm.py:696-706` does).

### JS-3 [C] S3 — JS handlers cannot be async

> **Fixed on `35-transport-bugs` (2026-11-23):** a thenable result is awaited and its value (or error)
> sent; meanwhile the comm's RP-1 record says "running", so a replay of it is dropped, as in Python.
`cm.ts:805-817` sends the handler's return value as the reply immediately; a handler returning a Promise
would send `{}`. Fine today if all JS handlers are synchronous; worth a guard (`await` if thenable).

### PY-1 [C] S2 — `abandon_inflight` / `_reset_for_reconnect` and late replies

> **Fixed on `35-transport-bugs` (2026-11-23):** DEBUG once `connection_generation > 0`, WARNING before
> (`tests/manual/remote/test_bug_reproduction.py` relies on the warning at generation 0).
After either, a late reply for a forgotten request logs `Received response for unknown request` at WARNING
(`cm.py:1356-1358`) — expected and harmless, but noisy; downgrade to DEBUG when
`_connection_generation > 0`.

### PY-2 [C] S3 — leftovers in `CommMgr`

> **Fixed on `35-transport-bugs` (2026-11-23), all bullets:** no-loop branch removed; `Comm.__init__`
> no longer touches `kwargs['description']` and tests `comm_mgr is not None`; Bokeh's `List` / `Tuple`
> imported as `PList` / `PTuple`; `# REMOVED` comments dropped and `_errors` is a `deque(maxlen=100)`;
> the 2-parameter handler wrapper chains `from e`; handler arity cached per handler
> (`_takes_context`); response callbacks go by `inspect.isawaitable(result)`.
- `cm.py:1531`: `self._shutdown_event.set` is missing `()` (the no-running-loop branch; unreachable from a
  coroutine, so harmless — delete the branch).
- `cm.py:123`: `kwargs['description']` is a bare expression → `KeyError` if `Comm(...)` is constructed
  without `description`. `cm.py:124`: `if 'comm_mgr' and 'comm_mgr_id' not in kwargs` — the first operand
  is a constant string; and `comm_mgr.comm_mgr_id` dereferences `None` when `comm_mgr` is omitted.
  `CommMgr.open()` always passes both, so this only bites direct `Comm(...)` construction.
- `cm.py:19-20` rebinds `List`/`Tuple` from `typing` to Bokeh's property types; the later annotations
  (`Dict[str, Tuple[...]]`, `List[Exception]`) then use the Bokeh ones. Works only because of
  `from __future__ import annotations` for signatures — the inline variable annotations in `__init__` are
  not evaluated either, so no crash, but rename the imports.
- Fields commented `# REMOVED` (`_state_lock`, `_shutdown_reason`, `_errors`, `_on_shutdown`, `_on_error`)
  are all still used. `_errors` grows without bound.
- `_handle_request` wraps handler exceptions in `RuntimeError(...) from None` only for 2-parameter
  handlers (`cm.py:1430-1436`), discarding the original traceback for exactly those.
- `inspect.signature(handler)` on every request (`cm.py:1428`); cache at `register()` time.
- `cm.py:1371`: `inspect.iscoroutinefunction(callback)` misses partials/callable objects returning
  awaitables; use the `inspect.isawaitable(result)` pattern used a few lines later.

---

## 4. Jupyter comm leg (notebook ↔ kernel)

### JC-1 [C/L] S1 — no close detection, no reset, no replay
`CommsTransport._on_comm_open` (`llt.py:625-835`) appends each newly opened comm and wires `_recv`, but
never registers `comm.on_close`. `_connected` is set True once and only cleared in `close()`; the
docstring of `run()` (`llt.py:1480-1486`) says a reload "briefly clears `_connected`" — nothing does.
`CommMgr` only resets/replays from `process_messages`' `finally`, which for comm transports runs only when
`run()` returns, i.e. at final close.

So when the frontend side is replaced (JupyterLab page reload, kernel reconnect after sleep, output
re-rendered): Python keeps sending on the newest comm (good, `llt.py:1458-1470`), but any Python→JS
request that was in flight when the old page went away never gets a reply, its comm id stays in
`_pending`, and every later send on that comm queues forever. [C for the mechanism; **L** for how often a
reload actually re-creates a working frontend — confirm in JupyterLab 4.]

**Fix:** in `_on_comm_open`, when a comm opens while already connected, call a CommMgr hook that does what
`abandon_inflight()` or `_reset_for_reconnect()` + `_flush_all_queues()` do; register `on_close` to prune
`_comm_objs` and close superseded comms.

### JC-2 [C] S2 — send failures are swallowed, wedging the comm
- `llt.py:1472-1474`: if no comm accepts the message, a warning is logged and `send_message` returns
  normally → `_send_immediate` has already marked the comm pending → wedged.
- `llt.py:1450`: raises plain `RuntimeError`, not `TransportNotConnectedError`, so CommMgr treats it as a
  handler error instead of re-queuing.
- Colab branch `llt.py:1444-1445`: same swallow.
**Fix:** raise `TransportNotConnectedError` in all three places.

### JC-3 [L] S2 — frontend looks for the bridge comm exactly once
`llt.ts:651-666`: `retrieveComm()` reads `window["cubevis_"+id]` once. The anywidget bridge sets it from an
ESM that is imported asynchronously; if BokehJS initialises the `CommMgr` model first, the result is
"Could not create Jupyter comm" and the Showable is disabled with "No active session — re-run the cell"
(`cm.ts:230-248`), which is indistinguishable from a genuinely stale notebook. **Fix:** poll for the
window entry for a few seconds before giving up.

### JC-4 [C] S3
- Callback tasks are created with `asyncio.ensure_future` inside an IOLoop callback and not retained
  (`llt.py:648-650`) — the GC hazard the WebSocket path guards against with `_pending_message_tasks`.
- The comm target is never unregistered on `close()`; each session leaves a closure holding the transport
  in the kernel's comm manager.
- `run()` on both sides is a 100 ms sleep loop (`llt.py:1487-1488`, `llt.ts:630-632`); use an event/promise.
- `llt.ts:616-618` (`console.group('---connect---')`), `llt.ts:654-655`: debug output on the normal path.
- `_bridge_started` (`llt.py:569`) is a per-instance dict keyed by the instance's own id — a boolean.
- **Design constraint to keep documented:** comm messages are shell-channel messages; ipykernel does not
  process them while a cell is executing. Anything that blocks/awaits *inside a cell* for a reply from the
  browser deadlocks. `connect()`'s 30 s wait (`llt.py:1313-1317`) is subject to this if it is ever awaited
  from cell-execution context.

---

## 5. Colab leg (`eval_js` + BroadcastChannel)

The constraint is accepted as given: each cell output is its own iframe, kernel `comm.send()` does not
reach JS, so Python→JS goes through `eval_js` posting on a BroadcastChannel and the bridge widget relays.
The findings below are about the implementation inside that constraint.

### CO-1 [C] S2 — `_colab_inflight` does not count what its name says
Incremented for **every** inbound `cubevis_message` envelope (`llt.py:772`), decremented (clamped at 0) for
**every** outbound message (`llt.py:1421-1422`). But:
- the JS handshake `comm_opened` and the `closing` notice are sent *inside* a `cubevis_message` envelope
  (`llt.ts:606-619`, `878-885`), so they increment it and are never answered (stuck +1). They are also not
  filtered — the control-message filter at `llt.py:799-800` is only on the non-envelope branch — so each
  reaches `CommMgr._route_message` and logs `unrecognized direction None; dropping message`;
- replies from JS to Python-originated requests increment it;
- Python-originated requests decrement it.

Effects:
1. **Path selection depends on history.** `llt.py:1428`: when the counter reaches 0 the message is sent by
   `self._bridge.send(...)` (anywidget `msg:custom` → `bc_rx`); otherwise it is queued for the poll and
   delivered by threaded `eval_js` chunks. With the stuck +1, the first reply goes by poll, later traffic
   flips between paths. The class docstring says *all* messages use the chunk path; the code does not.
   If one of the two paths is unreliable in some context (the comments disagree about when `bridge.send`
   works), failures will look random.
2. **The poll never stops.** `_has_more` (`llt.py:732`) is true whenever the counter is > 0, so `stop_fn`
   is rarely sent; the bridge keeps sending `cubevis_poll` every 400 ms (`llt.py:1035`: `50·2^min(n,3)`
   never reaches the 500 ms cap) for the life of the page — a kernel message every 400 ms, forever.
3. The direct path pops the **newest** queued envelope (`llt.py:1434`, `.pop()`), overtaking anything still
   queued.

**Fix direction:** count only inbound messages that will be answered (`direction == peer` and
`request_id` present) and decrement only when that reply is sent; choose the delivery path explicitly
(one path for everything, or "reply to an in-flight request → poll; unsolicited → bridge.send") rather
than from the counter; stop the poll from JS after N empty polls as a backstop.

### CO-2 [C] S2 — chunk reassembly assumes `init` arrives first
`llt.py:1065-1072`: a `chunk` for an unknown `tok` is discarded. The code already handles `done` arriving
before chunks, i.e. ordering of separate `eval_js` calls is not trusted — but an early chunk is lost for
good, the message never completes, the reply never arrives, and that comm stays pending (no per-request
timeout exists on either side). Partial buffers are also never expired.
**Fix:** put `total` in every chunk and create the buffer on first sight of a `tok`; expire incomplete
buffers after a timeout and log.

### CO-3 [C] S3 (cheap win) — dead `js_code` build in `send_message`

> **Fixed on `35-transport-bugs` (2026-11-23):** `env_json`, `js_code`, `cb_name`, `bc_name` and
> `_parent_header` removed. Not run in Colab: the code removed was never read.
`llt.py:1396-1407` builds `env_json = json.dumps(envelope)` and a `js_code` string that is never used
(delivery happens later in `_deliver_in_thread`). For an image-sized reply this is a full extra
serialisation plus a string copy per message. Delete it, with `_parent_header` (`llt.py:1409`).

### CO-4 [C] S2 — encoding overhead on the chunk path
Payload is `serialize(message)` (JSON text) → placed as a string inside the envelope →
`json.dumps(envelope)` (escapes every `"` and `\`) → sliced → each slice `json.dumps`'d again into JS
source (`llt.py:730-751`). Arrays are already base64 inside the first layer; two further rounds of string
escaping inflate and re-scan the whole payload, and the browser then does `JSON.parse` on the assembled
string and `deserialize` on the inner string. **Options:** base64 the envelope once and chunk that (no
escaping needed on any later layer); or drop the envelope's string-in-string layer for this path.
Also each `eval_js` call constructs `new BroadcastChannel(...)` and never closes it (`llt.py:741-757`);
reuse one channel object cached on `window`.

### CO-5 [C] S2 — kernel `_parents` is overwritten from a worker thread and never restored
`_deliver_in_thread` (`llt.py:717-726`) and `close()` (`llt.py:1524-1526`) do
`kernel._parents.clear(); .update(bridge_parents)`. From a thread this races with the main thread; and
because it is not restored, output produced afterwards by whatever cell is executing (prints, displays,
tracebacks) is attributed to the bridge's cell. It is benign only while the executing cell *is* the bridge
cell. **Fix:** save and restore around the `eval_js` calls, and do the swap on the kernel thread
(`_main_ioloop.add_callback`) rather than in the worker — or check whether `eval_js` needs a valid parent
at all now that delivery is by BroadcastChannel.

### CO-6 [C] S2 — the "BroadcastChannel does not deliver to its own context" premise is per *object*, not per window
Comments at `llt.py:590-596`, `1124-1128`, `1266-1271` say a BroadcastChannel post is not delivered within
the same iframe, and the bridge is wrapped in an `ipywidgets.Output` to force a separate iframe. Per the
HTML spec, a post is delivered to every *other BroadcastChannel object* of that name, including ones in
the same window; only the posting object itself is skipped. The bridge posts with a fresh
`new BroadcastChannel(...)` each time (`llt.py:1092-1094`, `1184-1186`), so a listener in the same window
would receive it. Two consequences:
- If bridge and app ever share a window (the `except ImportError` fallback at `llt.py:1280-1281`, or
  JupyterLab should it ever take this branch), every message is delivered **twice**: once via
  `window["cubevis_rx_cb_…"]` and once via the channel. For Python→JS requests that runs the handler twice.
- The Output-widget wrapper may be unnecessary. [?] Worth one experiment: drop the wrapper and the
  `window` callback path, rely on the channel alone.

### CO-7 [L] S2 — messages posted before the bridge is listening are lost
BroadcastChannel does not buffer. `llt.ts:689-730` returns the Colab pseudo-comm immediately; `connect()`
then sends `comm_opened`, and init scripts may send real requests, possibly before the bridge ESM has run
`colabComms.open()` and attached `bc_tx.onmessage`. A lost request leaves its comm pending (no timeout).
Python waits for the bridge (`_conn_event`); the JS side does not. **Fix:** a ready handshake — the app
posts `hello` on `bc_tx` until the bridge answers `ready` on `bc_rx`; `connect()` resolves on `ready`.

### CO-8 [C] S3 — dead and half-dead code on this path
- Binary path: the ESM `cubevis_binary` branch references an undefined `buffers` (`llt.py:1140`; the
  `msg:custom` callback is declared with one parameter), and Python never sends `cubevis_binary` or
  `pending_binary_tokens`. In `llt.ts` `substituteBinary`, `checkDeferredMessages`, `_cubevis_deferred_msgs`
  (a window global shared by all transports), `cubevis_binary_arrived_*` are therefore unreachable.
  `colab_binary_threshold` is unused. Either finish it (it would remove CO-4's base64 cost) or delete it.
- `_last_parent_header` / `_last_parent_ident` are captured per message (`llt.py:663-681`) and never used.
- ESM: `bc_rx` is created (`llt.py:1129`) but only ever closed; `console.log("CUBEVIS poll-deliver…")`
  (`llt.py:1173`) is unconditional; `_cubevis_startPoll_*` is exported but never called from Python.
- `_recv` re-imports `traceback`, `pathlib`, `json`, `threading`, `uuid` on every message.
- One thread is created per delivered reply (`llt.py:765`); two replies delivered by two threads can
  complete out of order (reassembly is per `tok`, so content is safe; only cross-message order is not).
  A single long-lived delivery thread with a queue fixes both.
- The ESM text embeds a timestamp (`llt.py:928-929`), so every instance is a distinct module; and
  `CommBridge` is a new class per transport.

---

## 6. Remote leg (`cubevis/remote`, sshpyk)

Shorthand: `kt.py` = `_kernel_transport.py`, `wt.py` = `_worker_transport.py`, `sup.py` = `_supervisor.py`,
`link.py` = `_link.py`, `ro.py` = `_remote_object.py`, `ad.py` = `_async_dispatch.py`, all under `cubevis/remote/`.

Chain: P_local `CommMgr(mirror)` ⇄ `KernelClientTransport` ⇄ (jupyter_client shell/iopub, sshpyk tunnels) ⇄
`KernelCommTransport` ⇄ kernel `CommMgr(default)` = supervisor ⇄ per-context `CommMgr(mirror)` ⇄
`WorkerProcessTransport` ⇄ (stdin/stdout frames) ⇄ `WorkerCommTransport` ⇄ worker `CommMgr(default)`.

### RK-1 [C] S1 — a request that times out leaves its comm pending; the link has one comm
`request()` (`_bridge.py`) and every `asyncio.wait_for(request(...))` in `link.py` only abandon the
*Future*. `CommMgr._pending[comm_id]` stays set until a reply arrives. `RemoteAppLink` sends everything on
one comm, `"worker"` (`link.py:246`), so if a reply is lost, every later call queues behind it and times
out in turn. `ro.py` calls `abandon_inflight()` only on the heartbeat-gap paths (`ro.py:624, 659, 662, 733`);
a plain timeout with a healthy heartbeat (`ro.py:599-601`) does not.

Replies travel on **iopub, which is PUB/SUB and lossy** [L]: a reply published while the SUB socket is
reconnecting (a gap shorter than `RECONNECT_AFTER` = 4 s never sets `gap_seen`), or dropped at the
high-water mark, is gone with no heartbeat evidence. That is the case that wedges the link permanently.

**Fix:** give `CommMgr` a per-request cancel (`forget(request_id)` → clear `_pending`, run
`_process_next_queued`) and call it from `request()`'s timeout/cancel path; or have `ro.py` call
`_aabandon()` on every `TimeoutError`.

### RK-2 [C] S2 — one comm serialises every remote call, including status polls
P_local's send-side throttle allows one in-flight request per comm, and the supervisor holds the comm's
lock across handler + reply (`cm.py:1425`). With the single `"worker"` comm on both ends
(`link.py:246`, `sup.py:158`): a long `dispatch_fast` blocks `job_status`, `list_contexts`,
`supervisor_info`, and calls to *other* contexts (separate worker processes that could run in parallel).
The docstring in `ad.py` ("splitting comm categories doesn't help … one receive loop") describes the
receive loop as it was; `KernelCommTransport._on_comm_msg` now dispatches with `ensure_future`
(`kt.py:464`), so splitting comms *would* help.
**Fix:** a control comm (`job_status`, `list_contexts`, `supervisor_info`, `create/shutdown_context`) plus
one comm per `context_id`.

### RK-3 [C] S2 — one undecodable frame silently kills the supervisor's relay for that worker
`_read_frame` re-raises decode errors (`wt.py:124-128`); `WorkerProcessTransport.run` (`wt.py:242-254`) has
no `try/finally`, so the task dies without setting `_exited`. `relay()` (`sup.py:117-136`) then waits on
both the request and `wait_exited()` forever. The `wire_types` import in `sup.py:184-202` works around one
cause (unregistered type tag) but not the mechanism. Framing is length-prefixed, so a bad payload does
not desynchronise the stream. **Fix:** catch decode errors per frame (log, answer the request id with an
error if it can be recovered from the header, continue); set `_exited` in a `finally`.
Related: `_exited`/`_connected` are also not updated if `self._callback` raises.

### RK-4 [C] S2 — background `run()` tasks are unobserved
`asyncio.ensure_future(transport.run())` at `link.py:281` and `sup.py:207`, and
`asyncio.ensure_future(self._callback(inner))` at `kt.py:464`: no reference kept, no done-callback. If
`run()` raises, the link is dead and the only symptom is every call timing out. **Fix:** keep the task;
on completion with an exception, log it, mark the transport disconnected and fail pending requests.

### RK-5 [C] S2 — `KernelClientTransport.run` error handling
- `kt.py:295-299`: any exception from `get_iopub_msg` is logged with a traceback and the loop continues
  immediately — a persistent error (closed socket/context) becomes a tight loop of tracebacks.
  Add a short sleep/backoff and a give-up threshold.
- `kt.py:306-309`: `comm_close` sets `_connected = False` and carries on; nothing tells `CommMgr`, so
  in-flight requests are never resolved and later sends are queued forever ("Transport not ready").
- `kt.py:207-208`: `connect()` marks connected as soon as `comm_open` is *sent*. An unknown target (kernel
  bootstrapped with a different `target_name` — `ensure_remote_worker` ignores the argument on reattach)
  shows up only as timeouts. A `supervisor_info` round trip at the end of `RemoteAppLink.open` would turn
  that into an immediate, clear error.
- `kt.py:331`: the callback is awaited inline in the read loop (see RK-8).
- `kt.py:259`: raises `RuntimeError`, not `TransportNotConnectedError` (same point as JC-2).

### RK-6 [C] S2 (latent) — kernel-side transport never reports connected
`ensure_remote_worker` deliberately skips `connect()` (`_worker.py`), and `_on_comm_open` (`kt.py:445-451`)
does not set `_connected`; only `connect()` does (`kt.py:443`). So `KernelCommTransport.is_connected()` is
always False in the real bootstrap path. Replies still work (`_handle_request` calls `send_message`
directly), but any *kernel-originated* `comm.send()` is held by `_send_immediate` ("Transport not ready;
holding … for reconnect") and never flushed. Harmless while the kernel only ever replies; a trap for the
first push/progress message. `_on_comm_close` also clears the flag for good, including after a reattach.
**Fix:** set `_connected = True` in `_on_comm_open` and flush queues there.

### RK-7 [C] S2 — `JobRegistry` keeps every job and its result forever
`ad.py`: `_jobs` is never pruned; a completed job's (possibly large) result stays in the supervisor and
is re-sent in full on every later `job_status`. `STUCK` is reported for any job running longer than 30 s
because `updated_at` is only touched at the end. **Fix:** drop the record when a terminal status has been
delivered (or TTL); feed `updated_at` from worker progress, or drop `STUCK`.

### RK-8 [C] S3 (latent deadlock) — read loops that await their callback inline
`KernelClientTransport.run` (`kt.py:331`), `WorkerProcessTransport.run` (`wt.py:251`) and
`WorkerCommTransport.run` (`wt.py:384`) still `await self._callback(...)` before reading the next message
(the WebSocket leg stopped doing this). Two hazards:
1. A handler or reply-callback on that loop that itself awaits a `request()` over the same transport can
   never see its reply. Nothing does this today.
2. Pipe deadlock: a callback that ends in `_process_next_queued → _write_frame → drain()` (supervisor
   side) blocks the supervisor's *read* loop until the worker reads stdin; if the worker is at that moment
   blocked in `drain()` writing a frame the supervisor is not reading, both stop. Today this cannot happen
   because each worker has one comm and only ever writes replies (so the supervisor sends the next request
   only after the previous reply was fully read). It becomes reachable as soon as a worker sends
   unsolicited frames (progress pushes — `trigger_push` in the toy handlers already does) or gets a second
   comm, with frames larger than the pipe + stream buffers (~200 kB).
**Fix:** dispatch callbacks with retained tasks, as `llt.py:427-438` does; per-comm locks already keep order.
Also note for the worker: `handle_call_method` runs synchronously on the worker's loop, so a long call
cannot be cancelled or pinged; the only stop is killing the process.

### RK-9 [C] S3 — small items
- Frame length uses the top bit as a flag (`wt.py:52, 88`): a JSON payload ≥ 2 GiB is misread as "has
  segments"; ≥ 4 GiB raises `struct.error`. Add an explicit size check with a clear message.
- `RemoteAppLink.open` starts a `SyncBridge` thread + loop per link that nothing uses (`link.py:295-296`).
- `SyncBridge.run(coro, timeout)` leaves the coroutine running on the bridge loop after a timeout; cancel
  the `concurrent.futures.Future`.
- `WorkerProcessTransport.send_message` returns silently when not connected (`wt.py:230-231`), leaving the
  per-context `CommMgr` with a pending request; `relay()`'s race with `wait_exited()` covers the caller,
  but the pending entry leaks.
- `ro.py:641`: each waiting call runs its own watcher that calls `km.is_alive()` every liveness interval;
  with several concurrent calls that is several polls of the same fact (and through sshpyk each may cost
  a remote round trip) [L]. One shared watcher task per connection would do.
- [?] `remote_reduction_context.py:380` uses the blocking `conn.call(...)`. Any path that reaches it from
  a handler running on the browser-leg event loop (not inside `asyncio.to_thread`) blocks that loop for
  the whole remote call → HB-1. Calling it from the bridge loop's own thread would deadlock outright.
  Worth an audit of call sites.
- `_reconnect.py` read in full: no findings beyond its own stated fragility (instance-patching sshpyk);
  `SshReconnector.reconnect` has no guard against two concurrent runs, but `_watched` only starts one at a
  time per call — concurrent calls each start their own [L].

### Remote data-path optimisations

| ID | What | Where | Expected gain |
|---|---|---|---|
| OPT-1 | Arrays are base64 inside JSON at every hop (`remote_serialize` = Bokeh serializer + `serialize_json`). Bokeh's serializer can emit buffers out of band; both wire formats already carry raw segments (`wt.py` extended frame, comm `buffers`). Ship ndarray payloads as segments. | `utils/_conversion.py`, `wt.py`, `kt.py` | −25 % bytes, and removes base64 encode/decode + JSON scanning of the bulk data on worker, supervisor and P_local. Largest single win for raster/scatter data. |
| OPT-2 | Pre-encoded pass-through still does `bytes → str` (`_restore_pre_encoded`, `kt.py:116-117`) then `str → bytes` (`_extract_pre_encoded`, `kt.py:105`) in the supervisor, and `bytes → str` again in P_local: three full copies with UTF-8 validation per large reply. Keep it as `bytes`/`memoryview` end to end (`json.loads` accepts bytes). | `kt.py`, `wt.py`, `ro.py:757-762` | Removes ~3 passes over each large reply. |
| OPT-3 | Non-pre-encoded envelopes are a JSON *string inside* the comm message's JSON content (`kt.py:263, 484-486`), so Jupyter's packer escapes and unescapes the whole thing. Always send the envelope as `buffers=[...]`. | `kt.py` | Removes one escape + one unescape pass per message. |
| OPT-4 | The supervisor fully deserialises and re-serialises every relayed message that is not pre-encoded (requests *to* the worker always are not). A relay needs only the routing header. | `sup.py` `relay`, `wt.py` | Removes a decode + encode of each request payload; matters for large flag/selection payloads. |
| OPT-5 | `_wrap_tuples` / `_unwrap_tuples` walk the entire value on every remote (de)serialise. Confirm they short-circuit on ndarray and large homogeneous lists. | `utils/_conversion.py` | [?] not yet read in detail. |

---

## 7. Suggested order of work

1. WS-1 + WS-2 + WS-3 together (one change to `process_messages`/`connect`): handshake on a local
   transport with a timeout, never fatal, retire the old transport only after success, never await a dead
   socket's close on the new connection's path.
2. RP-1 request de-dup (Python side first), which also defuses HB-1 and the tab-fight.
3. JS-1 single-flight connect in `cm.ts`.
4. JC-1/JC-2 (comm reopen → reset; raise `TransportNotConnectedError`).
5. Colab: CO-1 accounting, CO-2 lazy buffer, CO-3 delete dead build; then CO-5, CO-7; CO-4/CO-6 as
   experiments.
6. Remote: RK-1 (cancel/forget on timeout) and RK-3/RK-4 (never die silently) first; then RK-2 (split
   comms), RK-5, RK-6, RK-7. OPT-2 and OPT-3 are small; OPT-1 is the one worth measuring with
   `tests/manual/visplot/bench_remote_overhead.py` before and after.
7. Cleanup lists (PY-2, JC-4, CO-8, WS-5, RK-9).

## 8. Not done yet (for the next session)

- Read the files marked **not read** in Coverage; in particular `_persistence.py` (session reattach) and
  `cubevis/setup` for the sshpyk side, and `_conversion.py` for OPT-5 and the cost of `serialize()`.
- Audit handler call sites for HB-1 and RK-9 `[?]`: which handlers in `iclean/_ui.py`, `_cube.py`,
  `visibility_plotter.py` run synchronously on the loop, and for how long.
- Check the existing manual tests against this list, then write the fake-websocket tests in §7.
- Confirm the **[L]** items at runtime: WS-3 timing, JC-1/JC-3 in JupyterLab 4, CO-1 (watch
  `cubevis_poll` traffic in the Colab console), CO-6 (drop the Output wrapper), RK-1 iopub loss.
- `cm.ts` lines 1–149 were not read line by line.

Tests worth adding alongside (all can run without a browser, against `CommMgr` with a fake websocket):
connect-then-drop before `initialize`; garbage first frame; second connection while the first is healthy;
replayed request id while the original is still running; reply sent across a reconnect.
