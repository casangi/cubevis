"""Real calibration tables from the CASA VLBA tutorial data (TL016B), for calplot.

Follows the calibration of the CASA guide "VLBA Basic Phase-referencing
Calibration and Imaging" (casaguides.nrao.edu; the steps as copied in
https://wiki.ira.inaf.it/index.php/Tutorial), stopping before imaging.
Between them its tables are every type calplot's C0 starts with:

    tl016b.accor   G Jones  amplitude (accor: autocorrelation scaling)
    tl016b.tsys    B TSYS   system temperature per antenna, SPW, time   (gencal tsys)
    tl016b.gc      gain curve, if the MS has a GAIN_CURVE table         (gencal gc)
    tl016b.sbd     Fringe Jones  instrumental delay, one scan            (fringefit)
    tl016b.gff     Fringe Jones  phase, delay, rate every 30 s           (fringefit)
    tl016b.bpass   B Jones  bandpass on the fringe finder                (bandpass)
    cal1.dcal      K Jones  residual delay on the phase calibrator       (gaincal K)
    cal1.pcal      G Jones  phase every 20 s                             (gaincal G p)
    cal1.apcal     G Jones  amplitude and phase per scan                 (gaincal G ap)

Input: the tutorial's FITS-IDI file (TL016B.idifits, 3.8 GB, from the guide
or the NRAO archive), the MS made from it, or a subset split from that MS
(DATA only is fine).  Needs casatasks and CASA's measures data.

    python devel/tools/calplot/tl016b_caltables.py INPUT OUTDIR [--refant FD]

It works on a copy of the MS in OUTDIR (applycal writes CORRECTED_DATA), so
INPUT is left as it is.  At the end, OUTDIR/layout.txt lists each table's
columns, shapes and keywords, and OUTDIR/caltables.tgz holds the tables
alone (small; the MS copies are not in it).

Unlike the guide, the fringe finder's scan for the instrumental delay is
found from the data (its first scan, the middle minute), so a subset with
other times works, and the channel edges dropped at the split are 5 % of
the SPW's channels.  The guide flags the data before this; without flags
a few solutions will be noisy, which is fine for calplot.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tarfile

FRINGE_FINDER = "4C39.25"
PHASE_CAL = "J1154+6022"
TARGET = "J1203+6031"
REFANTS = "FD,PT,LA,KP,OV,NL,HN"      # the guide's list for the global fringe fit


def log(msg: str) -> None:
    print(f"[tl016b] {msg}", flush=True)


def as_ms(src: str, out: str) -> str:
    """A working MS in *out*: imported from FITS-IDI, or copied."""
    ms = os.path.join(out, "tl016b.ms")
    if os.path.exists(ms):
        log(f"using existing {ms}")
        return ms
    if os.path.isdir(src):
        log(f"copying {src} -> {ms}")
        shutil.copytree(src, ms)
        return ms
    from casatasks import importfitsidi
    log(f"importing {src} -> {ms}")
    importfitsidi(fitsidifile=[src], vis=ms, constobsid=True, scanreindexgap_s=15.0)
    return ms


def subtables(ms: str) -> set:
    from casatools import table
    tb = table()
    tb.open(ms)
    kw = set(tb.getkeywords())
    tb.close()
    have = set()
    for name in ("SYSCAL", "GAIN_CURVE"):
        if name in kw:
            tb.open(os.path.join(ms, name))
            if tb.nrows():
                have.add(name)
            tb.close()
    return have


def fringe_scan_timerange(ms: str, field: str) -> tuple:
    """The middle minute of *field*'s first scan, as a CASA timerange."""
    from casatools import msmetadata, quanta
    md, qa = msmetadata(), quanta()
    md.open(ms)
    scans = sorted(md.scansforfield(field))
    t = md.timesforscan(int(scans[0]))
    md.close()
    mid = 0.5 * (t.min() + t.max())
    half = min(30.0, 0.5 * (t.max() - t.min()))

    def hms(sec):
        return qa.time(qa.quantity(sec, "s"), form="ymd", prec=8)[0]

    return f"{hms(mid - half)}~{hms(mid + half)}", int(scans[0])


def edge_spw(ms: str) -> str:
    from casatools import msmetadata
    md = msmetadata()
    md.open(ms)
    n = md.nchan(0)
    md.close()
    e = max(1, int(round(0.05 * n)))
    return f"*:{e}~{n - 1 - e}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("input", help="TL016B FITS-IDI file, MS, or a subset MS")
    ap.add_argument("out", help="output directory")
    ap.add_argument("--refant", default="FD")
    ap.add_argument("--refants", default=REFANTS,
                    help="stations to try as reference in the global fringe fit")
    ap.add_argument("--gff-solint", default="30s",
                    help="global fringe fit interval (the guide's 30 s suits 2 s integrations)")
    ap.add_argument("--fringe-finder", default=FRINGE_FINDER)
    ap.add_argument("--phase-cal", default=PHASE_CAL)
    ap.add_argument("--target", default=TARGET)
    a = ap.parse_args()

    import casatasks as ct
    os.makedirs(a.out, exist_ok=True)
    ms = as_ms(a.input, a.out)
    P = lambda name: os.path.join(a.out, name)                  # noqa: E731
    have = subtables(ms)
    log(f"subtables with rows: {sorted(have) or 'none'}")

    tabs, interp = [], []

    def made(path):
        ok = os.path.isdir(path)
        log(("wrote " if ok else "NOT written: ") + os.path.basename(path))
        return ok

    # --- a priori amplitude ------------------------------------------------
    shutil.rmtree(P("tl016b.accor"), ignore_errors=True)
    try:                                   # needs autocorrelations
        ct.accor(vis=ms, caltable=P("tl016b.accor"), solint="30s")
    except Exception as exc:               # noqa: BLE001
        log(f"accor failed ({str(exc)[:80]}); going on without it")
    if made(P("tl016b.accor")):
        tabs.append(P("tl016b.accor")); interp.append("nearest")
    if "SYSCAL" in have:
        shutil.rmtree(P("tl016b.tsys"), ignore_errors=True)
        ct.gencal(vis=ms, caltable=P("tl016b.tsys"), caltype="tsys", uniform=False)
        if made(P("tl016b.tsys")):
            tabs.append(P("tl016b.tsys")); interp.append("nearest,nearest")
    else:
        log("no SYSCAL rows: no Tsys table (use the full MS or the FITS-IDI file)")
    if "GAIN_CURVE" in have:
        shutil.rmtree(P("tl016b.gc"), ignore_errors=True)
        ct.gencal(vis=ms, caltable=P("tl016b.gc"), caltype="gc")
        if made(P("tl016b.gc")):
            tabs.append(P("tl016b.gc")); interp.append("nearest")

    # --- instrumental delay: one minute of the fringe finder --------------
    tr, scan = fringe_scan_timerange(ms, a.fringe_finder)
    log(f"instrumental delay on {a.fringe_finder} scan {scan}, {tr}")
    shutil.rmtree(P("tl016b.sbd"), ignore_errors=True)
    ct.fringefit(vis=ms, caltable=P("tl016b.sbd"), field=a.fringe_finder, timerange=tr,
                 solint="inf", zerorates=True, refant=a.refant, minsnr=10,
                 gaintable=list(tabs), interp=list(interp), parang=True)
    if made(P("tl016b.sbd")):
        tabs.append(P("tl016b.sbd")); interp.append("nearest")

    # --- global fringe fit: phase, delay and rate every 30 s --------------
    shutil.rmtree(P("tl016b.gff"), ignore_errors=True)
    ct.fringefit(vis=ms, caltable=P("tl016b.gff"), field=f"{a.fringe_finder},{a.phase_cal}",
                 solint=a.gff_solint, minsnr=5, zerorates=False, refant=a.refants,
                 gaintable=list(tabs), interp=list(interp), parang=True)
    if made(P("tl016b.gff")):
        tabs.append(P("tl016b.gff")); interp.append("linear")

    # --- bandpass on the fringe finder -------------------------------------
    shutil.rmtree(P("tl016b.bpass"), ignore_errors=True)
    ct.bandpass(vis=ms, caltable=P("tl016b.bpass"), field=a.fringe_finder, solint="inf",
                refant=a.refant, solnorm=True, bandtype="B",
                gaintable=list(tabs), interp=list(interp), parang=True)
    if made(P("tl016b.bpass")):
        tabs.append(P("tl016b.bpass")); interp.append("linear,linear")

    # --- apply, split the calibrators, refine: K, G phase, G amplitude ----
    ct.applycal(vis=ms, gaintable=list(tabs), interp=list(interp), parang=True)
    cal1 = P("tl016b_cal1.ms")
    shutil.rmtree(cal1, ignore_errors=True)
    ct.split(vis=ms, outputvis=cal1, field=f"{a.phase_cal},{a.target}", spw=edge_spw(ms),
             antenna="*&*", datacolumn="corrected")
    for t in ("dcal", "pcal", "apcal"):
        shutil.rmtree(P("cal1." + t), ignore_errors=True)
    ct.gaincal(vis=cal1, field=a.phase_cal, caltable=P("cal1.dcal"), solint="inf",
               refant=a.refant, minblperant=3, gaintype="K", calmode="p", parang=False)
    made(P("cal1.dcal"))
    ct.gaincal(vis=cal1, field=a.phase_cal, caltable=P("cal1.pcal"), solint="20s",
               refant=a.refant, minblperant=3, gaintype="G", calmode="p",
               gaintable=[P("cal1.dcal")], interp=["linear"], parang=False)
    made(P("cal1.pcal"))
    ct.gaincal(vis=cal1, field=a.phase_cal, caltable=P("cal1.apcal"), solint="inf",
               refant=a.refant, minblperant=4, gaintype="G", calmode="ap", solnorm=True,
               gaintable=[P("cal1.dcal"), P("cal1.pcal")], interp=["linear", "linear"],
               parang=False)
    made(P("cal1.apcal"))

    # --- what calplot needs to know, and a small archive ------------------
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from make_caltables import layout
    names = [n for n in ("tl016b.accor", "tl016b.tsys", "tl016b.gc", "tl016b.sbd",
                         "tl016b.gff", "tl016b.bpass", "cal1.dcal", "cal1.pcal", "cal1.apcal")
             if os.path.isdir(P(n))]
    with open(P("layout.txt"), "w") as fh:
        for n in names:
            fh.write(layout(P(n)) + "\n\n")
    with tarfile.open(P("caltables.tgz"), "w:gz") as tf:
        for n in names + ["layout.txt"]:
            tf.add(P(n), arcname=n)
    log(f"{len(names)} tables; {P('layout.txt')}; {P('caltables.tgz')} "
        f"({os.path.getsize(P('caltables.tgz')) / 2**20:.1f} MiB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
