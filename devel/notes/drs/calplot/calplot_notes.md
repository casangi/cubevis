# calplot: session notes

One section per slice.  The plan is
`devel/docs/gridplot/grid_and_calplot_plan.md` (stages C0–C4; section 10
for the reader).  The grid work calplot builds on is recorded in
`devel/notes/drs/gridplot/gridplot_notes.md`; its section 14 holds what
the cal tables showed before any code.

## 1. C0a: the `casa-caltab` reader (base `3ef11065`, 2026-11-15)

Branch `calplot`, started from `gridplot` at `3ef11065`.

**What it is.**  A CASA calibration table opens as an `xarray.DataTree`,
read with arcae alone:

```python
import xarray as xr
dt = xr.open_datatree("tl016b.gff", engine="casa-caltab")   # or with no engine=
ds = dt["spw_0"].ds
```

The same without the entry point:
`cubevis.xarray.caltab.open_caltab(path)`.

**Files.**
- `cubevis/xarray/__init__.py`: the package (docstring only).
- `cubevis/xarray/_arcae_util.py`: what later backends will share:
  `table.info`, keywords, subtables, the FLAG cast, reads by row.
- `cubevis/xarray/caltab/_schema.py`: every schema choice: names,
  dimensions, units, the per-type packing, the receptor labels.
- `cubevis/xarray/caltab/_reader.py`: `open_caltab`, `caltab_info`.
- `cubevis/xarray/caltab/_backend.py`: xarray's entry point.
- `pyproject.toml`: `[project.entry-points."xarray.backends"]`,
  `casa-caltab`.
- `tests/manual/calplot/test_caltab.py`, and a `caltab` suite in
  `tests/ci/suites.toml`.

Nothing in `cubevis/xarray` imports Bokeh or CASA.  arcae and xarray are
imported when a table is opened, not when the package is (a test checks
both).

**The shape of a node** (one per spectral window, `spw_<id>`):

| | |
|---|---|
| dims | `direction` (length 1), `time`, `antenna_name`, `frequency`, `receptor_label` |
| variables | `CALPARAM_<name>`, `CALERROR_<name>`, `SNR`, `FLAG` over all five dims |
| | `SOURCE_ROW`, `INTERVAL` over `(time, antenna_name)` |
| coords | `field_name(time)`, `scan_name(time)` |
| attrs | `type`, `caltype`, `casa_subtype`, `spectral_window_id`, `spectral_window_name`, `reference_antenna` |

The root holds the table's own attributes: `source`, `ms_name`,
`pol_basis`, `casa_version`, `par_type`, `schema_draft`,
`schema_deviations`.

| CASA subType | `caltype` | Variables | Units |
|---|---|---|---|
| G Jones, B Jones, Accor Jones | G, B, Accor | `CALPARAM_GAIN` (complex) | dimensionless |
| K Jones | K | `CALPARAM_DELAY` | ns |
| B TSYS | Tsys | `CALPARAM_TSYS` | K |
| Fringe Jones | Fringe | `CALPARAM_PHASE`, `_DELAY`, `_RATE`, `_DISP_DELAY` | rad, ns, dimensionless (s/s), none given |
| anything else | the subType | `CALPARAM` over a `parameter` dim | none given |

**Decisions** (Darrell's go, 2026-11-15):
1. The draft's dims are followed, `direction` included.  Its one label
   is `direction_independent`.
2. CASA's units are kept as stored.  Delay is in ns, where the draft has
   seconds; the variable's `units` says which.
3. Each SPW node has its own `time`: the union of that window's solution
   times.  `antenna_name` is every antenna with a solution anywhere in
   the table, so the nodes of one table line up.
4. A time and antenna with no row is NaN, with `FLAG` set and
   `SOURCE_ROW` -1.  `SOURCE_ROW < 0` means "no solution"; a set `FLAG`
   with a row means "flagged".  No separate variable is needed.
5. Receptor labels: X, Y for a linear `PolBasis`, R, L for circular,
   and `pol_0`, `pol_1` otherwise.  `receptor_labels=` overrides.
6. `reference_antenna` is an attribute when the table has one.  It is
   left off when `ANTENNA2` is -1.  A table with several gets a
   `REFERENCE_ANTENNA(time, antenna_name)` variable instead (not seen
   yet, so not tested on real data).
7. `time` is Unix seconds with xradio's time attributes.  The shift from
   CASA's MJD seconds is a whole number, so it is exact.
8. Everything is read at once.

**What is the draft's and what is ours.**  xradio's draft (PR 578,
`545-calset-schema`, commit `f871ae3`) is one file, unchanged since
2026-08-06 and not on main.  Issue 545 is one paragraph asking for a
schema.  The draft defines only `FringeJonesXds`.
- From the draft: the five dims and their order;
  `CALPARAM_PHASE`/`_DELAY`/`_RATE`; `CALERROR_<name>`; `SNR`;
  `field_name`, `scan_name`; `type = "antenna_calibration"`;
  `reference_antenna`.
- Ours: a node per SPW; `CALPARAM_GAIN`, `_TSYS` and `_DISP_DELAY`;
  `FLAG` in every dataset; ns; the generic `CALPARAM`; `SOURCE_ROW`
  and `INTERVAL`.
- The draft has already changed form once: May's version had a generic
  parameter axis with a units array, June's has a variable per
  parameter.
- All of this is in `_schema.py`, and the tree carries it as
  `schema_draft` and `schema_deviations`.  A changed draft is an edit to
  that file.

Questions for the draft's author (Des Small), when there is a chance:
how G, B, K and Tsys will be represented; how a calibration set is
arranged as a tree; what `direction` holds for a direction-independent
table; where flags go and how "no solution" is marked; whether delays
are always in seconds, and whether dispersive delay is coming.

**Found on the way.**
- **`PolBasis` is `unknown` in all 14 tables**, simulated and real
  (CASA 6.7.6 and 6.7.7).  It cannot name the receptors.  A cal table
  has no FEED or POLARIZATION subtable either, so the MS is the only
  place that knows.
- **One SNR and one FLAG per receptor.**  A fringe table has 8 of each
  per row.  Within a receptor CASA writes the same SNR for each
  parameter it solved and 0 for the one it did not (dispersive delay),
  and flags the four together.  The reader keeps the first parameter's
  SNR and the "or" of the flags.  The comparison test checks that
  nothing is lost by this on every table, and the reader warns if a
  table's SNRs ever differ.
- **`PARAMERR` is 1.0 throughout the fringe tables**: CASA does not
  fill it.
- **The gain curve has no field.**  `FIELD_ID` and `SCAN_NUMBER` are -1
  in `tl016b.gc`.  `field_name` is then empty, and `scan_name` is "-1".
- **Tsys is ragged, the rest are not.**  TL016B's Tsys has 1181 times
  per SPW and 5664 rows, where 11810 would fill it: each antenna has its
  own times.  Every other table has a row for every time and antenna of
  a window.  The gff's four windows have 58 times each, 217 different
  times in all.
- **All of the Tsys flags are in SPWs 2 and 3** (7.2 % and 9.7 %; none
  in 0 and 1).
- **Channel counts may differ by SPW.**  The parameter column's cells
  then differ in shape by row, and a whole-column read cannot return
  them.  The reader reads each window's rows on their own
  (`getcol(column, index=(rows,))`, which arcae 0.5.9 supports).
  Tested on a copy of `sim.B` with one window cut to 16 channels.
- **xarray's `open_datatree` guesses an engine only if the backend says
  `supports_groups = True`.**  Without it a cal table went to xarray-ms.
- **A correction to gridplot notes section 14**: "K delays are within
  0.5 ns" of `truth.npz` does not hold; the difference reaches 1.4 ns.
  It is not error.  The simulated bandpass has a phase slope of
  0.3 rad × antenna / nant across the band, which is a delay of
  0.154 ns × antenna number.  With that added to the truth, K is within
  0.017 ns and the fringe delays within 0.009 ns.  The fringe rates
  equal the applied rate plus the phase drift, over the frequency, to
  1.5e-15 (the rates reach 5e-14).

**Tests** (`tests/manual/calplot/test_caltab.py`): 45 pass with
`CALTABLES` and casatools; 6 pass and 13 skip without `CALTABLES`.
- Without tables: the schema table, the receptor labels, the FLAG cast,
  what is not a table, the entry point, and that importing brings no
  Bokeh, CASA, arcae or xarray.
- On all 14 tables: the shape, and every row accounted for once.
- On all 14 tables, against casatools: every row's parameters, errors,
  flags, SNR, time, antenna, field, scan, interval and reference
  antenna, exactly; and each window's frequencies.
- The known answers in `truth.npz`: G amplitudes within 0.005, delays
  within 0.05 ns, rates within 5e-15, the reference antenna exactly 0.
- The real set: each table's type and variables; the gain curve kept in
  the generic form; times per SPW; the ragged Tsys.
- `spw=`, `receptor_labels=`, `drop_variables=`, `open_dataset` with
  `group=`, and the engine guessed.
- An unknown subType read in the generic form, with the same values.
- Channel counts that differ by SPW.
- To Zarr and back, identical.

`CALTABLES` is a folder with `sim/` (from `make_caltables.py`) and
`tl016b/` (from `caltables.tgz`).  The CI suite `caltab` runs the tests
that need no tables.

Reading is quick: 12–65 ms a table once arcae is loaded (65 ms is the
22656-row Tsys).

**Not done here.**
- Not covered by any table yet: an ALMA Tsys with channels, a table
  with one receptor (`T Jones`), a table whose reference antenna
  changes, and VLA-style tables (antpos, fluxscale).  `T Jones` is not
  in the schema table, so it reads in the generic form for now.
- The meaning of EPowerCurve's 16 values, and the unit of the
  dispersive delay: both need CASA's code.
- Lazy reads, and lock behavior while CASA rewrites a table (plan
  10.5).

**Next C0 slices**, as in the handoff: the synthetic backend in the same
shape; `commit_flags` with backup and read-back (`SOURCE_ROW` is what it
maps cells to rows with; one `FLAG` cell of a known type is all of that
receptor's parameters in the row); the thin `CalTableReader`.

## 2. C0b: the synthetic source and the table writer (base `e5e11d15`, 2026-11-16)

**What it is.**  A tree without a table, and a table from a tree:

```python
from cubevis.xarray.caltab import synthetic_caltab, synthetic_value, write_caltab
dt = synthetic_caltab("Fringe", nant=27)          # the shape open_caltab gives a CASA table
path = write_caltab(dt, "fringe.cal")             # a CASA cal table, written with arcae alone
```

**Files.**
- `cubevis/xarray/caltab/_build.py` (new): `RawCalTable`, a cal table
  as rows; `build_tree`, rows to tree; `tree_to_raw`, tree to rows.
- `cubevis/xarray/caltab/_reader.py`: now only reads the rows
  (`read_raw`).  The tree is `build_tree`'s.
- `cubevis/xarray/caltab/_synthetic.py` (new): `synthetic_caltab`,
  `synthetic_raw`, `synthetic_value`.
- `cubevis/xarray/caltab/_write.py` (new): `write_caltab`.
- `cubevis/xarray/caltab/_schema.py`: `T Jones`; the receptor label
  `all`; `pol_basis_of`.
- `devel/tools/calplot/make_caltables.py`: also solves `sim.T`.
- `tests/manual/calplot/test_caltab_synthetic.py` (new); additions to
  `test_caltab.py`; the `caltab` CI suite runs both files.

**One builder.**  Every source fills in a `RawCalTable` (CASA's layout:
a row per time, antenna and window, with a (channel, parameter) cell)
and `build_tree` makes the tree.  The reader's and the synthetic
source's trees are the same shape because the same code makes them.
An xradio calset source, when there is one, opens Zarr and needs
neither.

**The synthetic source.**  `synthetic_caltab(caltype, ...)`:
- `caltype`: G, T, B, K, Tsys, Fringe or Accor.  Any other name is a
  type the schema does not know, in the generic form (`npar`).
- Sizes: `nant`, `nspw`, `ntime`, `nchan` (one number, or one per
  window), `nreceptor`.
- Names: `antenna_names`, `receptor_labels`, `field_names`; `nscan`.
- `reference_antenna`: a number or name, None, or `"changing"` (the
  first antenna for the first half of the times, the second after).
  K, Tsys and Accor have none, as CASA's.
- `flag_fraction`; `missing_fraction` (antennas with times of their
  own); `noise`; `outliers` (unflagged values 300 times too large, as
  the real Tsys has); `own_times`; `start`, `interval`; `seed`.
- Without noise, every value is `synthetic_value(caltype, name, antenna,
  receptor, tau, x)`: a rule stated in its docstring.  Phases, fringe
  delays and rates are relative to the reference antenna, so its values
  are exactly 0, with noise or without.

It covers what no table here does: a Tsys with channels, one receptor,
a reference antenna that changes, 66 antennas.

**The writer.**  `write_caltab(tree, path)` makes a CASA cal table with
`arcae.Table.from_descriptor`: CASA's main columns, the keywords,
`table.info`, and the five subtables.
- A written table reads back as the same tree.  Checked for 11
  synthetic cases and for all 15 real tables.
- casatools reads it as CASA's own: type, keywords, every cell.
- **`applycal` accepts it**, and corrects the data exactly as it does
  with the table the tree was read from.  Checked for `sim.G`, `sim.B`
  and `sim.K` on `sim.ms`.  So tree → CASA table → applied works for
  those types.
- For that the subtables must be valid MeasurementSet subtables.  With
  only the columns a tree knows, `applycal` refused the table
  ("not a valid MSObservation").  `arcae`'s `ms_descriptor(name)` gives
  each subtable's required columns.
- The subtables hold names and frequencies only.  Antenna positions and
  field directions are zero.  A type that needs them when applied (a
  gain curve, anything with parallactic angle) is not checked.
- The tree holds names, not CASA's ids.  Written from a tree, antennas
  and fields are numbered in the tree's order, and those with no
  solution are left out.  Written from a `RawCalTable`, ids are kept.
- A fringe table's unsolved parameter (dispersive delay) has SNR 0 in
  CASA's table.  Written from a tree it gets the receptor's SNR: the
  tree keeps one SNR per receptor.

**`T Jones`** (`gaincal(gaintype="T")`) is now a known type.  A real one
(`sim.T`) is (1, 1) complex per row: one gain for both polarizations.
Its receptor label is `all`, not `pol_0`, which would say it belongs to
one of them.  In `sim.T`, 45 % of the solutions are flagged.  That is
the simulation: each polarization has its own phase, so one gain cannot
fit both, and CASA flags the antennas where they differ most.

**Tests.**  98 pass with `CALTABLES` and casatools (48 in
`test_caltab.py`, 50 in `test_caltab_synthetic.py`).  With neither, 32
pass and the rest skip: the synthetic source, and tables written and
read back, now run in the quick tests, so the arcae reader is exercised
in CI.
- Each known type: shape, and every value the rule's.
- Times, scans and fields; the reference antenna (one, none, changing).
- The cases above that no table covers; an unknown type; seed and
  noise.
- Written and read back, identical: 11 cases, including windows of 32
  and 16 channels, missing rows, flags, R/L and X/Y labels.
- Written from rows: CASA's ids kept.  Overwrite: refused by default,
  and never removes a directory that is not a table.
- With casatools: every column and cell of a written table.
- With `CALTABLES`: each real table written from its tree and read
  back, identical.  `applycal` on written G, B and K.

`CALTABLES/sim` should be made again to get `sim.T` (2 minutes).  The
tests that want it skip without it.

**Schema assumptions added**: `T Jones` → `CALPARAM_GAIN`, one receptor
labelled `all` (ours; the draft has nothing for it).

**Next C0 slices**: `commit_flags` with backup and read-back, tested on
written tables in CI and on copies of the real ones; then the thin
`CalTableReader`.

## 3. C0c: the engine without the entry point (base `5016309a`, 2026-11-16)

**The problem.**  xarray finds `casa-caltab` by its entry point, and an
entry point exists only for an installed package.  Darrell runs from the
checkout on `PYTHONPATH`, not installed, so the engine was unknown until
he added it to xarray's table by hand.

**What changed.**  Importing the package is now enough:

```python
from cubevis.xarray.caltab import open_caltab     # "casa-caltab" is now an engine
dt = xr.open_datatree("tl016b.gff")
```

- `cubevis/xarray/_register.py` (new): `register(name, module, attr)`
  and `entry_point_installed(name)`.  Shared, for the later backends.
- `cubevis/xarray/caltab/__init__.py`: calls `register_backend()` on
  import.

**How it behaves.**
- **Installed** (`pip install .` or `-e .`): the entry point is there,
  nothing is registered, and xarray is not imported by the import.
- **Not installed**: xarray is imported, the class is added to
  `xarray.backends.plugins.BACKEND_ENTRYPOINTS`, and xarray's cached
  engine list is cleared, so it works when xarray was used first.  It
  took 20 ms here with xarray already loaded.
- It never raises.  Without xarray, or if xarray moves its table, the
  import still works and `open_caltab` is unaffected.

**Why at `cubevis.xarray.caltab` and not at `import cubevis`**
(Darrell's first thought, 2026-11-16):
- `import cubevis` takes 0.17 s and loads neither xarray nor numpy;
  xarray would add 0.42 s for everyone, iclean users included.
- Whoever imports `caltab` needs xarray anyway.
- It moves with `caltab` if that becomes a package of its own, where
  the entry point makes it a no-op.

**Risk.**  `BACKEND_ENTRYPOINTS` is not xarray's documented interface.
`test_registered_without_the_entry_point` is the test that fails if a
release moves it.  The entry point stays the real mechanism: a process
that has not imported the package (an xradio user with cubevis
installed) is served by it.

**The alternative** for a checkout: `pip install --no-deps -e .` once.
Python still imports from the checkout, and the entry point is
registered.  It needs repeating only when the entry points in
`pyproject.toml` change.

**Found on the way.**  `xr.backends.list_engines()` loads every
installed backend, so it imports arcae (through xarray-ms).  Importing
our package does not.

**Tests**: 102 pass with an editable install; 101 pass and 1 skips run
from the checkout, not installed (the skip is the entry-point test).
New: the engine known after import; registered without the entry
point, with cubevis first and with xarray used first; `register` never
raises.

**To close C0** (agreed 2026-11-16): a short document for the people
working on xradio's calibration schema, with a runnable example that
needs no CASA and no data.  It says what the tree looks like, which
names are the draft's and which are ours and why, what the draft leaves
open, and what was checked (casatools parity, `applycal`).  Written
when `commit_flags` and `CalTableReader` are done.

## 4. C0d: `commit_flags` (base `7f989ffb`, 2026-11-16)

**What it is.**  The tree says what the flags should be; `commit_flags`
makes the table say the same.

```python
from cubevis.xarray.caltab import open_caltab, commit_flags, restore_flags, list_flag_backups
dt = open_caltab("cal.G")
dt["spw_0"]["FLAG"].loc[dict(antenna_name="ea05")] = True
report = commit_flags(dt)                 # commit_flags(dt, dry_run=True) only reports
restore_flags(report["backup"])           # puts back what that commit replaced
```

**Files.**
- `cubevis/xarray/caltab/_flags.py` (new): `commit_flags`,
  `restore_flags`, `list_flag_backups`, `FlagCommitError`.
- `tests/manual/calplot/test_caltab_flags.py` (new); the `caltab` CI
  suite runs it.

**The steps**, as visplot's `flag_commit.commit_msv2_arcae`:
1. **Check** that every row the tree points at (`SOURCE_ROW`) is still
   that time, antenna and window.  If CASA has solved the table again,
   nothing is written and `FlagCommitError` says to read it again.
2. **Plan** from a fresh read of the table's flags.  A cell the tree
   does not change is never written.
3. **Back up** the previous flags of the rows to be written, in
   `<table>.caltab_flag_backup_<stamp>.npz` next to the table.
4. **Write** those rows.
5. **Read back** through a new handle and compare (`verified`).
A `HISTORY` row is added, best effort.  With nothing to change there is
no write, no backup and no history row.

**Choices.**
- **The tree is the request.**  There is no list of flag operations at
  this level.  C2's FlagDB (list, undo, preview) edits the tree's
  `FLAG`; this writes the result.
- **One flag per receptor.**  A row has a flag per parameter and the
  tree one per receptor.  A changed tree cell sets all of that
  receptor's parameters; an unchanged one leaves them as they are, even
  when they differ among themselves.
- **No solution, nothing to write.**  Unflagging a time and antenna
  that has no row is counted (`no_solution`) and skipped.  It stays
  flagged in every later read.
- **A backup holds only the rows its commit wrote.**  Restoring one
  undoes that commit and leaves a later commit's other rows alone.
  Restore refuses a table of another type or row count.
- **Reader independence.**  `commit_flags` picks a *committer* by the
  tree's `source_format` (`COMMITTERS`).  There is one, for CASA
  tables.  `SOURCE_ROW` is its business alone.  An xradio calset
  committer (a Zarr write, no rows) registers beside it, with the same
  five steps and the same report.

**The report**: `flagged`, `unflagged` (solutions of the tree),
`no_solution`, `written` (values in the table), `to_write`, `rows`,
`backup`, `verified`, `mismatched`, `history`, `timing`, `dry_run`.

**Checked.**
- Only the changed cells differ afterwards, and no other column is
  touched: on written tables with arcae, and on a copy of each of the 15
  real tables with casatools.
- Restore brings each real table's flags back exactly.
- **`applycal` honors a flag written here.**  With antenna 3 flagged in
  window 0 of `sim.G`, `applycal` flags every baseline of that antenna
  in that window, and nothing else.
- The tests fail when the row check is removed, when unchanged cells
  are rewritten, and when restore writes the wrong values (each tried).

**Timing**: a commit of 571 rows of the real Tsys takes 32 ms (plan 25,
backup 2, write and read-back 5).

**Tests** (`test_caltab_flags.py`): 25 with `CALTABLES` and casatools; 9
run with neither.  All three files: 127 pass.

**Not done.**
- Locks: what happens if CASA holds the table open for writing during a
  commit (plan 10.5).
- A committer for a tree saved to Zarr.  It waits for xradio's layout.

**Next**: the thin `CalTableReader` (selection, K evaluated to phase,
the flag hook), which is the only thing calplot should talk to.  Then
the document and example for xradio's schema authors, which closes C0.

## 5. C0e: `CalTableReader` (base `1d1dbbfd`, 2026-11-16)

**What it is.**  The one thing calplot asks about a calibration table.

```python
from cubevis.toolbox.calplot import CalTableReader
reader = CalTableReader.open("tl016b.gff")       # a CASA table, a Zarr store, or a tree
info = reader.info()                             # type, antennas, windows, quantities ...
s = reader.read("delay", antenna=["BR", "KP"], spw=[1])
s.time, s.y, s.flag, s.antenna                   # one entry per solution: numpy arrays
```

**Files.**
- `cubevis/toolbox/calplot/__init__.py`, `reader.py` (new): the first
  of the application's package.  No Bokeh, no CASA.
- `cubevis/xarray/caltab/`: each channel's width now goes through the
  reader, the tree (`channel_width`), the writer and the synthetic
  source.
- `tests/manual/calplot/test_calplot_reader.py` (new); in the `caltab`
  CI suite.

**The rule it exists for.**  calplot code gets `CalInfo` and
`CalSamples` only: names, numbers and flat numpy arrays.  It does not
get the tree.  How the tree is arranged (a node per window, a variable
per parameter) is then this class's business alone, and a change in
xradio's layout is a change here, not in the application.  C1 should be
written against this class and never import `cubevis.xarray.caltab`.

**The interface.**
- `open(source)`: a CASA cal table, a tree saved to Zarr, or a tree in
  hand.
- `info()` → `CalInfo`: `caltype`, `antennas`, `receptors`, `spws`
  (id, name, channels, frequencies, bandwidth, times), `fields`,
  `scans`, `time_range`, `reference_antenna` (a name, None, or
  `"varies"`), `quantities`, `evaluations`, `nsolutions`, `nflagged`,
  `can_write_flags`.
- `read(quantity, antenna=, spw=, field=, scan=, receptor=, time=,
  channel=, frequency=, include_flagged=)` → `CalSamples`.
  - Quantities: `amplitude`, `phase`, `real`, `imag` of a complex gain;
    a parameter by name (`delay`, `rate`, `tsys`, ...); `snr`; `value`
    for a type the schema does not know (its "receptor" numbers the
    parameter).
  - Selection is by the names a person uses, or by number.  A wrong
    name is an error that lists the right ones.
  - A time and antenna with no solution has no entry.
  - `samples.where(mask)` cuts a set of samples: a grid cell is
    `s.where(s.antenna == 3)`.
- `evaluate("phase", nfreq=, frequencies=, wrap=)`: a K table's delays
  as the phase they apply (below).
- `set_flags(flagged, <selection>)`, `flag_changes()`, `commit_flags()`,
  `restore_flags(backup)`, `flag_backups()`, `reload_flags()`.  Flags
  are set in memory by the same selection as `read`, and written with
  section 4's `commit_flags`.

**A K table as phase** (plan 9.4 item 1; plotms shows only the stored
delay).  phase = 2π × delay × (frequency − the table's frequency).
- CASA references the phase to the frequency the table gives the
  solution, which is the middle of the band it was solved over.  Found
  by applying `sim.K` alone with `applycal`: of zero, the MS's first
  channel and the table's frequency, only the last fits, and it fits
  to 0.0000 rad.  It is now a test.
- A K table has one channel per window, and its `CHAN_WIDTH` is the
  whole band (64 MHz for 32 channels of 2 MHz).  That is where the
  default frequencies come from, and why `channel_width` is now in the
  tree.  (Its `TOTAL_BANDWIDTH` is not usable: CASA leaves the
  frequency there.)
- Not done: a fringe fit's phase, delay and rate together as phase
  over time and frequency.

**Found on the way.**
- **Zarr returns a store's variables sorted by name.**  The quantities
  came back in another order from Zarr than from the table.  The reader
  now orders them by the type's own parameter order.  This is the kind
  of dependence on the tree the class is there to absorb, and the test
  that found it (the same answers from a tree, a table and Zarr) stays.

**Checked.**
- The same `info` and the same samples from a tree, the CASA table
  written from it, and the Zarr store written from it.
- On real tables, with casatools: every sample is CASA's value and flag
  for that time, antenna, window and receptor (gff delay and rate,
  Tsys, a K table).
- The evaluated K phase against what `applycal` applies: 12 baselines,
  windows and polarizations, within 1e-4 rad.
- The tests fail with the phase referenced to zero frequency, with
  no-solution cells returned, and with the antenna selection ignored
  (each tried).

**Timing**: all 45312 Tsys samples in 19 ms; a two-antenna selection of
the gff in 4 ms.

**Tests**: 16 in `test_calplot_reader.py` (11 without `CALTABLES`).  All
four files: 143 pass.

**Schema assumptions added**: `channel_width`, a coordinate along
`frequency` (ours; MSv4 keeps a width as an attribute of `frequency`).

**Left for C1.**
- Mapping quantities to visplot's `Axis` members (`GAIN_AMPLITUDE`,
  `DELAY`, ...).  Note `Axis.DELAY` is in seconds and CASA's is ns.
- Cal-table detection in `data_format.py` (plan 9.6).
- A CASA-style selection string ("ea01,ea05", "0~3"), if wanted.

**Next**: the document and example for xradio's schema authors, which
closes C0.

## 6. C0f: the document, the example and the handoff (base `d9a7690d`, 2026-11-16)

This closes C0.

**Files.**
- `devel/docs/calplot/casa_caltab_xarray.md` (new): the backend
  explained for the people working on CASA calibration and on xradio's
  calibration schema.  How to try it, what a table becomes, what was
  checked, twelve findings about CASA's tables, the choices made and
  why, ten questions (eleven after section 7), and suggestions.
- `devel/tools/calplot/caltab_example.py` (new): the tour that document
  tells them to run.  No CASA, no data: it makes a synthetic fringe
  table, writes it as a CASA table, opens it, sends it to Zarr and back,
  flags and restores, reads samples, and evaluates a delay as phase.
  Given a path, it also describes that table.
- `tests/manual/calplot/test_calplot_reader.py`: `test_the_example_runs`
  runs it, so the document's instructions cannot go stale unnoticed.
- `devel/notes/drs/calplot/calplot_handoff_2026-11-16.md` (new), also
  the project doc `claude/calplot_handoff_2026-11-16.md`.

**How it goes out** (Darrell, 2026-11-16).  A short message of
invitation to George (who leads CASA's calibration team) and Des (fringe
fitting in AstroVIPER, and the schema draft), pointing at the document
in the repository.  The document is the information; the message is not
kept here.  It is an update, with suggestions that would make things
easier later, and a note that the backend might serve CASA or
xradio/AstroVIPER for legacy cal tables.

**Checked for this slice.**  A fresh environment with only
`pip install "cubevis[xarray] @ git+https://github.com/casangi/cubevis@calplot"`
(no casatools) runs the example and has the engine registered.  It
needs Python 3.12 or later.

**A correction on the way**: in CASA's cal tables `TOTAL_BANDWIDTH`
holds the sum of the channel frequencies (4.5792e10 for 32 channels
around 1.431 GHz), not a width.  Section 5 said only that it was not
usable.

**Tests**: 144 pass with `CALTABLES` and casatools.

**Next**: C1, the application.  The handoff has a first slice to
propose and the questions to settle first.

## 7. C0h: PR 578's discussion and PR 690 read; the reader brought into line (base `460f502e`, 2026-11-16)

This slice replaces `calplot-c0g`, which was never applied.

**What happened.**  Sections 1 to 6 took xradio's draft from the branch's
code alone: the sandbox cannot open GitHub's pages or API, so the PR's
conversation had not been read.  Darrell saved the pages of PR 578 and
PR 690 and uploaded them.  PR 578 has 25 comments, the last substantive
one on 2026-11-09.  The reader was then brought into line with what is
settled, and the document for George and Des revised, before it was
sent.

**Corrections to what these notes said.**
- "The draft is one file, unchanged since 2026-08-06" is true of the
  **code**.  The **discussion** is active: calibration meetings on
  2026-06-30 and 2026-10-28, and a long text from George on 2026-11-09.
- The work is Des Small's (`dessmalljive`).  Simon Perkins
  (`sjperkins`) opened the PR on 2026-05-25 so it could be found and
  reviewed.  Reviewers: J. Kenyon, V. Morello, and George Moellenbrock
  (`gmoellen`, review requested 2026-06-30).  So both of the document's
  readers are already in this discussion.
- Section 1's first "question for the draft's author" (how G, B, K and
  Tsys will be represented) is largely answered there.
- **Section 1's decisions 2 and 5 are reversed** (below): delays are
  now in seconds, and the receptor axis is never relabelled R, L.

**What PR 578's discussion says** (dates are the comments').
- *Ontological and phenomenological* (Des 10-19, George 10-27 and
  11-09).  Ontological calibration is physical parameters (delay,
  rate, TEC, antenna position, gain curve, Tsys).  Phenomenological is
  gains in a form equivalent to Jones matrices, ready to apply.
- *Each ontological parameter is its own array* (Des 10-20), because
  the schema framework requires explicit units on every array.
- *Six axes* (George 11-09): Direction, Time, Antenna or Baseline,
  Frequency, Receptor or Correlation, and **parameter**.
  - Antenna-based gains: receptor and parameter together are the Jones
    matrix.  A general Jones has 2 receptors by 2 parameters; a
    diagonal gain has 1 parameter; a scalar gain also has 1 receptor,
    which is "non-specific".
  - Along `parameter` the units must be uniform.  Parameters with
    different units go in separate arrays.  Polynomial coefficients
    are the stated exception: they go along `parameter`.
- *`receptor_label` is positional* (meeting of 10-28; Des 10-19): not
  X, Y, R, L.  What each receptor is comes from
  `polarization_type(antenna_name, receptor_label)`, as in MSv4's
  antenna dataset, so that heterogeneous arrays work.
  - The thread writes the labels as `pos_0`, `pos_1`.  xradio's own
    converters (MSv2 and ASDM, main at 2026-11-15) write `pol_0`,
    `pol_1`, which is what the reader uses.
- *`direction`*: a singleton is acceptable (06-30).
- *The parameter axis has come and gone and come back*: present in May,
  removed in June, proposed again by George in October for full-Jones
  gains and polynomials.  The branch's code is the June state.

**What PR 690 says** (Jan-Willem, draft, 2026-11-15; code read at
`cb97eb4`).  Shared bases for xradio's schemas and a Conventions page;
no schema content changes.
- `time` and `frequency` coordinates must increase.
- Data variables are UPPER_SNAKE; coordinates and attributes
  lower_snake.
- `FrequencyCoordArrayBase`: `type`, `long_name`, `units`, `observer`,
  and `channel_width` as an attribute (a quantity), which is how MSv4
  already stores it.
- `ReceptorLabelArrayBase`, and `FlagArrayBase` with `flag_bits`.  Its
  open question 7 asks whether that flag base fits PR 578's
  calibration flags.
- PR 578's module uses `UnitsRadians`, `UnitsSeconds` and
  `UnitsDimensionless` without importing them; it will fail to import
  once PR 690 lands, until it imports them.

**What was changed to follow them.**
1. **Delays in seconds, float parameters float64.**  The draft's code
   has `CALPARAM_DELAY` as float64 in `s`.  CASA stores single-precision
   ns.  `Param` has a `scale` (1e-9 for a delay); `build_tree`
   multiplies, `tree_to_raw` divides and casts back.  ns → s → ns gives
   the same bits (tested).  Complex gains stay complex64, and an unknown
   type's values stay as stored.
2. **`polarization_type(antenna_name, receptor_label)`.**  The receptor
   axis is always `pol_0`, `pol_1` (`all` for T Jones).  The coordinate
   is there when `PolBasis` is linear or circular, or when the caller
   gives `polarization_type=["R", "L"]`; it is left out otherwise, so
   for every CASA 6.7 table.  The argument `receptor_labels=` is gone
   from `open_caltab`, the backend and `synthetic_caltab`.
3. **`frequency` increases.**  A window CASA stores in decreasing
   frequency is reversed on reading, and its node has
   `casa_channel_order = "reversed"`.  `tree_to_raw` and the flag commit
   reverse it back, so the table is written as it was (CHAN_WIDTH
   negative there).
4. **`frequency`'s attributes are MSv4's**: `type`, `long_name`,
   `units`, and `channel_width` as a quantity
   (`{"data": w, "dims": [], "attrs": {"units": "Hz", "type":
   "quantity"}}`).  It replaces section 5's `channel_width` coordinate.
   One number per window, positive; a window whose channels differ in
   width keeps the first's, with a warning.
5. **The draft's attributes**: `schema_version` (now 0.2.0),
   `type_version`, `creator` (with cubevis's version) on each node;
   `creation_date` on the root, from the table's modification time.
6. **`SNR` has units** (`dimensionless`).
7. **`CalTableReader`**: `info().polarization` (`("R", "L")`, or `()`
   when the data do not say); `read(receptor="R")` works when they do.
   Delays are in seconds (`units` says so); `evaluate` reads the units.
   The application should show ns.

**Still behind the discussion, on purpose.**
- No `parameter` axis on gains (G, B, T, Accor).  It is proposed, not
  in the schema's code, and the document asks whether a diagonal gain
  will always carry it.
- The gain curve is read as an unknown type.  It is probably receptor
  by polynomial coefficient (MSv4's `gain_curve_xds` has `poly_term`).
- No units on the dispersive delay or on an unknown type's values.
- The trees have not been run through xradio's schema checker.

Not found in either PR: how a calibration set is arranged as a tree,
where flags live and how a missing solution is marked.

**Files.**
- `cubevis/xarray/caltab/`: `_schema.py`, `_build.py`, `_reader.py`,
  `_backend.py`, `_synthetic.py`, `_flags.py`, `__init__.py`.
- `cubevis/toolbox/calplot/reader.py`.
- All four test files; `devel/tools/calplot/caltab_example.py`.
- `devel/docs/calplot/casa_caltab_xarray.md`: rewritten for the reader
  as it now is.  New section "Against the discussion on PR 578"; eleven
  questions (six for the schema, five for CASA).
- The handoff and the plan (section 24).

**Checked.**
- 151 tests pass with `CALTABLES` and casatools.
- The casatools comparison is still exact on all 15 real tables, now
  through the units: the tree's value is CASA's float32 times the
  scale, and back.
- `applycal` still gives identical corrected data from written G, B and
  K tables, and still honors a flag written here.
- New: a table stored in decreasing frequency (synthetic) gives the
  same tree as one stored increasing; is written back in its own order;
  and a flag set at a frequency lands on that frequency's channel.
- The tests fail when the flag commit ignores a reversed window, when
  the writer does not go back to CASA's units, and when a reversed
  window is not reversed back (each tried).

**For any later session**: GitHub's pages are closed to the sandbox.
To read a PR or issue, ask Darrell to save the page ("Webpage, single
file", `.mhtml`) and upload it; he says that is no trouble.
`git fetch <repo> refs/pull/<n>/head` gives the code.  Unread, and
possibly relevant: `casangi/RADPS-roadmap#4` (the calibration epic),
`JSKenyon/gain-skeletons` (mock-ups; its PRs 8 and 10), and xradio
PR 693 (checker upgrades).
