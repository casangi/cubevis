# CASA calibration tables as xarray: example and notes

*2026-11-16, `casangi/cubevis` branch `calplot`.  Written for the people
working on CASA calibration and on xradio's calibration schema.  It is a
status report on work in progress, with an example to run, what we found
in CASA's tables, the choices we made, and some questions.*

## What this is

cubevis is getting a calibration-table plotter, `calplot`.  Its first
piece is a reader, and that reader may be useful on its own:

- **An xarray backend for CASA calibration tables**, engine
  `casa-caltab`.  It reads a table with [arcae](https://github.com/ratt-ru/arcae),
  so it needs no CASA.
- **The result is an `xarray.DataTree`** shaped after xradio's draft
  calibration schema (PR 578, branch `545-calset-schema`), where the
  draft says something.
- **It goes both ways.**  A tree can be saved to Zarr, and it can be
  written back as a CASA calibration table that `applycal` accepts.
- **Flags can be written back** to the table, with a backup and a
  read-back.

It is not released, and the application on top of it is not built yet.

## Try it

It needs Python 3.12 or later, and neither CASA nor any data.

```bash
pip install "cubevis[xarray] @ git+https://github.com/casangi/cubevis@calplot"
curl -O https://raw.githubusercontent.com/casangi/cubevis/calplot/devel/tools/calplot/caltab_example.py
python caltab_example.py
python caltab_example.py /path/to/your/cal.G      # the same, then a table of yours
```

The example makes a synthetic fringe-fit table and writes it as a CASA
table, then does what one would do with a real one.  Its output, trimmed:

```text
=== 1. the table as an xarray.DataTree ====================================
example.fringe: 'Fringe Jones' -> caltype 'Fringe', 2 spectral window(s): spw_0, spw_1
  dims       {'direction': 1, 'time': 8, 'antenna_name': 6, 'frequency': 1, 'receptor_label': 2}
  CALPARAM_PHASE         float32    (direction, time, antenna_name, frequency, receptor_label) [rad]
  CALPARAM_DELAY         float32    (...) [ns]
  CALPARAM_RATE          float32    (...) [dimensionless]
  CALPARAM_DISP_DELAY    float32    (...)
  CALERROR_<each>        float32    (...)
  SNR                    float32    (...)
  FLAG                   bool       (...)
  SOURCE_ROW             int64      (time, antenna_name)
  INTERVAL               float64    (time, antenna_name) [s]
  coords     direction, time, antenna_name, frequency, channel_width, receptor_label, field_name, scan_name
  attrs      {'type': 'antenna_calibration', 'caltype': 'Fringe', 'casa_subtype': 'Fringe Jones',
              'spectral_window_id': 0, 'spectral_window_name': 'spw0', 'reference_antenna': 'FD'}

=== 2. to Zarr and back ===================================================
wrote example.zarr; read back identical: True

=== 3. flag an antenna and write the flags to the table ===================
flagged 16 solutions in 8 rows; read back and verified: True
previous flags saved in example.fringe.caltab_flag_backup_20261116_151015.npz
after restore_flags:   MK in spw 1 flagged: 0 %

=== 5. a delay table as the phase it applies ==============================
A03 delay 3.0 ns, as phase across the band:
    1400.0 MHz  -0.6032 rad
    1432.0 MHz  +0.0000 rad
    1464.0 MHz  +0.6032 rad
```

In a session:

```python
import xarray as xr
import cubevis.xarray.caltab                          # only needed if cubevis is not pip-installed

dt = xr.open_datatree("tl016b.gff")                   # engine="casa-caltab" is found from the table
ds = dt["spw_0"].ds
ds.CALPARAM_DELAY.sel(antenna_name="KP")              # ns: see its "units"

dt.to_zarr("tl016b_gff.zarr")                         # the whole table, as Zarr

from cubevis.xarray.caltab import write_caltab, commit_flags
write_caltab(dt, "copy.gff")                          # a CASA table again, written with arcae

dt["spw_1"]["FLAG"].loc[dict(antenna_name="KP")] = True
commit_flags(dt)                                      # the table now says the same
```

## What a table becomes

One node per spectral window, `spw_<id>`.  Each node is a Dataset:

| | |
|---|---|
| dims | `direction` (length 1), `time`, `antenna_name`, `frequency`, `receptor_label` |
| variables | `CALPARAM_<name>`, `CALERROR_<name>`, `SNR`, `FLAG` over all five dims |
| | `SOURCE_ROW`, `INTERVAL` over `(time, antenna_name)` |
| coords | `field_name(time)`, `scan_name(time)`, `channel_width(frequency)` |
| attrs | `type`, `caltype`, `casa_subtype`, `spectral_window_id`, `spectral_window_name`, `reference_antenna` |

| CASA subType | `caltype` | Variables | Units |
|---|---|---|---|
| G Jones, B Jones, Accor Jones | G, B, Accor | `CALPARAM_GAIN` (complex) | dimensionless |
| T Jones | T | `CALPARAM_GAIN` (complex), one receptor | dimensionless |
| K Jones | K | `CALPARAM_DELAY` | ns |
| B TSYS | Tsys | `CALPARAM_TSYS` | K |
| Fringe Jones | Fringe | `CALPARAM_PHASE`, `_DELAY`, `_RATE`, `_DISP_DELAY` | rad, ns, dimensionless, not given |
| anything else | the subType | `CALPARAM` over a `parameter` dim | not given |

A type the reader does not know is still read: its parameters are kept
as they are, over a `parameter` dimension.  The gain curve
(`EPowerCurve`) is read that way today.

## What was checked

The tables: six solved by CASA 6.7.6 from a simulated VLA MS with known
corruptions (G, T, B, K, Fringe, Tsys), and nine from a real VLBA
observation (TL016B, calibrated as in the CASA VLBA guide with CASA
6.7.7: Accor, Tsys, gain curve, two fringe fits, B, K, and two G).

- **Against casatools, exactly.**  Every row's parameters, errors, flags,
  SNR, time, antenna, field, scan and interval, on all 15 tables.
- **Against known answers.**  G amplitudes within 0.005, delays within
  0.05 ns, fringe rates within 5e-15, on the simulated set.
- **To Zarr and back**: identical.
- **Written as a CASA table and read back**: identical, for all 15.
- **`applycal` accepts a written table.**  With G, B and K tables written
  from their trees, the corrected data and flags are exactly those the
  original tables give.
- **`applycal` honors a flag written here.**  With one antenna flagged in
  one window of a G table, `applycal` flags that antenna's baselines in
  that window and nothing else.
- **A delay as phase.**  The phase computed from a K table's delays
  matches what `applycal` applies, within 1e-4 rad.

The tests are in `tests/manual/calplot/`.  Most run with no CASA and no
data, on tables written there.

## What CASA's tables turned out to hold

These may be known to you; they were not obvious from outside.

1. **`PolBasis` is `unknown`** in every table, simulated and real.  A cal
   table has no FEED or POLARIZATION subtable, so nothing in it says
   whether its two receptors are R, L or X, Y.
2. **A fringe table has 8 values per row**: phase, delay, rate and
   dispersive delay for each of two polarizations.  The reference
   antenna's are exactly 0.  The dispersive delay is 0 throughout, with
   SNR 0.
3. **FLAG and SNR are per parameter, but vary only per polarization.**
   Within a polarization CASA writes one SNR for each solved parameter and
   flags the parameters together.  This held on every table.
4. **`PARAMERR` is 1.0 throughout the fringe tables.**
5. **`ANTENNA2` is the reference antenna** for G, T, B and Fringe, and -1
   for K, Accor, Tsys and the gain curve.
6. **The gain curve has `FIELD_ID` and `SCAN_NUMBER` -1.**  It has 16
   values per row; only the first is non-zero on TL016B.
7. **Solution times differ by spectral window.**  The TL016B fringe fit
   has 58 times in each of four windows and 217 different times in all.
8. **Tsys is ragged.**  Each antenna has its own times: 5,664 rows per
   window where 11,810 would fill a time by antenna grid.  The real Tsys
   also has unflagged values up to 13,800 K.
9. **A table with one channel per window carries the band in
   `CHAN_WIDTH`.**  For G, K and Fringe tables, `CHAN_FREQ` is the middle
   of the band the solution was made over and `CHAN_WIDTH` its full width.
   `TOTAL_BANDWIDTH` holds the sum of the channel frequencies, not a width.
10. **A K delay's phase is referenced to that `CHAN_FREQ`.**
    phase = 2π × delay × (frequency − `CHAN_FREQ`).  Zero frequency and
    the MS's first channel do not reproduce what `applycal` applies.
11. **`applycal` needs the subtables to be valid MeasurementSet
    subtables.**  A table whose OBSERVATION subtable had only a telescope
    name was refused ("not a valid MSObservation").
12. **`WEIGHT` exists but has no cells.**

## Choices we made

xradio's draft defines one dataset, `FringeJonesXds`.  Where it says
something we followed it.  The rest is ours, and all of it is in one
module (`cubevis/xarray/caltab/_schema.py`), so it is cheap to change.

From the draft:
- the five dimensions and their order;
- `CALPARAM_PHASE`, `_DELAY`, `_RATE`; `CALERROR_<name>`; `SNR`;
- `field_name(time)` and `scan_name(time)`;
- `type = "antenna_calibration"` and `reference_antenna`.

Ours, and why:

| Choice | Why |
|---|---|
| One node per spectral window | Windows differ in channel count and in solution times.  The draft does not say how a calibration set is arranged. |
| `CALPARAM_GAIN`, `_TSYS`, `_DISP_DELAY` | The draft has no dataset for G, B, K or Tsys. |
| `FLAG` in every dataset | The draft has a flag array type, but `FringeJonesXds` has no flags. |
| CASA's units as stored (delay in ns) | Exact comparison with CASA.  The draft has seconds.  Each variable says its units. |
| A missing solution is NaN, flagged, with `SOURCE_ROW` -1 | "No solution" and "flagged" need telling apart. |
| One SNR and one FLAG per receptor | See finding 3.  The reader warns if a table ever differs. |
| Receptor labels `pol_0`, `pol_1`; `all` for T Jones | See finding 1.  A caller who knows can give R, L or X, Y. |
| `direction` with one label, `direction_independent` | A CASA table is direction-independent; the draft has the dimension. |
| `channel_width(frequency)` | See finding 9.  Evaluating a delay across the band needs it. |
| `CALPARAM` over `parameter` for an unknown type | Better read than refused. |
| `SOURCE_ROW`, `INTERVAL` | Writing flags back to the right row; the solution interval. |

Each tree lists these in its `schema_deviations` attribute.

## Questions

For the schema:

1. How will G, B, K and Tsys be represented: a named variable per
   parameter, as the fringe fit is, or something generic?
2. How is a calibration set arranged as a tree: a node per spectral
   window, per table, per something else?
3. What does `direction` hold for a direction-independent solution?
4. Where do flags live, and how is "no solution" told from "flagged"?
5. Are delays always in seconds?  Is dispersive delay coming?

For CASA:

6. What are the 16 values of an `EPowerCurve` row?
7. What are the units of the fringe table's dispersive delay?
8. Is finding 10 the rule for every K table, or particular to
   `gaincal(gaintype="K")`?
9. Is finding 3 guaranteed, or can a polarization's parameters be
   flagged separately?
10. Which other types matter most to read properly?  We have not seen
    polarization tables (D-terms, X-Y phase), antpos, opacity, or an
    ALMA Tsys with channels.

## Suggestions

Things that would make a reader's, and a plotter's, life easier later.
None is urgent.

For the schema:

- **Say how a set is arranged before much is built on it.**  A node per
  window is an easy thing for us to change now and a hard one later.
- **Record what the receptors are.**  A calibration set that says R, L or
  X, Y avoids repeating `PolBasis = unknown`.
- **Give every dataset type its flags**, and one stated way to mark a
  solution that does not exist.
- **Keep units as attributes on each variable**, whichever units are
  chosen, so a reader never has to know them by type.
- **Store the frequency a delay-like parameter is referenced to**, and the
  band a single-channel solution stands for.  Finding 10 had to be found
  by experiment.
- **Keep a short, stable type name** (`caltype`) beside any longer
  description, and a defined fallback for types the schema does not list.
- **Keep the field and scan of each solution**, and its interval.  They
  are what a person selects by.

For CASA:

- **Fill in `PolBasis`** when the MS is at hand.
- **`TOTAL_BANDWIDTH`** in the tables' SPECTRAL_WINDOW subtable could hold
  the bandwidth.

## Where it might be useful beyond calplot

- **Legacy CASA tables in xradio or AstroVIPER.**  `casa-caltab` followed
  by `to_zarr` is a converter from a CASA calibration table to a
  calibration set, for whatever layout the schema settles on.  It is the
  cal-table counterpart of xarray-ms.
- **From a calibration set back to CASA.**  `write_caltab` makes a table
  `applycal` accepts, so solutions made outside CASA could be applied
  with it.
- **For CASA**, a reader and writer of calibration tables that needs only
  arcae.

It lives in cubevis for now, with no Bokeh or CASA imports, so moving it
into xradio or a package of its own would be a move and not a rewrite.
Where it should live is your call as much as ours.

## Limits

- **Types**: G, T, B, K, Fringe, Tsys and Accor are known.  Others are
  read in the generic form.
- **A written table's subtables hold names and frequencies only.**
  Antenna positions and field directions are zero, so a type that needs
  them when applied is unchecked.
- **A written table numbers antennas and fields in the tree's order**,
  since a tree holds names and not CASA's ids.
- **Everything is read at once.**  Fine for these tables (the largest read
  takes 65 ms); a very large bandpass table would want lazy reads.
- **Locks**: not tested against CASA writing the same table.
- **Not covered by any table yet**: an ALMA Tsys with channels, a table
  whose reference antenna changes (synthetic only), baseline-based
  tables.

## Where things are

| | |
|---|---|
| The backend | `cubevis/xarray/caltab/` (`_schema.py` holds every schema choice) |
| The reader a plotter uses | `cubevis/toolbox/calplot/reader.py` |
| The example | `devel/tools/calplot/caltab_example.py` |
| The simulated tables | `devel/tools/calplot/make_caltables.py` (needs CASA) |
| The tests | `tests/manual/calplot/` |
| The record | `devel/notes/drs/calplot/calplot_notes.md` |
| The plan | `devel/docs/gridplot/grid_and_calplot_plan.md`, sections 10 and 19 to 22 |

Contact: Darrell Schiebel.
