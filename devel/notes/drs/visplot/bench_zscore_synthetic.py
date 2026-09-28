# Synthetic Z-Score raster benchmark (Part 6). See ZSCORE_OPTIMIZATION_HANDOFF.md.
# Compares: Amplitude mean, exact Z-Score (what _raster_2d does today), and a
# Z-Score whose per-baseline reference (median/scale) is estimated from a strided subsample.
# Identical data for every variant; plants a bad time window (bl 7) and a bad sample (bl 33).
import time, numpy as np, dask.array as da, xarray as xr, dask, resource
dask.config.set(scheduler="threads")
rng = np.random.default_rng(0)
nt, nb, nf = 400, 200, 384
RE = rng.normal(5, 1, (nt, nb, nf)).astype("float32"); IM = rng.normal(0, 1, (nt, nb, nf)).astype("float32")
RE[100:110, 7, :] += 12      # a genuine bad time window on one baseline
IM[250, 33, 200] += 15       # one bad sample
def mk():
    f = lambda a: xr.DataArray(da.from_array(a, chunks=(50, nb, nf)), dims=("time","baseline_id","frequency"))
    return f(RE), f(IM)
C = np.sqrt(2*np.log(2)); dims=["time","frequency"]

def amplitude():
    re, im = mk(); return np.sqrt(re**2+im**2).mean(dim="frequency").compute()
def zscore_exact():
    re, im = mk()
    dr = re - re.median(dim=dims, skipna=True); di = im - im.median(dim=dims, skipna=True)
    r = np.sqrt(dr**2+di**2); scale = r.median(dim=dims, skipna=True)
    return xr.where(scale>0, r/scale*C, np.nan).max(dim="frequency", skipna=True).transpose("time","baseline_id").compute()
def zscore_sub(k_t, k_f):
    def f():
        re, im = mk()
        sre = re.isel(time=slice(None,None,k_t), frequency=slice(None,None,k_f))
        sim = im.isel(time=slice(None,None,k_t), frequency=slice(None,None,k_f))
        mre = sre.median(dim=dims, skipna=True); mim = sim.median(dim=dims, skipna=True)
        scale = np.sqrt((sre-mre)**2+(sim-mim)**2).median(dim=dims, skipna=True)
        r = np.sqrt((re-mre)**2+(im-mim)**2)
        return xr.where(scale>0, r/scale*C, np.nan).max(dim="frequency", skipna=True).transpose("time","baseline_id").compute()
    return f
cases = [("amplitude (mean)", amplitude), ("zscore EXACT (current)", zscore_exact),
         ("zscore ref from 1/8 samples", zscore_sub(4,2)), ("zscore ref from 1/32", zscore_sub(8,4)), ("zscore ref from 1/128", zscore_sub(16,8))]
ref=None
for name, fn in cases:
    t=time.perf_counter(); out=fn(); dt=time.perf_counter()-t
    line=f"{name:30s} {dt:5.2f}s"
    if name.startswith("zscore EXACT"): ref=out.values
    elif ref is not None and name.startswith("zscore"):
        v=out.values; rel=np.abs(v-ref)/ref
        line += f"   mean rel diff {rel.mean()*100:5.2f}%  max {rel.max()*100:5.1f}%   outliers still found: bad-window={v[100:110,7].min()>10} bad-sample={v[250,33]>10}"
    print(line)
print("peak RSS MB:", resource.getrusage(resource.RUSAGE_SELF).ru_maxrss//1024)
