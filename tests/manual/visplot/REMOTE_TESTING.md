# Testing `visplot` with a remote kernel

Three layers, from fastest and most automatic to slowest and most manual.
Run them in order: each one assumes the one before passed.

| Layer | What | How long | Needs |
|---|---|---|---|
| 1 | `test_remote_flagging.py` (pytest) | ~1 min | a kernel; data optional |
| 2 | `remote_visplot_check.py` (script) | 1–5 min | a kernel + data |
| 3 | Manual GUI checklist (below) | ~15 min | a browser |

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
pytest test_remote_flagging.py

# real kernel, real data
ulimit -n 8096 && MS=sis14_twhya_calibrated_flagged.ms \
  CUBEVIS_TEST_KERNEL=cvpost106_python312 \
  CUBEVIS_TEST_KERNEL_MS=/home/zuul06-2/dschieb/casa/visplot/sis14_twhya_calibrated_flagged.ms \
  pytest test_remote_flagging.py test_remote_reduction_context.py -v
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
15. flagdata/JSONL export and the pending-flag report
16. Close

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
- [ ] Describe pending flags, Export flagdata, Export JSONL. Note that exports
      are written on the machine running Python, not on the kernel host.
- [ ] Optional: apply the exported flagdata file to a **copy** of the MS in CASA
      (`flagdata(vis=copy, mode='list', inpfile=…)`) and confirm it flags what
      visplot showed.

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
