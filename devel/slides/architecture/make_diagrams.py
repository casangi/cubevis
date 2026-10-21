"""Generate cubevis architecture slide diagrams (16:9 SVG) from one shared visual vocabulary.

Usage:
    python make_diagrams.py               # write the SVGs next to this script
    python make_diagrams.py --png         # also write 2x PNGs (for Google Slides)
    python make_diagrams.py --png --scale 3

PNG output needs cairosvg (pip install cairosvg).
"""
import argparse
from pathlib import Path
from xml.sax.saxutils import escape

OUT = Path(__file__).resolve().parent
WRITTEN = []
TITLES = {}


def save(name, svg, title=None):
    path = OUT / name
    TITLES[name] = title or name
    path.write_text(svg, encoding="utf-8")
    WRITTEN.append(path)

W, H = 1600, 900
FONT = "Helvetica, Arial, sans-serif"

# ---- palette / vocabulary -------------------------------------------------
HOST_FILL, HOST_STROKE = "#F3F5F8", "#8A94A0"
PROC = {  # process kind -> header colour
    "python": "#3B6EA8",
    "browser": "#C9711F",
    "jupyter": "#6A4C93",
    "exec": "#2E7D5B",
}
COMP_FILL, COMP_STROKE = "#EEF2F7", "#A9B8CA"
CHAN = {  # channel kind -> (colour, dash, label for legend)
    "ws": ("#1F6FB2", None, "WebSocket"),
    "comms": ("#6A4C93", None, "Jupyter Comm (notebook's own channel)"),
    "ccomm": ("#6A4C93", None, "Colab kernel comm (JS to Python)"),
    "evaljs": ("#6A4C93", "10 6", "google.colab.output.eval_js (Python to JS, chunked)"),
    "bc": ("#0F8B8D", "3 5", "BroadcastChannel (cross-iframe)"),
    "ssh": ("#C0392B", "14 8", "SSH tunnel (sshpyk) carrying a Jupyter comm"),
    "pipe": ("#2E7D5B", "4 6", "Subprocess stdin/stdout (length-prefixed frames)"),
    "fs": ("#555555", None, "File I/O"),
}


class Slide:
    def __init__(self, title, subtitle):
        self.parts = []
        self.used = []
        self.title, self.subtitle = title, subtitle

    def add(self, s):
        self.parts.append(s)

    def text(self, x, y, s, size=20, weight="normal", fill="#222", anchor="start", style=""):
        self.add(f'<text x="{x}" y="{y}" font-family="{FONT}" font-size="{size}" '
                 f'font-weight="{weight}" fill="{fill}" text-anchor="{anchor}" {style}>{escape(s)}</text>')

    def host(self, x, y, w, h, label):
        self.add(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="18" fill="{HOST_FILL}" '
                 f'stroke="{HOST_STROKE}" stroke-width="2.5" stroke-dasharray="10 6"/>')
        self.text(x + 20, y + 32, label, size=21, weight="bold", fill="#4A5563")

    def process(self, x, y, w, h, kind, label, stacked=False):
        c = PROC[kind]
        if stacked:  # "one or more" processes
            for d in (16, 8):
                self.add(f'<rect x="{x+d}" y="{y-d}" width="{w}" height="{h}" rx="10" fill="white" '
                         f'stroke="{c}" stroke-width="2" opacity="0.6"/>')
        self.add(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="10" fill="white" stroke="{c}" stroke-width="2.5"/>')
        self.add(f'<path d="M{x} {y+44} V{y+10} a10 10 0 0 1 10 -10 H{x+w-10} a10 10 0 0 1 10 10 V{y+44} Z" fill="{c}"/>')
        self.text(x + w / 2, y + 29, label, size=20, weight="bold", fill="white", anchor="middle")

    def comp(self, x, y, w, h, label, sub=None):
        self.add(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="7" fill="{COMP_FILL}" stroke="{COMP_STROKE}" stroke-width="1.5"/>')
        if sub:
            self.text(x + w / 2, y + h / 2 - 3, label, size=18, weight="bold", anchor="middle")
            self.text(x + w / 2, y + h / 2 + 19, sub, size=15, fill="#5A6573", anchor="middle")
        else:
            self.text(x + w / 2, y + h / 2 + 6, label, size=18, weight="bold", anchor="middle")

    def frame(self, x, y, w, h, label, sub=None):
        """A sandboxed iframe inside a browser process."""
        self.add(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="8" fill="#FBF6F0" '
                 f'stroke="#C9711F" stroke-width="1.8" stroke-dasharray="6 4"/>')
        self.text(x + w / 2, y + 24, label, size=16, weight="bold", fill="#8A4A10", anchor="middle")
        if sub:
            self.text(x + w / 2, y + 43, sub, size=13, fill="#8A4A10", anchor="middle")

    def data(self, cx, y, w, h, label, sub=None):
        rx, ry = w / 2, 14
        x0, x1 = cx - rx, cx + rx
        self.add(f'<path d="M{x0} {y+ry} V{y+h-ry} A{rx} {ry} 0 0 0 {x1} {y+h-ry} V{y+ry}" fill="#FFF7E0" stroke="#A88A2E" stroke-width="2"/>')
        self.add(f'<ellipse cx="{cx}" cy="{y+ry}" rx="{rx}" ry="{ry}" fill="#FFEFC2" stroke="#A88A2E" stroke-width="2"/>')
        self.text(cx, y + h / 2 + 10, label, size=18, weight="bold", anchor="middle")
        if sub:
            self.text(cx, y + h / 2 + 32, sub, size=14, fill="#5A6573", anchor="middle")

    def doc(self, x, y, w, h, label, sub=None):
        f = 22  # folded corner
        self.add(f'<path d="M{x} {y} H{x+w-f} L{x+w} {y+f} V{y+h} H{x} Z" fill="white" stroke="#555" stroke-width="2"/>')
        self.add(f'<path d="M{x+w-f} {y} V{y+f} H{x+w}" fill="#E4E7EB" stroke="#555" stroke-width="2"/>')
        self.text(x + w / 2, y + h / 2 + (0 if sub else 6), label, size=17, weight="bold", anchor="middle")
        if sub:
            self.text(x + w / 2, y + h / 2 + 22, sub, size=14, fill="#5A6573", anchor="middle")

    def chan(self, pts, kind, both=True):
        col, dash, _ = CHAN[kind]
        if kind not in self.used:
            self.used.append(kind)
        d = "M" + " L".join(f"{x} {y}" for x, y in pts)
        da = f'stroke-dasharray="{dash}"' if dash else ""
        ms = f'marker-start="url(#a-{kind})"' if both else ""
        self.add(f'<path d="{d}" fill="none" stroke="{col}" stroke-width="5" {da} {ms} marker-end="url(#a-{kind})"/>')

    def label(self, x, y, s, kind, size=18, anchor="middle"):
        self.text(x, y, s, size=size, weight="bold", fill=CHAN[kind][0], anchor=anchor)

    def render(self, footnote=None):
        defs = "".join(
            f'<marker id="a-{k}" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="4" markerHeight="4" '
            f'orient="auto-start-reverse"><path d="M0 0 L10 5 L0 10 z" fill="{c}"/></marker>'
            for k, (c, _, _) in CHAN.items())
        head = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}">',
                f'<defs>{defs}</defs>', f'<rect width="{W}" height="{H}" fill="white"/>']
        self.parts = head + self.parts
        self.text(40, 62, self.title, size=40, weight="bold", fill="#1C2733")
        self.text(40, 102, self.subtitle, size=21, fill="#4A5563")
        # legend
        x = 40
        for k in self.used:
            col, dash, name = CHAN[k]
            da = f'stroke-dasharray="{dash}"' if dash else ""
            self.add(f'<line x1="{x}" y1="845" x2="{x+50}" y2="845" stroke="{col}" stroke-width="5" {da}/>')
            self.text(x + 60, 851, name, size=17, fill="#333")
            x += 110 + len(name) * 7.6
        if footnote:
            self.text(W - 40, 885, footnote, size=15, fill="#8A94A0", anchor="end", style='font-style="italic"')
        return "\n".join(self.parts + ["</svg>"])


# ---- Slide 1: Python CLI --------------------------------------------------
s = Slide("Python CLI", "Everything on one host: Bokeh writes an HTML page, the browser opens it, then GUI traffic flows over a WebSocket.")
s.host(40, 140, 1520, 650, "User's host (laptop / workstation)")
s.process(110, 200, 560, 420, "python", "Python process  (python / ipython)")
s.comp(140, 260, 500, 60, "User script / REPL")
s.comp(140, 330, 500, 60, "VisibilityPlotter", "Showable.show() writes the HTML page")
s.comp(140, 400, 500, 60, "CommMgr", "WebSocketTransport (websockets.serve)")
s.comp(140, 470, 500, 60, "VisibilityRaster / VisibilityScatter", "VisibilityPlot (Bokeh Models)")
s.comp(140, 540, 500, 60, "LocalVisibilityReader", "MSv2Backend / MSv4Backend (XArrayReader)")
s.process(930, 200, 560, 420, "browser", "Browser process")
s.comp(960, 260, 500, 130, "cubevis GUI (BokehJS)", "VisibilityPlotter layout · plots · sidebar · tools")
s.comp(960, 400, 500, 60, "CommMgr / Comm (JS)", "casalib.min.js · reconnects on sleep / reload")
s.chan([(640, 430), (960, 430)], "ws")
s.label(800, 415, "WebSocket", "ws")
s.text(800, 458, "JSON + binary payloads", size=15, fill="#5A6573", anchor="middle")
s.doc(720, 655, 160, 95, "HTML page", "generated by Bokeh")
s.chan([(600, 620), (600, 700), (720, 700)], "fs", both=False)
s.chan([(880, 700), (1210, 700), (1210, 620)], "fs", both=False)
s.text(1225, 680, "browser opens file", size=15, fill="#5A6573")
s.chan([(390, 600), (390, 660)], "fs", both=False)
s.data(390, 665, 300, 105, "Measurement Set", "MSv2 (casacore)  /  MSv4 (Zarr)")
save("arch_1_python_cli.svg", s.render("Draft · v0.3"), s.title)

# ---- Slide 2: Jupyter -----------------------------------------------------
s = Slide("Jupyter  (JupyterLab / Notebook)", "No connection of its own: cubevis borrows a Jupyter Comm, opened by an invisible anywidget bridge in the page.")
s.host(40, 140, 480, 650, "User's host")
s.process(70, 200, 420, 465, "browser", "Browser process")
s.comp(95, 258, 370, 60, "Notebook frontend", "JupyterLab / Notebook")
s.comp(95, 335, 370, 60, "anywidget bridge", "kernel.createComm(target)")
s.comp(95, 410, 370, 60, "CommMgr / Comm (JS)", "uses the bridge's comm · casalib.min.js")
s.comp(95, 485, 370, 160, "cubevis GUI (BokehJS)", "VisibilityPlotter layout in cell output")
s.host(680, 140, 880, 650, "Kernel host — same machine or remote")
s.process(710, 295, 250, 140, "jupyter", "Jupyter server")
s.text(835, 375, "notebook's existing", size=16, fill="#5A6573", anchor="middle")
s.text(835, 397, "connection (not cubevis)", size=16, fill="#5A6573", anchor="middle")
s.process(1040, 200, 490, 440, "python", "Kernel process  (ipykernel)")
s.comp(1065, 262, 440, 50, "Notebook cells")
s.comp(1065, 335, 440, 60, "CommMgr", "CommsTransport (Jupyter path)")
s.comp(1065, 410, 440, 60, "VisibilityPlotter", "Showable.show() into cell output")
s.comp(1065, 485, 440, 60, "VisibilityRaster / VisibilityScatter", "VisibilityPlot (Bokeh Models)")
s.comp(1065, 560, 440, 60, "LocalVisibilityReader", "MSv2Backend / MSv4Backend")
s.chan([(465, 365), (710, 365)], "comms")
s.chan([(960, 365), (1065, 365)], "comms")
s.label(600, 350, "Comm", "comms", size=17)
s.label(1012, 350, "Comm", "comms", size=15)
s.text(600, 390, "same channel both ways", size=13, fill="#5A6573", anchor="middle")
s.chan([(1285, 620), (1285, 665)], "fs", both=False)
s.data(1285, 670, 300, 100, "Measurement Set", "MSv2 / MSv4 on kernel host")
save("arch_2_jupyter.svg", s.render("Draft · v0.3"), s.title)

# ---- Slide 3: Colab -------------------------------------------------------
s = Slide("Google Colab", "Each cell output is a sandboxed iframe, so a bridge relays over BroadcastChannels; Python replies to polls in chunks.")
s.host(40, 140, 750, 650, "User's host")
s.process(70, 195, 695, 575, "browser", "Browser process  (colab.research.google.com)")
s.frame(95, 255, 300, 495, "Bokeh app cell iframe")
s.comp(112, 310, 266, 75, "cubevis GUI (BokehJS)", "VisibilityPlotter layout")
s.comp(112, 395, 266, 185, "CommMgr / Comm (JS)", "casalib.min.js")
s.frame(520, 255, 228, 495, "bridge widget iframe", "anywidget ESM · zero height")
s.comp(534, 310, 200, 70, "Poll driver", "polls over the Colab comm")
s.comp(534, 395, 200, 50, "Colab comm", "comms.open(target)")
s.comp(534, 505, 200, 60, "Chunk reassembly", "cubevis_chunk_<id>")
s.chan([(378, 420), (534, 420)], "bc", both=False)
s.label(456, 408, "cubevis_tx", "bc", size=15)
s.chan([(534, 545), (378, 545)], "bc", both=False)
s.label(456, 572, "cubevis_rx", "bc", size=15)
s.host(900, 140, 660, 650, "Google-hosted Colab VM")
s.process(960, 195, 570, 470, "python", "Kernel process  (Colab ipykernel)")
s.comp(985, 255, 520, 45, "Notebook cells")
s.comp(985, 395, 520, 170, "CommMgr", "CommsTransport (Colab path) · replies in 500 KB chunks")
s.comp(985, 312, 520, 70, "VisibilityPlotter", "VisibilityRaster / VisibilityScatter")
s.comp(985, 580, 520, 65, "LocalVisibilityReader", "MSv2Backend / MSv4Backend")
s.chan([(734, 420), (985, 420)], "ccomm", both=False)
s.label(845, 408, "comm.send", "ccomm", size=15)
s.chan([(985, 530), (734, 530)], "evaljs", both=False)
s.label(845, 518, "eval_js", "evaljs", size=15)
s.chan([(1245, 665), (1245, 690)], "fs", both=False)
s.data(1245, 690, 280, 90, "Measurement Set", "on the Colab VM")
save("arch_3_colab.svg", s.render("Draft · v0.3 · BroadcastChannel avoids eval_js's iframe-routing race"), s.title)

# ---- Slide 4: Remote kernel (sshpyk) --------------------------------------
s = Slide("Remote Kernel  (sshpyk)", "GUI and Python stay local; a dedicated kernel on the data host supervises worker subprocesses that run visplot.")
s.host(40, 140, 600, 660, "User's host")
s.process(70, 190, 540, 140, "browser", "Browser process")
s.comp(95, 245, 490, 70, "cubevis GUI (BokehJS) · CommMgr / Comm (JS)", "loaded from generated HTML page")
s.process(70, 390, 540, 390, "python", "Local Python process  (P_local)")
for i, (lab, sub) in enumerate([
        ("User script / REPL", None),
        ("VisibilityPlotter", "VisibilityRaster / VisibilityScatter"),
        ("CommMgr  (browser leg)", "WebSocketTransport"),
        ("RemoteReductionContext", "reader + context · SyncBridge"),
        ("RemoteAppLink · ExecutionContext", "CommMgr (mirror) · KernelClientTransport"),
        ("sshpyk", "AsyncKernelManager · SSH provisioner")]):
    s.comp(95, 444 + 55 * i, 490, 50, lab, sub)
s.chan([(340, 330), (340, 390)], "ws")
s.label(355, 367, "WebSocket", "ws", anchor="start")
s.host(760, 140, 800, 660, "Remote host (data host / cluster node)")
s.process(790, 195, 300, 330, "jupyter", "Remote Jupyter kernel")
s.comp(810, 255, 260, 80, "CommMgr", "KernelCommTransport")
s.comp(810, 350, 260, 80, "ExecutionContextPool", "create · dispatch · shutdown")
s.text(940, 470, "the supervisor: relays", size=15, fill="#5A6573", anchor="middle")
s.text(940, 490, "messages to its workers", size=15, fill="#5A6573", anchor="middle")
s.process(1210, 215, 300, 310, "exec", "Execution context(s)", stacked=True)
s.text(1360, 283, "python -m cubevis.remote.worker_main", size=13, fill="#5A6573", anchor="middle")
s.comp(1230, 295, 260, 55, "CommMgr", "WorkerCommTransport")
s.comp(1230, 360, 260, 55, "VisplotRemoteBackend", "installed by configure")
s.comp(1230, 425, 260, 55, "LocalVisibilityReader", "MSv2Backend / MSv4Backend")
s.chan([(1070, 390), (1150, 390), (1150, 322), (1230, 322)], "pipe")
s.label(1150, 415, "stdin/stdout", "pipe", size=14)
s.chan([(585, 689), (955, 689), (955, 525)], "ssh")
s.label(775, 677, "SSH tunnel", "ssh", anchor="start")
s.text(775, 717, "one Jupyter comm per kernel", size=15, fill="#5A6573")
s.chan([(1360, 525), (1360, 620)], "fs", both=False)
s.data(1360, 625, 300, 105, "Measurement Set", "MSv2 / MSv4 local to data")
save("arch_4_remote_kernel.svg", s.render("Draft · v0.3 · worker stderr is relayed to the kernel's log"), s.title)

# ---- slideshow viewer ------------------------------------------------------
# A self-contained page that steps through the SVGs above.  Served by GitHub
# Pages it gives a browser slideshow; it also works opened as a local file.
SLIDES_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>cubevis architectures</title>
<style>
  :root { --bg: #14181d; --fg: #d7dde4; --dim: #7d8794; }
  html, body { margin: 0; height: 100%; background: var(--bg); color: var(--fg);
               font-family: Helvetica, Arial, sans-serif; overflow: hidden; }
  #stage { position: absolute; inset: 0 0 44px 0; display: flex;
           align-items: center; justify-content: center; }
  #stage img { max-width: 100%; max-height: 100%; aspect-ratio: 16 / 9;
               background: white; box-shadow: 0 6px 30px rgba(0,0,0,.5); }
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
// preload so stepping is instant
slides.forEach(s => { const p = new Image(); p.src = s.src; });

function show(n) {
  i = Math.max(0, Math.min(slides.length - 1, n));
  img.src = slides[i].src;
  img.alt = slides[i].title;
  document.title = slides[i].title + " \\u00b7 cubevis architectures";
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
let x0 = null;  // touch swipe
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

for path in WRITTEN:
    print(f"wrote {path.name}")
    if args.png:
        try:
            import cairosvg
        except ImportError:
            raise SystemExit("--png needs cairosvg: pip install cairosvg")
        png = path.with_suffix(".png")
        cairosvg.svg2png(url=str(path), write_to=str(png), scale=args.scale)
        print(f"wrote {png.name}")

import json
slides = [{"src": p.name, "title": " ".join(TITLES[p.name].split())} for p in WRITTEN]
(OUT / "slides.html").write_text(SLIDES_HTML.replace("__SLIDES__", json.dumps(slides, indent=2)),
                                 encoding="utf-8")
print("wrote slides.html")
