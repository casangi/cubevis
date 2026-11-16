"""``CalTableReader``: the one thing calplot asks about a calibration table
(calplot C0).

    pytest -q tests/manual/calplot/test_calplot_reader.py

Most of this runs on synthetic tables, and on tables written from them
with arcae, so it needs no CASA and no data and runs in the quick tests.

With ``CALTABLES`` (see ``test_caltab.py``) and casatools, what the reader
returns is compared with CASA's reads of real tables, and a K table's
evaluated phase with the phase ``applycal`` applies.
"""
from __future__ import annotations

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

from cubevis.toolbox.calplot import CalInfo, CalSamples, CalTableReader          # noqa: E402
from cubevis.xarray.caltab import (FlagCommitError, open_caltab, synthetic_caltab,   # noqa: E402
                                   synthetic_value, write_caltab)

CALTABLES = os.environ.get("CALTABLES")
SIM = os.path.join(CALTABLES, "sim") if CALTABLES else ""
REAL = os.path.join(CALTABLES, "tl016b") if CALTABLES else ""


def _reader(caltype="G", **kw):
    return CalTableReader.open(synthetic_caltab(caltype, **kw))


def test_import_is_light():
    code = ("import sys, cubevis.toolbox.calplot, cubevis.toolbox.calplot.reader\n"
            "print(','.join(m for m in ('bokeh', 'casatools', 'casatasks', 'arcae') if m in sys.modules))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == ""


def test_the_example_runs():
    """``devel/tools/calplot/caltab_example.py`` is what the document for
    CASA's and xradio's people tells them to run
    (``devel/docs/calplot/casa_caltab_xarray.md``): it must keep working."""
    pytest.importorskip("zarr")
    example = os.path.join(os.path.dirname(__file__), "..", "..", "..", "devel", "tools", "calplot",
                           "caltab_example.py")
    if not os.path.exists(example):
        pytest.skip("the example is not in this checkout")
    out = subprocess.run([sys.executable, example], capture_output=True, text=True, timeout=300)
    assert out.returncode == 0, out.stderr[-1500:]
    for line in ("caltype 'Fringe', 2 spectral window(s)", "read back identical: True",
                 "read back and verified: True", "MK in spw 1 all flagged: True",
                 "MK in spw 1 flagged: 0 %", "KP:  16 points", "+0.6032 rad"):
        assert line in out.stdout, line


# --- what a table is --------------------------------------------------------------------------

def test_info():
    r = _reader("Fringe", nant=5, nspw=3, ntime=8, nscan=4, field_names=["a", "b"], flag_fraction=0.25,
                reference_antenna="A02", seed=1)
    info = r.info()
    assert isinstance(info, CalInfo) and info is r.info()            # worked out once
    assert (info.caltype, info.casa_subtype, info.source_format) == ("Fringe", "Fringe Jones", "casa-caltab")
    assert info.antennas == ("A00", "A01", "A02", "A03", "A04") and info.receptors == ("pol_0", "pol_1")
    assert [w.id for w in info.spws] == [0, 1, 2] and info.spws[1].name == "spw1"
    assert info.spws[0].nchan == 1 and info.spws[0].ntime == 8 and info.spws[0].bandwidth == 64e6
    assert info.fields == ("a", "b") and info.scans == ("1", "2", "3", "4")
    assert info.reference_antenna == "A02"
    assert [(q.name, q.units) for q in info.quantities] == [
        ("phase", "rad"), ("delay", "ns"), ("rate", ""), ("disp_delay", ""), ("snr", "")]
    assert info.evaluations == ()
    assert info.nsolutions == 3 * 8 * 5 * 2 and 0.15 < info.nflagged / info.nsolutions < 0.35
    assert info.can_write_flags is False                             # a tree from numbers: nowhere to write
    t0, t1 = info.time_range
    assert t1 - t0 == 7 * 60.0 + 2.0                                 # 8 times a minute apart; windows 1 s apart


def test_info_by_type():
    g = _reader("G").info()
    assert [q.name for q in g.quantities] == ["amplitude", "phase", "real", "imag", "snr"]
    assert [q.label for q in g.quantities][:2] == ["Gain amplitude", "Gain phase"]
    assert [q.units for q in g.quantities] == ["", "rad", "", "", ""]
    assert _reader("T").info().receptors == ("all",)
    k = _reader("K").info()
    assert k.evaluations == ("phase",) and k.reference_antenna is None
    assert [q.name for q in _reader("Tsys").info().quantities] == ["tsys", "snr"]
    assert _reader("G", reference_antenna="changing").info().reference_antenna == "varies"
    z = _reader("Zeta Jones", npar=3).info()                         # a type the schema does not know
    assert z.caltype == "Zeta Jones" and z.receptors == ("0", "1", "2")
    assert [q.name for q in z.quantities] == ["value", "snr"]
    assert _reader("B", nspw=2, nchan=[32, 8]).info().spws[1].nchan == 8


# --- reading -------------------------------------------------------------------------------------

def test_read_is_flat_arrays_of_the_right_values():
    r = _reader("K", nant=4, nspw=2, ntime=3)
    s = r.read("delay")
    assert isinstance(s, CalSamples) and len(s) == 2 * 3 * 4 * 2
    assert (s.quantity, s.units, s.label) == ("delay", "ns", "Delay")
    for col in ("y", "flag", "time", "frequency", "channel", "antenna", "spw", "receptor", "field",
                "scan", "error"):
        v = getattr(s, col)
        assert type(v) is np.ndarray and v.shape == (len(s),), col   # numpy, nothing of xarray's
    assert s.flag.dtype == bool and s.antenna.dtype == np.int64
    want = synthetic_value("K", "DELAY", s.antenna, s.receptor, 0.0, 0.0).astype(np.float32)
    np.testing.assert_array_equal(s.y, want)                         # each entry is its own antenna's
    assert s.antennas == r.info().antennas and s.receptors == ("pol_0", "pol_1")
    np.testing.assert_array_equal(s.error, (1.0 / (20.0 + s.antenna)).astype(np.float32))
    assert sorted(set(s.spw.tolist())) == [0, 1] and set(s.channel.tolist()) == {0}
    assert set(np.round(s.frequency[s.spw == 1] - s.frequency[s.spw == 0][0]).tolist()) == {128e6}
    cell = s.where(s.antenna == 2)                                   # a grid cell
    assert len(cell) == 2 * 3 * 2 and set(cell.antenna.tolist()) == {2} and cell.antennas == s.antennas


def test_quantities():
    r = _reader("G", nant=3, ntime=5, nspw=1)
    amp, phase, re, im = (r.read(q) for q in ("amplitude", "phase", "real", "imag"))
    np.testing.assert_allclose(amp.y * np.exp(1j * phase.y), re.y + 1j * im.y, rtol=1e-6)
    np.testing.assert_allclose(amp.y, 1 + 0.02 * amp.antenna + 0.01 * amp.receptor, rtol=1e-6)
    assert amp.error is None and phase.units == "rad"                # no error for a complex gain's parts
    assert (phase.y[phase.antenna == 0] == 0).all()                  # the reference antenna
    snr = r.read("snr")
    np.testing.assert_array_equal(snr.y, 20.0 + snr.antenna)
    assert snr.error is None
    b = _reader("B", nant=2, ntime=1, nspw=1, nchan=16).read("amplitude")
    assert len(b) == 2 * 16 * 2 and sorted(set(b.channel.tolist())) == list(range(16))
    z = _reader("Zeta Jones", npar=3, nant=2, ntime=1, nspw=1).read("value")
    np.testing.assert_array_equal(z.y, 100.0 * z.receptor + z.antenna)       # "receptor" numbers the parameter
    with pytest.raises(ValueError, match="there are: amplitude, phase, real, imag, snr"):
        r.read("delay")


def test_selection():
    r = _reader("B", nant=6, nspw=3, ntime=8, nscan=4, nchan=8, field_names=["a", "b"])
    full = r.read("amplitude")
    assert len(full) == 3 * 8 * 6 * 8 * 2

    def n(**sel):
        return len(r.read("amplitude", **sel))
    assert n(antenna="A03") == n(antenna=3) == n(antenna=["A03"]) == len(full) // 6
    assert n(antenna=["A01", 4]) == len(full) // 3
    assert n(spw=1) == n(spw=[1]) == len(full) // 3 and n(spw=[0, 2]) == 2 * len(full) // 3
    assert n(receptor="pol_1") == n(receptor=1) == len(full) // 2
    assert n(field="a") == n(field=0) == len(full) // 2
    assert n(scan="2") == n(scan=2) == len(full) // 4 and n(scan=[1, 4]) == len(full) // 2
    assert n(channel=(2, 5)) == len(full) // 2
    f = full.frequency[(full.spw == 0) & (full.channel == 3)][0]
    assert n(frequency=(f, f)) == len(full) // (3 * 8)               # one channel of one window
    t = np.unique(full.time[full.spw == 0])
    assert n(time=(t[2], t[4]), spw=0) == 3 * len(full) // (3 * 8)
    s = r.read("phase", antenna=["A05", "A00"], spw=2, receptor="pol_0", scan=3, channel=(0, 0))
    assert len(s) == 2 * 2 and set(s.antenna.tolist()) == {0, 5} and set(s.spw.tolist()) == {2}
    assert set(s.scan.tolist()) == {2} and s.scans[2] == "3" and set(s.field.tolist()) == {0}
    assert n(antenna="A00", spw=0, time=(0, 1)) == 0                 # nothing there: empty, not an error
    for bad, match in ((dict(antenna="ea01"), "there are: A00"), (dict(antenna=9), "there are 6"),
                       (dict(spw=7), "there are: \\[0, 1, 2\\]"), (dict(field="c"), "there are: a, b"),
                       (dict(scan=9), "there are: 1, 2, 3, 4"), (dict(receptor="X"), "there are: pol_0, pol_1")):
        with pytest.raises(ValueError, match=match):
            r.read("amplitude", **bad)


def test_no_solution_and_flagged():
    r = _reader("Tsys", ntime=30, missing_fraction=0.4, flag_fraction=0.2, seed=3)
    info = r.info()
    s = r.read("tsys")
    assert len(s) == info.nsolutions < 2 * 30 * 10 * 2               # no entry where there is no solution
    assert not np.isnan(s.y).any()
    assert int(s.flag.sum()) == info.nflagged
    good = r.read("tsys", include_flagged=False)
    assert len(good) == info.nsolutions - info.nflagged and not good.flag.any()


# --- a delay table as the phase it applies ----------------------------------------------------------

def test_evaluate_delay_as_phase():
    r = _reader("K", nant=4, nspw=2, ntime=2)
    e = r.evaluate("phase", nfreq=9, wrap=False)
    assert e.evaluated and (e.quantity, e.units) == ("phase", "rad") and e.error is None
    assert len(e) == 2 * 2 * 4 * 2 * 9
    info = r.info()
    f0 = np.array([w.frequency[0] for w in info.spws])[e.spw]
    delay_ns = synthetic_value("K", "DELAY", e.antenna, e.receptor, 0.0, 0.0) \
        .astype(np.float32).astype(np.float64)                       # as stored: single precision
    np.testing.assert_allclose(e.y, 2 * np.pi * delay_ns * 1e-9 * (e.frequency - f0), rtol=1e-12, atol=1e-12)
    one = e.where((e.antenna == 3) & (e.spw == 0) & (e.receptor == 0) & (e.time == e.time.min()))
    assert one.channel.tolist() == list(range(9))
    np.testing.assert_allclose(one.frequency, f0[0] + np.linspace(-32e6, 32e6, 9))      # the band it stands for
    assert one.y[4] == 0 and np.all(np.diff(one.y) > 0)              # zero in the middle; a slope across
    wrapped = r.evaluate("phase", nfreq=9, antenna=3, spw=0)
    assert np.abs(wrapped.y).max() <= np.pi
    np.testing.assert_allclose(np.exp(1j * wrapped.y), np.exp(1j * e.where((e.antenna == 3) & (e.spw == 0)).y),
                               atol=1e-9)
    mine = r.evaluate("phase", frequencies={0: [1.40e9, 1.41e9]}, spw=0, antenna=1, receptor=0, wrap=False)
    assert len(mine) == 2 * 2 and set(mine.frequency.tolist()) == {1.40e9, 1.41e9}
    with pytest.raises(ValueError, match="nothing to evaluate"):
        _reader("G").evaluate("phase")
    with pytest.raises(ValueError, match="it offers: phase"):
        r.evaluate("amplitude")
    with pytest.raises(ValueError, match="nfreq= or frequencies="):
        r.evaluate("phase", channel=(0, 0))


# --- flags --------------------------------------------------------------------------------------------

def test_flags_set_committed_restored(tmp_path):
    path = write_caltab(synthetic_caltab("G", nant=5, ntime=6, flag_fraction=0.2, seed=2), str(tmp_path / "t.cal"))
    r = CalTableReader.open(path)
    info = r.info()
    assert info.can_write_flags and info.source == path
    before = r.read("amplitude")
    n0 = info.nflagged
    assert r.flag_changes()["to_write"] == 0

    changed = r.set_flags(antenna="A03", spw=1)
    sel = r.read("amplitude", antenna="A03", spw=1)
    assert sel.flag.all() and changed == len(sel) - int(before.where((before.antenna == 3) & (before.spw == 1)).flag.sum())
    assert r.set_flags(antenna="A03", spw=1) == 0                    # already flagged
    cleared = r.set_flags(False, antenna="A00", time=(before.time.min(), before.time.min() + 200))
    assert r.info().nflagged == n0 + changed - cleared               # info follows
    pending = r.flag_changes()
    assert (pending["flagged"], pending["unflagged"]) == (changed, cleared) and pending["written"] == 0
    assert CalTableReader.open(path).info().nflagged == n0           # nothing on disk yet

    report = r.commit_flags()
    assert report["verified"] and (report["flagged"], report["unflagged"]) == (changed, cleared)
    fresh = CalTableReader.open(path).read("amplitude")
    np.testing.assert_array_equal(fresh.flag, r.read("amplitude").flag)
    assert [b["path"] for b in r.flag_backups()] == [report["backup"]]

    r.set_flags(antenna="A01")                                       # not committed ...
    assert r.reload_flags() > 0                                      # ... and dropped
    np.testing.assert_array_equal(r.read("amplitude").flag, fresh.flag)
    r.restore_flags(report["backup"])                                # the table, and the reader with it
    np.testing.assert_array_equal(r.read("amplitude").flag, before.flag)
    np.testing.assert_array_equal(CalTableReader.open(path).read("amplitude").flag, before.flag)
    np.testing.assert_array_equal(r.read("amplitude").y, before.y)   # values untouched throughout


def test_flags_where_there_is_nowhere_to_write():
    r = _reader("G")
    assert r.set_flags(antenna="A02") == 2 * 12 * 2                  # in memory it works
    assert r.read("amplitude", antenna="A02").flag.all()
    with pytest.raises(FlagCommitError):
        r.commit_flags()


# --- where the data come from does not show ---------------------------------------------------------------

def test_same_answers_from_a_tree_a_table_and_zarr(tmp_path):
    """A tree in hand, a CASA table, a Zarr store: the same samples."""
    pytest.importorskip("zarr")
    tree = synthetic_caltab("Fringe", nant=4, ntime=5, flag_fraction=0.2, missing_fraction=0.2, seed=6)
    table = write_caltab(tree, str(tmp_path / "t.cal"))
    store = str(tmp_path / "t.zarr")
    tree.to_zarr(store)
    readers = [CalTableReader.open(tree), CalTableReader.open(table), CalTableReader.open(store)]
    assert [r.info().source_format for r in readers] == ["casa-caltab", "casa-caltab", "zarr"]
    infos = [r.info() for r in readers]
    for name in ("caltype", "antennas", "receptors", "spws", "fields", "scans", "time_range",
                 "reference_antenna", "quantities", "nsolutions", "nflagged"):
        assert getattr(infos[1], name) == getattr(infos[0], name) == getattr(infos[2], name), name
    assert [i.can_write_flags for i in infos] == [False, True, False]
    for q in ("phase", "delay", "rate", "snr"):
        a, b, c = (r.read(q, antenna=["A01", "A03"]) for r in readers)
        for col in CalSamples._COLUMNS:
            np.testing.assert_array_equal(getattr(a, col), getattr(b, col), err_msg=f"{q} {col}")
            np.testing.assert_array_equal(getattr(a, col), getattr(c, col), err_msg=f"{q} {col}")
    readers[2].set_flags(antenna="A01")
    with pytest.raises(FlagCommitError, match="zarr"):               # no committer for Zarr yet
        readers[2].commit_flags()
    assert isinstance(readers[1].to_datatree(), type(tree))
    with pytest.raises(ValueError, match="not a CASA calibration table or a calibration set"):
        CalTableReader.open(str(tmp_path))
    with pytest.raises(Exception):
        CalTableReader.open(os.path.join(table, "ANTENNA"))          # a table, but not a cal table


# --- with CASA --------------------------------------------------------------------------------------------

def _casatools():
    try:
        import casatools
        return casatools.table()
    except Exception as exc:
        pytest.skip(f"casatools is not usable here ({type(exc).__name__})")


@pytest.mark.skipif(not os.path.isdir(os.path.join(REAL, "tl016b.gff")),
                    reason="set CALTABLES; $CALTABLES/tl016b from caltables.tgz")
@pytest.mark.parametrize("table,quantity,par", [("tl016b.gff", "delay", 1), ("tl016b.gff", "rate", 2),
                                                 ("tl016b.tsys", "tsys", 0), ("cal1.dcal", "delay", 0)])
def test_real_samples_are_casas(table, quantity, par):
    """Each sample is the value CASA reads for that time, antenna, window and receptor."""
    tb = _casatools()
    path = os.path.join(REAL, table)
    tb.open(path)
    time, ant, spw = tb.getcol("TIME"), tb.getcol("ANTENNA1"), tb.getcol("SPECTRAL_WINDOW_ID")
    fpar, flag = tb.getcol("FPARAM"), tb.getcol("FLAG")              # (npar, nchan, nrow)
    tb.close()
    tb.open(os.path.join(path, "ANTENNA")); names = list(tb.getcol("NAME")); tb.close()
    r = CalTableReader.open(path)
    s = r.read(quantity)
    nparam = fpar.shape[0] // 2
    assert len(s) == 2 * len(time) == r.info().nsolutions
    row = {(t, a, w): i for i, (t, a, w) in enumerate(zip(time.tolist(), ant.tolist(), spw.tolist()))}
    index = {n: i for i, n in enumerate(names)}
    rows = np.array([row[(t + 3506716800.0, index[s.antennas[a]], w)]
                     for t, a, w in zip(s.time.tolist(), s.antenna.tolist(), s.spw.tolist())])
    np.testing.assert_array_equal(s.y, fpar[s.receptor * nparam + par, 0, rows])
    np.testing.assert_array_equal(s.flag, flag[s.receptor * nparam + par, 0, rows].astype(bool))
    assert sorted(set(rows.tolist())) == list(range(len(time)))      # every row, twice (two receptors)


def test_evaluated_phase_is_what_applycal_applies(tmp_path):
    """Apply the K table alone: the phase CASA takes off each baseline is
    the difference of the two antennas' evaluated phases."""
    if not os.path.isdir(os.path.join(SIM, "sim.ms")):
        pytest.skip("set CALTABLES; $CALTABLES/sim from make_caltables.py")
    tb = _casatools()
    try:
        import casatasks
    except Exception as exc:
        pytest.skip(f"casatasks is not usable here ({type(exc).__name__})")
    ms = str(tmp_path / "sim.ms")
    shutil.copytree(os.path.join(SIM, "sim.ms"), ms)
    cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        casatasks.applycal(vis=ms, gaintable=[os.path.join(SIM, "sim.K")], calwt=False, flagbackup=False)
    finally:
        os.chdir(cwd)
    tb.open(ms)
    data, corrected = tb.getcol("DATA"), tb.getcol("CORRECTED_DATA")         # (corr, chan, row)
    a1, a2, dd = tb.getcol("ANTENNA1"), tb.getcol("ANTENNA2"), tb.getcol("DATA_DESC_ID")
    tb.close()
    tb.open(os.path.join(ms, "SPECTRAL_WINDOW"))
    chan_freq = {s: tb.getcell("CHAN_FREQ", s) for s in range(tb.nrows())}
    tb.close()
    r = CalTableReader.open(os.path.join(SIM, "sim.K"))
    first = r.info().time_range[0]
    e = r.evaluate("phase", frequencies=chan_freq, time=(first, first + 1.5), wrap=False)
    assert len(e)
    checked = 0
    for spw in (0, 1):
        for rec, corr in ((0, 0), (1, 3)):                           # pol 0: RR, pol 1: LL
            def phase(a):
                m = (e.antenna == a) & (e.spw == spw) & (e.receptor == rec)
                return e.y[m][np.argsort(e.channel[m])][:len(chan_freq[spw])]
            for i, j in ((0, 7), (2, 5), (4, 9)):
                row = np.flatnonzero((a1 == i) & (a2 == j) & (dd == spw))[0]
                applied = data[corr, :, row] / corrected[corr, :, row]       # g_i conj(g_j)
                residual = np.angle(applied * np.exp(-1j * (phase(i) - phase(j))))
                assert np.abs(residual).max() < 1e-4, (spw, rec, i, j)
                checked += 1
    assert checked == 12
