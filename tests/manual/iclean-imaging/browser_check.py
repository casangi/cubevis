"""Run interactive clean (browser mode) from a cubevis checkout and look at the
page in a headless Chrome: console errors, the world coordinates the GUI
computes (ImagePipe.wcs(), as the axis labels and cursor readout do) for some
pixels, the spectral values of the coordinate description when there is one,
and a screenshot.

The GUI's transform works in Bokeh data space (the image glyph is drawn at
x=0, y=0, so pixel index i covers [i, i+1]); the page is asked for the world
coordinates of each pixel's center, (i + 0.5, j + 0.5), as the cursor readout
does since 2026-11-06, and these are compared with casatools' own pixel ->
world for the residual image (``--tolerance`` arcsec, default 0.01).  Before
2026-11-06 the readout was one pixel off and coordtxl treated every image as
linear, so an older checkout fails this check; ``--no-casatools`` skips it.

``--stop`` presses the red stop at the end: the app must end with its
result, keep its history and close a remote session (design step 5).
``--controls`` also presses continue: a refused threshold must show gclean's
message with the buttons enabled again, then a good one must run a cycle
(design step 4).

The page's mask outline (ImageDataSource._mask_contour, d3-contour) is
checked on a 3x2 block of pixels: it must run along their edges, as it does
since 2026-11-07.

Two runs (two checkouts) can then be compared:

    python browser_check.py REPO_A MS WORKDIR_A
    python browser_check.py REPO_B MS WORKDIR_B
    python browser_check.py --compare WORKDIR_A WORKDIR_B

Needs casatasks and playwright (``pip install playwright``) with a Chrome
(``--chrome PATH``, default /usr/bin/google-chrome; ``playwright install
chromium`` also works where its download is allowed).
"""
import os, sys, json, time, subprocess, argparse

PIXELS = [[0, 0], [64, 64], [65, 65], [10, 100], [127, 127]]

PAGE_JS = """(pixels) => {
  const out = {pipes: []}
  if (!window.Bokeh || !Bokeh.documents) return {error: 'no Bokeh'}
  for (const doc of Bokeh.documents) {
    for (const m of doc._all_models.values()) {
      if (m.constructor.name !== 'ImagePipe') continue
      const wcs = m.wcs()
      const item = {coords_json: 'coords_json' in m, fits_header_json: 'fits_header_json' in m,
                    wcs: wcs != null, world: [], text: []}
      if (wcs) {
        for (const [x, y] of pixels) {
          const pt = new casalib.coordtxl.Point2D(x + 0.5, y + 0.5)
          wcs.imageToWorldCoords(pt, false)
          item.world.push([pt.getX(), pt.getY()])
          item.text.push(new casalib.coordtxl.WorldCoords(pt.getX(), pt.getY()).format(2000.0))
        }
      }
      if (m.coordinates) {
        const c = m.coordinates()
        item.spectral = c && c.spectral ? c.spectral.values : null
      }
      out.pipes.push(item)
    }
    // the mask outline: ImageDataSource._mask_contour on a 3x2 block of pixels
    // x 3..5, y 2..3 should run along their edges, x 3..6, y 2..4 (Bokeh data space)
    for (const m of doc._all_models.values()) {
      if (typeof m._mask_contour !== 'function' || !m.image_source) continue
      const [nx, ny] = m.image_source.shape.slice(0, 2)
      const mask = new Uint8Array(nx * ny)
      for (let x = 3; x <= 5; x++) for (let y = 2; y <= 3; y++) mask[y * nx + x] = 1
      const c = m._mask_contour([mask])
      const xs = c.xs.flat(3), ys = c.ys.flat(3)
      out.contour = {x: [Math.min(...xs), Math.max(...xs)], y: [Math.min(...ys), Math.max(...ys)]}
      break
    }
  }
  return out
}"""

#: the clean controls through the page (``--controls``): set a TextInput by
#: title, click a named button (its view, found from the roots), read the
#: status line, the buttons' state and the convergence data's length
CONTROLS_JS = {
    'set': """([title, value]) => { for (const m of Bokeh.documents[0]._all_models.values())
                if (m.constructor.name === 'TextInput' && m.title === title) { m.value = value; return true }
              return false }""",
    'click': """(name) => {
                const find = (v, m, seen) => { if (!v || seen.has(v)) return null; seen.add(v); if (v.model === m) return v
                    const kids = typeof v.children === 'function' ? [...v.children()] : [...(v.child_views ?? [])]
                    for (const c of kids) { const f = find(c, m, seen); if (f) return f } return null }
                const roots = typeof Bokeh.index.roots === 'function' ? Bokeh.index.roots() : Bokeh.index.roots
                for (const m of Bokeh.documents[0]._all_models.values()) if (m.name === name) {
                    for (const r of roots) { const v = find(r, m, new Set()); if (v) { v.click(); return 'clicked' } }
                    return 'no view' }
                return 'no model' }""",
    'state': """() => { const out = {points: 0}
                for (const m of Bokeh.documents[0]._all_models.values()) {
                    if (m.constructor.name === 'Div' && /^<p>/.test(m.text || '')) out.status = m.text
                    if (m.name === 'continue' || m.name === 'finish') out[m.name + '_disabled'] = m.disabled
                    if (m.constructor.name === 'ColumnDataSource' && m.data && m.data.type &&
                        m.data.type[0] === 'residual') out.points = Math.max(out.points, m.data.values.length)
                }
                return out }""",
}


def exercise_controls(page, timeout=180):
    '''``--controls``: a refused threshold is shown as gclean's message with the
    buttons enabled (design step 4, notes §68); then a good one runs a cycle.
    Returns (ok, what happened).'''
    def wait(pred):
        t0 = time.time()
        while time.time() - t0 < timeout:
            time.sleep(0.5)
            st = page.evaluate(CONTROLS_JS['state'])
            if pred(st):
                return st
        return page.evaluate(CONTROLS_JS['state'])
    before = page.evaluate(CONTROLS_JS['state'])
    page.evaluate(CONTROLS_JS['set'], ['threshold', 'abc'])
    clicked = page.evaluate(CONTROLS_JS['click'], 'continue')
    bad = wait(lambda st: 'threshold must be' in st.get('status', ''))
    page.evaluate(CONTROLS_JS['set'], ['threshold', '0.001Jy'])
    page.evaluate(CONTROLS_JS['click'], 'continue')
    ### the cycle's own result in the status line, the buttons enabled again (with no mask
    ### drawn tclean ends the cycle with "Zero mask" and adds no convergence point); the
    ### status lines seen meanwhile are the cycle's progress (design step 4b)
    seen = []
    def cycle_done(st):
        if not seen or seen[-1] != st.get('status'):
            seen.append(st.get('status'))
        return (st.get('status') not in (bad.get('status'), '<p>Running one set of deconvolution iterations</p>')
                and not st.get('continue_disabled') and '·' not in st.get('status', ''))
    good = wait(cycle_done)
    good['seen'] = seen
    ok = (clicked == 'clicked' and 'threshold must be' in bad.get('status', '') and not bad.get('continue_disabled')
          and not good.get('continue_disabled') and 'error' not in good.get('status', '').lower()
          and good.get('status') not in (bad.get('status'), before.get('status')))
    return ok, {'before': before, 'refused': bad, 'cycle': good}


APP = """import sys, os
sys.path.insert(0, sys.argv[1])
from cubevis.private.apps import InteractiveClean
kernel = sys.argv[3] if len(sys.argv) > 3 else ''
scripted = float(sys.argv[4]) if len(sys.argv) > 4 else 0.0
if scripted:
    ### the scripted imager (cubevis.imaging.testing), each cycle taking that long, here or
    ### in the kernel's worker: progress can be watched in the page
    import cubevis.toolbox.iclean._app as app
    from cubevis.imaging import LocalImagingSession, RemoteImagingSession
    from cubevis.imaging import testing as T
    class Scripted:
        @staticmethod
        def from_args(args, iclean_backend='PROD'):
            args = dict(args, cycle_seconds=scripted, nchan=5)
            if kernel:
                return RemoteImagingSession(args, kernel, deconvolver='cubevis.imaging.testing:ScriptedDeconvolver',
                                            store='cubevis.imaging.testing:numpy_store')
            return LocalImagingSession(T.ScriptedDeconvolver(**args), store_factory=T.numpy_store)
    app.LocalImagingSession = Scripted
ic = InteractiveClean(vis=sys.argv[2], imagename=os.path.abspath('br'), imsize=128, cell='12.0arcsec', specmode='cube',
                      interpolation='nearest', nchan=5, start='1.0GHz', width='0.2GHz', pblimit=-1e-05,
                      deconvolver='hogbom', threshold='0.001Jy', cyclefactor=3, niter=50, cycleniter=10,
                      **({'kernel_name': kernel} if kernel and not scripted else {}))
result = ic()
print('result', {k: v for k, v in result.items() if k in ('stopcode', 'stopDescription', 'iterdone', 'nmajordone')}
      if isinstance(result, dict) else result)
print('history', len(ic._ui.history()), 'entries')
session = ic._ui._clean['session']
conn = getattr(session, 'connection', None)
print('session closed:', conn.closed if conn is not None else 'local')
print('APP-ENDED', flush=True)

"""


def run(repo, ms, work, chrome, kernel=None, controls=False, scripted=0.0, stop=False):
    from playwright.sync_api import sync_playwright
    work = os.path.abspath(work)
    os.makedirs(work, exist_ok=True)
    url_log = os.path.join(work, 'urls.txt')
    if os.path.exists(url_log):
        os.remove(url_log)
    recorder = os.path.join(work, 'record_url.sh')
    with open(recorder, 'w') as f:
        f.write(f'#!/bin/sh\necho "$1" >> "{url_log}"\n')
    os.chmod(recorder, 0o755)
    app = os.path.join(work, 'app.py')
    with open(app, 'w') as f:
        f.write(APP)
    env = dict(os.environ, BROWSER=recorder)
    proc = subprocess.Popen([sys.executable, '-I', app, os.path.abspath(repo), os.path.abspath(ms)] +
                            ([kernel or '', str(scripted)] if (kernel or scripted) else []), cwd=work,
                            env=env, stdout=open(os.path.join(work, 'iclean.log'), 'w'), stderr=subprocess.STDOUT)
    try:
        for _ in range(120):
            if os.path.exists(url_log) and os.path.getsize(url_log):
                break
            time.sleep(1)
        else:
            raise RuntimeError('iclean did not open a page (see iclean.log)')
        url = open(url_log).readline().strip()
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path=chrome, headless=True,
                                        args=['--allow-file-access-from-files', '--no-sandbox'])
            page = browser.new_page(viewport={'width': 1500, 'height': 1000})
            console = []
            page.on('console', lambda m: console.append(f'{m.type}: {m.text}'))
            page.on('pageerror', lambda e: console.append(f'pageerror: {e}'))
            page.goto(url)
            result = None
            for _ in range(60):
                time.sleep(1)
                try:
                    result = page.evaluate(PAGE_JS, PIXELS)
                except Exception as e:                    # pylint: disable=broad-except
                    result = {'error': str(e)}
                if result and any(pp.get('wcs') for pp in result.get('pipes', [])):
                    break
            time.sleep(3)
            page.screenshot(path=os.path.join(work, 'page.png'))
            if controls:
                ok, what = exercise_controls(page)
                result = dict(result or {}, controls={'ok': ok, **what})
                page.screenshot(path=os.path.join(work, 'controls.png'))
            if stop:
                ### the red stop: the browser asks to confirm, the app ends and returns its result
                ### (and closes a remote session: design step 5)
                page.on('dialog', lambda d: d.accept())
                page.evaluate(CONTROLS_JS['click'], 'stop')
                log = os.path.join(work, 'iclean.log')
                t0 = time.time()
                while time.time() - t0 < 120 and 'APP-ENDED' not in open(log).read():
                    time.sleep(0.5)
                ended = [l.strip() for l in open(log) if l.startswith(('result', 'history', 'session closed', 'APP-ENDED'))]
                try:
                    proc.wait(timeout=60)
                except subprocess.TimeoutExpired:
                    pass
                result = dict(result or {}, stop={'ended': ended, 'exit': proc.poll()})
            browser.close()
    finally:
        proc.kill()
    json.dump({'result': result, 'console': console}, open(os.path.join(work, 'page.json'), 'w'), indent=1)
    contour = (result or {}).get('contour')
    if contour is not None:
        print(f'    mask outline of pixels x 3..5, y 2..3: x {contour["x"]}, y {contour["y"]} '
              f'(pixel edges: x [3, 6], y [2, 4])')
    errors = [c for c in console if c.startswith(('error', 'pageerror'))]
    print(f'{work}: {len((result or {}).get("pipes", []))} ImagePipe(s), {len(errors)} console error(s)')
    for e in errors[:8]:
        print('   ', e[:200])
    for pp in (result or {}).get('pipes', []):
        print('   ', 'coords_json' if pp['coords_json'] else 'fits_header_json',
              'wcs' if pp['wcs'] else 'NO wcs', [t[0] + ' ' + t[1] for t in pp['text'][:2]],
              'spectral values' if pp.get('spectral') else '')
    return 1 if errors or not result or not any(pp.get('wcs') for pp in result.get('pipes', [])) else 0


def casatools_world(work, pixels):
    '''(ra, dec) in degrees of the residual image's 0-based pixels, from casatools'''
    import math
    from casatools import image as _image
    ia = _image(); ia.open(os.path.join(work, 'br.residual'))
    try:
        cs = ia.coordsys()
        out = []
        for x, y in pixels:
            w = cs.toworld([x, y, 0, 0], 'n')['numeric']
            out.append([math.degrees(w[0]) % 360.0, math.degrees(w[1])])
        cs.done()
        return out
    finally:
        ia.close()


def separation_arcsec(a, b):
    import math
    ra1, de1, ra2, de2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((de2 - de1) / 2) ** 2 + math.cos(de1) * math.cos(de2) * math.sin((ra2 - ra1) / 2) ** 2
    return math.degrees(2 * math.asin(min(1.0, math.sqrt(h)))) * 3600.0


def check_casatools(work, result, tolerance):
    '''compare the page's world coordinates with casatools; 0 if all agree'''
    truth = casatools_world(work, PIXELS)
    status = 0
    for pp in (result or {}).get('pipes', []):
        seps = [separation_arcsec(w, t) for w, t in zip(pp['world'], truth)]
        worst = max(seps) if seps else float('inf')
        print(f'    vs casatools: max separation {worst:.3g} arcsec over {len(seps)} pixels '
              f'(tolerance {tolerance})')
        if not worst <= tolerance:
            for (x, y), w, t, sp in zip(PIXELS, pp['world'], truth, seps):
                print(f'      pixel ({x},{y}): page {w[0]:.7f} {w[1]:.7f}  casatools {t[0]:.7f} {t[1]:.7f}  {sp:.3g}"')
            status = 1
    return status


def compare(a, b):
    import numpy as np
    from PIL import Image
    pa = json.load(open(os.path.join(a, 'page.json')))['result']['pipes'][0]
    pb = json.load(open(os.path.join(b, 'page.json')))['result']['pipes'][0]
    same_world = np.array_equal(np.array(pa['world']), np.array(pb['world']))
    same_text = pa['text'] == pb['text']
    ia = np.asarray(Image.open(os.path.join(a, 'page.png')).convert('RGB'), int)
    ib = np.asarray(Image.open(os.path.join(b, 'page.png')).convert('RGB'), int)
    ndiff = int((np.abs(ia - ib).sum(axis=2) > 0).sum()) if ia.shape == ib.shape else -1
    print(f'world coordinates identical: {same_world}; strings identical: {same_text}; '
          f'screenshot pixels differing: {ndiff}')
    return 0 if same_world and same_text and ndiff == 0 else 1


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--compare', nargs=2, metavar=('WORKDIR_A', 'WORKDIR_B'))
    ap.add_argument('--chrome', default='/usr/bin/google-chrome')
    ap.add_argument('--tolerance', type=float, default=0.01, help='arcsec, against casatools')
    ap.add_argument('--no-casatools', action='store_true', help='skip the comparison with casatools')
    ap.add_argument('--kernel', help='run the imaging layer on this Jupyter kernel (RemoteImagingSession)')
    ap.add_argument('--controls', action='store_true', help='also press continue: a refused threshold, then a cycle')
    ap.add_argument('--stop', action='store_true', help='at the end press the red stop: the app must end with its result')
    ap.add_argument('--scripted', type=float, default=0.0, metavar='SECONDS',
                    help='the scripted imager (no tclean), each cycle taking SECONDS: progress shows; '
                         'no comparison with casatools')
    ap.add_argument('args', nargs='*', metavar='REPO MS WORKDIR')
    ns = ap.parse_args()
    if ns.compare:
        sys.exit(compare(*ns.compare))
    if len(ns.args) != 3:
        ap.error('REPO MS WORKDIR')
    status = run(*ns.args, ns.chrome, kernel=ns.kernel, controls=ns.controls, scripted=ns.scripted, stop=ns.stop)
    if ns.scripted:
        ns.no_casatools = True
    page_result = json.load(open(os.path.join(ns.args[2], 'page.json')))['result']
    if 'controls' in page_result:
        c = page_result['controls']
        print(f"    controls: refused threshold -> {c['refused'].get('status')!r} "
              f"(continue disabled: {c['refused'].get('continue_disabled')}); then a cycle -> "
              f"{c['cycle'].get('status')!r} (continue disabled: {c['cycle'].get('continue_disabled')}), "
              f"convergence points {c['before'].get('points')} -> {c['cycle'].get('points')}: "
              f"{'ok' if c['ok'] else 'FAILED'}")
        progress = [t for t in c['cycle'].get('seen', []) if t and '·' in t]
        print(f"    progress lines during the cycle: {len(progress)}" +
              ''.join(f"\n      {t}" for t in progress[:6]))
        if ns.scripted and len(progress) < 2:
            print('    expected progress in the status line during the cycle')
            c['ok'] = False
        status = status or (0 if c['ok'] else 1)
    if 'stop' in page_result:
        st = page_result['stop']
        good = (st['exit'] == 0 and any(l.startswith('result {') for l in st['ended'])
                and any(l == 'APP-ENDED' for l in st['ended'])
                and (not ns.kernel or ns.scripted or 'session closed: True' in st['ended']))
        print(f"    stop: exit {st['exit']}; " + '; '.join(st['ended']) + f": {'ok' if good else 'FAILED'}")
        status = status or (0 if good else 1)
    contour = page_result.get('contour')
    if contour is not None and (contour['x'] != [3, 6] or contour['y'] != [2, 4]):
        print('    the mask outline is not on the pixel edges')      # before 2026-11-07 (notes §66)
        status = 1
    if not ns.no_casatools:
        page = json.load(open(os.path.join(ns.args[2], 'page.json')))['result']
        status = check_casatools(ns.args[2], page, ns.tolerance) or status
    sys.exit(status)
