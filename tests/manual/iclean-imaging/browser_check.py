"""Run interactive clean (browser mode) from a cubevis checkout and look at the
page in a headless Chrome: console errors, the world coordinates the GUI
computes (ImagePipe.wcs(), as the axis labels and cursor readout do) for some
pixels, the spectral values of the coordinate description when there is one,
and a screenshot.  Two runs (two checkouts) can then be compared:

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
          const pt = new casalib.coordtxl.Point2D(x, y)
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
  }
  return out
}"""

APP = """import sys
sys.path.insert(0, sys.argv[1])
from cubevis.private.apps import InteractiveClean
ic = InteractiveClean(vis=sys.argv[2], imagename='br', imsize=128, cell='12.0arcsec', specmode='cube',
                      interpolation='nearest', nchan=5, start='1.0GHz', width='0.2GHz', pblimit=-1e-05,
                      deconvolver='hogbom', threshold='0.001Jy', cyclefactor=3, niter=50, cycleniter=10)
print('result', ic())
"""


def run(repo, ms, work, chrome):
    from playwright.sync_api import sync_playwright
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
    proc = subprocess.Popen([sys.executable, '-I', app, os.path.abspath(repo), os.path.abspath(ms)], cwd=work,
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
            browser.close()
    finally:
        proc.kill()
    json.dump({'result': result, 'console': console}, open(os.path.join(work, 'page.json'), 'w'), indent=1)
    errors = [c for c in console if c.startswith(('error', 'pageerror'))]
    print(f'{work}: {len((result or {}).get("pipes", []))} ImagePipe(s), {len(errors)} console error(s)')
    for e in errors[:8]:
        print('   ', e[:200])
    for pp in (result or {}).get('pipes', []):
        print('   ', 'coords_json' if pp['coords_json'] else 'fits_header_json',
              'wcs' if pp['wcs'] else 'NO wcs', [t[0] + ' ' + t[1] for t in pp['text'][:2]],
              'spectral values' if pp.get('spectral') else '')
    return 1 if errors or not result or not any(pp.get('wcs') for pp in result.get('pipes', [])) else 0


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
    ap.add_argument('args', nargs='*', metavar='REPO MS WORKDIR')
    ns = ap.parse_args()
    if ns.compare:
        sys.exit(compare(*ns.compare))
    if len(ns.args) != 3:
        ap.error('REPO MS WORKDIR')
    sys.exit(run(*ns.args, ns.chrome))
