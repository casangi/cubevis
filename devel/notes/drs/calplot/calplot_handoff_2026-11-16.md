# Handoff: calplot after C0 (2026-11-16)

C0 is done: CASA calibration tables can be read, written, flagged and
queried without CASA.  What remains is the application (C1), solution
flagging in it (C2), and the later stages.

Read first:
- `devel/notes/drs/calplot/calplot_notes.md`: one section per slice
  (1 the reader, 2 the synthetic source and writer, 3 engine
  registration, 4 `commit_flags`, 5 `CalTableReader`, 6 the document,
  7 what xradio's PR 578 discussion and PR 690 say, and how the
  reader was brought into line).
- `devel/docs/gridplot/grid_and_calplot_plan.md`, mirrored to the
  claude.ai project doc `claude/grid_and_calplot_plan.md`: sections 4
  (calplot's scope), 3 (the grid component), 9.4 to 9.6, and 19 to 23
  (what C0 built).
- `devel/docs/calplot/casa_caltab_xarray.md`: the short account of the
  backend, written for CASA's and xradio's people.
- For the grid component C1 builds on:
  `devel/notes/drs/gridplot/gridplot_notes.md` sections 3, 4, 7 and 12,
  and `gridplot_handoff_2026-11-13.md`.

## Where things stand

* **Branch `calplot`** (started from `gridplot` at `3ef11065`).  The
  last slice is `calplot-c0h_460f502e.zip` (it replaces `c0g`, which was
  never applied); ask Darrell whether it is pushed before starting.
* **`gridplot` is not yet merged to main.**  A grid fix found while
  building C1 has to go to both branches until it is.
* **Tests**: `tests/manual/calplot/`, 151 passing with `CALTABLES` and
  casatools; the `caltab` suite in `tests/ci/suites.toml` runs the ones
  that need neither.

## What exists

```
cubevis/xarray/                       no Bokeh, no CASA
    _arcae_util.py                    table.info, keywords, subtables, FLAG cast, reads by row
    _register.py                      the engine without an installed entry point
    caltab/
        _schema.py                    EVERY schema choice: names, dims, units, per-type packing
        _build.py                     RawCalTable (rows); build_tree; tree_to_raw
        _reader.py                    read_raw, open_caltab, caltab_info        (arcae)
        _backend.py                   xarray's entry point, engine "casa-caltab"
        _synthetic.py                 synthetic_caltab, synthetic_value
        _write.py                     write_caltab: a tree as a CASA table      (arcae)
        _flags.py                     commit_flags, restore_flags, list_flag_backups
cubevis/toolbox/calplot/
    reader.py                         CalTableReader, CalInfo, CalSamples
devel/tools/calplot/
    make_caltables.py                 simulated MS and 6 tables with known answers (needs CASA)
    tl016b_caltables.py               the CASA VLBA guide's calibration of TL016B (needs CASA)
    caltab_example.py                 the tour the document tells people to run (no CASA, no data)
```

In one breath:

```python
import xarray as xr
from cubevis.xarray.caltab import (open_caltab, synthetic_caltab, write_caltab,
                                   commit_flags, restore_flags)
from cubevis.toolbox.calplot import CalTableReader

dt = xr.open_datatree("cal.G")                    # engine "casa-caltab"; one node per SPW
dt = xr.open_datatree("cal.G", polarization_type=["R", "L"])   # if the receptors are known
dt = synthetic_caltab("Fringe", nant=27)          # the same shape, from numbers
write_caltab(dt, "x.cal")                         # a CASA table; applycal accepts it
dt["spw_0"]["FLAG"].loc[dict(antenna_name="A03")] = True
commit_flags(dt, "x.cal")                         # checked, backed up, read back

r = CalTableReader.open("cal.G")                  # also a Zarr store, or a tree
r.info()                                          # CalInfo: names, windows, quantities
s = r.read("phase", antenna="ea05", spw=[0])      # CalSamples: flat numpy arrays
r.evaluate("phase")                               # a K table as the phase it applies
r.set_flags(antenna="ea05"); r.commit_flags()
```

## The rule for C1

**calplot code talks to `CalTableReader` only.**  It gets `CalInfo` and
`CalSamples` (names, numbers, flat arrays) and never the tree, and it
does not import `cubevis.xarray.caltab`.  How the tree is arranged is
then free to follow xradio's schema when that settles.  If C1 needs
something the reader does not give, add it to the reader.

Likewise, a schema change (a name, a unit, a dimension) is an edit to
`caltab/_schema.py`, and then to `_build.py` only if the arrangement
changes.

## Data the new session needs

Nothing in the sandbox survives.  Ask Darrell to upload:
* **`caltables.tgz`** (0.86 MiB): the nine real TL016B tables and
  `layout.txt`.  Unpack it into a new, empty directory.

Make the simulated set there (needs casatools; about 2 minutes):

```bash
python devel/tools/calplot/make_caltables.py $CALTABLES/sim
```

`CALTABLES` is a folder holding `sim/` and `tl016b/`.  Tests that want
tables skip without it.

## Environment (cloud sandbox)

```bash
python3 -m venv /tmp/ivenv && /tmp/ivenv/bin/pip install -U pip
cd <repo> && /tmp/ivenv/bin/pip install ".[iclean,visplot]" casadata six pytest
/tmp/ivenv/bin/pip uninstall -y cubevis && /tmp/ivenv/bin/pip install --no-deps -e .
rm -rf .pdm-build                                  # left by the install; not for the repo
mkdir -p ~/.casa
D=$(/tmp/ivenv/bin/python -c "import casadata, os; print(os.path.dirname(casadata.__file__))")/__data__
printf 'measurespath = "%s"\nmeasures_auto_update = False\ndata_auto_update = False\n' "$D" > ~/.casa/config.py
CALTABLES=/path/to/tables /tmp/ivenv/bin/python -m pytest -q tests/manual/calplot
```

* The code itself needs only `pip install ".[xarray]"`: arcae, xarray,
  zarr.  casatools is for making tables and for the comparison tests.
* The editable install (`-e`) is what lets the tests see edits.  After
  verifying a zip with a normal install, put the editable one back, or
  the next test run uses the stale copy.
* GitHub's API and web pages are closed to the sandbox; `git clone` and
  `git fetch` of public repositories work.
* casatools' leap-second warning from the `casadata` wheel is harmless.

## Working conventions (Darrell)

* **Never commit or push.**  Deliver a zip of complete files, unzipped
  at the repo root and named with the base commit
  (`calplot-c1a_<commit>.zip`).  List files to delete separately.
* Before each slice: `git fetch origin calplot`, then
  `git reset --mixed origin/calplot` once Darrell says GitHub is up to
  date.
* Verify each zip on a clean checkout (`git worktree add --detach
  /tmp/verify origin/calplot`, unzip, run the tests both uninstalled
  with `PYTHONPATH` and with a normal install).
* One notes section per slice in `calplot_notes.md`.  When the plan
  changes, update it and `project_write` the project doc from the file.
* "color", not "colour", in docs and code names.
* Answer design questions with a recommendation.  Wait for his "go" when
  the answer changes the plan.
* **Darrell runs from the checkout on `PYTHONPATH`, not installed.**
  Anything that depends on an entry point or installed metadata must
  also work that way (notes section 3).
* He is not an astronomer.  Say what a calibration term means when it
  matters to a decision.

## Things that are easy to get wrong

* **arcae returns FLAG as `uint8`.**  Cast it (`_arcae_util.as_bool`).
* **arcae arrays are (row, chan, par); casatools' are (par, chan, row).**
* **A parameter cell is receptor-major**: all of the first receptor's
  parameters, then the second's.
* **Solution times differ by SPW**, and a Tsys table is ragged: a time
  and antenna with no row has `SOURCE_ROW` -1, NaN and FLAG set.
* **`PolBasis` is `unknown`** in every table.  Receptor labels are
  always positional (`pol_0`, `pol_1`; `all` for T Jones).  What a
  receptor is goes in the `polarization_type` coordinate, which is
  there only when the caller or `PolBasis` says.  Do not relabel the
  axis R, L.
* **Zarr returns variables sorted by name.**  Do not rely on a tree's
  variable order.
* **CASA's delays are ns; the tree's are seconds**, float64, as the
  draft's code has them (`Param.scale`).  `RawCalTable` holds CASA's
  values; `build_tree` and `tree_to_raw` convert.  Show ns to people.
* **`frequency` always increases in the tree.**  A window CASA stores
  the other way is reversed on reading (`casa_channel_order =
  "reversed"` on its node) and reversed back by `tree_to_raw` and by the
  flag commit.  Anything new that maps tree channels to table channels
  must do the same.
* **`channel_width` is an attribute of `frequency`**, a quantity dict as
  in MSv4, not a coordinate: one positive number per window.
* **A K delay's phase is referenced to the table's own `CHAN_FREQ`**
  (the band's middle), which is why the tree has `channel_width`.
* **`applycal` refuses a cal table whose subtables are not valid MS
  subtables.**  `_write.py` uses arcae's `ms_descriptor` for them.
* **`xr.backends.list_engines()` loads every backend** (so arcae, via
  xarray-ms).  It is cached; `_register.py` clears the cache.
* **CASA's values are single precision.**  The tree's float parameters
  are float64, so compare with `float32 value .astype(float64) * scale`,
  or convert back and compare in float32.

## The schema: what follows xradio, and what waits

Notes section 7 has the detail.  The reader follows, in this order: the
code on xradio's branch `545-calset-schema` (PR 578); the discussion on
that PR where it has settled something; and xradio's Conventions page
(PR 690).  As of this slice:

* **Done**: delays in seconds and float parameters as float64;
  positional receptor labels with a `polarization_type(antenna_name,
  receptor_label)` coordinate; `frequency` increasing, with MSv4's
  attributes (`channel_width` a quantity); the draft's `schema_version`,
  `type_version`, `creator` and `creation_date`.
* **Waiting for the schema, or for George and Des**:
  1. a `parameter` axis on gains (G, B, T, Accor): proposed in the
     discussion, not in the schema's code;
  2. the gain curve as receptor by polynomial coefficient;
  3. units for the dispersive delay, and for an unknown type;
  4. how a calibration set is arranged (we use a node per window), and
     where flags and missing solutions go.

Each of these is an edit to `_schema.py` and `_build.py`, and each
changes the schema, so it needs Darrell's go.  `CalTableReader` hides
them from the application, so C1 need not wait.  The trees have not
been run through xradio's schema checker; that would be a good test to
add once PR 578's module is on a branch that imports.

## Next: C1, the application (to propose, then wait for go)

The plan's section 4 has the scope and section 6 the exit: gain,
bandpass and fringe tables from a real reduction, as a grid per antenna,
with an overlay mode, flagged solutions shown, the reference antenna in
titles, PNG pages, and remote kernels.

What it stands on:
* `cubevis/toolbox/gridplot`: `GridSource` (the protocol: `labels`,
  `axis_names`, `extent`, `render`, `probe`), `GridView`, `GridApp`,
  `PanelSpec`/`RenderedPanel`, `export_png`.
* `cubevis/toolbox/visplot/vis_grid_source.py`: visplot's source, the
  model for calplot's.
* `CalTableReader`: a cell is `samples.where(samples.antenna == i)`.

A first slice might be a `CalGridSource` over `CalTableReader`, shown
in `GridApp`, with no application around it yet: one quantity against
time or frequency, a cell per antenna, colored by receptor or SPW.  That
proves the path from table to pixels and exposes what the reader lacks.

Settle with Darrell before writing code:
1. **How a cell is drawn.**  visplot's scatter cells go through
   datashader (`visplot/data/_scatter_render.py`).  Cal tables are
   small (a cell is tens to thousands of points), so calplot may want
   plain markers and lines, and error bars, which that path does not
   do.  Read gridplot notes section 12 first.
2. **Default view per type** (plan 4.2): G is amp and phase against
   time; B against frequency; K is delay against time, or evaluated
   phase; Fringe is phase, delay, rate; Tsys against time, or frequency
   when it has channels.
3. **Iteration and overlay**: a cell per antenna by default, then per
   antenna and SPW; color by receptor, SPW or field.
4. **Flagged solutions**: shown in another color, or hidden.
5. **The task interface**: `calplot(caltable=..., ...)` in the style of
   visplot's wrapper, and what `plotfile=` writes.
6. **Quantities and `Axis`**: reuse visplot's `Axis` members
   (`GAIN_AMPLITUDE`, `GAIN_PHASE`, `DELAY`, `TSYS`, `SNR`) or keep
   calplot's own names.  The reader gives delays in seconds, as
   `Axis.DELAY` says; a plot should show ns.
7. **Detection** in `data_format.py`, beside MS detection (plan 9.6).
8. **Titles and legends** should name a receptor by
   `info().polarization` ("R", "L") when it is known, and by its
   position otherwise.
9. **Finding the bad antenna: sort, badges, outlines** (plan 9.4 items
   7 and 8; placed in G2 by 9.8, not built there, moved here
   2026-11-17 when `gridplot` closed).  Antenna-based tables are where
   they pay most:
   * *Sort cells by a statistic* ("worst first"): order the iteration
     values by phase RMS, flagged fraction, median SNR or a Z-Score
     instead of antenna number, so page 1 shows the problems.  One
     number per cell and a sort key.
   * *Cell badges*: that number in a corner of each cell.
   * *Outlier outlines*: a colored frame on cells far from the page's
     (or the grid's) median of it.
   Build them in the shared component (`toolbox/gridplot`: a statistic
   per cell from the `GridSource`, the sort applied to its values, the
   badge and outline drawn by `GridView`), so visplot's grid gets them
   too.  Settle with Darrell: which statistics per cal type, and whether
   sorting is a control in the Grid section -- for visplot that is a new
   control, and visplot's rule is that controls land before its GUI
   refactor (`hrs_visplot_plan.md` section 8).

Then C2 (plan 3.6): FlagDB's list, undo and preview edit the reader's
flags (`set_flags`), and `commit_flags` writes them.

## Open

* **The document has gone, or is going, to George and Des.**  Their
  answers to its eleven questions may change `_schema.py`: the tree's
  arrangement, the `parameter` axis, units, EPowerCurve's values, the
  dispersive delay's units.  Ask Darrell what came back.
* **GitHub's pages are closed to the sandbox.**  The discussion on a PR
  or issue can only be read if Darrell saves the page ("Webpage, single
  file", `.mhtml`) and uploads it; he says that is no trouble.  PR 578
  and PR 690 were read that way on 2026-11-16.  Ask for PR 578 again
  before changing the schema.  Unread: `casangi/RADPS-roadmap#4`,
  `JSKenyon/gain-skeletons` (mock-ups of the calibration layout), and
  xradio PR 693 (checker upgrades).
* **Not covered by any table**: an ALMA Tsys with channels (the ALMA
  Antennae Band 7 guide ships one), polarization tables, antpos and
  opacity, a real table whose reference antenna changes.
* **Fringe-fit evaluation**: phase, delay and rate together as phase
  over time and frequency.  `evaluate` does K only.
* **A committer for Zarr.**  `commit_flags` has one committer, for CASA
  tables.  A calset's waits for xradio's layout.
* **Locks**: `commit_flags` is untested against CASA holding the table
  open for writing.
* **Lazy reads** for a very large bandpass table.
* **Where the backend should live**: cubevis for now.  xradio, or a
  package of its own, is a move (`cubevis/xarray` imports nothing of
  cubevis's GUI).
* **xradio's draft** (PR 578, `545-calset-schema`, commit `f871ae3`):
  the code was unchanged from 2026-08-06 to 2026-11-15, but the
  discussion is active (meetings on 2026-06-30 and 2026-10-28, George's
  text of 2026-11-09).  Check the code at each stage with `git fetch
  https://github.com/casangi/xradio.git refs/pull/578/head`, and ask
  for the page.
* ~~Carried from the gridplot handoff: retake the G0 to G3 timings on
  TW Hya and the VLBA subset.~~  Dropped as a task (2026-11-17,
  `gridplot_notes.md` section 15 on `gridplot`): they come with use.
