# visplot optimization: remote execution and flagging performance

*September 2026 — FlagDB v2 development (parts 8–20)*

This document records a round of performance work on `visplot` (`VisibilityPlotter`) driven by the FlagDB v2 flagging workflow, with an emphasis on sessions whose data access runs in a remote Jupyter kernel. It describes how performance was measured, the inefficiencies that were found, what was changed, and recommendations for future features and for managing remote sessions.

The detailed day-by-day log, including every intermediate measurement, is in `flagdb_v2_notes.md` in this directory.

---

## 1. Summary

A user-visible flag operation — draw a box, apply the flag, redraw both panels — was measured end to end on two remote hosts. Over the course of the work, the time a remote session adds on top of an equivalent local session fell by roughly a factor of 3.5–8, depending on the operation.

**Extra time over a local session, zuul06 (sshpyk kernel), TW Hya `Ceres` field:**

| operation (flag + both redraws) | first remote run | final | 
|---|---:|---:|
| raster flag box | +3.3 s | **+0.95 s** |
| scatter flag box | +6.4 s | **+0.83 s** |
| zoomed raster flag box | (not measured) | **+0.91 s** |

**Final totals (bench 013):**

| operation (flag + both redraws) | zuul06 | cvpost140 | local (Mac) |
|---|---:|---:|---:|
| raster flag box | 1.34 s | 1.10 s | 0.39 s |
| scatter flag box | 1.19 s | 1.11 s | 0.36 s |
| undo | 1.29 s | 1.14 s | 0.36 s |
| zoomed raster flag box | 1.25 s | 1.06 s | 0.34 s |

All four agreed success criteria are met (Section 5). What remains is mostly the worker actually redrawing (on CPUs 2–3× slower than a current Mac) plus an irreducible per-call network floor. Session **start-up** on zuul06 (~160 s) is the largest remaining remote cost, and it lies outside visplot (Section 7).

---

## 2. Test setup and measurement method

### 2.1 Environment

- **Data:** `sis14_twhya_calibrated_flagged.ms` (ALMA, one SPW of 384 channels, 2 correlations), usually restricted to field `Ceres` to keep runs short and comparable.
- **Hosts:** `zuul06` and `cvpost140`, reached through sshpyk kernel specs (`zuul06_python312`, `cvpost140_python312`), with the client on a macOS laptop. Development measurements also used a local `python3` kernel on the same machine as the client, which isolates protocol and serialization costs from the network.
- **Topology of a remote call:** P_local (client, holds the Bokeh GUI) → Jupyter kernel on the remote host (supervisor) → worker subprocess (opens the MS, does the computation) → back the same way.

### 2.2 Tools added for this work

| tool | what it gives |
|---|---|
| `tests/manual/visplot/bench_remote_overhead.py` | Per-operation local vs remote vs worker time, overhead and payload; `--ref-scales` sweep; `--gui` user-visible latency through `VisibilityPlotter`, a zoomed case, a per-operation breakdown (calls, bytes, worker compute, worker encode, client decode, relay/net) and worker time per method. |
| `RemoteReductionContext.call_stats()` / `VisibilityPlotter.remote_call_stats()` | Client round-trip time, worker compute time and their difference, per remote method; relay counters (bytes, encode/decode seconds) on the worker, kernel and client. |
| `RemoteReductionContext.runtime_info()` | Which `cubevis` the remote worker actually imported (path, Python, relay counters present, frame debugging on/off, size of any leftover debug log). The benchmark prints it and warns on a mismatch. |
| `tests/manual/visplot/test_flagdb_remote.py`, `test_remote_flagging.py` | Remote results must equal local results (flag evaluation, flag views, InfoTool probe, large results through the relay, incremental pending-flag sync, memoised coordinate queries). |
| `tests/manual/visplot/remote_visplot_check.py`, `REMOTE_TESTING.md` | Scripted end-to-end local-vs-remote comparison without a browser, and a manual GUI checklist. |

### 2.3 Reading the breakdown

For each GUI operation the benchmark splits remote time into:

- **worker** — time the worker spent computing, summed over calls;
- **w-encode** — worker serializing its replies;
- **c-decode** — the client deserializing them;
- **relay/net** — everything else: kernel relay, Jupyter messaging, the network, and client work between calls.

`w-encode` reading 0 ms is the signature of a remote host running an older `cubevis` without the relay counters (see Section 7.2).

---

## 3. Inefficiencies found and improvements made

Items are grouped by kind. Gains are measured, not estimated, unless noted; "sandbox" means the local-kernel development environment.

### 3.1 Configuration and correctness issues that distorted performance

**A. `kernel_name=` was silently ignored.** With the default `backend="auto"`, `VisibilityPlotter(kernel_name=...)` opened the MS locally. A kernel-host path then failed with a local `FileNotFoundError`, and any benchmark with a path valid on both sides silently measured a *local* session. **Fix:** `kernel_name` with `backend="auto"` now selects the remote backend (logged); with another explicit backend it warns. The benchmark and tests assert that the reader really is remote.

**B. A re-entrant lock deadlock in the flag handler.** The panel's flag Comm already runs its handler under the panel render lock; the handler took the same non-reentrant `asyncio.Lock` again, so a flag request waited forever with no error on either side. **Fix:** the handler no longer re-acquires the lock, and flag evaluation runs in a worker thread (`asyncio.to_thread`) so the event loop stays responsive.

**C. A shared pan/zoom debounce timer.** All figures shared one `window._cvRerenderTimer`, so refreshing two panels at once cancelled one of them. **Fix:** one timer per figure.

### 3.2 Payload size

**D. Oversized scatter reference for remote sessions.** The two-level scatter renderer ships a reference aggregate for local pan/zoom. The remote default was `ref_scale=4` (16× the display pixels), shipped on every full render — including the redraw after every flag. **Fix:** remote default `ref_scale=1.0`.

| ref_scale | wire | overhead, zuul06 (before relay fix) |
|---:|---:|---:|
| 0 (none) | 126 kB | 65 ms |
| 1 | 846 kB | 246 ms |
| 2 | 1.8 MB | 437 ms |
| 4 (old default) | 3.9 MB | 1110 ms |

At 1.0, panning and zooming out stay local; zooming in re-queries (~0.2–0.3 s).

### 3.3 Transport (the relay)

**E. Leftover frame diagnostics.** `cubevis/remote/_worker_transport.py` still ran a September-6 debugging aid unconditionally: it opened, appended to and closed `/tmp/cubevis_frame_debug2.log` for every frame *and every 4 kB chunk*, and MD5-hashed each payload twice. Logs had grown to ~20 MB on the hosts. **Fix:** off unless `CUBEVIS_FRAME_DEBUG=1`; one `readexactly()` per payload. The cost depends on `/tmp`; on the test hosts it turned out small, but it was unbounded.

**F. The kernel decoded and re-encoded every reply.** Each worker reply was fully deserialized in the kernel (arrays rebuilt as Python objects) and serialized again for the client, with base64/JSON text at every hop. **Fix (opt-in per request, so older clients are unaffected):**
1. the worker encodes a `call_method` result once (`pre_encoded`);
2. worker → kernel frames carry it as a raw segment after the JSON header (length word high bit = flag), never JSON-escaped;
3. kernel → client, it travels as a Jupyter comm **binary buffer**; the client decodes it once.

| ref_scale 1 full scatter render, overhead | before | after |
|---|---:|---:|
| zuul06 | 247 ms | 126 ms |
| cvpost140 | 227 ms | 120 ms |

Relay cost fell from roughly 250 ms/MB to ~140 ms/MB on the hosts.

**G. Pending-flag state through the Bokeh serializer.** Pushing pending flags to the worker as nested lists of dicts cost 0.72 s for 100 region deltas naming 325 baselines each (sandbox). **Fix:** one JSON string (52 ms), then **incremental sync** — after the first full send only the ordered delta ids plus unseen deltas cross the wire, with automatic full resend on any mismatch. zuul06: adding one delta to 100 costs ~30 ms instead of ~210–280 ms.

**H. Repeated coordinate-only round trips.** Every redraw asked the worker `axis_info` ×4 and `identity_tables` ×1 — the same answers each time, ~22–25 ms each on the hosts. **Fix:** client-side memo keyed on the selection (minus flag view) and the data generation (Reload); coordinates cannot depend on flags. A redraw after a flag went from 9 remote calls to 4.

### 3.4 Worker computation

**I. The scatter re-read the MS after every flag change.** Cached scatter frames were keyed on the pending-flag version, so any flag, unflag, undo or display switch re-read the selection from disk. **Fix:** cache *raw* frames — every valid sample with plotted x/y, identity (time, baseline, frequency, SPW, channel) and on-disk flag — independent of pending state and flag view, and apply the current flag view per row (`flag_engine.frame_keep_mask`). Z-Score is finalized after the view, so its reference remains the drawn population. Sandbox: scatter render after a flag 776 → ~510 ms.

**J. The per-row flag view was itself slow for sample-set flags.** Matching 1.46 M rows against an explicit sample set used one binary search per row per delta (~0.33 s; 2–3× that on the hosts) — this briefly made the remote scatter-flag redraw *slower*. **Fix:** decode row identity once per frame (unique times/frequencies + inverse index, cached baseline lookups) and cache the effective flag state per pending-delta list, reusing the longest cached prefix (a flag applies one delta; an undo returns a cached state). Row filter: 330 ms → 166 ms first time, ~0 ms unchanged, 66 ms per added delta.

**K. The scatter image and its reference binned the same data twice.** For each layer, the display image and the pan/zoom reference were built by separate passes over the same frame and viewport: the hover-probe id grid (always identical) and, at equal resolution (remote `ref_scale=1`), the (x, y) aggregation. **Fix:** a small identity-keyed, weakref-checked, thread-safe memo shares both; output is bit-identical. Sandbox two-layer render 352 → 265 ms; zuul06 worker −120 ms per redraw.

**L. Flag and InfoTool boxes re-read the MS.** A scatter flag box (and the InfoTool box probe) re-read the whole selection to find the samples inside the box. **Fix:** resolve both over the panel's cached raw frames with identical rules (visible layers, hidden categories, flag state). Scatter-box evaluation: 414 → 32 ms on zuul06; TW Hya two-layer box 607 → 38 ms; InfoTool probe 360 → 51 ms. The equivalence tests caught one real bug on the way: sample identity must include the SPW, because windows can share frequencies.

### 3.5 Redundant queries

**M. A second scatter query after flagging outliers.** Flagging outliers shrinks the data extent while the view stays put; the Level-1 rule "viewport outside the reference → re-query" forced a second query, although a *full-extent* reference has nothing outside it. **Fix:** Level-1 may serve viewports beyond a full-extent reference. zuul06 scatter flag + redraws 3.08 → 2.21 s at the time.

**N. A double query when zoomed in.** After a flag while zoomed, the panel re-read the full extent and then queried the zoomed view. A viewport query already returns the full-data extent, global scaling and colour-bar inputs. **Fix:** when clearly zoomed (10% margin), drop the stale references and make only the viewport query; image, extent, colour bar and histograms are identical to the old path. zuul06 zoomed flag + redraws 1.91 → 1.25 s.

### 3.6 Fidelity fixes found along the way

These were not performance work, but were found while measuring and are listed for completeness: the Flag-fraction raster removed padded slots *after* averaging (a Time × Frequency Flag raster came back 3-D and padding inflated the fraction); raster boxes are now resolved on the displayed (union) grid; a raster Channel axis that the backend could not relabel is treated as Frequency; light/dark theming of the info strips now uses CSS variables so a late style update cannot revert them.

### 3.7 Progress by benchmark run (zuul06, flag + both redraws, totals)

| run | change deployed | raster | scatter | undo | zoomed |
|---|---|---:|---:|---:|---:|
| 002 | remote ref_scale 1.0 | 1.94 s | 3.12 s | 1.95 s | — |
| 005 | one query after scatter flag (M) | 1.95 s | 2.21 s | 1.96 s | — |
| 006 | raw frame cache (I) | 1.80 s | 2.45 s | 1.81 s | — |
| 007 | row-identity / incremental view (J) | 1.87 s | 2.09 s | 1.76 s | — |
| 008 | relay pass-through (F) | 1.62 s | 1.94 s | 1.60 s | — |
| 009 | coordinate memo (H) | 1.51 s | 1.73 s | 1.39 s | 2.02 s |
| 010 | shared binning (K) | 1.33 s | 1.65 s | 1.29 s | 1.86 s |
| 011 | scatter box from frames (L) | 1.35 s | 1.19 s | 1.26 s | 1.91 s |
| 013 | single zoomed query (N) | 1.34 s | 1.19 s | 1.29 s | 1.25 s |

(Run 006 includes Zoom-call network noise on zuul06; cvpost140 followed the same trend.)

---

## 4. Current cost model (zuul06, one flag operation)

| component | cost |
|---|---:|
| worker compute (scatter render ~0.5 s, raster render ~0.1 s, flag evaluation 0.03–0.22 s) | 0.7–0.9 s |
| relay/net (4 calls × ~20 ms floor + ~0.14 s/MB of results) | ~0.25–0.3 s |
| worker encode + client decode | ~0.13 s |

Remote calls per flag operation: scatter render, raster render, flag evaluation, pending-flag sync. No data are accessed twice.

---

## 5. Success criteria (agreed) and status

1. No duplicated data access or computation in a flag operation — **met**.
2. Remote adds ≤ ~0.3 s beyond the worker's compute — **met** (~0.28 s for a 1.8 MB redraw).
3. Flag / unflag / undo + both redraws ≤ 1.5 s on zuul06, zoomed or not — **met** (slowest 1.34 s).
4. The samples a flag operation addresses are exact and covered by tests — **met**; every fast path has an equivalence test against the original path.

---

## 6. Recommendations for future feature additions

### 6.1 Design rules that came out of this work

1. **Never re-read the MS for a state change that only changes which samples are drawn.** Flags, views, previews and hidden categories are masks over cached rows. If a new feature needs data the raw frames lack, extend the raw frame (a column costs a few bytes per sample) rather than adding a re-read.
2. **One query per interaction.** When adding a redraw path, count the backend calls it makes (the benchmark's `calls` column does this) and justify each.
3. **Coordinate-only queries are cacheable.** Anything that reads only partition coordinates (axes, identity tables, SPW keys) should go through the client memo (`RemoteReductionContext._memo_call`) and must not depend on flags.
4. **Keep remote payloads proportional to what is displayed.** Default to display-resolution results; ship references or grids only when they save a round trip that would otherwise be needed. Prefer compact encodings (JSON strings for structured state, pre-encoded pass-through for large results).
5. **Share work between paths that see the same data.** The Flag box, InfoTool box and scatter render now resolve samples from the same cached rows; a new selection-based tool should do the same, which also keeps tools in exact agreement.
6. **Every fast path gets an equivalence test** against the straightforward path (`force_ms=True` exists for flag evaluation and the InfoTool probe for exactly this purpose), plus a test that the fast path does not read visibilities.
7. **Measure remote and local.** Run `bench_remote_overhead.py --gui` against a local kernel during development and against a real host before release; add a benchmark case for any new interactive operation.

### 6.2 Checklist for a new remote-capable method

- Add it to `VisplotRemoteBackend` (worker timing is automatic) and to `RemoteReductionContext` via `_call` (pre-encoded pass-through is automatic).
- If it is coordinate-only, route it through `_memo_call`.
- Structured arguments (lists of dicts) → send as one JSON string.
- User-supplied Python callables cannot cross the wire; fail clearly on remote (as flag filters do).
- Add a local-vs-remote equality test to `test_flagdb_remote.py` (or a sibling).

### 6.3 Further optimization opportunities (not done; estimated)

| opportunity | estimated gain (zuul06) | notes |
|---|---:|---|
| Optimistic client-side masking of the flagged box while the exact redraw is computed | perceived latency → ~0.2 s | small TypeScript change; the exact image still replaces it |
| Redraw only the panel(s) whose drawn samples changed | 0.1–0.5 s | needs per-panel change detection; a raster box on a different correlation does not change the scatter |
| Raster box sample counts from a cached raster frame | ~0.2 s | counts feed the notification/preview, so keep them exact |
| Binary arrays end to end (no base64/JSON for array data) | remaining ~0.1 s/MB | larger change to the shared serialization layer |
| Z-Score scatter layers and non-identity filters from cached data | 0.3–0.6 s for those cases | needs the reference population / complex visibilities in the cache |
| Adaptive `ref_scale` per link (measure RTT and bandwidth at connect) | varies | a slow link favours `ref_scale` 0–1, a fast one 2 |

### 6.4 Functional features queued next

Unflagging on-disk flags (draw disk-flagged data so an Unflag box can select it); commit / export (apply pending flags to the MS or via `flagdata`, reload an exported JSONL); a partial-cell marker for the raster in "Hide flagged" mode. Each should follow the rules in 6.1 — in particular, the disk-flagged display is a new flag *view* over the existing raw frames, not a new read.

---

## 7. Remote session management recommendations

### 7.1 Start-up

- **zuul06 spends ~160 s before the first call**, as three consecutive ~51 s waits (kernel start, link open, worker context), where cvpost140 takes 2–4 s per step. Three near-identical waits indicate a per-connection stall (typically reverse DNS or GSSAPI authentication in SSH), not work. The sshpyk PR (`casangi/sshpyk#12`) addresses part of this; `ssh -v` to zuul06 will show whether a pause precedes authentication, and `UseDNS no` / `GSSAPIAuthentication no` would confirm the cause.
- **SSH connection sharing** (`ControlMaster auto`, `ControlPersist`) avoids repeating the handshake for the multiple connections a session opens.
- **Reuse sessions.** Kernel start dominates remote start-up; keeping a kernel alive across `visplot` invocations (or pre-starting it) turns a 1–3 minute wait into seconds.

### 7.2 Keeping both sides on the same code

- The kernel environment on each host imports its **own installed** `cubevis` (e.g. `…/envs/sshpyk-python312/lib/python3.12/site-packages/cubevis/`), independent of the client's working tree. Two benchmark rounds in this work were run against stale remote code before this was noticed.
- The benchmark now prints `runtime_info()` and warns when the remote side lacks the current relay counters. **Recommendation:** promote this to a version handshake at connect time — compare the client and worker `cubevis` versions (or a protocol version) and fail fast, or warn prominently, on mismatch.
- Update both kernel environments whenever `cubevis/remote/`, `remote_registrations.py`, `flag_engine.py` or the data backends change.

### 7.3 Paths and data

- MS paths are paths **on the kernel host**; `--remote-ms` (benchmark) and `CUBEVIS_TEST_KERNEL_MS` (tests) separate them from the client path. `REMOTE_TESTING.md` covers this.
- Verify a remote path with the kernel environment's own Python before starting a GUI session.

### 7.4 Housekeeping on the hosts

- Remove leftover `/tmp/cubevis_frame_debug2.log` files (~20 MB each on the test hosts) from before the diagnostic was disabled.
- The worker's frame cache is memory-resident; on shared hosts, size it for the host's memory and the expected selection sizes (it is already adjusted for remote sessions).

### 7.5 Diagnostics

| switch / call | use |
|---|---|
| `CUBEVIS_DEBUG=1` | debug logging on both sides, including every flag call with size and timing |
| `CUBEVIS_FRAME_DEBUG=1` (+ `CUBEVIS_FRAME_DEBUG_PATH`) | frame-level transport tracing — for transport bugs only; it is slow |
| `plotter.remote_call_stats()` | per-method client, worker and overhead times in a live session |
| `remote.runtime_info()` | which code and Python the worker is running |
| `bench_remote_overhead.py --gui` | reproducible end-to-end numbers for a host |

---

## 8. Files touched by this work

- `cubevis/remote/`: `_worker_transport.py` (diagnostics off by default, relay counters, raw frame segments), `_kernel_transport.py` (relay counters, binary-buffer pass-through), `worker_main.py` (pre-encoded results).
- `cubevis/toolbox/visplot/`: `remote_reduction_context.py` (call stats, runtime info, JSON and incremental pending sync, coordinate memo, pre-encoded decode), `remote_registrations.py` (worker timing, runtime info, JSON/incremental sync), `visibility_plotter.py` (`kernel_name` selects remote, `remote_call_stats`), `visibility_plot.py` / `visibility_scatter.py` (stale-redraw hook, Level-1 beyond full-extent reference, single zoomed query, remote `ref_scale`), `flag_engine.py` (row-level flag views, cached row identity, incremental state, box and probe resolution from frames), `data/reader.py` (raw frame cache, `_raw_frames`), `data/_scatter_render.py` (shared binning memo, Level-1 rule).
- Tests and tools: `test_flagdb_v2.py`, `test_flagdb_remote.py`, `test_remote_flagging.py`, `bench_remote_overhead.py`, `remote_visplot_check.py`, `REMOTE_TESTING.md`.
