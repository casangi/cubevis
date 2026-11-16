"""Writing a tree's flags back to its calibration table (calplot C0).

    pytest -q tests/manual/calplot/test_caltab_flags.py

``commit_flags(tree)`` makes the table's flags those of the tree: it
checks the table is the one the tree was read from, backs up the rows it
will write, writes them and reads them back.  Most of this file runs on
tables written here with arcae (no CASA, no data), so it runs in the
quick tests.

With ``CALTABLES`` (see ``test_caltab.py``) and casatools, a copy of
every real table is flagged, checked with CASA's own reads, and
restored; and ``applycal`` is checked to flag the data of a solution
flagged here.
"""
from __future__ import annotations

import glob
import os
import shutil
import warnings

import numpy as np
import pytest

warnings.filterwarnings("ignore")
pytest.importorskip("xarray")
pytest.importorskip("arcae")

import xarray as xr                                              # noqa: E402

from cubevis.xarray import _arcae_util as au                     # noqa: E402
from cubevis.xarray.caltab import (FlagCommitError, commit_flags, list_flag_backups,   # noqa: E402
                                   open_caltab, restore_flags, synthetic_caltab, write_caltab)
from cubevis.xarray.caltab import _schema as S                   # noqa: E402
from cubevis.xarray.caltab._flags import COMMITTERS              # noqa: E402
from cubevis.xarray.caltab._reader import read_raw               # noqa: E402
from cubevis.xarray.caltab._synthetic import synthetic_raw       # noqa: E402

CALTABLES = os.environ.get("CALTABLES")


def _real_tables():
    if not CALTABLES:
        return []
    return sorted(p for p in glob.glob(os.path.join(CALTABLES, "*", "*"))
                  if au.is_table(p) and au.table_info(p).get("type") == "Calibration")


REAL = _real_tables()


def _table(tmp_path, caltype="G", name="t.cal", **kw):
    """A table written here, and the tree read from it."""
    path = write_caltab(synthetic_caltab(caltype, **kw), str(tmp_path / name))
    return path, open_caltab(path)


def _flags(path):
    """Every row's FLAG cell, by row: {row: (chan, par) bool}."""
    raw = read_raw(path)
    return {int(r): c.flag[i] for c in raw.cells.values() for i, r in enumerate(c.rows)}


def _other_columns(path):
    raw = read_raw(path)
    out = dict(raw.main)
    for s, c in raw.cells.items():
        out.update({f"par{s}": c.par, f"err{s}": c.err, f"snr{s}": c.snr, f"freq{s}": c.chan_freq})
    return out


def _history_rows(path):
    t = au.open_subtable(path, "HISTORY")
    try:
        return t.nrow()
    finally:
        t.close()


def _backups(path):
    return sorted(glob.glob(path + ".caltab_flag_backup_*"))


# --- the commit ---------------------------------------------------------------------------

def test_nothing_to_write(tmp_path):
    path, dt = _table(tmp_path, flag_fraction=0.2)
    before, hist = _flags(path), _history_rows(path)
    report = commit_flags(dt)
    assert report["written"] == 0 and report["flagged"] == 0 and report["unflagged"] == 0
    assert report["backup"] is None and _backups(path) == [] and report["history"] is False
    assert _history_rows(path) == hist
    after = _flags(path)
    assert all((after[r] == before[r]).all() for r in before)


def test_flag_and_unflag(tmp_path):
    """Exactly the changed solutions are written; everything else is as it was."""
    path, dt = _table(tmp_path, nant=6, ntime=10, flag_fraction=0.3, seed=2)
    before, other, hist = _flags(path), _other_columns(path), _history_rows(path)
    flag = dt["spw_1"]["FLAG"]
    old = flag.values.copy()
    flag.loc[{S.ANTENNA: "A03"}] = True                          # an antenna
    flag.loc[{S.ANTENNA: "A01", S.TIME: flag[S.TIME][2:6]}] = False      # a time range of another
    flag.loc[{S.ANTENNA: "A05", S.RECEPTOR: "pol_1"}] = True     # one receptor of a third
    new = dt["spw_1"]["FLAG"].values
    want_flagged, want_unflagged = int((new & ~old).sum()), int((~new & old).sum())
    assert want_flagged and want_unflagged

    dry = commit_flags(dt, dry_run=True)
    assert dry["dry_run"] and dry["written"] == 0 and dry["backup"] is None
    assert (dry["flagged"], dry["unflagged"]) == (want_flagged, want_unflagged)
    assert _backups(path) == [] and all((_flags(path)[r] == before[r]).all() for r in before)

    report = commit_flags(dt)
    assert (report["flagged"], report["unflagged"]) == (want_flagged, want_unflagged)
    assert report["written"] == want_flagged + want_unflagged == dry["to_write"]
    assert report["verified"] is True and report["mismatched"] == 0 and report["no_solution"] == 0
    assert report["history"] is True and _history_rows(path) == hist + 1
    assert _backups(path) == [report["backup"]]

    back = open_caltab(path)                                     # the table now says what the tree says
    for name in dt.children:
        np.testing.assert_array_equal(back[name].ds[S.FLAG].values, dt[name].ds[S.FLAG].values)
    after = _flags(path)
    changed = sum(int((after[r] != before[r]).sum()) for r in before)
    assert changed == report["written"]
    assert report["rows"] == sum(bool((after[r] != before[r]).any()) for r in before)
    now = _other_columns(path)                                   # nothing but FLAG was touched
    for key in other:
        np.testing.assert_array_equal(now[key], other[key], err_msg=key)
    assert commit_flags(open_caltab(path))["written"] == 0       # and a second commit finds nothing


def test_backup_and_restore(tmp_path):
    path, dt = _table(tmp_path, "B", nchan=[8, 4], flag_fraction=0.2)
    original, as_read = _flags(path), open_caltab(path)
    dt["spw_0"]["FLAG"].loc[{S.ANTENNA: "A02"}] = True
    first = commit_flags(dt)
    dt["spw_1"]["FLAG"][:] = False                               # a second commit, of other rows
    second = commit_flags(dt)
    assert first["backup"] != second["backup"]                   # even within one second
    listed = list_flag_backups(path)
    assert [b["path"] for b in listed] == [second["backup"], first["backup"]]      # newest first
    assert listed[1]["rows"] == first["rows"] and listed[1]["flagged"] == first["flagged"]
    assert listed[0]["unflagged"] == second["unflagged"]

    # the second backup holds what its rows were; restoring it undoes only that commit
    r = restore_flags(second["backup"])
    assert r["verified"] and r["restored_rows"] == second["rows"]
    now = open_caltab(path)
    np.testing.assert_array_equal(now["spw_1"].ds[S.FLAG].values, as_read["spw_1"].ds[S.FLAG].values)
    assert as_read["spw_1"].ds[S.FLAG].any()                     # (there was something to put back)
    assert now["spw_0"].ds[S.FLAG].sel({S.ANTENNA: "A02"}).all()     # the first commit stays
    restore_flags(first["backup"])
    after = _flags(path)
    assert all((after[k] == original[k]).all() for k in original)    # and now it is as it began

    quiet = commit_flags(_flagged(open_caltab(path)), backup=False)
    assert quiet["backup"] is None and quiet["written"] and len(_backups(path)) == 2
    elsewhere = str(tmp_path / "mine.npz")
    assert commit_flags(_flagged(open_caltab(path), "A04"), backup_path=elsewhere)["backup"] == elsewhere
    assert os.path.exists(elsewhere) and len(list_flag_backups(path)) == 2


def _flagged(dt, antenna="A00"):
    dt["spw_0"]["FLAG"].loc[{S.ANTENNA: antenna}] = True
    return dt


def test_one_flag_per_receptor(tmp_path):
    """A fringe row has four parameters per receptor and the tree one flag:
    a changed flag sets all four; an unchanged one leaves them as they
    are, even when they differ."""
    raw = synthetic_raw("Fringe", nant=3, ntime=4, nspw=1)
    cell = raw.cells[0].flag                                     # (row, chan, 8): pol 0's four, pol 1's
    cell[0, 0, 0] = True                                         # row 0, pol 0: PHASE only
    cell[1, 0, 5] = True                                         # row 1, pol 1: DELAY only
    cell[2, 0, 4:] = True                                        # row 2, pol 1: all four
    path = write_caltab(raw, str(tmp_path / "t.cal"))
    dt = open_caltab(path)
    flag = dt["spw_0"]["FLAG"]
    src = dt["spw_0"].ds[S.SOURCE_ROW].values
    where = {int(src[t, a]): (t, a) for t in range(src.shape[0]) for a in range(src.shape[1])}
    assert flag.values[0][where[0]][0].tolist() == [True, False]     # any of the four
    assert commit_flags(dt)["written"] == 0                      # partly flagged, untouched: not written
    assert _flags(path)[0][0].tolist() == [True] + [False] * 7

    t, a = where[1]
    flag[0, t, a, 0, 1] = False                                  # unflag row 1, pol 1: clears its DELAY
    t, a = where[3]
    flag[0, t, a, 0, 0] = True                                   # flag row 3, pol 0: sets all four
    report = commit_flags(dt)
    assert (report["flagged"], report["unflagged"], report["written"], report["rows"]) == (1, 1, 5, 2)
    now = _flags(path)
    assert not now[1].any()
    assert now[3][0].tolist() == [True] * 4 + [False] * 4
    assert now[0][0].tolist() == [True] + [False] * 7            # still as it was
    assert now[2][0].tolist() == [False] * 4 + [True] * 4


def test_unknown_type_flags_each_parameter(tmp_path):
    path, dt = _table(tmp_path, "Zeta Jones", npar=5, nant=3, ntime=2, nspw=1)
    dt["spw_0"]["FLAG"][0, 1, 2, 0, 3] = True
    report = commit_flags(dt)
    assert report["written"] == 1 and report["flagged"] == 1
    row = int(dt["spw_0"].ds[S.SOURCE_ROW].values[1, 2])
    assert _flags(path)[row][0].tolist() == [False, False, False, True, False]


def test_no_solution_is_not_written(tmp_path):
    """Unflagging where there is no solution has nothing to write."""
    path, dt = _table(tmp_path, "Tsys", ntime=20, missing_fraction=0.4, flag_fraction=0.1, seed=5)
    ds = dt["spw_0"].ds
    absent = (ds[S.SOURCE_ROW] < 0).values
    assert absent.any()
    solved_flagged = int(ds[S.FLAG].values[0][~absent].sum())
    dt["spw_0"]["FLAG"][:] = False                               # "unflag everything"
    report = commit_flags(dt)
    assert report["no_solution"] == int(absent.sum()) * 2        # two receptors each
    assert report["unflagged"] == solved_flagged == report["written"] and report["verified"]
    back = open_caltab(path)["spw_0"].ds
    assert back[S.FLAG].values[0][absent].all()                  # still flagged, as every reader has them
    assert not back[S.FLAG].values[0][~absent].any()


# --- when it must not write -------------------------------------------------------------------

def test_refuses_a_table_that_changed(tmp_path):
    """The tree points at rows; if they are no longer that time and
    antenna, nothing is written."""
    path, dt = _table(tmp_path, ntime=6, missing_fraction=0.3, seed=1)
    dt["spw_0"]["FLAG"][:] = True
    # CASA solves again: the same name, other rows
    write_caltab(synthetic_caltab("G", ntime=6, missing_fraction=0.3, seed=9), path, overwrite=True)
    before = _flags(path)
    with pytest.raises(FlagCommitError, match="changed since it was read"):
        commit_flags(dt)
    after = _flags(path)
    assert all((after[r] == before[r]).all() for r in before) and _backups(path) == []
    # fewer rows than the tree points at
    write_caltab(synthetic_caltab("G", ntime=2), path, overwrite=True)
    with pytest.raises(FlagCommitError, match="not the table the tree was read from"):
        commit_flags(dt)
    # another table altogether, named on purpose
    other, _ = _table(tmp_path, "K", name="other.cal", ntime=6, start=2e9)
    with pytest.raises(FlagCommitError):
        commit_flags(dt, other)
    assert _backups(other) == []


def test_which_store(tmp_path):
    dt = synthetic_caltab("G")
    with pytest.raises(FlagCommitError, match="give the path"):
        commit_flags(dt)                                         # read from nowhere
    path = write_caltab(dt, str(tmp_path / "t.cal"))             # ... but its own table will do
    dt["spw_0"]["FLAG"].loc[{S.ANTENNA: "A07"}] = True
    report = commit_flags(dt, path)
    assert report["path"] == path and report["flagged"] == 24 and report["verified"]
    assert open_caltab(path)["spw_0"].ds[S.FLAG].sel({S.ANTENNA: "A07"}).all()
    # the committer is chosen by where the tree came from
    assert set(COMMITTERS) == {"casa-caltab"}
    zarr_like = open_caltab(path)
    zarr_like.attrs["source_format"] = "xradio-calset"
    with pytest.raises(FlagCommitError, match="xradio-calset"):
        commit_flags(zarr_like)
    with pytest.raises(Exception):
        commit_flags(open_caltab(path), str(tmp_path))           # not a table


def test_restore_refuses_another_table(tmp_path):
    path, dt = _table(tmp_path)
    backup = commit_flags(_flagged(dt))["backup"]
    other, _ = _table(tmp_path, "K", name="other.cal", ntime=3)
    before = _flags(other)
    with pytest.raises(FlagCommitError, match="Nothing was written"):
        restore_flags(backup, other)
    after = _flags(other)
    assert all((after[r] == before[r]).all() for r in before)
    assert list_flag_backups(str(tmp_path / "none.cal")) == []


# --- with CASA: its reads of the flags written here, and applycal ---------------------------------

def _casatools():
    try:
        import casatools
        return casatools.table()
    except Exception as exc:
        pytest.skip(f"casatools is not usable here ({type(exc).__name__})")


def _casa_columns(tb, path):
    tb.open(path)
    try:
        cols = {c: [tb.getcell(c, r) for r in range(tb.nrows())] if tb.isvarcol(c) or c == "FLAG"
                else tb.getcol(c) for c in tb.colnames() if c != "WEIGHT"}
    finally:
        tb.close()
    return cols


@pytest.mark.skipif(not REAL, reason="set CALTABLES to the folder with sim/ and tl016b/")
@pytest.mark.parametrize("src", REAL, ids=os.path.basename)
def test_real_table_flagged_and_restored(tmp_path, src):
    """On a copy of each real table: flag an antenna in one window and
    unflag another; CASA reads exactly that change; restore undoes it."""
    tb = _casatools()
    path = str(tmp_path / os.path.basename(src))
    shutil.copytree(src, path)
    original = _casa_columns(tb, path)
    dt = open_caltab(path)
    name = sorted(dt.children)[-1]
    ds = dt[name].ds
    old = ds[S.FLAG].values.copy()
    dt[name]["FLAG"].loc[{S.ANTENNA: ds[S.ANTENNA].values[1]}] = True
    dt[name]["FLAG"].loc[{S.ANTENNA: ds[S.ANTENNA].values[-1]}] = False
    solved = (ds[S.SOURCE_ROW] >= 0).values
    new = dt[name].ds[S.FLAG].values
    changed_cells = (new != old)[0][solved]                      # (solution, chan, last)
    report = commit_flags(dt)
    assert report["verified"] and report["flagged"] + report["unflagged"] == int(changed_cells.sum())
    assert report["flagged"] + report["unflagged"] > 0

    now = _casa_columns(tb, path)
    for col in original:                                         # CASA sees nothing else changed
        if col != "FLAG":
            for a, b in zip(now[col], original[col]):
                np.testing.assert_array_equal(a, b, err_msg=col)
    rows = ds[S.SOURCE_ROW].values[solved]
    touched = set(rows[changed_cells.reshape(len(rows), -1).any(axis=1)].tolist())
    differ = {r for r in range(len(original["FLAG"])) if not np.array_equal(now["FLAG"][r], original["FLAG"][r])}
    assert differ == touched and len(differ) == report["rows"]
    again = open_caltab(path)                                    # and the tree read back is the tree committed
    np.testing.assert_array_equal(again[name].ds[S.FLAG].values[0][solved], new[0][solved])

    restore_flags(report["backup"])
    back = _casa_columns(tb, path)
    for a, b in zip(back["FLAG"], original["FLAG"]):
        np.testing.assert_array_equal(a, b)


def test_applycal_honors_a_flag_written_here(tmp_path):
    """A solution flagged with commit_flags is a flagged solution to CASA:
    applycal flags the data it would have corrected."""
    sim = os.path.join(CALTABLES or "", "sim")
    if not (CALTABLES and os.path.isdir(os.path.join(sim, "sim.ms"))):
        pytest.skip("set CALTABLES; $CALTABLES/sim from make_caltables.py")
    tb = _casatools()
    try:
        import casatasks
    except Exception as exc:
        pytest.skip(f"casatasks is not usable here ({type(exc).__name__})")
    ms, table = str(tmp_path / "sim.ms"), str(tmp_path / "sim.G")
    shutil.copytree(os.path.join(sim, "sim.ms"), ms)
    shutil.copytree(os.path.join(sim, "sim.G"), table)
    dt = open_caltab(table)
    antenna = str(dt["spw_0"].ds[S.ANTENNA].values[3])
    dt["spw_0"]["FLAG"].loc[{S.ANTENNA: antenna}] = True         # antenna 3, window 0, every time
    assert commit_flags(dt)["verified"]

    tb.open(ms)
    a1, a2, dd, before = tb.getcol("ANTENNA1"), tb.getcol("ANTENNA2"), tb.getcol("DATA_DESC_ID"), tb.getcol("FLAG")
    tb.close()
    tb.open(os.path.join(ms, "DATA_DESCRIPTION")); spw_of_dd = tb.getcol("SPECTRAL_WINDOW_ID"); tb.close()
    cwd = os.getcwd()
    os.chdir(tmp_path)                                           # CASA's log goes where it runs
    try:
        casatasks.applycal(vis=ms, gaintable=[table], calwt=False, flagbackup=False)
    finally:
        os.chdir(cwd)
    tb.open(ms); after = tb.getcol("FLAG"); tb.close()           # (corr, chan, row)
    hit = ((a1 == 3) | (a2 == 3)) & (spw_of_dd[dd] == 0)
    assert hit.any() and not before[..., hit].all()
    assert after[..., hit].all()                                 # its baselines in that window: flagged
    np.testing.assert_array_equal(after[..., ~hit], before[..., ~hit])   # and nothing else
