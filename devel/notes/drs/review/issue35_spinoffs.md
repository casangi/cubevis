# Issues spun off from #35 (2026-11-25)

Drafts for GitHub, one section per issue. Each refers to the review
(`devel/notes/drs/review/transport_review_2026-11-16.md`, "the review") by item.

---

## 1. Restore GUI state when a page reconnects to a running session

**Labels:** iclean, visplot, notebook

After a page reload in JupyterLab -- saved notebook or `show(redisplay=True)` -- the GUI starts from its
state at `show()` while the kernel-side session carries on. A saved notebook restores the output as it
was first rendered; `redisplay=True` renders the kernel-side Bokeh models, which the browser's changes
never reach. Checked with `devel/tools/jupyterlab_headless/examples/reload_state.py`: after one iclean
cycle with niter=40 (5 left) and an unsent cycleniter edit, both paths came back with niter 50,
cycleniter 10, status "initial residual image" and a one-point convergence plot. Images and masks come
from the cubes and are current.

**The hazard:** Continue after such a reload sends niter=50, not 5 -- the user's iteration budget is
reset without any sign.

Shape of a fix: when a page connects to an existing session, it asks the kernel for the session's state
and brings its controls up to date. The kernel already holds most of it (iterations left, convergence
data, the last stop code). Edits never sent (a cycleniter change, regions not yet applied) stay lost
unless the GUI sends them as they happen. iclean first, then visplot (its view state: selection,
iteration position, layout).

Related: the earlier question of clean vs resumed state, restoring a saved-as-active notebook (a new
kernel has no session: the GUI is shuttered, which is correct).

**iclean done on `44-visplot-in-notebooks` (2026-11-25):** a page that loads asks the kernel for the
session's state (`InteractiveCleanUI.session_state`: the iteration and automask values the imager has,
niter and nmajor as left, the last result, the whole log, whether a cycle runs) and applies it; a page
loaded while a cycle runs waits for it with the buttons disabled. `reload_state.py` (saved and
`redisplay=True`) and `reload_running.py` check it. visplot still to do.

Design agreed 2026-11-25: `devel/notes/drs/review/notebook_state_design.md` (reattach, snapshot on
open, checked restore on run).

---

## 2. visplot in notebooks

**Labels:** visplot, notebook

Found in the 2026-11-24/25 notebook checks (`devel/tools/jupyterlab_headless`):

- `visplot.notebook(...)` is not implemented ("not yet available"); notebooks use `VisibilityPlotter`
  directly. Needs a `visplot_notebook` layer from `scripts/sync_layers`, as iclean has
  `iclean_notebook`.
- The top toolbar row is wider than a default notebook output and is cut off on the right.
- When the cell output and an Output View of it render at the same moment (JupyterLab restoring a
  workspace with the view open), both copies share one kernel comm; the copy that does not hold
  `comm.onMsg` gets no replies (review JC-1, "found, not fixed").
- Occasional stray "Bokeh is not defined" when a restored output's script runs before BokehJS
  (`_showable._wait_for_bokehjs` fixed the common case).
- In a kernel, a long synchronous render holds the kernel: TW Hya's initial scatter (102 M samples,
  ~10.5 GB with 4.7 GB free) took 10.7 s. See issue 3.

Direction agreed 2026-11-25 (`notebook_state_design.md`): a single configured panel as the notebook
idiom, the full app still supported.

Fixed on `44-visplot-in-notebooks` (2026-11-25): a reloaded page that sent before its comm was found
closed the comm and retried a WebSocket for ever (`comm_mgr.ts`; see the `jupyterlab_headless` README).

Fixed on `35-transport-bugs` (for reference): `visplot(...)` run in a cell opened a tab that could not
reach the kernel (review WS-7).

---

## 3. Long handlers off the event loop (HB-1, rest)

**Labels:** transport, performance

The review's HB-1 (1) and (2) are done (busy-tolerant heartbeat, loop-lag monitor). Still to do: move
handlers the lag monitor shows to be long off the event loop. Static audit: every image-cube handler
(`_image_pipe` channel fetch with statistics and histogram, spectrum, `edit_mask` -- cube scope can be
long -- and the colormap histogram) runs on the loop; moving them needs one lock per cube, as casacore is
not thread-safe and loop-side code touches the same cube.

Decided 2026-11-23: measure first. Run iclean and visplot on TW Hya with `CUBEVIS_DEBUG=1` and collect
`event loop blocked for N s (running: <handler> ...)` lines (DEBUG from 1 s, INFO from 8 s); move what
they show, in order of time.

---

## 4. Colab transport (review §5)

**Labels:** transport, colab

Untested in this round. Open: CO-1 (`_colab_inflight` accounting), CO-2 (chunk reassembly assumes `init`
first), CO-4 (encoding overhead), CO-5 (kernel `_parents` from a worker thread), CO-6 (BroadcastChannel
premise), CO-7 (messages before the bridge listens), CO-8 (dead code). CO-3 fixed. Also: check JC-1's
behaviour (reload, second view, `show(redisplay=True)` -- which currently says "not available" in Colab)
on Colab.

---

## 5. Remote-kernel bridge (review §6)

**Labels:** transport, remote

RK-1 fixed (forget on timeout). Open: RK-2 (one comm serialises every remote call, including status
polls), RK-3 (an undecodable frame kills the relay for a worker), RK-4 (unobserved background tasks),
RK-5 (`KernelClientTransport.run` error handling), RK-6 (kernel-side transport never reports connected),
RK-7 (`JobRegistry` keeps every job), RK-8 (read loops awaiting callbacks inline), RK-9 (small items),
and the remote data-path optimisations (OPT-*). Order from the review: RK-3/RK-4 first, then RK-2, RK-5,
RK-6, RK-7; measure OPT-1 with `tests/manual/visplot/bench_remote_overhead.py`.

---

## 6. User preferences: save and restore

**Labels:** iclean, visplot, notebook

Save and restore data-independent preferences: gear tab shown / pinned (`gear_pin`), `pin_layout`,
initial layout or preset, sidebar collapsed, theme, `time_labels`, colormap defaults. Wanted inside
notebooks (issue 1's notebook state) and outside them alike.

Agreed 2026-11-25 (design in `devel/notes/drs/review/notebook_state_design.md`): a `prefs` scope in
`view_state.py`; layers built-in defaults < `~/.config/cubevis/prefs.json` < override file (`prefs=` on
`VisibilityPlotter` and the tasks, or `CUBEVIS_PREFS`; partial, merged over the user file) < the
notebook output's saved state < explicit arguments. Saving is explicit ("Save as my default"). One
preference set, read where the GUI is built; no kernel-specific preferences for remote kernels.
Preference-backed constructor arguments need a sentinel default, as `sync_layers` copies defaults
verbatim into the task layers.

---

## Not an issue: cubevis release

The kernel fingerprint (`python -m cubevis._fingerprint`) changed several times on `35-transport-bugs`;
a release when the branch merges, so remote kernels and notebooks pick up the new JS tag.
