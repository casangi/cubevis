# visplot with a remote kernel — demos

These scripts are **not tests**. They are meant to be read and run at a
terminal, one section at a time, to see how visplot behaves when its data
side runs in a Jupyter kernel (`backend="remote"`). Each section prints what
it is about to do and why, then does it. With `--step` the script waits for
Enter between sections.

The automated checks of the same behaviour are in `tests/manual/visplot/`
(see `REMOTE_TESTING.md` there): `test_remote_flagging.py`,
`test_remote_selection.py` (pytest) and `remote_visplot_check.py` (a script
with PASS/FAIL lines).

## The idea in one paragraph

visplot has two halves. The *plotter* (panels, controls, the browser
connection, the list of pending flags) always runs in the Python where you
start it. The *data side* (opening the MS, applying the selection, averaging,
aggregating to images, evaluating flag boxes) runs either in the same process
or, with `backend="remote", kernel_name=...`, in a worker started through a
Jupyter kernel — usually on the machine that holds the data. Only small
results cross between them: images, sample counts, flag deltas.

## The demos

| Script | Shows | Browser |
|---|---|---|
| `01_local_and_remote.py` | the two halves; the version check; a UV range applied in the kernel; identical local and remote images; "rows with data" for stepping; the plot summary of a remote plotter; a PNG from each side | no |
| `02_remote_flagging.py` | a flag box resolved in the worker; the same box under a UV range (stored as exact samples); the pending-flag record and JSON export; undo / redo | no |
| `03_remote_gui.py` | a guided tour, then the real window on a remote kernel | yes |

## Running them

Options common to all three:

* `--ms PATH` — the MS on this machine (required);
* `--remote-ms PATH` — the same data as a path on the kernel's host, when it
  differs (default: `--ms`);
* `--kernel NAME` — the Jupyter kernel (default `python3`, a kernel on this
  machine: the full remote path without a second host);
* `--field NAME` — plot one field;
* `--step` — pause between sections.

```
cd demos/visplot-remote

# everything on one machine (local python3 kernel)
python 01_local_and_remote.py --ms sis14_twhya_calibrated_flagged.ms --field 3c279 --step
python 02_remote_flagging.py  --ms sis14_twhya_calibrated_flagged.ms --field 3c279 --step

# kernel on the data host
python 01_local_and_remote.py --ms sis14_twhya_calibrated_flagged.ms \
    --remote-ms /home/zuul06-2/dschieb/casa/visplot/sis14_twhya_calibrated_flagged.ms \
    --kernel cvpost106_python312 --step

python 03_remote_gui.py --ms sis14_twhya_calibrated_flagged.ms \
    --remote-ms /path/on/kernel/host.ms --kernel cvpost106_python312
```

Demo 1 needs a local copy of the data as well (it compares the two sides).
Demo 3 calls the `visplot` task; without casatasks installed it starts the
plotter the same way the task does. Outputs (PNG, summary page, flag
exports) go to a temporary directory whose path is printed; nothing is
written to the MS.

## What to look for

* **Version warning.** If the kernel's cubevis differs from yours (another
  commit, or uncommitted changes on either side), visplot says so in the log,
  in a yellow line in the status bar, and in the plot summary. Results may
  still be right, but report problems with both versions.
* **Timings.** Each step prints how long it took. Opening a remote plotter
  costs a few seconds (kernel start, worker spawn, opening the data); after
  that a re-render or a flag box should be close to the local time plus a
  network round trip.
* **"identical: NO"** in demo 1 means the two sides disagree — that is a bug
  worth reporting with the demo's output and `CUBEVIS_DEBUG=1` logs.
