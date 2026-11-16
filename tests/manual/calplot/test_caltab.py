"""The ``casa-caltab`` xarray backend (calplot C0): CASA calibration tables
read with arcae into an ``xarray.DataTree``.

    CALTABLES=/path/to/tables pytest -q tests/manual/calplot/test_caltab.py

Data: ``CALTABLES`` names a folder holding two sets of tables, which are
not in the repository:

    $CALTABLES/sim/      python devel/tools/calplot/make_caltables.py $CALTABLES/sim
                         (6 tables and truth.npz; needs casatools; 2 minutes)
    $CALTABLES/tl016b/   caltables.tgz from devel/tools/calplot/tl016b_caltables.py
                         (9 tables from a real VLBA observation)

Without ``CALTABLES`` the tests of the tables skip; the rest (the schema
table, the entry point, what is not a table) still run.  The comparison
with casatools' own reads also skips without casatools.
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
from cubevis.xarray.caltab import NotACalTable, caltab_info, open_caltab   # noqa: E402
from cubevis.xarray.caltab import _schema as S                   # noqa: E402
from cubevis.xarray.caltab._backend import CaltabBackendEntrypoint         # noqa: E402

CALTABLES = os.environ.get("CALTABLES")
SIM = os.path.join(CALTABLES, "sim") if CALTABLES else None
REAL = os.path.join(CALTABLES, "tl016b") if CALTABLES else None


def _tables(folder):
    if not folder or not os.path.isdir(folder):
        return []
    return sorted(p for p in glob.glob(os.path.join(folder, "*"))
                  if au.is_table(p) and au.table_info(p).get("type") == "Calibration")


TABLES = _tables(SIM) + _tables(REAL)
need_tables = pytest.mark.skipif(not TABLES, reason="set CALTABLES to the folder with sim/ and tl016b/")
need_sim = pytest.mark.skipif(not _tables(SIM), reason="set CALTABLES; $CALTABLES/sim from make_caltables.py")
need_real = pytest.mark.skipif(not _tables(REAL), reason="set CALTABLES; $CALTABLES/tl016b from caltables.tgz")
LEAD = (S.DIRECTION, S.TIME, S.ANTENNA, S.FREQUENCY)


def _id(path):
    return os.path.basename(path)


def _casatools():
    """casatools' table tool, or skip (it needs CASA's measures data too)."""
    try:
        import casatools
        return casatools.table()
    except Exception as exc:                                     # not installed, or no data
        pytest.skip(f"casatools is not usable here ({type(exc).__name__})")


# --- without tables ------------------------------------------------------------

def test_schema_lookup():
    assert S.lookup("G Jones", "Complex", 2).caltype == "G"
    assert S.lookup("B TSYS", "Float", 2).params[0].units == "K"
    fringe = S.lookup("Fringe Jones", "Float", 8)
    assert [p.name for p in fringe.params] == ["PHASE", "DELAY", "RATE", "DISP_DELAY"]
    assert S.lookup("K Jones", "Float", 1).caltype == "K"        # one receptor
    t = S.lookup("T Jones", "Complex", 1)                        # one gain for both polarizations
    assert t.caltype == "T" and t.one_for_all and [p.name for p in t.params] == ["GAIN"]
    # the generic form: an unknown type, the wrong parameter type, a count that does not divide
    assert S.lookup("EPowerCurve", "Float", 16) is None
    assert S.lookup("G Jones", "Float", 2) is None
    assert S.lookup("Fringe Jones", "Float", 6) is None
    assert S.lookup(None, None, 2) is None


def test_receptor_labels():
    assert S.receptor_labels("unknown", 2) == ["pol_0", "pol_1"]
    assert S.receptor_labels(None, 2) == ["pol_0", "pol_1"]
    assert S.receptor_labels("linear", 2) == ["X", "Y"]
    assert S.receptor_labels("Circular", 2) == ["R", "L"]
    assert S.receptor_labels("circular", 1) == ["R"]
    assert S.receptor_labels("linear", 3) == ["pol_0", "pol_1", "pol_2"]
    assert S.receptor_labels("unknown", 2, ["R", "L"]) == ["R", "L"]
    with pytest.raises(ValueError):
        S.receptor_labels("unknown", 2, ["R"])
    assert S.receptor_labels("circular", 1, one_for_all=True) == ["all"]
    assert S.receptor_labels("unknown", 1, ["I"], one_for_all=True) == ["I"]
    # the keyword that reads back as the same labels
    assert [S.pol_basis_of(v) for v in (["X", "Y"], ["R", "L"], ["R"], ["pol_0", "pol_1"], ["all"], [])] \
        == ["linear", "circular", "circular", "unknown", "unknown", "unknown"]


def test_flag_cast():
    """arcae gives Bool columns as uint8; ``~`` on that is not a logical not."""
    raw = np.array([0, 1, 1, 0], dtype=np.uint8)
    assert (~raw).max() == 255                                   # the trap
    flags = au.as_bool(raw)
    assert flags.dtype == bool and (~flags).tolist() == [True, False, False, True]


def test_not_a_table(tmp_path):
    entry = CaltabBackendEntrypoint()
    assert not entry.guess_can_open(str(tmp_path))
    assert not entry.guess_can_open(str(tmp_path / "missing"))
    assert not entry.guess_can_open(42)
    with pytest.raises(NotACalTable):
        open_caltab(str(tmp_path))
    assert au.table_info(str(tmp_path)) == {}


def test_entry_point_registered():
    from importlib.metadata import entry_points
    names = [e.name for e in entry_points(group="xarray.backends")]
    if "casa-caltab" not in names:
        pytest.skip("cubevis is not installed (the entry point comes with the install)")
    assert "casa-caltab" in xr.backends.list_engines()


def test_engine_known_after_import():
    """Installed or run from a checkout, importing the package is enough."""
    import cubevis.xarray.caltab                                 # noqa: F401
    assert "casa-caltab" in xr.backends.list_engines()
    assert isinstance(xr.backends.list_engines()["casa-caltab"], CaltabBackendEntrypoint)


_NOT_INSTALLED = (
    "import sys\n"
    "import cubevis.xarray._register as r\n"
    "r.entry_point_installed = lambda name: False               # as from a checkout\n"
    "{before}"
    "from cubevis.xarray.caltab import open_caltab\n"
    "import xarray as xr\n"
    "from xarray.backends import plugins\n"
    "assert 'casa-caltab' in plugins.BACKEND_ENTRYPOINTS, 'not registered'\n"
    "assert 'casa-caltab' in xr.backends.list_engines(), 'not listed'\n"
    "bad = [m for m in ('bokeh', 'casatools', 'casatasks') if m in sys.modules]\n"
    "assert not bad, bad      # (arcae may be there: listing engines loads xarray-ms)\n"
    "from cubevis.xarray.caltab import register_backend\n"
    "assert register_backend() is False                         # once is enough\n"
)


@pytest.mark.parametrize("before", ["", "import xarray as xr; xr.backends.list_engines()\n"],
                         ids=["cubevis-first", "xarray-used-first"])
def test_registered_without_the_entry_point(before):
    """From a checkout that is not installed there is no entry point: the
    import registers the engine, also when xarray has already listed its
    engines (its list is cached).

    This uses ``xarray.backends.plugins.BACKEND_ENTRYPOINTS``, which xarray
    does not document: if a release moves it, this is the test that fails.
    """
    out = subprocess.run([sys.executable, "-c", _NOT_INSTALLED.format(before=before)],
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr[-800:]


def test_register_never_raises():
    from cubevis.xarray import _register
    assert _register.register("casa-caltab", "cubevis.xarray.caltab._backend",
                              "CaltabBackendEntrypoint") is False        # there already
    assert _register.register("no-such-engine", "cubevis.no_such_module", "Nothing",
                              force=True) is False                       # cannot be done: no error
    assert "no-such-engine" not in xr.backends.list_engines()
    assert _register.entry_point_installed("no-such-engine") is False


def test_import_is_light():
    """Importing the backend brings neither Bokeh nor CASA nor arcae; and,
    where the entry point is installed, not xarray either."""
    code = ("import sys, cubevis.xarray, cubevis.xarray.caltab, cubevis.xarray.caltab._schema, "
            "cubevis.xarray._arcae_util\n"
            "from cubevis.xarray._register import entry_point_installed\n"
            "names = ['bokeh', 'casatools', 'casatasks', 'arcae']\n"
            "names += ['xarray'] if entry_point_installed('casa-caltab') else []\n"
            "print(','.join(m for m in names if m in sys.modules))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == ""


# --- every table: shape and bookkeeping -----------------------------------------

@need_tables
@pytest.mark.parametrize("path", TABLES, ids=_id)
def test_shape(path):
    info = caltab_info(path)
    dt = xr.open_datatree(path, engine="casa-caltab")
    assert dt.attrs["type"] == S.TREE_TYPE and dt.attrs["source"] == info["path"]
    assert dt.attrs["schema_draft"] == S.SCHEMA_DRAFT
    assert len(dt.children) >= 1
    rows = 0
    for name, node in dt.children.items():
        ds = node.ds
        assert name == f"spw_{ds.attrs['spectral_window_id']}"
        assert ds.attrs["type"] == S.DATASET_TYPE
        assert ds.sizes[S.DIRECTION] == 1
        params = [v for v in ds.data_vars if v.startswith(S.PARAM_PREFIX)]
        assert params
        last = S.RECEPTOR if info["subType"] in S.CAL_TYPES else S.PARAMETER
        for v in params + [S.SNR, S.FLAG]:
            assert ds[v].dims == LEAD + (last,), v
        assert ds[S.FLAG].dtype == bool
        assert ds[S.SOURCE_ROW].dims == (S.TIME, S.ANTENNA)
        assert ds.field_name.dims == (S.TIME,) and ds.scan_name.dims == (S.TIME,)
        assert np.all(np.diff(ds.time.values) > 0)
        assert ds.time.attrs == {"type": "time", "units": "s", "scale": "utc", "format": "unix"}
        # a cell with no row: NaN, flagged, and told apart by SOURCE_ROW
        absent = (ds[S.SOURCE_ROW] < 0).values
        for v in params:
            assert np.isnan(ds[v].values[0][absent]).all()
        assert ds[S.FLAG].values[0][absent].all()
        present = ds[S.SOURCE_ROW].values[~absent]
        assert len(np.unique(present)) == len(present)
        rows += len(present)
    assert rows == info["nrow"]                                  # every row is somewhere, once


# --- every table: the same values casatools reads --------------------------------

@need_tables
@pytest.mark.parametrize("path", TABLES, ids=_id)
def test_parity_with_casatools(path):
    tb = _casatools()
    tb.open(path)
    try:
        col = {c: tb.getcol(c) for c in ("TIME", "FIELD_ID", "SPECTRAL_WINDOW_ID", "ANTENNA1",
                                         "ANTENNA2", "INTERVAL", "SCAN_NUMBER")}
        pcol = "CPARAM" if "CPARAM" in tb.colnames() else "FPARAM"
        # casatools: (npar, nchan, nrow); a cell at a time, as channel counts may differ by SPW
        cell = {c: [tb.getcell(c, r) for r in range(tb.nrows())] for c in (pcol, "PARAMERR", "SNR", "FLAG")}
        subtype = tb.info()["subType"]
        partype = tb.getkeyword("ParType")
    finally:
        tb.close()
    tb.open(os.path.join(path, "ANTENNA")); ant = list(tb.getcol("NAME")); tb.close()
    tb.open(os.path.join(path, "FIELD")); fld = list(tb.getcol("NAME")); tb.close()
    tb.open(os.path.join(path, "SPECTRAL_WINDOW"))
    chan_freq = [tb.getcell("CHAN_FREQ", s) for s in range(tb.nrows())]
    tb.close()

    dt = xr.open_datatree(path, engine="casa-caltab")
    assert dt.attrs["casa_subtype"] == subtype
    assert sorted(int(n[4:]) for n in dt.children) == sorted(set(col["SPECTRAL_WINDOW_ID"].tolist()))
    seen = 0
    for node in dt.children.values():
        ds = node.ds
        s = ds.attrs["spectral_window_id"]
        np.testing.assert_array_equal(ds.frequency.values, chan_freq[s])
        ct = S.lookup(subtype, partype, cell[pcol][0].shape[0])
        if ct is not None:                                       # back to CASA's packing: (npar, nchan)
            def packed(prefix, ti, ai):
                per = [ds[f"{prefix}_{p.name}"].values[0, ti, ai] for p in ct.params]   # each (chan, rec)
                return np.stack(per, axis=-1).reshape(per[0].shape[0], -1).T
        else:
            def packed(prefix, ti, ai):
                return ds[prefix].values[0, ti, ai].T
        src = ds[S.SOURCE_ROW].values
        for ti, ai in zip(*np.nonzero(src >= 0)):
            r = int(src[ti, ai])
            seen += 1
            assert col["SPECTRAL_WINDOW_ID"][r] == s
            assert ds.time.values[ti] + au.MJD_UNIX_OFFSET_S == col["TIME"][r]
            assert ds.antenna_name.values[ai] == ant[col["ANTENNA1"][r]]
            fid = col["FIELD_ID"][r]                             # -1: no field (the gain curve)
            assert ds.field_name.values[ti] == (fld[fid] if fid >= 0 else "")
            assert ds.scan_name.values[ti] == str(col["SCAN_NUMBER"][r])
            assert ds[S.INTERVAL].values[ti, ai] == col["INTERVAL"][r]
            np.testing.assert_array_equal(packed(S.PARAM_PREFIX, ti, ai), cell[pcol][r])
            np.testing.assert_array_equal(packed(S.ERROR_PREFIX, ti, ai), cell["PARAMERR"][r])
            flag, snr = cell["FLAG"][r], cell["SNR"][r]
            if ct is not None:
                n = len(ct.params)                               # per receptor: any flag, the first SNR
                np.testing.assert_array_equal(ds[S.FLAG].values[0, ti, ai].T,
                                              flag.reshape(-1, n, flag.shape[1]).any(axis=1))
                np.testing.assert_array_equal(ds[S.SNR].values[0, ti, ai].T, snr[::n])
                # what "the first SNR" rests on: the others are the same, or 0 (not solved)
                first = np.repeat(snr[::n], n, axis=0)
                assert ((snr == first) | (snr == 0)).all()
                # and "any flag": a receptor's parameters are flagged together
                assert (flag == np.repeat(flag[::n], n, axis=0)).all()
            else:
                np.testing.assert_array_equal(ds[S.FLAG].values[0, ti, ai].T, flag)
                np.testing.assert_array_equal(ds[S.SNR].values[0, ti, ai].T, snr)
            ref = col["ANTENNA2"][r]
            if "reference_antenna" in ds.attrs:
                assert ds.attrs["reference_antenna"] == ant[ref]
            elif S.REFERENCE_ANTENNA in ds:
                assert ds[S.REFERENCE_ANTENNA].values[ti, ai] == (ant[ref] if ref >= 0 else "")
            else:
                assert ref == -1
    assert seen == len(col["TIME"])


# --- the simulated set: known answers ---------------------------------------------

def _sim(name):
    return xr.open_datatree(os.path.join(SIM, name), engine="casa-caltab")


def _expected_delay_ns(truth, freq):
    """The delay applied, plus what the simulated bandpass's phase slope
    (0.3 rad x antenna / nant across the band) adds to it."""
    nant = truth["delay_ns"].shape[0]
    slope = 0.3 * 2 * (np.arange(nant) / nant) / (2 * np.pi * (freq[-1] - freq[0])) * 1e9
    return truth["delay_ns"] + slope[:, None]


@need_sim
def test_truth():
    truth = np.load(os.path.join(SIM, "truth.npz"))
    g, k, b, fr = _sim("sim.G"), _sim("sim.K"), _sim("sim.B"), _sim("sim.fringe")
    for name in g.children:
        squeeze = (S.DIRECTION, S.FREQUENCY)
        # G: amplitudes, averaged over the solutions
        gds = g[name].ds
        amp = np.abs(gds.CALPARAM_GAIN.where(~gds.FLAG)).mean(S.TIME).squeeze().values
        assert np.abs(amp - truth["amp"]).max() < 0.005
        # K and Fringe: delays in ns, each against the truth and against the other
        expected = _expected_delay_ns(truth, b[name].ds.frequency.values)
        kds, fds = k[name].ds, fr[name].ds
        assert kds.CALPARAM_DELAY.attrs["units"] == "ns" and fds.CALPARAM_DELAY.attrs["units"] == "ns"
        kd = kds.CALPARAM_DELAY.squeeze(squeeze).values           # (time, antenna, receptor)
        fd = fds.CALPARAM_DELAY.squeeze(squeeze).values
        assert np.abs(kd - expected[None]).max() < 0.05
        assert np.abs(fd - expected[None]).max() < 0.05
        assert np.abs(fd - kd).max() < 0.05
        # Fringe: rate (s/s) is the phase rate, drift included, over the frequency
        f0 = float(fds.frequency[0])
        hz = truth["rate_mhz"][:, None] * 1e-3 + truth["phase_drift"] / 3600.0 / (2 * np.pi)
        rate = fds.CALPARAM_RATE.squeeze(squeeze).values
        assert np.abs(rate - (hz / f0)[None]).max() < 5e-15       # the rates are up to 5e-14
        # the reference antenna is exactly zero, and named
        ref = fds.attrs["reference_antenna"]
        for v in ("CALPARAM_PHASE", "CALPARAM_DELAY", "CALPARAM_RATE"):
            assert (fds[v].sel({S.ANTENNA: ref}).values == 0).all()
        assert "reference_antenna" not in kds.attrs               # K has none (ANTENNA2 is -1)


@need_sim
def test_one_receptor_table():
    """gaintype="T": one gain for both polarizations, (1, 1) per row."""
    path = os.path.join(SIM, "sim.T")
    if not os.path.isdir(path):
        pytest.skip("sim.T is not there: make the simulated set again (make_caltables.py)")
    dt = _sim("sim.T")
    assert dt.attrs["caltype"] == "T" and dt.attrs["casa_subtype"] == "T Jones"
    for node in dt.children.values():
        ds = node.ds
        assert ds.sizes[S.RECEPTOR] == 1 and list(ds[S.RECEPTOR].values) == ["all"]
        assert ds.CALPARAM_GAIN.dims == LEAD + (S.RECEPTOR,) and np.iscomplexobj(ds.CALPARAM_GAIN.values)
        assert "reference_antenna" in ds.attrs


@need_sim
def test_tsys_units_and_first_value():
    truth = np.load(os.path.join(SIM, "truth.npz"))
    ts = _sim("sim.tsys")
    for i, name in enumerate(sorted(ts.children)):
        ds = ts[name].ds
        assert ds.CALPARAM_TSYS.attrs["units"] == "K"
        first = ds.CALPARAM_TSYS.isel({S.TIME: 0, S.ANTENNA: 0}).squeeze().values
        np.testing.assert_allclose(first, truth["tsys_base"][0, i], rtol=1e-6)   # sin(0) = 0 there


# --- the real set -------------------------------------------------------------------

@need_real
def test_real_types():
    want = {"tl016b.accor": ("Accor", ["CALPARAM_GAIN"]), "tl016b.tsys": ("Tsys", ["CALPARAM_TSYS"]),
            "tl016b.bpass": ("B", ["CALPARAM_GAIN"]), "cal1.dcal": ("K", ["CALPARAM_DELAY"]),
            "cal1.pcal": ("G", ["CALPARAM_GAIN"]), "cal1.apcal": ("G", ["CALPARAM_GAIN"]),
            "tl016b.sbd": ("Fringe", ["CALPARAM_PHASE", "CALPARAM_DELAY", "CALPARAM_RATE",
                                      "CALPARAM_DISP_DELAY"]),
            "tl016b.gff": ("Fringe", ["CALPARAM_PHASE", "CALPARAM_DELAY", "CALPARAM_RATE",
                                      "CALPARAM_DISP_DELAY"])}
    for name, (caltype, params) in want.items():
        dt = xr.open_datatree(os.path.join(REAL, name), engine="casa-caltab")
        assert dt.attrs["caltype"] == caltype and len(dt.children) == 4
        ds = dt["spw_0"].ds
        assert [v for v in ds.data_vars if v.startswith(S.PARAM_PREFIX)] == params
        assert list(ds.antenna_name.values) == ["BR", "FD", "HN", "KP", "LA", "MK", "NL", "OV", "PT", "SC"]
        assert list(ds[S.RECEPTOR].values) == ["pol_0", "pol_1"]   # PolBasis is "unknown"


@need_real
def test_real_gain_curve_is_generic():
    """EPowerCurve is not in the schema table: its 16 values stay as they are."""
    dt = xr.open_datatree(os.path.join(REAL, "tl016b.gc"), engine="casa-caltab")
    ds = dt["spw_0"].ds
    assert dt.attrs["caltype"] == "EPowerCurve"
    assert ds.CALPARAM.dims == LEAD + (S.PARAMETER,) and ds.sizes[S.PARAMETER] == 16
    assert list(ds[S.PARAMETER].values) == list(range(16))
    assert S.RECEPTOR not in ds.dims


@need_real
def test_real_times_differ_by_spw_and_tsys_is_ragged():
    gff = xr.open_datatree(os.path.join(REAL, "tl016b.gff"), engine="casa-caltab")
    times = [gff[n].ds.time.values for n in sorted(gff.children)]
    assert all(len(t) == 58 for t in times)
    assert len(np.unique(np.concatenate(times))) == 217           # each SPW has its own
    assert all((gff[n].ds[S.SOURCE_ROW] >= 0).all() for n in gff.children)   # dense within one
    tsys = xr.open_datatree(os.path.join(REAL, "tl016b.tsys"), engine="casa-caltab")
    ds = tsys["spw_0"].ds
    assert ds.sizes[S.TIME] == 1181
    present = (ds[S.SOURCE_ROW] >= 0).values
    assert present.sum() == 5664 and not present.all()            # antennas at their own times
    flag = ds[S.FLAG].values[0, :, :, 0, :]
    assert flag[~present].all()                                   # no solution reads as flagged
    solved = np.concatenate([tsys[n].ds[S.FLAG].values[0, :, :, 0, :][(tsys[n].ds[S.SOURCE_ROW] >= 0).values]
                             for n in tsys.children])
    assert solved.shape == (22656, 2)
    assert 0.03 < solved.mean() < 0.06                            # 4.2 % of the real solutions are flagged


@need_real
def test_real_fringe_reference_is_zero():
    ds = xr.open_datatree(os.path.join(REAL, "tl016b.gff"), engine="casa-caltab")["spw_1"].ds
    assert ds.attrs["reference_antenna"] == "FD"
    for v in ("CALPARAM_PHASE", "CALPARAM_DELAY", "CALPARAM_RATE", "CALPARAM_DISP_DELAY"):
        assert (ds[v].sel({S.ANTENNA: "FD"}).values == 0).all()
    assert "units" not in ds.CALPARAM_DISP_DELAY.attrs            # not established: not claimed


# --- arguments, and the other ways in ------------------------------------------------

@need_sim
def test_arguments():
    path = os.path.join(SIM, "sim.G")
    one = xr.open_datatree(path, engine="casa-caltab", spw=[1])
    assert list(one.children) == ["spw_1"]
    with pytest.raises(ValueError):
        open_caltab(path, spw=[7])
    rl = open_caltab(path, receptor_labels=["R", "L"])
    assert list(rl["spw_0"].ds[S.RECEPTOR].values) == ["R", "L"]
    ds = xr.open_dataset(path, engine="casa-caltab", group="spw_1")
    assert ds.attrs["spectral_window_id"] == 1 and ds.attrs["type"] == S.DATASET_TYPE
    assert ds.attrs["source"] == one.attrs["source"]             # the table's attributes come along
    with pytest.raises(ValueError):
        xr.open_dataset(path, engine="casa-caltab")              # two windows: which?
    dropped = xr.open_datatree(path, engine="casa-caltab", drop_variables=[S.SNR])
    assert S.SNR not in dropped["spw_0"].ds
    assert CaltabBackendEntrypoint().guess_can_open(path)
    assert not CaltabBackendEntrypoint().guess_can_open(os.path.join(path, "ANTENNA"))


@need_sim
def test_engine_is_guessed():
    if "casa-caltab" not in xr.backends.list_engines():
        pytest.skip("cubevis is not installed (the entry point comes with the install)")
    dt = xr.open_datatree(os.path.join(SIM, "sim.K"))            # no engine=
    assert dt.attrs["source_format"] == "casa-caltab"


@need_sim
def test_unknown_subtype_is_passed_through(tmp_path):
    """A type the backend has not heard of is read, not refused."""
    src = os.path.join(SIM, "sim.G")
    dst = str(tmp_path / "odd.cal")
    shutil.copytree(src, dst)
    with open(os.path.join(dst, "table.info"), "w") as f:
        f.write("Type = Calibration\nSubType = Zeta Jones\n\n")
    t = au.open_table(dst, readonly=False)
    try:
        t.putkeywords({"VisCal": "Zeta Jones"})
    finally:
        t.close()
    known, odd = open_caltab(src), open_caltab(dst)
    assert odd.attrs["caltype"] == "Zeta Jones" and odd.attrs["casa_subtype"] == "Zeta Jones"
    for name in known.children:
        k, o = known[name].ds, odd[name].ds
        assert "CALPARAM_GAIN" not in o
        assert o.CALPARAM.dims == LEAD + (S.PARAMETER,) and o.sizes[S.PARAMETER] == 2
        np.testing.assert_array_equal(o.CALPARAM.values, k.CALPARAM_GAIN.values)
        np.testing.assert_array_equal(o[S.FLAG].values, k[S.FLAG].values)
        np.testing.assert_array_equal(o[S.SOURCE_ROW].values, k[S.SOURCE_ROW].values)


@need_sim
def test_channel_counts_may_differ_by_spw(tmp_path):
    """A B table over windows with different channel counts (the usual VLA
    case): the parameter column's cells then differ in shape by row."""
    tb = _casatools()
    dst = str(tmp_path / "varchan.B")
    shutil.copytree(os.path.join(SIM, "sim.B"), dst)
    tb.open(dst, nomodify=False)
    rows = np.flatnonzero(tb.getcol("SPECTRAL_WINDOW_ID") == 1)
    kept = {}
    for r in rows:
        for c in ("CPARAM", "PARAMERR", "FLAG", "SNR"):
            v = tb.getcell(c, int(r))[:, :16]
            tb.putcell(c, int(r), v)
            kept[(c, int(r))] = v
    tb.close()
    tb.open(os.path.join(dst, "SPECTRAL_WINDOW"), nomodify=False)
    freq = tb.getcell("CHAN_FREQ", 1)[:16]
    tb.putcell("CHAN_FREQ", 1, freq); tb.putcell("NUM_CHAN", 1, 16)
    tb.close()
    dt = open_caltab(dst)
    assert dt["spw_0"].ds.sizes[S.FREQUENCY] == 32 and dt["spw_1"].ds.sizes[S.FREQUENCY] == 16
    ds = dt["spw_1"].ds
    np.testing.assert_array_equal(ds.frequency.values, freq)
    src = ds[S.SOURCE_ROW].values
    for ti, ai in zip(*np.nonzero(src >= 0)):
        np.testing.assert_array_equal(ds.CALPARAM_GAIN.values[0, ti, ai].T, kept[("CPARAM", int(src[ti, ai]))])


@need_sim
def test_zarr_round_trip(tmp_path):
    """The tree goes to Zarr and back unchanged: a CASA table to a
    calibration set is ``open_datatree(...).to_zarr(...)``."""
    pytest.importorskip("zarr")
    for name in ("sim.fringe", "sim.B"):
        dt = open_caltab(os.path.join(SIM, name))
        store = str(tmp_path / f"{name}.zarr")
        dt.to_zarr(store)
        back = xr.open_datatree(store, engine="zarr").load()
        assert back.attrs["schema_draft"] == S.SCHEMA_DRAFT
        for node in dt.children:
            xr.testing.assert_identical(back[node].to_dataset(), dt[node].to_dataset())
