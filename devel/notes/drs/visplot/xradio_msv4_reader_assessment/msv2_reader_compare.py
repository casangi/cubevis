"""Compare one MSv2 opened with xarray-ms and with xradio.open_msv2: open/read time, partitions, helpers.
Usage: python msv2_reader_compare.py my.ms   (assessment §10.2)"""
import time, warnings, sys, statistics
warnings.filterwarnings("ignore")
import numpy as np, xarray as xr, dask
from xradio.measurement_set import open_msv2
MS = sys.argv[1]
def t(f, n=3):
    f(); ts=[]
    for _ in range(n):
        s=time.perf_counter(); r=f(); ts.append(time.perf_counter()-s)
    return statistics.median(ts), r
def parts(dt):
    out={}
    for node in dt.subtree:
        if node.has_data and node.attrs.get("type")=="visibility": out[node.name]=node.ds
    return out
def red(dt):
    return dask.compute(*[abs(ds["VISIBILITY"]).where(~ds["FLAG"].astype(bool)).sum().data for ds in parts(dt).values()])
for name, opener in [("xarray-ms", lambda: xr.open_datatree(MS, engine="xarray-ms:msv2")),
                     ("xradio.open_msv2", lambda: open_msv2(MS))]:
    to, dt = t(opener)
    p = parts(dt)
    tr, r = t(lambda: red(dt))
    k = next(iter(p)); ds = p[k]
    print(f"{name:18} open {to:6.3f}s  reduce {tr:6.3f}s  root.type={dt.attrs.get('type')!s:15} nparts={len(p)} total={sum(float(x) for x in r):.6e}")
    print(f"{'':18} first={k} dims={dict(ds.sizes)} chunks={getattr(ds['VISIBILITY'].data,'chunksize','not dask')}")
    print(f"{'':18} children={list(dt[k].children)[:4]} data_groups={list(dt[k].attrs.get('data_groups',{}))}")
    try:
        dt.xr_ps.summary(); print(f"{'':18} xr_ps.summary OK")
    except Exception as e: print(f"{'':18} xr_ps.summary FAIL {type(e).__name__}")
