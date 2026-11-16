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
