"""Time a first and a second open of an MSv2 in one fresh process.
Usage: python msv2_open_scaling.py {xms|xradio} my.ms   (assessment §10.3)"""
import sys, time, warnings; warnings.filterwarnings("ignore")
which, ms = sys.argv[1], sys.argv[2]
import xarray as xr
if which == "xradio":
    from xradio.measurement_set import open_msv2
t = time.perf_counter()
dt = xr.open_datatree(ms, engine="xarray-ms:msv2") if which == "xms" else open_msv2(ms)
t1 = time.perf_counter() - t
n = sum(1 for nd in dt.subtree if nd.attrs.get("type") == "visibility")
t = time.perf_counter()
dt2 = xr.open_datatree(ms, engine="xarray-ms:msv2") if which == "xms" else open_msv2(ms)
t2 = time.perf_counter() - t
print(f"{which:7} {ms:14} parts={n:3}  first-open={t1:6.2f}s  second-open-same-process={t2:6.2f}s")
