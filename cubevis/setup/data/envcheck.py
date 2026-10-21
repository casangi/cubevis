# Run by `python -m cubevis.setup node-here` with each candidate Python, to
# see whether it can run cubevis kernels.  It changes nothing.  Standard
# library only; it must run (and report) even in a Python too old for
# cubevis, so no f-strings or other recent syntax.
#
# One line on stdout:  CUBEVIS-ENVCHECK {json}
#   python, executable, prefix, base_prefix, conda (a conda/mamba prefix),
#   versions {dist: version or null}, kernel (true: cubevis' kernel side and
#   ipykernel import), visplot (true: its data backend imports), errors
#   {what: last line of the error}.
import json
import os
import sys
import traceback

out = {
    "python": "%d.%d.%d" % tuple(sys.version_info[:3]),
    "executable": sys.executable,
    "prefix": sys.prefix,
    "base_prefix": getattr(sys, "base_prefix", sys.prefix),
    "conda": os.path.isdir(os.path.join(sys.prefix, "conda-meta")),
    "versions": {},
    "errors": {},
}

try:
    from importlib.metadata import version as _version
except Exception:          # Python < 3.8
    _version = None

for dist in ("cubevis", "ipykernel", "jupyter_client", "bokeh", "xarray-ms", "arcae",
             "casatasks"):
    v = None
    if _version is not None:
        try:
            v = _version(dist)
        except Exception:
            v = None
    out["versions"][dist] = v


def _try(what, module):
    try:
        __import__(module)
        return True
    except BaseException:
        lines = traceback.format_exc().strip().splitlines()
        out["errors"][what] = lines[-1] if lines else "import failed"
        return False


# The kernel side: what a remote kernel imports to serve visplot/iclean.
kernel = _try("cubevis", "cubevis.remote.worker_main")
kernel = _try("ipykernel", "ipykernel") and kernel
out["kernel"] = kernel
# visplot reads measurement sets through xarray-ms / arcae.
out["visplot"] = _try("visplot", "xarray_ms") if kernel else False

sys.stdout.write("CUBEVIS-ENVCHECK " + json.dumps(out) + "\n")
