# Run by `python -m cubevis.setup node-here` with each candidate Python, to
# see whether it can run cubevis kernels.  It changes nothing.  Standard
# library only; it must run (and report) even in a Python too old for
# cubevis, so no f-strings or other recent syntax.
#
# One line on stdout:  CUBEVIS-ENVCHECK {json}
#   python, executable, prefix, base_prefix, conda (a conda/mamba prefix),
#   versions {dist: version or null}, kernel (true: cubevis' kernel side and
#   ipykernel import), visplot (true: its data backend imports), errors
#   {what: last line of the error}, install (how cubevis got here: from its
#   packaging metadata -- see below).
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


# How cubevis was installed, from what the installers record (nothing is
# guessed): INSTALLER names the program (pip, uv, conda); direct_url.json
# (PEP 610) is there for an install from a local directory -- editable or
# not -- or from a VCS URL; conda-meta/cubevis-*.json for conda (or mamba,
# micromamba) installing it as a conda package.
install = {"installer": None, "conda": False, "url": None, "editable": False, "vcs": None,
           "archive": False}
try:
    from importlib.metadata import distribution
    dist = distribution("cubevis")
    inst = dist.read_text("INSTALLER")
    install["installer"] = inst.strip() if inst else None
    du = dist.read_text("direct_url.json")
    if du:
        d = json.loads(du)
        install["url"] = d.get("url")
        install["editable"] = bool((d.get("dir_info") or {}).get("editable"))
        install["vcs"] = (d.get("vcs_info") or {}).get("vcs")
        install["archive"] = "archive_info" in d     # a wheel or sdist file, not a directory
except Exception:
    pass
try:
    import glob
    install["conda"] = bool(glob.glob(os.path.join(sys.prefix, "conda-meta", "cubevis-*.json")))
except Exception:
    pass
out["install"] = install

# The kernel side: what a remote kernel imports to serve visplot/iclean.
kernel = _try("cubevis", "cubevis.remote.worker_main")
kernel = _try("ipykernel", "ipykernel") and kernel
out["kernel"] = kernel
# visplot reads measurement sets through xarray-ms / arcae.
out["visplot"] = _try("visplot", "xarray_ms") if kernel else False

sys.stdout.write("CUBEVIS-ENVCHECK " + json.dumps(out) + "\n")
