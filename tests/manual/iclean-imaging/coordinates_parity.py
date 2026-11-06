"""World coordinates from the coordinate description (design §2.5) against
what they were before (2026-11-06) and against casatools.

For a set of CASA images (made here with casatools from IMAGE: its own
frame, then ICRS, FK4/B1950, galactic and a rotated and a TAN-projected
copy, and a non-square one with an off-centre reference pixel) it compares

* in the browser: coordtxl built from the FITS header keyword map
  ImagePipe sent until 2026-11-06 (``fits_header_json``) with coordtxl
  built from the description (``description_keywords`` in
  ``cubevisjs/src/bokeh/util/coordinate_description.ts``, compiled and run
  under node with the coordtxl package), including the formatted strings
  the GUI shows;
* in Python: ``astropy_wcs(description)`` with the astropy WCS made from the
  FITS header string (what createregion's ``RegionList`` used) and with
  casatools' own pixel -> world.

    python coordinates_parity.py IMAGE WORKDIR COORDTXL_DIR TSC

COORDTXL_DIR: an unpacked coordtxl npm package (``npm pack coordtxl@0.1.4``);
TSC: a TypeScript compiler (``npx tsc`` from ``npm install typescript``).
Needs casatools.  Exit status 1 if a check fails.
"""
import sys, os, json, math, shutil, subprocess
import numpy as np

image_path, work, coordtxl_dir, tsc = sys.argv[1:5]
here = os.path.dirname(os.path.abspath(__file__))
repo = os.path.abspath(os.path.join(here, '..', '..', '..'))
os.makedirs(work, exist_ok=True)

from casatools import image as _image
from astropy.wcs import WCS
from cubevis.imaging import open_image_cube, astropy_wcs
from cubevis.utils import strip_arrays

R2D = 180.0 / math.pi


def make_case(name, edit=None, shape=None, refpix=None):
    '''a copy of IMAGE (one plane) with its coordinate system edited'''
    ia = _image(); ia.open(image_path)
    cs = ia.coordsys()
    full = list(ia.shape())
    ia.close()
    shp = list(shape or full[:2]) + [1, 1]
    if refpix is not None:
        rp = cs.referencepixel()['numeric']; rp[0:2] = refpix; cs.setreferencepixel(rp)
    if edit:
        edit(cs)
    out = os.path.join(work, name + '.im')
    if os.path.exists(out):
        shutil.rmtree(out)
    ib = _image(); ib.fromshape(out, shp, csys=cs.torecord(), overwrite=True); ib.close()
    return out


def rotate(deg):
    def f(cs):
        a = math.radians(deg)
        cs.setlineartransform('direction', np.array([[math.cos(a), -math.sin(a)], [math.sin(a), math.cos(a)]]))
    return f


cases = {
    'own_frame': make_case('own_frame'),
    'icrs': make_case('icrs', lambda cs: cs.setreferencecode('ICRS', 'direction', adjust=False)),
    'fk4_b1950': make_case('fk4_b1950', lambda cs: cs.setreferencecode('B1950', 'direction', adjust=False)),
    'galactic': make_case('galactic', lambda cs: cs.setreferencecode('GALACTIC', 'direction', adjust=False)),
    'rotated_30': make_case('rotated_30', rotate(30.0)),
    'tan': make_case('tan', lambda cs: cs.setprojection('TAN')),
    'nonsquare_offcentre': make_case('nonsquare_offcentre', shape=[200, 90], refpix=[10.0, 80.0]),
}

exported, truth, py = {}, {}, {}
for name, path in cases.items():
    ia = _image(); ia.open(path)
    cs = ia.coordsys()
    nx, ny = list(ia.shape())[:2]
    pixels = [[x, y] for x in np.linspace(0, nx - 1, 7) for y in np.linspace(0, ny - 1, 5)] + [[nx / 2 + 0.3, ny / 3 + 0.7]]
    header, header_str = ia.fitsheader(exclude='HISTORY'), ia.fitsheader(retstr=True, exclude='HISTORY')
    truth[name] = [list(cs.toworld([x, y, 0, 0])['numeric'][:2] * R2D) for x, y in pixels]
    ia.close()
    desc = open_image_cube(path).coordinates()
    exported[name] = {'header_map': json.loads(json.dumps(strip_arrays(header))),   # exactly what the browser got
                      'description': desc, 'pixels': pixels}
    w_desc = astropy_wcs(desc).sub([1, 2])
    w_hdr = WCS(header=header_str).sub([1, 2])
    py[name] = {'desc': w_desc.wcs_pix2world(np.array(pixels), 0).tolist(),
                'header': w_hdr.wcs_pix2world(np.array(pixels), 0).tolist()}

with open(os.path.join(work, 'cases.json'), 'w') as f:
    json.dump(exported, f)

# compile coordinate_description.ts alone (it has no imports) to CommonJS
ts = os.path.join(repo, 'cubevisjs', 'src', 'bokeh', 'util', 'coordinate_description.ts')
subprocess.run([tsc, '--module', 'commonjs', '--target', 'ES2019', '--strict', '--outDir', work, ts], check=True)

# coordtxl 0.1.4 reads the projection from CTYPE1 with substring(3, 7) ("--SI" for
# "RA---SIN"), so it never recognizes one and treats every image as linear; a copy with
# substring(4, 8) shows what the description gives once that is fixed
fixed_dir = os.path.join(work, 'coordtxl_fixed')
if os.path.exists(fixed_dir):
    shutil.rmtree(fixed_dir)
shutil.copytree(coordtxl_dir, fixed_dir)
wcs_js = os.path.join(fixed_dir, 'coords', 'WCSTransform.js')
src = open(wcs_js).read()
if src.count('this.ptype = wcstemp.substring(3, 7);') == 1:
    open(wcs_js, 'w').write(src.replace('this.ptype = wcstemp.substring(3, 7);', 'this.ptype = wcstemp.substring(4, 8);'))
else:
    print('note: coordtxl does not have the substring(3, 7) projection parsing; the "fixed" copy is unchanged')


def run_node(cdir, out):
    subprocess.run(['node', os.path.join(here, 'coordtxl_parity.cjs'), cdir,
                    os.path.join(work, 'coordinate_description.js'), os.path.join(work, 'cases.json'),
                    os.path.join(work, out)], check=True)
    return json.load(open(os.path.join(work, out)))


js = run_node(coordtxl_dir, 'coordtxl.json')
js_fixed = run_node(fixed_dir, 'coordtxl_fixed.json')


def sep_arcsec(a, b):
    '''largest angular separation (haversine: precise for small angles)'''
    a, b = np.radians(np.array(a)), np.radians(np.array(b))
    lon1, lat1, lon2, lat2 = a[:, 0], a[:, 1], b[:, 0], b[:, 1]
    h = np.sin((lat2 - lat1) / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin((lon2 - lon1) / 2) ** 2
    return float(np.degrees(2 * np.arcsin(np.sqrt(np.clip(h, 0, 1)))).max() * 3600)


failures = []
print(f"{'case':22} {'coordtxl desc vs header':>24} {'strings':>8} {'astropy desc vs casatools':>26} {'header WCS vs casatools':>24}")
for name in cases:
    j = js[name]
    if 'error' in j['description']:
        failures.append(f'{name}: coordtxl from the description: {j["description"]["error"]}')
        print(f'{name:22} ERROR {j["description"]["error"]}')
        continue
    if 'error' in j['header']:
        hdr_vs_desc, strings = f"header: {j['header']['error'][:20]}", '-'
    else:
        hdr_vs_desc = f"{sep_arcsec(j['description']['world'], j['header']['world']):.3g}\""
        same = sum(a == b for a, b in zip(j['description']['text'], j['header']['text']))
        strings = f"{same}/{len(j['header']['text'])}"
    a_vs_t = sep_arcsec(py[name]['desc'], truth[name])
    h_vs_t = sep_arcsec(py[name]['header'], truth[name])
    print(f"{name:22} {hdr_vs_desc:>24} {strings:>8} {a_vs_t:>25.3g}\" {h_vs_t:>23.3g}\"")
    if a_vs_t > 1e-6:
        failures.append(f'{name}: astropy_wcs(description) differs from casatools by {a_vs_t}"')

# coordtxl against casatools.  coordtxl's pixels are 1-based (FITS): its (x+1, y+1) is
# casatools' (x, y); the GUI passes 0-based pixel indices.
print()
print(f"{'coordtxl vs casatools (1-based)':32} {'header, 0.1.4':>14} {'desc, 0.1.4':>12} {'header, fixed':>14} {'desc, fixed':>12}")
for name in cases:
    ia = _image(); ia.open(cases[name]); cs = ia.coordsys()
    t1 = [list(cs.toworld([x - 1, y - 1, 0, 0])['numeric'][:2] * R2D) for x, y in exported[name]['pixels']]
    ia.close()
    cols = []
    for result in (js, js_fixed):
        for way in ('header', 'description'):
            w = result[name][way]
            cols.append('error' if 'error' in w else f"{sep_arcsec(w['world'], t1):.3g}\"")
    print(f"{name:32} {cols[0]:>14} {cols[1]:>12} {cols[2]:>14} {cols[3]:>12}")
    # with the projection parsed, the description must be exact wherever the header
    # keywords are, and never worse (FK4: coordtxl itself differs, both ways)
    wd, wh = js_fixed[name]['description'], js_fixed[name]['header']
    if 'error' in wd:
        failures.append(f'{name}: coordtxl (projection parsing fixed) cannot use the description')
    else:
        dd = sep_arcsec(wd['world'], t1)
        dh = float('inf') if 'error' in wh else sep_arcsec(wh['world'], t1)
        if dd > 1e-3 and dd > dh + 1e-3:
            failures.append(f'{name}: coordtxl (projection parsing fixed): description {dd}" vs header {dh}"')

print()
print('FAILED:\n  ' + '\n  '.join(failures) if failures else 'all checks passed')
sys.exit(1 if failures else 0)
