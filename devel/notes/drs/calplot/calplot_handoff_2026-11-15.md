# Handoff: calplot C0, the `casa-caltab` reader (2026-11-15)

This is the start of **calplot** (plan stages C0–C4).  The grid work it
builds on (G0–G3) is done.  That work's handoff and record are
`devel/notes/drs/gridplot/gridplot_handoff_2026-11-13.md` and
`gridplot_notes.md`.  Section 14 of those notes holds everything learned
about cal tables so far; read it first.

The plan is `devel/docs/gridplot/grid_and_calplot_plan.md`, mirrored to
the claude.ai project doc `claude/grid_and_calplot_plan.md`.  For calplot,
read:
- section 4 (calplot's scope);
- section 9.5 (first cal types);
- section 9.6 (reader independence: xradio/RADPS is the stakeholder);
- section 10 (an arcae-based xarray backend), with 10.5 marked with what
  is now known;
- the stage table in section 6.

C0's record starts a new file in this folder, `calplot_notes.md`, at
section 1.

## Where things stand

* **Branch `gridplot` at `baa0b753`**, all committed.  Ask Darrell
  whether calplot carries on on `gridplot` or starts a branch of its own
  (e.g. `calplot` from `gridplot`), before the first zip.
* **No calplot code yet.**  `cubevis/xarray/` and `cubevis/toolbox/calplot/`
  do not exist.  The reader's tools are:
  * `devel/tools/calplot/make_caltables.py OUTDIR` simulates a 10-antenna
    VLA MS (2 SPWs × 32 channels, 4 pols, 4 scans).  It corrupts the MS
    with known per-antenna terms, solves K, B, G, Fringe and Tsys with
    CASA, and writes `truth.npz` and `layout.txt`.  It takes about 2
    minutes.
  * `devel/tools/calplot/tl016b_caltables.py INPUT OUTDIR` is the CASA
    VLBA guide's calibration of TL016B, up to imaging.  Darrell ran it on
    `tl016b_sub.ms`.
* **Plain pip works for both apps**: `pip install cubevis[iclean,visplot]`
  (README fixed 2026-11-15).  casatools and arcae share a process
  without trouble (notes section 14).

## Data the new session needs

Nothing from this session's `/tmp` survives.  Ask Darrell to upload:
* **`caltables.tgz`** (0.86 MiB) from his `tl016b_cal` output folder:
  the nine real TL016B tables plus `layout.txt`.  Unpack it into a new,
  empty directory, as with any upload.  The MS copies are not needed for
  C0; his full archive was 1.8 GB.

Regenerate the simulated set here (it needs casatools; see the next
section):

```bash
python devel/tools/calplot/make_caltables.py /tmp/caltables
```

For the tests, point `CALTABLES` at a folder holding both sets, e.g.
`$CALTABLES/sim/` and `$CALTABLES/tl016b/`.  Tests skip without it, as
the visplot tests do without `VLBA`.  The tables are not committed.

## Environment (cloud sandbox)

The C0 code needs only arcae and xarray; casatools is only for making
tables and for the parity test.

```bash
python3 -m venv /tmp/ivenv
/tmp/ivenv/bin/pip install -U pip
cd <repo> && /tmp/ivenv/bin/pip install ".[iclean,visplot]" casadata   # casadata: 350 MB of measures
rm -rf .pdm-build                                  # left by the local install; not for the repo
mkdir -p ~/.casa
D=$(/tmp/ivenv/bin/python -c "import casadata, os; print(os.path.dirname(casadata.__file__))")/__data__
printf 'measurespath = "%s"\nmeasures_auto_update = False\ndata_auto_update = False\n' "$D" > ~/.casa/config.py
```

* casatools will not import without measures data.  The sandbox cannot
  reach NRAO's download, and the PyPI `casadata` wheel covers that.  Its
  leap-second table is out of date (a warning, harmless).
* `casatasks` 6.7.6.14 needed `six`.  CASA has since dropped that
  dependency, so it should fix itself; until then, `pip install six`.
* The repo's usual dev environment (what the gridplot tests ran in) has
  arcae but not casatools.

## Working conventions (Darrell)

As in the gridplot handoff:
* Deliver a zip of complete files, unzipped at the repo root and named
  with the base commit, e.g. `calplot-c0a_baa0b753.zip`.  List any files
  to delete separately.
* Never commit or push.  `git fetch origin <branch>` before each slice,
  then `git reset --mixed origin/<branch>` once Darrell says GitHub is up
  to date.
* One notes section per slice in `calplot_notes.md`.  When the plan
  changes, update it, then `project_read` and `project_write` the
  project doc (with `local_path`).
* "color", not "colour", in docs and code names.
* Answer design questions with a recommendation and wait for his "go"
  when the answer changes the plan.

## What the tables showed (details: gridplot notes section 14)

* **Type**: in `table.info`, `type = Calibration`, with `subType` one of:
  G Jones, B Jones, K Jones, Fringe Jones, B TSYS, Accor Jones or
  EPowerCurve.  The `VisCal` keyword says the same, and `ParType` is
  Complex or Float.  Other keywords: `MSName`, `PolBasis`,
  `CASA_Version`, and the subtables ANTENNA, FIELD, SPECTRAL_WINDOW,
  OBSERVATION and HISTORY.
* **Columns**: TIME, FIELD_ID, SPECTRAL_WINDOW_ID, ANTENNA1, ANTENNA2
  (the reference antenna), INTERVAL, SCAN_NUMBER, OBSERVATION_ID, then
  CPARAM or FPARAM, PARAMERR, FLAG and SNR, each (npar, nchan) per row.
  arcae returns these as (row, chan, par).  WEIGHT exists but has no
  cells.
* **Per type**:
  * G and Accor: (2, 1) complex.
  * B: (2, nchan) complex.
  * K: (2, 1) float, in ns.
  * Tsys: (2, 1) float, in K.  An ALMA TSYS_SPECTRUM would give
    channels; none of the tables has one.
  * Fringe: (8, 1), that is phase (rad), delay (ns), rate (s/s) and
    dispersive delay for each of two pols, confirmed on real data.  The
    reference antenna is exactly 0.
  * EPowerCurve: (16, 1), two pols × 8.  Only the first value is non-zero
    on TL016B; the meaning of each is still to be checked in CASA's
    `EPowerCurve` code.
* **arcae returns FLAG as `uint8`**, not bool, so `~flag` gives 255s.
  Cast it.
* **Real data has outliers**: Tsys up to 13,800 K unflagged (the typical
  range is 34–60 K).  4–8 % of solutions are flagged.
* **Known answers** in the simulated set (`truth.npz`): G amplitudes
  within 0.002 of the truth; K delays within 0.5 ns; fringe delays equal
  to K's within 0.01 ns; fringe rates within the noise.

## Next: C0, first slice (to propose, then wait for go)

The plan (section 10.2) has `xr.open_datatree("cal.G",
engine="casa-caltab")`:
* one node per SPW;
* dims `(time, antenna_name, frequency, receptor_label)`, from xradio's
  draft schema;
* variables `CALPARAM_<name>`, `CALERROR_<name>`, `SNR` and `FLAG`;
* coords `field_name(time)` and `scan_name(time)`;
* attrs `type`, `caltype` and `reference_antenna`;
* each value's source row kept as a hidden variable, for flag writes.

It lives in `cubevis/xarray/caltab/`, with shared helpers in
`cubevis/xarray/_arcae_util.py`.  Nothing there imports Bokeh or CASA at
import (the `cubevis.imaging` rule).  It is registered under
`[project.entry-points."xarray.backends"]`.

Settle with Darrell before writing code:
1. **Check xradio's draft first.**  PR #578, branch `545-calset-schema`,
   was last seen on 2026-08-06.  If its dims or names have changed, follow
   them.
2. **Time per SPW.**  Solution times differ by SPW and antenna.  For
   example, the TL016B gff has 217 times across 4 SPWs, and the Tsys has
   1181.  Proposal: each SPW node has its own `time`, the union of its
   solution times.  A cell with no solution is NaN with FLAG set, so
   "no solution" and "flagged" need telling apart, perhaps with a
   `SOLUTION_PRESENT` boolean.
3. **Parameter names per type**: a small table mapping (subType, ParType)
   to names.
   * G, B and Accor: `CALPARAM_GAIN`, complex.
   * K: `CALPARAM_DELAY`.
   * Tsys: `CALPARAM_TSYS`.
   * Fringe: `_PHASE`, `_DELAY`, `_RATE` and `_DISP_DELAY`, as in
     xradio's `FringeJonesXds`.
   * EPowerCurve, or any unknown type: `CALPARAM` with a `parameter`
     index, rather than refusing it.
   * Units go in attrs: ns, s/s, K and rad.
4. **Receptors**: `receptor_label` from the `PolBasis` keyword and
   the npar / 2 split (R/L for VLBA, X/Y elsewhere).
5. **Eager by default**: every table here is under 1 MiB.  A lazy
   `BackendArray` can wait for a large B or Tsys table.

Tests for this slice:
* round trip against casatools reads (values, flags, times, antennas)
  on all 14 tables (5 simulated, 9 TL016B);
* the known answers in `truth.npz`;
* the FLAG cast;
* an unknown subType passed through.

Each test skips without `CALTABLES`, and the casatools parity test
skips without casatools.

Later C0 slices:
* the synthetic backend, in the same shape;
* `commit_flags` with backup and read-back, reusing
  `flag_commit.commit_msv2_arcae`'s steps;
* the thin `CalTableReader` (selection; K evaluated to phase).

## Open

* An ALMA Tsys table with channels.  The ALMA Antennae Band 7 CASA guide
  ships Tsys tables with its data; its size is unknown.
* VLA-style tables (antpos, fluxscale) from the 3C391 tutorial (3.1 GB),
  if wanted.  Its calls can be scripted like `tl016b_caltables.py`'s.
* arcae's single-cell slicing (arcae PR 233) is merged but not in 0.5.9,
  the latest on PyPI.  It is not needed for small tables.
* Issue #2 (xradio/arcae in `ImagePipe`, for iclean) is a separate
  candidate.  Its upstream xradio PR 606 was merged on 2026-10-16.  It
  overlaps the plan's later `casa-image` backend (section 10.4).
