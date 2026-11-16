"""Make a small set of CASA calibration tables with known answers (calplot C0).

A cal table's layout (columns, keywords, how FPARAM is packed) is set by
CASA's calibration code, not by the data, so tables solved here from a
simulated MS have the structure real ones have.  The MS is corrupted with
known per-antenna terms, so the solutions can be checked too:

    gain     amplitude 0.8-1.2 and a phase drifting with time   -> G (gaincal)
             the same, one gain for both polarizations          -> T (gaincal T)
    bandpass a smooth amplitude ripple and phase slope per SPW  -> B (bandpass)
    delay    a few ns per antenna                               -> K (gaincal K)
    rate     up to 0.1 mHz phase rate per antenna                  -> Fringe (fringefit)
    Tsys     a SYSCAL subtable written here                     -> Tsys (gencal)

Usage (needs casatools/casatasks and CASA's measures data, e.g.
``pip install cubevis[iclean,visplot] casadata``)::

    python devel/tools/calplot/make_caltables.py OUTDIR

writes ``OUTDIR/sim.ms`` and ``OUTDIR/sim.{G,T,B,K,fringe,tsys}``, plus
``OUTDIR/truth.npz`` (the corruption applied) and ``OUTDIR/layout.txt``
(each table's columns, shapes and keywords: the "schema assumptions" of
the plan's section 10.5).
"""
from __future__ import annotations

import os
import shutil
import sys

import numpy as np

RNG = np.random.default_rng(20261115)
NCHAN = 32
SPWS = (("spw0", "1.40GHz"), ("spw1", "1.52GHz"))
CHAN_WIDTH = 2.0e6          # Hz


def simulate(ms: str) -> None:
    import casatools
    me = casatools.measures()
    cfg = casatools.ctsys.resolve("alma/simmos/vla.c.cfg")
    x, y, z, d, names = [], [], [], [], []
    for line in open(cfg):
        if line.startswith("#") or not line.strip():
            continue
        p = line.split()
        x.append(float(p[0])); y.append(float(p[1])); z.append(float(p[2]))
        d.append(float(p[3])); names.append(p[4])
    # 10 antennas: VLBA-sized, quick to solve
    x, y, z, d, names = x[:10], y[:10], z[:10], d[:10], names[:10]
    sm = casatools.simulator()
    sm.open(ms)
    sm.setconfig(telescopename="VLA", x=x, y=y, z=z, dishdiameter=d, mount=["alt-az"],
                 antname=names, coordsystem="global", referencelocation=me.observatory("VLA"))
    for name, f0 in SPWS:
        sm.setspwindow(spwname=name, freq=f0, deltafreq=f"{CHAN_WIDTH / 1e6}MHz",
                       freqresolution=f"{CHAN_WIDTH / 1e6}MHz", nchannels=NCHAN,
                       stokes="RR RL LR LL")
    sm.setfield(sourcename="J1200+3400", sourcedirection=me.direction("J2000", "12h00m00", "34d00m00"),
                calcode="C")
    sm.setlimits(shadowlimit=0.001, elevationlimit="8.0deg")
    sm.setauto(autocorrwt=0.0)
    sm.settimes(integrationtime="30s", usehourangle=True,
                referencetime=me.epoch("UTC", "2026/11/15/00:00:00"))
    # four 10-minute scans in each SPW
    for k in range(4):
        t0 = -60 + k * 30
        for name, _ in SPWS:
            sm.observe("J1200+3400", name, starttime=f"{t0}min", stoptime=f"{t0 + 10}min")
    cl = casatools.componentlist()
    cl_path = ms + ".cl"
    shutil.rmtree(cl_path, ignore_errors=True)
    cl.addcomponent(dir="J2000 12h00m00 34d00m00", flux=[1.0, 0, 0, 0], freq="1.4GHz",
                    shape="point")
    cl.rename(cl_path)
    cl.close()
    sm.predict(complist=cl_path)
    sm.close()
    shutil.rmtree(cl_path, ignore_errors=True)


def corrupt(ms: str) -> dict:
    """Multiply DATA by g_i g_j^* with known per-antenna terms; add noise."""
    import casatools
    tb = casatools.table()
    tb.open(ms + "/ANTENNA"); nant = tb.nrows(); tb.close()
    tb.open(ms + "/SPECTRAL_WINDOW")
    freqs = [tb.getcell("CHAN_FREQ", s) for s in range(tb.nrows())]
    tb.close()
    truth = {
        "amp": RNG.uniform(0.8, 1.2, (nant, 2)),              # per antenna, per pol
        "phase0": RNG.uniform(-np.pi, np.pi, (nant, 2)),
        "phase_drift": RNG.uniform(-0.5, 0.5, (nant, 2)),     # rad / hour
        "delay_ns": RNG.uniform(-5, 5, (nant, 2)),
        "rate_mhz": RNG.uniform(-0.1, 0.1, (nant,)),          # mHz, both pols: < 0.4 rad a scan
        "bp_ripple": RNG.uniform(0.02, 0.1, (nant, 2)),
    }
    truth["amp"][0] = truth["phase0"][0] = truth["phase_drift"][0] = 0.0
    truth["amp"][0] = 1.0                                       # the reference antenna
    truth["delay_ns"][0] = 0.0; truth["rate_mhz"][0] = 0.0
    tb.open(ms, nomodify=False)
    t_all = tb.getcol("TIME"); tref = t_all.min()
    for spw, f in enumerate(freqs):
        sub = tb.query(f"DATA_DESC_ID=={spw}")
        a1, a2, t = sub.getcol("ANTENNA1"), sub.getcol("ANTENNA2"), sub.getcol("TIME")
        data = sub.getcol("DATA")                                # (4, nchan, nrow)
        hrs = (t - tref) / 3600.0
        fr = (f - f[0])[:, None]                                 # (nchan, 1)
        x = np.linspace(-1, 1, len(f))[:, None]

        def g(ant, pol):
            amp = truth["amp"][ant, pol] * (1 + truth["bp_ripple"][ant, pol] * np.cos(3 * np.pi * x))
            ph = (truth["phase0"][ant, pol] + truth["phase_drift"][ant, pol] * hrs[None, :]
                  + 2 * np.pi * truth["delay_ns"][ant, pol] * 1e-9 * fr
                  + 2 * np.pi * truth["rate_mhz"][ant] * 1e-3 * (t - tref)[None, :]
                  + 0.3 * x * (ant / nant))                      # bandpass phase slope
            return amp * np.exp(1j * ph)                         # (nchan, nrow)

        # every antenna's gain on this SPW's rows, once: (nant, 2, nchan, nrow)
        gall = np.array([[g(a, p) for p in (0, 1)] for a in range(nant)])
        rows = np.arange(len(t))
        for c, (p, q) in enumerate(((0, 0), (0, 1), (1, 0), (1, 1))):
            gi = gall[a1, p, :, rows].T                          # (nchan, nrow)
            gj = gall[a2, q, :, rows].T
            data[c] = data[c] * gi * np.conj(gj)
        noise = 0.02 * (RNG.standard_normal(data.shape) + 1j * RNG.standard_normal(data.shape))
        sub.putcol("DATA", (data + noise).astype(np.complex64))
        sub.close()
    tb.close()
    return truth


def add_syscal(ms: str) -> dict:
    """A SYSCAL subtable with TSYS per antenna, SPW, scan time and pol."""
    import casatools
    tb = casatools.table()
    tb.open(ms + "/ANTENNA"); nant = tb.nrows(); tb.close()
    tb.open(ms)
    times = np.unique(tb.getcol("TIME")); ddid = np.unique(tb.getcol("DATA_DESC_ID"))
    tb.close()
    # one Tsys a minute
    tt = times[::2]
    desc = {
        "ANTENNA_ID": {"valueType": "int"}, "FEED_ID": {"valueType": "int"},
        "SPECTRAL_WINDOW_ID": {"valueType": "int"}, "TIME": {"valueType": "double"},
        "INTERVAL": {"valueType": "double"},
        "TSYS": {"valueType": "float", "ndim": 1},
        "TSYS_FLAG": {"valueType": "boolean"},
    }
    for c in desc.values():
        c.update({"dataManagerType": "StandardStMan", "dataManagerGroup": "StandardStMan",
                  "option": 0, "maxlen": 0, "comment": "", "keywords": {}})
    desc["TIME"]["keywords"] = {"QuantumUnits": ["s"], "MEASINFO": {"type": "epoch", "Ref": "UTC"}}
    desc["INTERVAL"]["keywords"] = {"QuantumUnits": ["s"]}
    desc["TSYS"]["keywords"] = {"QuantumUnits": ["K"]}
    path = ms + "/SYSCAL"
    shutil.rmtree(path, ignore_errors=True)
    tb.create(path, desc, nrow=0)
    base = RNG.uniform(25, 45, (nant, len(ddid), 2))
    rows = []
    for t in tt:
        hrs = (t - times[0]) / 3600.0
        for a in range(nant):
            for s in range(len(ddid)):
                ts = base[a, s] * (1 + 0.1 * np.sin(2 * np.pi * hrs + a))
                rows.append((a, s, t, ts))
    tb.addrows(len(rows))
    tb.putcol("ANTENNA_ID", np.array([r[0] for r in rows], np.int32))
    tb.putcol("FEED_ID", np.zeros(len(rows), np.int32))
    tb.putcol("SPECTRAL_WINDOW_ID", np.array([r[1] for r in rows], np.int32))
    tb.putcol("TIME", np.array([r[2] for r in rows]))
    tb.putcol("INTERVAL", np.full(len(rows), 60.0))
    tb.putcol("TSYS", np.array([r[3] for r in rows], np.float32).T)
    tb.putcol("TSYS_FLAG", np.zeros(len(rows), bool))
    tb.close()
    tb.open(ms, nomodify=False)
    tb.putkeyword("SYSCAL", "Table: " + os.path.abspath(path))
    tb.close()
    return {"tsys_base": base}


def solve(ms: str, out: str) -> dict:
    import casatasks as ct
    refant = "0"
    tabs = {k: os.path.join(out, "sim." + k) for k in ("K", "B", "G", "T", "fringe", "tsys")}
    for p in tabs.values():
        shutil.rmtree(p, ignore_errors=True)
    # per scan: the phase drifts and rates do not average out within one
    ct.gaincal(vis=ms, caltable=tabs["K"], gaintype="K", solint="inf", refant=refant)
    ct.bandpass(vis=ms, caltable=tabs["B"], solint="inf", refant=refant,
                gaintable=[tabs["K"]], solnorm=True)
    ct.gaincal(vis=ms, caltable=tabs["G"], gaintype="G", calmode="ap", solint="2min",
               refant=refant, gaintable=[tabs["K"], tabs["B"]])
    # one gain for both polarizations: a table with one receptor (npar = 1)
    ct.gaincal(vis=ms, caltable=tabs["T"], gaintype="T", calmode="ap", solint="2min",
               refant=refant, gaintable=[tabs["K"], tabs["B"]])
    ct.fringefit(vis=ms, caltable=tabs["fringe"], solint="inf", refant=refant,
                 zerorates=False, globalsolve=True)
    ct.gencal(vis=ms, caltable=tabs["tsys"], caltype="tsys")
    return tabs


def layout(path: str) -> str:
    """One table's columns, shapes, keywords and subtables."""
    import casatools
    tb = casatools.table()
    tb.open(path)
    lines = [f"== {os.path.basename(path)}: {tb.nrows()} rows",
             f"   table.info: {tb.info()}"]
    kw = tb.getkeywords()
    lines.append("   keywords: " + ", ".join(
        f"{k}={v if not isinstance(v, str) or len(v) < 60 else v[:57] + '...'}"
        for k, v in kw.items()))
    for c in tb.colnames():
        cd = tb.getcoldesc(c)
        shape = ""
        try:
            if tb.nrows() and cd.get("ndim", 0) != 0:
                shape = str(tb.getcell(c, 0).shape)
        except Exception:                                        # noqa: BLE001
            shape = "(no cell)"
        ck = tb.getcolkeywords(c)
        lines.append(f"   {c:<20} {cd['valueType']:<8} {shape:<10} "
                     + (str({k: ck[k] for k in ck if k in ('QuantumUnits', 'MEASINFO')}) if ck else ""))
    tb.close()
    return "\n".join(lines)


def main(out: str) -> None:
    os.makedirs(out, exist_ok=True)
    ms = os.path.join(out, "sim.ms")
    shutil.rmtree(ms, ignore_errors=True)
    simulate(ms)
    truth = corrupt(ms)
    truth.update(add_syscal(ms))
    np.savez(os.path.join(out, "truth.npz"), **truth)
    tabs = solve(ms, out)
    with open(os.path.join(out, "layout.txt"), "w") as fh:
        for p in tabs.values():
            fh.write(layout(p) + "\n\n")
    print("\n".join(tabs.values()))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "/tmp/caltables")
