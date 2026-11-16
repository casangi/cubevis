"""The synthetic calibration source and the arcae table writer (calplot C0).

    pytest -q tests/manual/calplot/test_caltab_synthetic.py

``synthetic_caltab`` makes a tree from numbers; ``write_caltab`` makes a
CASA table of a tree with arcae alone.  Together they give the reader a
table to read where there is no CASA and no data, so most of this file
needs neither: it runs in the quick tests.

With ``CALTABLES`` (see ``test_caltab.py``) every real table is also
written from its tree and read back; with casatools the written tables
are checked by CASA's own reads, and by ``applycal``.
"""
from __future__ import annotations

import glob
import os
import shutil
import subprocess
import sys
import warnings

import numpy as np
import pytest

warnings.filterwarnings("ignore")
pytest.importorskip("xarray")
pytest.importorskip("arcae")

import xarray as xr                                              # noqa: E402

from cubevis.xarray import _arcae_util as au                     # noqa: E402
from cubevis.xarray.caltab import (open_caltab, synthetic_caltab, synthetic_value,   # noqa: E402
                                   write_caltab)
from cubevis.xarray.caltab import _schema as S                   # noqa: E402
from cubevis.xarray.caltab._build import tree_to_raw             # noqa: E402
from cubevis.xarray.caltab._synthetic import synthetic_raw       # noqa: E402

CALTABLES = os.environ.get("CALTABLES")
LEAD = (S.DIRECTION, S.TIME, S.ANTENNA, S.FREQUENCY)
KNOWN = ["G", "T", "B", "K", "Tsys", "Fringe", "Accor"]


def _real_tables():
    if not CALTABLES:
        return []
    return sorted(p for p in glob.glob(os.path.join(CALTABLES, "*", "*"))
                  if au.is_table(p) and au.table_info(p).get("type") == "Calibration")


REAL = _real_tables()


def _same_tree(a, b, source_row=True):
    """The nodes of two trees are identical (attributes included)."""
    assert list(a.children) == list(b.children)
    for name in a.children:
        x, y = a[name].to_dataset(), b[name].to_dataset()
        if not source_row:
            x, y = x.drop_vars(S.SOURCE_ROW), y.drop_vars(S.SOURCE_ROW)
        xr.testing.assert_identical(x, y)


def _params(ds):
    return [v[len(S.PARAM_PREFIX) + 1:] for v in ds.data_vars if v.startswith(S.PARAM_PREFIX + "_")]


# --- the synthetic source ---------------------------------------------------------

@pytest.mark.parametrize("caltype", KNOWN)
def test_shape_and_values(caltype):
    """The same shape as a table read, and every value the stated rule's."""
    dt = synthetic_caltab(caltype, nant=7, nspw=3, ntime=9)
    ct = S.CAL_TYPES[dt.attrs["casa_subtype"]]
    assert dt.attrs["caltype"] == caltype and dt.attrs["type"] == S.TREE_TYPE
    assert dt.attrs["source"] == f"synthetic:{caltype}"
    assert list(dt.children) == ["spw_0", "spw_1", "spw_2"]
    nrec = 1 if caltype == "T" else 2
    nchan = 32 if caltype == "B" else 1
    for node in dt.children.values():
        ds = node.ds
        assert dict(ds.sizes) == {S.DIRECTION: 1, S.TIME: 9, S.ANTENNA: 7, S.FREQUENCY: nchan,
                                  S.RECEPTOR: nrec}
        assert _params(ds) == [p.name for p in ct.params]
        assert ds[S.FLAG].dtype == bool and not ds[S.FLAG].any()
        assert (ds[S.SOURCE_ROW] >= 0).all()
        assert ds.time.attrs == {"type": "time", "units": "s", "scale": "utc", "format": "unix"}
        a = np.arange(7)[None, :, None, None]
        r = np.arange(nrec)[None, None, None, :]
        tau = np.linspace(0, 1, 9)[:, None, None, None]
        x = (np.linspace(-1, 1, nchan) if nchan > 1 else np.zeros(1))[None, None, :, None]
        for p in ct.params:
            var = ds[f"{S.PARAM_PREFIX}_{p.name}"]
            assert var.dims == LEAD + (S.RECEPTOR,)
            assert ("units" in var.attrs) == (p.units is not None)
            want = synthetic_value(caltype, p.name, a, r, tau, x)
            if ct.partype == "Float":
                want = want.real
            np.testing.assert_array_equal(var.values[0], want.astype(var.dtype))
        np.testing.assert_array_equal(ds[S.SNR].values[0, 0, :, 0, 0], 20.0 + np.arange(7))


def test_times_scans_fields():
    dt = synthetic_caltab("G", ntime=8, nscan=4, field_names=["a", "b", "c"], interval=30.0, start=1e9)
    t0, t1 = dt["spw_0"].ds.time.values, dt["spw_1"].ds.time.values
    np.testing.assert_array_equal(t0, 1e9 + (np.arange(8) + 0.5) * 30.0)
    np.testing.assert_array_equal(t1, t0 + 1.0)                   # each window its own times
    ds = dt["spw_0"].ds
    assert list(ds.scan_name.values) == ["1", "1", "2", "2", "3", "3", "4", "4"]
    assert list(ds.field_name.values) == ["a", "a", "b", "b", "c", "c", "a", "a"]
    assert (ds[S.INTERVAL] == 30.0).all()
    same = synthetic_caltab("G", ntime=8, own_times=False)
    np.testing.assert_array_equal(same["spw_0"].ds.time.values, same["spw_1"].ds.time.values)


def test_reference_antenna():
    fr = synthetic_caltab("Fringe", nant=5, reference_antenna="A02", noise=0.1)["spw_0"].ds
    assert fr.attrs["reference_antenna"] == "A02" and S.REFERENCE_ANTENNA not in fr
    for v in ("CALPARAM_PHASE", "CALPARAM_DELAY", "CALPARAM_RATE", "CALPARAM_DISP_DELAY"):
        assert (fr[v].sel({S.ANTENNA: "A02"}).values == 0).all()   # exactly, noise or not
    assert (fr.CALPARAM_DELAY.sel({S.ANTENNA: "A04"}).values != 0).all()
    assert "reference_antenna" not in synthetic_caltab("K")["spw_0"].ds.attrs     # as CASA's K
    assert "reference_antenna" not in synthetic_caltab("G", reference_antenna=None)["spw_0"].ds.attrs
    # one that changes half way: no attribute, a variable instead
    ch = synthetic_caltab("G", ntime=6, reference_antenna="changing")["spw_0"].ds
    assert "reference_antenna" not in ch.attrs
    ref = ch[S.REFERENCE_ANTENNA]
    assert ref.dims == (S.TIME, S.ANTENNA)
    assert (ref.values[:3] == "A00").all() and (ref.values[3:] == "A01").all()
    # each half's phases are relative to its own reference
    phase = np.angle(ch.CALPARAM_GAIN.values[0, :, :, 0, 0])
    assert (phase[:3, 0] == 0).all() and (phase[3:, 1] == 0).all() and (phase[3:, 0] != 0).any()


def test_cases_no_table_here_covers():
    # a Tsys with channels, antennas with times of their own, some flagged
    ts = synthetic_caltab("Tsys", nchan=16, missing_fraction=0.4, flag_fraction=0.1, ntime=40)["spw_0"].ds
    assert ts.sizes[S.FREQUENCY] == 16
    absent = (ts[S.SOURCE_ROW] < 0).values
    assert 0.3 < absent.mean() < 0.5
    assert np.isnan(ts.CALPARAM_TSYS.values[0][absent]).all() and ts[S.FLAG].values[0][absent].all()
    solved = ts[S.FLAG].values[0][~absent]
    assert 0.07 < solved.mean() < 0.13 and not np.isnan(ts.CALPARAM_TSYS.values[0][~absent]).any()
    # one receptor that is for every polarization
    t = synthetic_caltab("T")["spw_0"].ds
    assert list(t[S.RECEPTOR].values) == ["all"]
    # one receptor of a two-receptor type; named receptors
    k = synthetic_caltab("K", nreceptor=1, receptor_labels=["R"])["spw_0"].ds
    assert list(k[S.RECEPTOR].values) == ["R"]
    assert list(synthetic_caltab("B", receptor_labels=["X", "Y"])["spw_0"].ds[S.RECEPTOR].values) == ["X", "Y"]
    # an array the size of ALMA
    assert synthetic_caltab("B", nant=66)["spw_1"].ds.sizes[S.ANTENNA] == 66
    assert list(synthetic_caltab("G", antenna_names=["BR", "FD", "HN"])["spw_0"].ds[S.ANTENNA].values) \
        == ["BR", "FD", "HN"]
    # windows that differ in channel count
    b = synthetic_caltab("B", nspw=3, nchan=[32, 16, 8])
    assert [b[n].ds.sizes[S.FREQUENCY] for n in b.children] == [32, 16, 8]
    with pytest.raises(ValueError):
        synthetic_caltab("B", nspw=2, nchan=[32, 16, 8])
    # unflagged values far too large, as a real Tsys has
    out = synthetic_caltab("Tsys", outliers=3, seed=4)["spw_0"].ds
    assert (out.CALPARAM_TSYS > 5000).sum() >= 1 and not out[S.FLAG].any()
    assert list(synthetic_caltab("G", nspw=3, spw=[2]).children) == ["spw_2"]


def test_unknown_type_is_generic():
    dt = synthetic_caltab("Zeta Jones", npar=5, nant=4)
    ds = dt["spw_0"].ds
    assert dt.attrs["caltype"] == "Zeta Jones" and dt.attrs["par_type"] == "Float"
    assert ds.CALPARAM.dims == LEAD + (S.PARAMETER,) and ds.sizes[S.PARAMETER] == 5
    assert S.RECEPTOR not in ds.dims and "reference_antenna" not in ds.attrs
    np.testing.assert_array_equal(ds.CALPARAM.values[0, 0], (100.0 * np.arange(5)[None, None, :]
                                                             + np.arange(4)[:, None, None]).astype(np.float32))


def test_seed_and_noise():
    kw = dict(flag_fraction=0.2, missing_fraction=0.2, noise=0.05)
    _same_tree(synthetic_caltab("G", seed=3, **kw), synthetic_caltab("G", seed=3, **kw))
    a, b = synthetic_caltab("G", seed=3, **kw), synthetic_caltab("G", seed=4, **kw)
    assert not np.array_equal(a["spw_0"].ds[S.FLAG].values, b["spw_0"].ds[S.FLAG].values)
    clean, noisy = synthetic_caltab("K")["spw_0"].ds, synthetic_caltab("K", noise=0.05)["spw_0"].ds
    diff = (noisy.CALPARAM_DELAY - clean.CALPARAM_DELAY).values
    assert 0.03 < diff.std() < 0.07


def test_import_is_light():
    code = ("import sys, cubevis.xarray.caltab._build, cubevis.xarray.caltab._synthetic, "
            "cubevis.xarray.caltab._write\n"
            "bad = [m for m in ('bokeh', 'casatools', 'casatasks', 'arcae', 'xarray') if m in sys.modules]\n"
            "print(','.join(bad))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == ""


# --- written as a CASA table, with arcae alone ---------------------------------------

CASES = [("G", {}), ("T", {}), ("B", {"nchan": [32, 16]}), ("K", {}), ("Accor", {}),
         ("Tsys", {"nchan": 8, "missing_fraction": 0.4, "flag_fraction": 0.1}),
         ("Fringe", {"reference_antenna": "changing", "noise": 0.01}),
         ("Fringe", {"flag_fraction": 0.2}),
         ("G", {"receptor_labels": ["R", "L"], "nant": 27, "noise": 0.01, "outliers": 2}),
         ("B", {"receptor_labels": ["X", "Y"], "reference_antenna": None}),
         ("Zeta Jones", {"npar": 5, "flag_fraction": 0.1})]


@pytest.mark.parametrize("caltype,kw", CASES, ids=[f"{c}-{i}" for i, (c, _) in enumerate(CASES)])
def test_write_and_read_back(tmp_path, caltype, kw):
    """A tree written as a table reads back as the same tree: the reader
    and the writer, on a table made here."""
    dt = synthetic_caltab(caltype, **kw)
    path = write_caltab(dt, str(tmp_path / "t.cal"))
    assert au.is_table(path) and au.table_info(path) == {"type": "Calibration",
                                                         "subType": dt.attrs["casa_subtype"]}
    back = xr.open_datatree(path, engine="casa-caltab")
    assert back.attrs["source"] == path
    for key in ("type", "caltype", "casa_subtype", "par_type", "schema_draft"):
        assert back.attrs[key] == dt.attrs[key]
    _same_tree(back, dt)


def test_write_from_rows_and_reference_ids(tmp_path):
    """Written from rows, a table keeps CASA's numbering: ANTENNA2 is the
    reference antenna's id, or -1."""
    raw = synthetic_raw("G", nant=4, ntime=4, reference_antenna="changing")
    path = write_caltab(raw, str(tmp_path / "t.cal"))
    t = au.open_table(path)
    try:
        np.testing.assert_array_equal(t.getcol("ANTENNA2"), raw.main["ANTENNA2"])
        np.testing.assert_array_equal(t.getcol("TIME"), raw.main["TIME"])
        assert set(np.unique(t.getcol("ANTENNA2"))) == {0, 1}
        plain, subtables = au.keywords(t)
        assert plain["VisCal"] == "G Jones" and plain["ParType"] == "Complex"
        assert sorted(subtables) == ["ANTENNA", "FIELD", "HISTORY", "OBSERVATION", "SPECTRAL_WINDOW"]
    finally:
        t.close()
    back = tree_to_raw(open_caltab(path))
    for col in raw.main:
        np.testing.assert_array_equal(back.main[col], raw.main[col], err_msg=col)


def test_write_overwrite(tmp_path):
    dt = synthetic_caltab("K", nant=3, ntime=2)
    path = write_caltab(dt, str(tmp_path / "t.cal"))
    with pytest.raises(FileExistsError):
        write_caltab(dt, path)
    write_caltab(synthetic_caltab("G", nant=3, ntime=2), path, overwrite=True)
    assert open_caltab(path).attrs["caltype"] == "G"
    other = tmp_path / "notes"
    other.mkdir()
    (other / "keep.txt").write_text("x")
    with pytest.raises(FileExistsError):                          # not a table: never removed
        write_caltab(dt, str(other), overwrite=True)
    assert (other / "keep.txt").exists()


# --- with CASA: its reads of a written table, and applycal ------------------------------

def _casatools():
    try:
        import casatools
        return casatools.table()
    except Exception as exc:
        pytest.skip(f"casatools is not usable here ({type(exc).__name__})")


@pytest.mark.parametrize("caltype,kw", CASES[:8], ids=[f"{c}-{i}" for i, (c, _) in enumerate(CASES[:8])])
def test_casatools_reads_written_table(tmp_path, caltype, kw):
    tb = _casatools()
    raw = synthetic_raw(caltype, **kw)
    path = write_caltab(raw, str(tmp_path / "t.cal"))
    tb.open(path)
    try:
        assert tb.info()["type"] == "Calibration" and tb.info()["subType"] == raw.subtype
        assert tb.getkeyword("VisCal") == raw.subtype and tb.getkeyword("ParType") == raw.keywords["ParType"]
        pcol = "CPARAM" if raw.keywords["ParType"] == "Complex" else "FPARAM"
        for col in raw.main:
            np.testing.assert_array_equal(tb.getcol(col), raw.main[col], err_msg=col)
        for c in raw.cells.values():
            for i, r in enumerate(c.rows):                        # casatools: (npar, nchan)
                np.testing.assert_array_equal(tb.getcell(pcol, int(r)), c.par[i].T)
                np.testing.assert_array_equal(tb.getcell("FLAG", int(r)), c.flag[i].T)
                np.testing.assert_array_equal(tb.getcell("SNR", int(r)), c.snr[i].T)
                np.testing.assert_array_equal(tb.getcell("PARAMERR", int(r)), c.err[i].T)
    finally:
        tb.close()
    tb.open(os.path.join(path, "ANTENNA")); names = list(tb.getcol("NAME")); tb.close()
    assert names == raw.antenna_names
    tb.open(os.path.join(path, "SPECTRAL_WINDOW"))
    for s, c in raw.cells.items():
        np.testing.assert_array_equal(tb.getcell("CHAN_FREQ", s), c.chan_freq)
    tb.close()


@pytest.mark.skipif(not REAL, reason="set CALTABLES to the folder with sim/ and tl016b/")
@pytest.mark.parametrize("path", REAL, ids=os.path.basename)
def test_real_table_rewritten(tmp_path, path):
    """A real table, written from its tree, reads back as the same tree."""
    dt = open_caltab(path)
    back = open_caltab(write_caltab(dt, str(tmp_path / "copy.cal")))
    _same_tree(back, dt)


def test_applycal_takes_a_written_table(tmp_path):
    """CASA applies a table written from a tree, and corrects the data as
    it does with the table the tree was read from."""
    sim = os.path.join(CALTABLES or "", "sim")
    if not (CALTABLES and os.path.isdir(os.path.join(sim, "sim.ms"))):
        pytest.skip("set CALTABLES; $CALTABLES/sim from make_caltables.py")
    tb = _casatools()
    try:
        import casatasks
    except Exception as exc:
        pytest.skip(f"casatasks is not usable here ({type(exc).__name__})")
    ms = str(tmp_path / "sim.ms")
    shutil.copytree(os.path.join(sim, "sim.ms"), ms)
    cwd = os.getcwd()
    os.chdir(tmp_path)                                            # CASA's log goes where it runs
    try:
        def corrected(table):
            casatasks.applycal(vis=ms, gaintable=[table], calwt=False, flagbackup=False)
            tb.open(ms)
            try:
                return tb.getcol("CORRECTED_DATA"), tb.getcol("FLAG"), tb.getcol("DATA")
            finally:
                tb.close()
        for name in ("sim.G", "sim.B", "sim.K"):
            original = os.path.join(sim, name)
            written = write_caltab(open_caltab(original), str(tmp_path / (name + ".copy")))
            d0, f0, data = corrected(original)
            d1, f1, _ = corrected(written)
            assert not np.array_equal(d0, data), name             # the table does something
            np.testing.assert_array_equal(d1, d0, err_msg=name)
            np.testing.assert_array_equal(f1, f0, err_msg=name)
    finally:
        os.chdir(cwd)
