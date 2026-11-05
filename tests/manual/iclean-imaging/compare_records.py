"""Deep-compare two record_iclean_handlers.py pickles; print every difference
(floats to 1e-6 relative; FITS cards that change from run to run -- DATE,
MEMAVAIL, IMAGENME, HISTORY -- ignored).  Exit status 1 when they differ."""
import sys, pickle, math
import numpy as np

a, b = (pickle.load(open(p, 'rb')) for p in sys.argv[1:3])
diffs = []
VOLATILE = ('MEMAVAIL', 'DATE', 'IMAGENME', 'HISTORY')
import json
for r in (a, b):
    if isinstance(r.get('fits_header'), dict):
        for k in VOLATILE: r['fits_header'].pop(k, None)
    if r.get('fits_header_json'):
        h = json.loads(r['fits_header_json'])
        for k in VOLATILE: h.pop(k, None)
        r['fits_header_json'] = h
    if r.get('fits_header_str'):
        s_ = r['fits_header_str']
        cards = [s_[i:i+80] for i in range(0, len(s_), 80)]
        r['fits_header_str'] = [c for c in cards if c[:8].strip() not in VOLATILE]

def cmp(x, y, path):
    if isinstance(x, np.ndarray) or isinstance(y, np.ndarray):
        x, y = np.asarray(x), np.asarray(y)
        if x.shape != y.shape:
            diffs.append(f'{path}: shape {x.shape} != {y.shape}'); return
        if x.dtype.kind in 'fc' or y.dtype.kind in 'fc':
            if not np.allclose(x, y, rtol=1e-6, atol=1e-9, equal_nan=True):
                diffs.append(f'{path}: max |d| {np.nanmax(np.abs(x - y))}')
        elif not np.array_equal(x, y):
            diffs.append(f'{path}: arrays differ ({x.dtype} vs {y.dtype})')
        return
    if isinstance(x, dict) and isinstance(y, dict):
        if set(x) != set(y):
            diffs.append(f'{path}: keys {sorted(set(x) ^ set(y))}')
        for k in set(x) & set(y):
            if k == 'id' and path.startswith('channel'):
                continue
            cmp(x[k], y[k], f'{path}.{k}')
        return
    if isinstance(x, (list, tuple)) and isinstance(y, (list, tuple)):
        if len(x) != len(y):
            diffs.append(f'{path}: len {len(x)} != {len(y)}'); return
        for i, (p, q) in enumerate(zip(x, y)):
            cmp(p, q, f'{path}[{i}]')
        return
    if isinstance(x, float) and isinstance(y, float):
        if not (math.isclose(x, y, rel_tol=1e-6, abs_tol=1e-9) or (math.isnan(x) and math.isnan(y))):
            diffs.append(f'{path}: {x} != {y}')
        return
    if type(x) is not type(y) and not (isinstance(x, (int, float, np.number)) and isinstance(y, (int, float, np.number))):
        diffs.append(f'{path}: type {type(x).__name__} != {type(y).__name__}')
        return
    try:
        eq = x == y
        eq = bool(np.all(eq))
    except Exception:
        eq = repr(x) == repr(y)
    if not eq:
        diffs.append(f'{path}: {repr(x)[:80]} != {repr(y)[:80]}')

for k in sorted(set(a) | set(b)):
    if k not in a or k not in b:
        diffs.append(f'{k}: only in {"first" if k in a else "second"}'); continue
    cmp(a[k], b[k], k)
print('\n'.join(diffs) if diffs else 'IDENTICAL (within tolerance)')
print(f'{len(a)} vs {len(b)} entries, {len(diffs)} differences')
sys.exit(1 if diffs else 0)
