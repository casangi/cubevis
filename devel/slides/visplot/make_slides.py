"""Generate the visplot overview slides (16:9 SVG) in the architecture slides' visual style.

Usage:
    python make_slides.py               # write the SVGs and slides.html next to this script
    python make_slides.py --png         # also write 2x PNGs (for Google Slides)
    python make_slides.py --png --scale 3

PNG output needs cairosvg (pip install cairosvg).

A slide can later be replaced by, or joined by, a screenshot: list a PNG in
EXTRA_IMAGES below and it is placed in the slideshow after the slide named.
"""
import argparse
import json
import random
from pathlib import Path
from xml.sax.saxutils import escape

OUT = Path(__file__).resolve().parent
WRITTEN = []          # (file name, title) in slideshow order
W, H = 1600, 900
FONT = "Helvetica, Arial, sans-serif"
MONO = "monospace"
FOOT = "visplot test release · October 2026"
# Glyphs Helvetica lacks (φ ▶ ◀ ↺ ✓) are drawn in a font that has them.
SYMBOL_FONT = "DejaVu Sans, Arial Unicode MS, sans-serif"
SAFE = set("—–…•·×’“”")


def needs_symbols(s):
    return any(ord(ch) > 0xFF and ch not in SAFE for ch in s)


# Same palette as ../architecture/make_diagrams.py
INK, SUB, DIM = "#1C2733", "#4A5563", "#5A6573"
COMP_FILL, COMP_STROKE = "#EEF2F7", "#A9B8CA"
BLUE, ORANGE, PURPLE, GREEN, TEAL, RED = "#3B6EA8", "#C9711F", "#6A4C93", "#2E7D5B", "#0F8B8D", "#C0392B"

# Screenshots to splice into the slideshow, e.g.
#   {"after": "visplot_2_window.svg", "src": "window.png", "title": "The window (screenshot)"}
EXTRA_IMAGES = []


def wrap(s, width, size):
    """Greedy word wrap using an average Helvetica glyph width."""
    per = max(1, int(width / (size * 0.53)))
    lines, cur = [], ""
    for word in s.split():
        if cur and len(cur) + 1 + len(word) > per:
            lines.append(cur)
            cur = word
        else:
            cur = f"{cur} {word}" if cur else word
    if cur:
        lines.append(cur)
    return lines


class Slide:
    def __init__(self, title, subtitle):
        self.parts = []
        self.title, self.subtitle = title, subtitle

    def add(self, s):
        self.parts.append(s)

    def text(self, x, y, s, size=20, weight="normal", fill="#222", anchor="start", family=FONT, extra=""):
        if family == FONT and needs_symbols(s):
            family = SYMBOL_FONT  # one font per element: cairosvg misplaces anchored <tspan>s
        self.add(f'<text x="{x}" y="{y}" font-family="{family}" font-size="{size}" font-weight="{weight}" '
                 f'fill="{fill}" text-anchor="{anchor}" {extra} xml:space="preserve">{escape(s)}</text>')

    def para(self, x, y, s, width, size=18, fill="#222", lead=1.32, weight="normal"):
        """Wrapped paragraph; returns the y below it."""
        for line in wrap(s, width, size):
            self.text(x, y, line, size=size, fill=fill, weight=weight)
            y += size * lead
        return y

    def bullets(self, x, y, items, width, size=18, fill="#222", mark="•", mark_fill=None, gap=8):
        for it in items:
            self.text(x, y, mark, size=size, fill=mark_fill or fill, weight="bold")
            y = self.para(x + 22, y, it, width - 22, size=size, fill=fill) + gap
        return y

    def rect(self, x, y, w, h, fill="white", stroke=COMP_STROKE, sw=1.5, rx=10, extra=""):
        self.add(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" fill="{fill}" '
                 f'stroke="{stroke}" stroke-width="{sw}" {extra}/>')

    def card(self, x, y, w, h, title, colour, sub=None):
        """White card with a coloured header band, like a process box on the architecture slides."""
        self.rect(x, y, w, h, stroke=colour, sw=2.5)
        self.add(f'<path d="M{x} {y+44} V{y+10} a10 10 0 0 1 10 -10 H{x+w-10} a10 10 0 0 1 10 10 V{y+44} Z" fill="{colour}"/>')
        self.text(x + 18, y + 30, title, size=21, weight="bold", fill="white")
        if sub:
            self.text(x + w - 18, y + 30, sub, size=16, fill="white", anchor="end")

    def pill(self, x, y, s, colour, size=15, anchor="start"):
        w = len(s) * size * 0.58 + 22
        x0 = x - w if anchor == "end" else x
        self.rect(x0, y, w, size + 13, fill=colour, stroke=colour, rx=(size + 13) / 2)
        self.text(x0 + w / 2, y + size + 2, s, size=size, weight="bold", fill="white", anchor="middle")
        return w

    def render(self, footnote=FOOT):
        head = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="100%" height="100%">',
                f'<rect width="{W}" height="{H}" fill="white"/>']
        self.parts = head + self.parts
        self.text(40, 62, self.title, size=40, weight="bold", fill=INK)
        self.text(40, 102, self.subtitle, size=21, fill=SUB)
        if footnote:
            self.text(W - 40, 885, footnote, size=15, fill="#8A94A0", anchor="end", extra='font-style="italic"')
        return "\n".join(self.parts + ["</svg>"])


def save(name, slide, footnote=FOOT):
    (OUT / name).write_text(slide.render(footnote), encoding="utf-8")
    WRITTEN.append((name, slide.title))


# ---- Slide 1: at a glance -------------------------------------------------
s = Slide("visplot at a glance",
          "One tool for what plotms and msview do today, built for commissioning and data inspection.")
rows = [
    ("Reads", BLUE, "MSv2 measurement sets through xarray-ms, and MSv4 / Processing Sets through xarray's own Zarr engine. "
                    "vis= detects which, as in any CASA task. Data column DATA, CORRECTED or MODEL."),
    ("Shows", ORANGE, "One plot or two (side by side, or one over the other), each a raster (a 2-D image over Baseline, Time, Channel or Antenna × Antenna) "
                      "or a scatter (Amplitude, Phase, Real, Imaginary, Phase RMS … against Time, Frequency, "
                      "Channel or UV distance). Datashader aggregates every sample: nothing is decimated."),
    ("Runs", PURPLE, "From ipython or a CASA6 prompt, with the window in a browser. Data access runs locally, "
                     "or in a remote kernel on the machine that holds the data, so large data sets need not move."),
    ("Does", GREEN, "Twelve preset views modelled on AIPS and CASA tasks, CASA-syntax selection, box flagging "
                    "with undo and write-back, PNG export (also without a window), and a summary of every plot "
                    "with the call that reproduces it."),
]
y = 140
for lab, col, body in rows:
    s.rect(40, y, 900, 155, stroke=COMP_STROKE)
    s.add(f'<path d="M40 {y+10} a10 10 0 0 1 10 -10 H170 V{y+155} H50 a10 10 0 0 1 -10 -10 Z" fill="{col}"/>')
    s.text(105, y + 85, lab, size=24, weight="bold", fill="white", anchor="middle")
    lines = wrap(body, 730, 19)
    y0 = y + 78 - (len(lines) - 1) * 25 / 2 + 6
    s.para(195, y0, body, 730, size=19, lead=1.32)
    y += 170

s.card(980, 140, 580, 330, "One call, CASA-style", INK)
code = ['from cubevis import visplot', '',
        'visplot(vis="twhya_selfcal_contsub_ms",',
        '        field="5", spw="0",',
        '        correlation="XX,YY",',
        '        preset="vplot")',
        '', '# or plotfile="out.png" for a PNG',
        '# and no window']
s.rect(1000, 200, 540, 250, fill="#1C2733", stroke="#1C2733", rx=8)
for i, ln in enumerate(code):
    col = "#8FA3B8" if ln.startswith("#") else "#E6EDF3"
    s.text(1020, 235 + i * 24, ln, size=17, fill=col, family=MONO)

s.card(980, 490, 580, 330, "Where it stands", TEAL)
s.bullets(1002, 568, [
    "Test release: astronomers are trying it now, guided by the user page on Read the Docs.",
    "Exercised on ALMA (TW Hya) and VLBA data, MSv2 and MSv4.",
    "Built for plotms parity plus commissioning needs: waterfalls, phase RMS, AIPS-style flagging.",
    "How it is deployed: see the architecture slides.",
], 535, size=19, mark_fill=TEAL, gap=14)
save("visplot_1_overview.svg", s)


# ---- Slide 2: the window --------------------------------------------------
s = Slide("The window",
          "Toolbar of preset views on top, data selection on the left, two linked panels, help for the control under the pointer.")
FX, FY, FW, FH = 40, 140, 1050, 690
s.rect(FX, FY, FW, FH, fill="#1E242B", stroke="#1E242B", rx=14)
# toolbar
bx = FX + 20
for lab, w, c in [("Plot ▶", 80, "#3E9B5C"), ("Reload ↺", 92, "#3A424C"), ("One", 56, "#3A424C"),
                  ("Side by Side", 112, "#59616B"), ("Over / Under", 112, "#3A424C")]:
    s.rect(bx, FY + 18, w, 34, fill=c, stroke=c, rx=6)
    s.text(bx + w / 2, FY + 41, lab, size=14, fill="white", anchor="middle")
    bx += w + 8
bx += 14
for lab in ["vplot", "radplot", "Waterfall", "φrms·t", "…", "Spectrum"]:
    w = len(lab) * 8.5 + 20
    s.rect(bx, FY + 18, w, 34, fill="#F3F5F8", stroke="#F3F5F8", rx=6)
    s.text(bx + w / 2, FY + 41, lab, size=14, fill=INK, anchor="middle")
    bx += w + 6
# sidebar
SX, SY, SW = FX + 20, FY + 70, 210
s.rect(SX, SY, SW, 525, fill="#262D35", stroke="#262D35", rx=6)
yy = SY + 30
for sec, fields in [("Data", ["Data column", "Field  ◀ ▶", "SPW  ◀ ▶", "Correlation"]),
                    ("", ["Antenna / Baseline ◀ ▶", "Scan", "Time range", "UV range"]),
                    ("Panels", ["Gear tab: axes, quantity,", "averaging, colormap"]),
                    ("Flagging", ["Filters, reaches, undo,", "write to MS"])]:
    if sec:
        s.text(SX + 14, yy, sec, size=15, weight="bold", fill="#C9D3DD")
        yy += 22
    for f in fields:
        s.rect(SX + 12, yy - 15, SW - 24, 24, fill="#333B45", stroke="#333B45", rx=4)
        s.text(SX + 20, yy + 2, f, size=13, fill="#AEB8C3")
        yy += 30
    yy += 8
# panel A: raster
rng = random.Random(7)
PAL = ["#0D0887", "#41049D", "#6A00A8", "#8F0DA4", "#B12A90", "#CC4778", "#E16462", "#F2844B", "#FCA636", "#FCCE25", "#F0F921"]
AX, AY, AW, AH = FX + 255, FY + 100, 370, 430
s.text(AX, AY - 10, "Panel A — raster: Amplitude [Baseline × Time]", size=14, weight="bold", fill="#DDE3EA")
nx, ny = 30, 34
cw, ch = AW / nx, AH / ny
bad_col, bad_rows = {7, 19}, set(range(22, 25))
for i in range(nx):
    base = rng.uniform(0.25, 0.55)
    for j in range(ny):
        v = base + rng.gauss(0, 0.08)
        if i in bad_col:
            v = 0.05 + rng.uniform(0, 0.08)
        if j in bad_rows:
            v = 0.9 + rng.uniform(0, 0.1)
        if 12 <= j <= 13:
            continue  # scan gap
        c = PAL[max(0, min(10, int(v * 10)))]
        s.add(f'<rect x="{AX + i*cw:.1f}" y="{AY + j*ch:.1f}" width="{cw+0.4:.1f}" height="{ch+0.4:.1f}" fill="{c}"/>')
s.rect(AX, AY, AW, AH, fill="none", stroke="#59616B", rx=0)
s.text(AX + AW / 2, AY + AH + 24, "Baseline", size=13, fill="#AEB8C3", anchor="middle")
s.text(AX - 12, AY + AH / 2, "Time", size=13, fill="#AEB8C3", anchor="middle",
       extra=f'transform="rotate(-90 {AX-12} {AY+AH/2})"')
# panel B: scatter
BX, BY, BW, BH = AX + AW + 40, AY, 350, AH
s.text(BX, BY - 10, "Panel B — scatter: Amplitude vs UV distance", size=14, weight="bold", fill="#DDE3EA")
s.rect(BX, BY, BW, BH, fill="#14181D", stroke="#59616B", rx=0)
for k in range(1600):
    u = rng.random()
    env = 0.85 - 0.55 * u
    a = rng.random() * env
    c = PAL[min(10, 4 + int(a * 8))]
    s.add(f'<circle cx="{BX + 10 + u*(BW-20):.1f}" cy="{BY + BH - 10 - a*(BH-20):.1f}" r="1.3" fill="{c}" opacity="0.75"/>')
s.text(BX + BW / 2, BY + BH + 24, "UV distance", size=13, fill="#AEB8C3", anchor="middle")
# readout + status
s.rect(AX, AY + AH + 38, AW + 40 + BW, 26, fill="#262D35", stroke="#262D35", rx=4)
s.text(AX + 10, AY + AH + 56, "Amplitude 8.31 | Baseline DA48&DV02 | Time 08:46:34 | Scan 24 | Field TW Hya",
       size=13, fill="#AEB8C3", family=MONO)
s.rect(FX + 20, FY + FH - 50, FW - 40, 34, fill="#262D35", stroke="#262D35", rx=4)
s.text(FX + 34, FY + FH - 28, "Status: what is selected, what is pending, and help for the control under the pointer",
       size=14, fill="#8FD19E", family=MONO)


def marker(x, y, n):
    s.add(f'<circle cx="{x}" cy="{y}" r="15" fill="{ORANGE}" stroke="white" stroke-width="2.5"/>')
    s.text(x, y + 6, str(n), size=17, weight="bold", fill="white", anchor="middle")


marks = [(FX + 1012, FY + 35), (SX + SW - 6, SY + 8), (AX + AW - 10, AY + 20),
         (BX + BW - 10, BY + 20), (AX + AW + 40 + BW - 14, AY + AH + 51), (FX + FW - 30, FY + FH - 33)]
for n, (x, y) in enumerate(marks, 1):
    marker(x, y, n)
notes = [
    ("Presets", "One button sets both panels and plots: vplot, radplot, waterfalls, phase RMS, Ant×Ant, Spectrum …"),
    ("Selection", "CASA syntax for field, spw, antenna, scan, timerange, uvrange; Prev / Next buttons step through fields, SPWs, antennas, baselines."),
    ("Raster", "Every sample binned into cells; scan and SPW gaps stay blank. Pan and zoom resample the cached image."),
    ("Scatter", "Several correlations or quantities as layers; colour by axis (antenna, scan, field …) or by Z-Score."),
    ("Readout", "Names the cell or point under the cursor: value, baseline, time, scan, field."),
    ("Status", "Says what is drawn and what is not yet; the Plot button turns orange until changes are plotted."),
]
y = 160
for n, (h, body) in enumerate(notes, 1):
    s.add(f'<circle cx="{1130}" cy="{y-6}" r="14" fill="{ORANGE}"/>')
    s.text(1130, y, str(n), size=16, weight="bold", fill="white", anchor="middle")
    s.text(1156, y, h, size=19, weight="bold", fill=INK)
    y = s.para(1156, y + 25, body, 405, size=16, fill=SUB, lead=1.3) + 16
save("visplot_2_window.svg", s, "Schematic, not a screenshot · " + FOOT)


# ---- Slide 3: presets -----------------------------------------------------
s = Slide("Predefined views",
          "Each preset sets both panels at once; the AIPS and CASA columns name the closest existing view.")
cols = [("Preset", 40, 150), ("Panels", 190, 470), ("Used for", 660, 455), ("AIPS", 1115, 195), ("CASA", 1310, 250)]
s.rect(40, 135, 1520, 40, fill=INK, stroke=INK, rx=6)
for lab, x, w in cols:
    s.text(x + 14, 162, lab, size=18, weight="bold", fill="white")
presets = [
    ("vplot", "Raster Baseline × Time amplitude · scatter Amplitude vs Time", "First look: dropouts, bad scans, bad baselines", "VPLOT, TVFLG", "plotms amp vs time"),
    ("radplot", "Same raster · scatter Amplitude vs UV distance", "Source structure: flat for a point source, falling for a resolved one", "UVPLT", "plotms amp vs uvdist"),
    ("Waterfall", "Raster Time × Channel amplitude, one baseline · Amplitude vs Time", "Interference narrow in frequency or short in time", "SPFLG", "msview raster"),
    ("All-BL", "Raster Time × Channel, largest amplitude over all baselines · Amplitude vs Channel", "Interference common to many baselines", "FTFLG", "—"),
    ("Z-Score", "Raster Baseline × Time coloured by Z-Score · Amplitude vs Time", "Samples unusual for their own baseline", "—", "—"),
    ("φrms·t", "Phase RMS: raster Baseline × Time · scatter vs Time", "Phase stability across the band, per integration", "—", "—"),
    ("φrms·ν", "Phase RMS: raster Baseline × Channel · scatter vs Channel", "Phase stability per channel; band edges", "—", "—"),
    ("φrms·uv", "Phase RMS raster · scatter vs UV distance", "Atmosphere (rises with length) or one bad antenna", "—", "—"),
    ("φ Wfall", "Raster Time × Channel phase, cyclic colormap · Phase vs Time", "Delay (stripes across the band) or rate (stripes in time)", "SPFLG (phase)", "msview raster"),
    ("Ant×Ant", "Antenna × antenna matrix · Amplitude vs UV distance", "An antenna bad on every baseline: a whole row and column", "LISTR matrix", "—"),
    ("Spectrum", "Amplitude over Phase vs Frequency, scan-averaged", "Bandpass shape, edge channels, phase slope", "POSSM", "plotms amp/phase vs freq"),
    ("Time series", "Amplitude over Phase vs Time, channels averaged", "Phase drifts and jumps, amplitude dropouts", "VPLOT", "plotms amp/phase vs time"),
]
y = 178
RH = 53
for k, row in enumerate(presets):
    if k % 2 == 0:
        s.rect(40, y, 1520, RH, fill="#F3F5F8", stroke="none", rx=0)
    for (lab, x, w), val in zip(cols, row):
        if lab == "Preset":
            pw = len(val) * 9 + 22
            s.rect(x + 12, y + 12, pw, 30, fill=COMP_FILL, stroke=COMP_STROKE, rx=6)
            s.text(x + 12 + pw / 2, y + 33, val, size=16, weight="bold", fill=INK, anchor="middle")
            continue
        lines = wrap(val, w - 26, 16)
        ty = y + RH / 2 + 6 - (len(lines) - 1) * 10
        colr = INK if lab in ("Panels", "Used for") else (ORANGE if lab == "AIPS" else BLUE)
        if val == "—":
            colr = "#A9B3BE"
        for ln in lines:
            s.text(x + 14, ty, ln, size=16, fill=colr, weight="bold" if lab in ("AIPS", "CASA") and val != "—" else "normal")
            ty += 20
    y += RH
s.text(40, 857, "Raster quantities beyond the presets include TVFLG's Amp V Diff and Phase Diff, Coherence, Real, Imaginary and Flag fraction.",
       size=16, fill=DIM)
save("visplot_3_presets.svg", s)


# ---- Slide 4: workflow ----------------------------------------------------
s = Slide("From survey to written flags",
          "The views are arranged for a first pass over a new observation; nothing reaches the data until it is committed.")
steps = [
    ("1  Survey", BLUE, "Which antennas, baselines and times are wrong?",
     ["Ant×Ant: a bad antenna is a whole row and column", "Z-Score: outliers in time per baseline",
      "Phase RMS vs time and vs UV distance: atmosphere or antenna?", "vplot: dropouts and bad scans",
      "RR / LL or XX / YY side by side: problems in one hand"]),
    ("2  Confirm", PURPLE, "Look closely, one baseline at a time",
     ["Step baseline by baseline, or antenna by antenna", "Amplitude and phase waterfalls: RFI, delay, rate",
      "Spectrum (POSSM) and Time series (VPLOT)", "All-BL (FTFLG): RFI on many baselines",
      "Narrow with Scan, Time range, UV range"]),
    ("3  Flag", ORANGE, "Box on whichever view shows it best",
     ["Flag or unflag boxes on raster and scatter", "Reaches widen a box, as TVFLG's scopes do: antenna, channels, correlations, SPWs, scan, field",
      "Preview each proposal before adding it", "Undo, redo, reason, and a report"]),
    ("4  Commit", GREEN, "Write only when satisfied",
     ["Write to MS saves a backup of the flags it changes first", "Export pending flags as JSON to keep or reapply",
      "Show flagged data to check the result"]),
]
x = 40
SW_, GAP = 370, 13
for lab, col, q, items in steps:
    pts = f"{x},150 {x+SW_-26},150 {x+SW_},195 {x+SW_-26},240 {x},240 {x+26 if x>40 else x},195"
    s.add(f'<polygon points="{pts}" fill="{col}"/>')
    s.text(x + (48 if x > 40 else 24), 205, lab, size=26, weight="bold", fill="white")
    s.rect(x, 258, SW_ - 14, 400, stroke=col, sw=2)
    yb = s.para(x + 18, 294, q, SW_ - 50, size=20, fill=col, weight="bold") + 10
    s.bullets(x + 18, yb, items, SW_ - 50, size=18, mark_fill=col, gap=11)
    x += SW_ + GAP
s.rect(40, 680, 1520, 140, fill=COMP_FILL, stroke=COMP_STROKE)
s.text(64, 715, "Around every plot", size=21, weight="bold", fill=INK)
extras = [("Describe this plot", "selection, settings, versions, and the visplot(...) call that reproduces it"),
          ("Export PNG", "from the toolbar, or plotfile= with no window at all"),
          ("Linked panels", "cursor tracked across panels on a shared axis; X pan and zoom follow a shared X"),
          ("Dark and light", "colormaps adjust so faint points stay visible")]
ex = 64
for h, b in extras:
    s.text(ex, 752, h, size=17, weight="bold", fill=INK)
    s.para(ex, 776, b, 340, size=15, fill=DIM, lead=1.3)
    ex += 375
save("visplot_4_workflow.svg", s)


# ---- Slide 5: under the hood, and what is not here yet ---------------------
s = Slide("Under the hood, and what is next",
          "One reader interface over both data formats; Datashader in Python, Bokeh in the browser, no Bokeh server.")
s.card(40, 140, 860, 680, "Under the hood", BLUE)


def layer(x, y, w, h, label, sub=None, fill=COMP_FILL, stroke=COMP_STROKE, size=18):
    s.rect(x, y, w, h, fill=fill, stroke=stroke, rx=7)
    if sub:
        s.text(x + w / 2, y + h / 2 - 3, label, size=size, weight="bold", fill=INK, anchor="middle")
        s.text(x + w / 2, y + h / 2 + 19, sub, size=15, fill=DIM, anchor="middle")
    else:
        s.text(x + w / 2, y + h / 2 + 6, label, size=size, weight="bold", fill=INK, anchor="middle")


def down(x, y0, y1, col="#8A94A0"):
    s.add(f'<path d="M{x} {y0} V{y1-8}" stroke="{col}" stroke-width="3"/>'
          f'<path d="M{x-7} {y1-10} L{x} {y1} L{x+7} {y1-10} Z" fill="{col}"/>')


L, R = 70, 870
layer(L, 205, R - L, 62, "Browser: BokehJS window", "panels, sidebar, flag boxes · cubevis's own Python ↔ JS channel",
      fill="#FBF6F0", stroke=ORANGE)
down(470, 267, 297)
layer(L, 297, R - L, 62, "VisibilityPlotter", "presets, selection, flagging (FlagDB), plot summary, PNG export",
      fill="#EEF3FA", stroke=BLUE)
down(470, 359, 389)
layer(L, 389, R - L, 62, "Datashader", "aggregates every selected sample into the raster or scatter image")
down(470, 451, 481)
layer(L, 481, R - L, 62, "XArrayReader", "one interface: raster and scatter queries, averaging, statistics")
down(270, 543, 573)
down(670, 543, 573)
layer(L, 573, 380, 62, "MSv2Backend", "reads: xarray-ms on arcae · flags: arcae")
layer(490, 573, 380, 62, "MSv4Backend", "reads and flags: xarray / Zarr, no xradio")
down(270, 635, 665)
down(670, 635, 665)
for cx, lab, sub in [(270, "MSv2 measurement set", "CASA table format"), (670, "MSv4 / Processing Set", "Zarr store")]:
    rx, ry, y0, hh = 150, 13, 667, 90
    s.add(f'<path d="M{cx-rx} {y0+ry} V{y0+hh-ry} A{rx} {ry} 0 0 0 {cx+rx} {y0+hh-ry} V{y0+ry}" '
          f'fill="#FFF7E0" stroke="#A88A2E" stroke-width="2"/>')
    s.add(f'<ellipse cx="{cx}" cy="{y0+ry}" rx="{rx}" ry="{ry}" fill="#FFEFC2" stroke="#A88A2E" stroke-width="2"/>')
    s.text(cx, y0 + 52, lab, size=17, weight="bold", fill=INK, anchor="middle")
    s.text(cx, y0 + 73, sub, size=14, fill=DIM, anchor="middle")
s.text(470, 800, "Local, or everything below the browser in a remote kernel beside the data (see the architecture slides)",
       size=14, fill=DIM, anchor="middle")

s.card(940, 140, 620, 680, "Not here yet", ORANGE)
y = 222
for head, body in [
    ("Jupyter notebooks",
     "JupyterLab and Colab. cubevis already talks over both; the visplot window does not yet run in a notebook."),
    ("Calibration tables",
     "What SNPLT, POSSM on BP tables, plotbandpass and plotms on cal tables show. Solutions are per antenna, "
     "not per baseline, so this needs its own data model."),
    ("Iteration over pages",
     "plotms's iteraxis: one plot per antenna, baseline, SPW or scan, laid out in a grid. visplot has two panels "
     "and steps through antennas and baselines with Prev / Next."),
    ("Elevation and angle axes",
     "Elevation, hour angle, azimuth and parallactic angle, as plotms offers."),
]:
    s.text(964, y, head, size=20, weight="bold", fill=ORANGE)
    y = s.para(964, y + 32, body, 570, size=19, fill=INK) + 30
save("visplot_5_internals.svg", s, "Scope as of the visplot user guide, 18 Oct 2026 · " + FOOT)


# ---- slideshow viewer (same page as ../architecture) -----------------------
SLIDES_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>visplot overview</title>
<style>
  :root { --bg: #14181d; --fg: #d7dde4; --dim: #7d8794; }
  html, body { margin: 0; height: 100%; background: var(--bg); color: var(--fg);
               font-family: Helvetica, Arial, sans-serif; overflow: hidden; }
  #stage { position: absolute; inset: 0 0 44px 0; display: flex;
           align-items: center; justify-content: center; }
  #stage img { width: 100%; height: 100%; object-fit: contain; }
  #bar { position: absolute; left: 0; right: 0; bottom: 0; height: 44px;
         display: flex; align-items: center; justify-content: center; gap: 18px;
         font-size: 14px; color: var(--dim); }
  #bar button { background: none; border: 1px solid #3a424c; color: var(--fg);
                border-radius: 6px; padding: 4px 12px; font-size: 14px; cursor: pointer; }
  #bar button:hover { border-color: var(--fg); }
  :fullscreen #bar { opacity: 0; transition: opacity .3s; }
  :fullscreen #bar:hover { opacity: 1; }
  :fullscreen #stage { inset: 0; }
</style>
</head>
<body>
<div id="stage"><img id="slide" alt=""></div>
<div id="bar">
  <button id="prev" aria-label="Previous slide">&lsaquo; Prev</button>
  <span id="count"></span>
  <button id="next" aria-label="Next slide">Next &rsaquo;</button>
  <button id="full" aria-label="Toggle fullscreen">Fullscreen (F)</button>
</div>
<script>
const slides = __SLIDES__;
const img = document.getElementById("slide");
let i = 0;
slides.forEach(s => { const p = new Image(); p.src = s.src; });
function show(n) {
  i = Math.max(0, Math.min(slides.length - 1, n));
  img.src = slides[i].src;
  img.alt = slides[i].title;
  document.title = slides[i].title + " \\u00b7 visplot overview";
  document.getElementById("count").textContent = (i + 1) + " / " + slides.length;
  history.replaceState(null, "", "#" + (i + 1));
}
function fullscreen() {
  if (document.fullscreenElement) document.exitFullscreen();
  else document.documentElement.requestFullscreen().catch(() => {});
}
document.getElementById("prev").onclick = () => show(i - 1);
document.getElementById("next").onclick = () => show(i + 1);
document.getElementById("full").onclick = fullscreen;
img.onclick = e => show(e.offsetX < img.clientWidth / 3 ? i - 1 : i + 1);
document.addEventListener("keydown", e => {
  if (["ArrowRight", "ArrowDown", "PageDown", " ", "Enter"].includes(e.key)) show(i + 1);
  else if (["ArrowLeft", "ArrowUp", "PageUp", "Backspace"].includes(e.key)) show(i - 1);
  else if (e.key === "Home") show(0);
  else if (e.key === "End") show(slides.length - 1);
  else if (e.key === "f" || e.key === "F") fullscreen();
  else return;
  e.preventDefault();
});
let x0 = null;
document.addEventListener("touchstart", e => { x0 = e.touches[0].clientX; });
document.addEventListener("touchend", e => {
  if (x0 === null) return;
  const dx = e.changedTouches[0].clientX - x0;
  if (Math.abs(dx) > 40) show(dx < 0 ? i + 1 : i - 1);
  x0 = null;
});
const fromHash = () => show((parseInt(location.hash.slice(1), 10) || 1) - 1);
window.addEventListener("hashchange", fromHash);
fromHash();
</script>
</body>
</html>
"""

# ---- command line ---------------------------------------------------------
ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
ap.add_argument("--png", action="store_true", help="also write PNG copies of each SVG")
ap.add_argument("--scale", type=float, default=2.0, help="PNG scale factor (default 2 -> 3200x1800)")
args = ap.parse_args()

for name, _ in WRITTEN:
    print(f"wrote {name}")
    if args.png:
        try:
            import cairosvg
        except ImportError:
            raise SystemExit("--png needs cairosvg: pip install cairosvg")
        png = (OUT / name).with_suffix(".png")
        cairosvg.svg2png(url=str(OUT / name), write_to=str(png),
                         output_width=int(W * args.scale), output_height=int(H * args.scale))
        print(f"wrote {png.name}")

order = [{"src": n, "title": t} for n, t in WRITTEN]
for extra in EXTRA_IMAGES:
    at = next((k for k, o in enumerate(order) if o["src"] == extra["after"]), len(order) - 1)
    order.insert(at + 1, {"src": extra["src"], "title": extra.get("title", extra["src"])})
(OUT / "slides.html").write_text(SLIDES_HTML.replace("__SLIDES__", json.dumps(order, indent=2)), encoding="utf-8")
print("wrote slides.html")
