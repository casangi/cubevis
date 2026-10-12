# Testing `visplot` with a remote kernel

Three layers, from fastest and most automatic to slowest and most manual.
Run them in order: each one assumes the one before passed.

| Layer | What | How long | Needs |
|---|---|---|---|
| 1 | `test_remote_flagging.py`, `test_remote_selection.py` (pytest) | ~2 min | a kernel; data optional |
| 2 | `remote_visplot_check.py` (script) | 1–5 min | a kernel + data |
| 3 | Manual GUI checklist (below) | ~15 min | a browser |

The pytest files need `pytest-asyncio` (`pip install pytest-asyncio`);
without it they fail to collect rather than skip.

For a guided, non-automated walk through the same behaviour, see
`demos/visplot-remote/` (scripts that explain each step as they run it).

Timeouts: calls that read data (images, flag boxes, rows with data) wait up
to 600 s for the kernel, other calls 30 s; `CUBEVIS_REMOTE_TIMEOUT=<seconds>`
changes the first. A call that runs out says which call and how long it
waited. (Before 2026-10-12 every call had 30 s; the tests passed
`call_timeout=300` and so never saw it.)

`CUBEVIS_DEBUG=1` turns on debug logging on both sides. Remote flag calls log
their timing (`remote evaluate_flag_request: kind=… -> delta, N changed, 0.4s`),
which is the first thing to look at if something is slow or silently does
nothing.

## Paths: local vs. kernel host

The worker opens the data **on the kernel's host**. When that path differs from
the one on your machine, give both:

* pytest: `MS=` (local) and `CUBEVIS_TEST_KERNEL_MS=` (kernel host), the same
  convention as `test_remote_reduction_context.py`; `PS=`/`CUBEVIS_TEST_KERNEL_PS=`
  for MSv4.
* script: `--ms` (local) and `--remote-ms` (kernel host); `--no-local` if there
  is no local copy (parity checks are then skipped, everything else still runs).

## Layer 1 — pytest

```
# local kernel, simulated MS (self-test of the remote path itself)
pytest test_remote_flagging.py test_remote_selection.py

# real kernel, real data
ulimit -n 8096 && MS=sis14_twhya_calibrated_flagged.ms \
  CUBEVIS_TEST_KERNEL=cvpost106_python312 \
  CUBEVIS_TEST_KERNEL_MS=/home/zuul06-2/dschieb/casa/visplot/sis14_twhya_calibrated_flagged.ms \
  pytest test_remote_flagging.py test_remote_selection.py \
         test_remote_reduction_context.py -v
```

Every flag call made against the remote worker is compared with the same call
against a local backend:

* box resolution for each built-in filter (identical deltas and counts);
* scatter boxes;
* all four flag views with pending flags and a proposal installed;
* the InfoTool box probe;
* refusal of user-supplied filters.

A headless remote plotter must also flag, undo and export exactly like a local
one. With real data the queries are limited to the first minute of the first
field.

`test_remote_selection.py` covers the selection and reporting features added
for H8, again local against remote:

* Scan, Time range, UV range (metres, kλ, two ranges) and the autocorrelation
  switch reach the worker: identical images and identical "baselines with
  data", and a UV range really narrows the image;
* a flag box under a UV range (stored as exact samples) matches local;
* the observation metadata carries the integration time (used for the
  half-integration around a single time);
* the worker reports its software versions, and the version comparison
  (`version_mismatch`) is quiet for identical versions and warns otherwise;
* a remote plotter built with `uvrange=`, `autocorr=`, `time_labels=` and
  `panels=` renders like a local one; its plot summary names the kernel and
  lists the kernel's software; its reproducing call includes
  `backend='remote'` and `kernel_name`; "rows with data" for stepping comes
  from the worker.

With `MS=`/`PS=` the same checks run on MSv2 and MSv4 data.

## Layer 2 — `remote_visplot_check.py`

```
python remote_visplot_check.py --kernel cvpost106_python312 \
  --ms sis14_twhya_calibrated_flagged.ms \
  --remote-ms /home/zuul06-2/dschieb/casa/visplot/sis14_twhya_calibrated_flagged.ms \
  --field J1037-295            # omit --field for the whole MS
```

This builds a local and a remote `VisibilityPlotter` on the same data. It drives
both through the handlers the browser uses and prints a PASS/FAIL/SKIP line with
the time for each step:

1. Open (kernel start, worker spawn, data open, first render)
2. Metadata
3. First render
4. Pan/zoom re-render
5. Hover probe
6. InfoTool box
7. Axis change
8. Flag: raster box
9. Flag: scatter box
10. Flag: Z-Score filter
11. Rendering with pending flags
12. Undo/redo
13. Colour display mode
14. User-filter refusal
15. JSONL export and the pending-flag report
16. Version check (kernel cubevis against local)
17. Selection in the worker: UV range, autocorrelations, scan, time range
18. Rows with data before Plot (stepping), as the Data section changes
19. Plot summary page and reproducing call
20. Time labels: UTC and elapsed
21. Close

Exit status 0 means everything passed. The timings double as a latency survey
of the remote path, and are worth keeping to compare later runs.

Expected differences that are **not** failures: a remote scatter keeps a larger
cached reference (`ref_scale`), so after a pan/zoom its image may have a
different pixel size than the local one.

`--gui` opens the real GUI on the remote kernel after the checks, ready for
layer 3.

## Layer 3 — manual GUI checklist (remote kernel)

Start with `remote_visplot_check.py … --gui`, or in Python:

```python
from cubevis import visplot
visplot(ms="/path/on/kernel/host.ms", backend="remote", kernel_name="cvpost106_python312")
```

Keep the terminal (Python log) and the browser DevTools console visible.

**Startup and viewing**

- [ ] Both panels render; the status bar names the MS and the selection.
- [ ] Hover shows cursor readouts on both panels; the InfoTool click and box
      pages open.
- [ ] Pan/zoom on both panels, including zooming far in (a re-query) and back
      out (served from the cache).
- [ ] Plot ▶ after changing field / SPW / correlation; Reload ↺.
- [ ] Presets (vplot, radplot, Waterfall, Z-Score) and Swap; One / Side by Side
      / Over-Under.
- [ ] Colorize (continuous, categorical with hide, statistical); colour
      scaling controls.
- [ ] Light/Dark (sidebar, Flagging controls, colour bars); Export PNG.
- [ ] No yellow version warning in the status line (or, if there is one, it
      names both versions correctly).
- [ ] Data section: type `<200m` in UV range without pressing Plot. The
      Baseline table dims baselines with no data; Baseline ▶ skips them.
      Press Plot: the status line shows the UV range and the panels narrow.
- [ ] Autocorrelations: Exclude / Include / Only, each followed by Plot.
- [ ] Scan and Time range (e.g. `09:30:00~09:45:00`), each followed by Plot.
- [ ] Time labels UTC ↔ Elapsed: axis labels change, nothing is re-read.
- [ ] "Describe this plot" (page icon on a plot's toolbar): opens once, titled
      with the MS name; it names the kernel, lists the software on both sides,
      and the reproducing call includes `backend='remote'` and `kernel_name`.
      Paste that call into a new session: it reproduces the plot.

**Flagging** (repeat on raster and scatter)

- [ ] Flag box with "All selected": status "✓ Flagged: N samples", Flag count
      increases, and the flagged data disappear from both panels.
- [ ] InfoTool box over the same area before flagging: its "Flag box here: N"
      equals the N reported by the Flag box.
- [ ] Unflag box over the same area restores the data.
- [ ] Filters: Z-Score, amplitude range and MAD with non-default parameters.
      Your own `flag_filters=` functions must be listed as "(user, local data
      only)" and refused with a clear message.
- [ ] Preview on: the proposal dialog appears, the proposed samples are orange,
      and Accept and Reject both work.
- [ ] Undo / Redo / Clear, with the panels refreshing each time.
- [ ] Show in colour: pending samples painted on both panels; change the colour.
- [ ] Describe pending flags; Save flags as JSON, then Load flags from JSON
      (as pending) into a fresh session. Note that the JSON is written on the
      machine running Python, not on the kernel host.
- [ ] Optional, on a **copy** of the MS: Write flags to the MS (the write runs
      in the worker, on the kernel host, with a backup), then open the copy
      locally and confirm the flagged data are gone. The MS HISTORY row and
      the backup record the cubevis version.

**Robustness**

- [ ] A large scatter box on the full data: the GUI stays responsive while it
      is resolved, and the result arrives.
- [ ] Close the browser tab: the Python session ends cleanly and the kernel's
      worker exits.

## What to send back when something fails

1. The script's summary lines, or the pytest output with `-v`.
2. The Python log with `CUBEVIS_DEBUG=1`, from the moment of the failing action.
3. Any browser console errors.
4. The kernel name and whether the kernel host differs from the local one.
