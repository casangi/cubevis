"""CASA calibration tables as xarray, without CASA: a short tour.

    pip install "cubevis[xarray] @ git+https://github.com/casangi/cubevis@calplot"
    python caltab_example.py                 # needs no CASA and no data
    python caltab_example.py /path/to/cal.G  # then also reads a table of yours

Without an argument it makes its own table: a synthetic fringe fit, written
as a CASA calibration table with arcae.  Everything after that is what one
would do with a real table.

    1. open the table as an xarray.DataTree           (engine "casa-caltab")
    2. save the tree to Zarr and read it back         (identical)
    3. flag an antenna, write the flags to the table  (backed up, read back)
    4. ask for solutions as flat arrays               (what a plotter wants)
    5. a delay (K) table as the phase it applies

The document that goes with this: devel/docs/calplot/casa_caltab_xarray.md
"""
from __future__ import annotations

import os
import sys
import tempfile

import numpy as np
import xarray as xr

from cubevis.toolbox.calplot import CalTableReader
from cubevis.xarray.caltab import (commit_flags, open_caltab, restore_flags, synthetic_caltab,
                                   write_caltab)


def heading(text: str) -> None:
    print(f"\n=== {text} " + "=" * max(0, 70 - len(text)))


def describe(path: str) -> None:
    """What a table looks like as a tree."""
    dt = xr.open_datatree(path, engine="casa-caltab")        # engine= is optional
    print(f"{os.path.basename(path)}: {dt.attrs['casa_subtype']!r} -> caltype {dt.attrs['caltype']!r}, "
          f"{len(dt.children)} spectral window(s): {', '.join(dt.children)}")
    ds = dt[next(iter(dt.children))].ds
    print(f"  dims       {dict(ds.sizes)}")
    for name, var in ds.data_vars.items():
        units = f" [{var.attrs['units']}]" if "units" in var.attrs else ""
        print(f"  {name:22s} {str(var.dtype):10s} {var.dims}{units}")
    print(f"  coords     {', '.join(ds.coords)}")
    print(f"  attrs      { {k: v for k, v in ds.attrs.items()} }")
    solved = (ds.SOURCE_ROW >= 0).values
    print(f"  solutions  {int(solved.sum())} of {solved.size} (time, antenna) cells have a row; "
          f"{100 * float(ds.FLAG.values[0][solved].mean()):.1f} % of those are flagged")


def main(argv: list[str]) -> int:
    work = tempfile.mkdtemp(prefix="caltab_example_")

    heading("0. a table to work on (synthetic, written with arcae)")
    tree = synthetic_caltab("Fringe", nant=6, nspw=2, ntime=8, flag_fraction=0.05,
                            antenna_names=["BR", "FD", "HN", "KP", "LA", "MK"], reference_antenna="FD")
    table = write_caltab(tree, os.path.join(work, "example.fringe"))
    print(f"wrote {table}")

    heading("1. the table as an xarray.DataTree")
    describe(table)
    dt = xr.open_datatree(table)                             # the engine is found from the table
    ds = dt["spw_0"].ds
    delay = ds.CALPARAM_DELAY.sel(antenna_name="KP", receptor_label="pol_0").squeeze()
    print(f"  KP delay, first times: {np.round(delay.values[:3], 3)} {delay.attrs['units']}"
          f"   (reference antenna {ds.attrs['reference_antenna']}: "
          f"{float(abs(ds.CALPARAM_DELAY.sel(antenna_name='FD')).max())} exactly)")

    heading("2. to Zarr and back")
    store = os.path.join(work, "example.zarr")
    dt.to_zarr(store)
    back = xr.open_datatree(store, engine="zarr")
    same = all(back[n].to_dataset().identical(dt[n].to_dataset()) for n in dt.children)
    print(f"wrote {store}; read back identical: {same}")

    heading("3. flag an antenna and write the flags to the table")
    dt["spw_1"]["FLAG"].loc[dict(antenna_name="MK")] = True  # edit the tree ...
    report = commit_flags(dt)                                # ... and make the table say the same
    print(f"flagged {report['flagged']} solutions in {report['rows']} rows; "
          f"read back and verified: {report['verified']}")
    print(f"previous flags saved in {os.path.basename(report['backup'])}")
    mk = open_caltab(table)["spw_1"].ds.FLAG.sel(antenna_name="MK")
    print(f"the table, read again: MK in spw 1 all flagged: {bool(mk.all())}")
    restore_flags(report["backup"])
    mk = open_caltab(table)["spw_1"].ds.FLAG.sel(antenna_name="MK")
    print(f"after restore_flags:   MK in spw 1 flagged: {100 * float(mk.mean()):.0f} %")

    heading("4. solutions as flat arrays (what a plotter asks for)")
    reader = CalTableReader.open(table)                      # also takes the Zarr store, or a tree
    info = reader.info()
    print(f"{info.caltype}: antennas {info.antennas}, receptors {info.receptors}, "
          f"spws {[w.id for w in info.spws]}")
    print(f"quantities: {', '.join(f'{q.name} [{q.units}]' if q.units else q.name for q in info.quantities)}")
    s = reader.read("delay", antenna=["KP", "MK"], spw=0, include_flagged=False)
    print(f"read('delay', antenna=['KP', 'MK'], spw=0): {len(s)} unflagged solutions")
    for a in sorted(set(s.antenna.tolist())):
        cell = s.where(s.antenna == a)                       # one antenna: a cell of a grid of plots
        print(f"  {s.antennas[a]}: {len(cell):3d} points, delay {cell.y.min():.2f} to {cell.y.max():.2f} {s.units}")

    heading("5. a delay table as the phase it applies")
    k = CalTableReader.open(synthetic_caltab("K", nant=4, nspw=1, ntime=1))
    e = k.evaluate("phase", antenna="A03", receptor="pol_0", nfreq=5)
    print(f"A03 delay {k.read('delay', antenna='A03', receptor='pol_0').y[0]:.1f} ns, as phase across the band:")
    for f, p in zip(e.frequency, e.y):
        print(f"  {f / 1e6:8.1f} MHz  {p:+.4f} rad")
    print("(2 pi x delay x (frequency - the table's frequency): what CASA's applycal applies)")

    for path in argv:
        heading(f"your table: {path}")
        describe(path)
        r = CalTableReader.open(path)
        i = r.info()
        print(f"  antennas   {', '.join(i.antennas)}")
        print(f"  fields     {', '.join(i.fields)}   scans: {len(i.scans)}   reference antenna: {i.reference_antenna}")
        print(f"  quantities {', '.join(q.name for q in i.quantities)}"
              + (f"   can evaluate: {', '.join(i.evaluations)}" if i.evaluations else ""))
    print(f"\n(the example's files are in {work})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
