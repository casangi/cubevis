# visplot with a remote kernel — demos

These scripts are **not tests**. They are meant to be read and run at a
terminal, one section at a time, to see how visplot behaves when its data
side runs in a Jupyter kernel (`backend="remote"`). Each section prints what
it is about to do and why, then does it. Where a step stands for something
you would write yourself, the call is shown first, under
`# in your own script:`, with the values of this run; where it is a GUI
action, the section says which. With `--step` the script waits for Enter
before each step (after showing its call).

They run from a cubevis checkout without installing it: the repository
root is put first on the Python path (`CUBEVIS_ROOT` overrides), and the
first line printed says which cubevis is in use. Nothing needs to be copied
or linked into this directory.

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

* `--ms PATH` — the MS on this machine (required by demo 1, which compares
  the two sides; demos 2 and 3 only need it when the kernel runs here);
* `--remote-ms PATH` — the data as a path on the kernel's host, when it
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

python 02_remote_flagging.py --remote-ms /path/on/kernel/host.ms \
    --kernel cvpost106_python312 --field 3c279 --step
python 03_remote_gui.py --remote-ms /path/on/kernel/host.ms \
    --kernel cvpost106_python312 --field 3c279
```

Demo 1 needs a local copy of the data as well (it compares the two sides).
Give `--field` for a first look: without it the first Plot reads every
field through the kernel, which on a large MS takes minutes.
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
* **A timeout.** A data call (an image, a flag box) may take up to 600 s
  before visplot gives up and says which call and how long it waited; set
  `CUBEVIS_REMOTE_TIMEOUT` (seconds) to change that. Small calls (metadata,
  versions) give up after 30 s.
* **"identical: NO"** in demo 1 means the two sides disagree — that is a bug
  worth reporting with the demo's output and `CUBEVIS_DEBUG=1` logs.
