"""What a kernel can run: its **capabilities** (iclean design, step 5b).

A kernel serves an application only when its environment has that
application's kernel-side requirements:

* ``visplot`` -- xarray-ms (and arcae under it) import;
* ``iclean`` -- casatasks and casatools are installed, with the gclean
  module interactive clean drives.  They are *not* imported: importing
  casatools writes a ``casa-*.log`` file into the current directory and may
  start CASA's own data updates.  The kernel's real import reports any
  failure when interactive clean starts.

and, for interactive clean, CASA's data (``casa_data``): the measures
casatools finds through casaconfig -- the site configuration
(``CASASITECONFIG``, ``/opt/casa`` or ``/home/casa``, a ``casasiteconfig``
module), then the user's ``~/.casa/config.py``, which overrides it.
``casaconfig.config`` alone reads that configuration without changing
anything.

``PROBE`` is run in the kernel's own context -- through its launcher, with
its working directory and variables (``launcher.test_program``) -- so what
it reports is what a kernel sees.  ``parse`` reads its line; ``require`` is
the laptop side: an application refuses a kernel that setup recorded as
lacking its capability, before connecting.  A kernel setup does not know
(not one of its own, or checked before capabilities were recorded) is
never refused here.

Standard library only (the laptop side runs before anything else is
imported).
"""
from __future__ import annotations

import json
from typing import Optional

LINE = "CUBEVIS-CAPS "

APPS = ("visplot", "iclean")
WHAT = {"visplot": "visplot", "iclean": "interactive clean"}
NEEDS = {"visplot": "xarray-ms", "iclean": "casatasks"}

# The site configuration setup writes in an environment it gave a private
# copy of CASA's data (``envmake``); kernels of that environment get
# CASASITECONFIG pointing at it (``launcher.text``).
SITE_CONFIG_FILE = "cubevis-casasiteconfig.py"
SITE_CONFIG_TEXT = '''\
# Written by python -m cubevis.setup: CASA's data for this environment's
# kernels -- the casadata package installed here, which setup never updates.
# Kernels get CASASITECONFIG pointing at this file; a measurespath in your
# own ~/.casa/config.py still overrides it.
import casadata
measurespath = casadata.datapath
datapath = [casadata.datapath]
measures_auto_update = False
data_auto_update = False
'''

# Run in the kernel's context.  Defines nothing it does not need; prints one
# line.
PROBE = r'''
def _cubevis_caps():
    import importlib.util, json, os
    caps, errors = {}, {}
    try:
        import xarray_ms                                    # noqa: F401
        caps["visplot"] = True
    except BaseException as exc:
        caps["visplot"] = False
        errors["visplot"] = "%s: %s" % (type(exc).__name__, exc)
    try:
        from importlib.metadata import version
    except ImportError:
        version = None
    vers = {}
    for d in ("casatasks", "casatools", "casaconfig", "casadata"):
        try:
            vers[d] = version(d) if version else None
        except Exception:
            vers[d] = None
    gclean = False
    try:
        spec = importlib.util.find_spec("casatasks")       # not imported
        for d in (spec.submodule_search_locations or []) if spec else []:
            gclean = gclean or os.path.isfile(os.path.join(d, "private", "imagerhelpers",
                                                           "_gclean.py"))
    except Exception:
        pass
    caps["iclean"] = bool(vers["casatasks"] and vers["casatools"] and gclean)
    if not caps["iclean"]:
        errors["iclean"] = ("casatasks is not installed" if not vers["casatasks"] else
                            "casatools is not installed" if not vers["casatools"] else
                            "casatasks has no gclean (interactive clean's imager)")
    data = None
    if caps["iclean"]:
        data = {"ready": False}
        try:
            from casaconfig import config as cfg               # reads the configuration only
            mp = cfg.measurespath
            data.update(measurespath=mp, datapath=list(cfg.datapath or []),
                        files=[f for f in getattr(cfg, "__loaded_config_files", [])[1:]],
                        site=os.environ.get("CASASITECONFIG"),
                        auto_update=bool(cfg.measures_auto_update or cfg.data_auto_update))
            errs = getattr(cfg, "__errors_encountered", {}) or {}
            if errs:
                data["errors"] = {k: str(v).strip().splitlines()[-1] for k, v in errs.items()}
            if mp is None:
                data["why"] = "no measurespath is set (a site configuration without one?)"
            elif os.path.isdir(os.path.join(mp, "geodetic")):
                data["ready"] = True
            elif os.path.isdir(mp):
                data["why"] = "%s has no measures (no geodetic directory)" % mp
            else:
                data["why"] = "%s does not exist" % mp
        except BaseException as exc:
            data["why"] = "casaconfig: %s: %s" % (type(exc).__name__, exc)
    print("\nCUBEVIS-CAPS " + json.dumps({"capabilities": caps, "errors": errors,
                                          "versions": vers, "casa_data": data}))

_cubevis_caps()
'''


def parse(out: str) -> Optional[dict]:
    """The probe's report from *out*: ``capabilities`` ({app: bool}),
    ``errors`` ({app: why not}), ``versions``, ``casa_data`` (None without
    interactive clean); None when it said nothing."""
    for line in (out or "").splitlines():
        if line.startswith(LINE):
            try:
                rep = json.loads(line[len(LINE):])
            except ValueError:
                return None
            return rep if isinstance(rep, dict) else None
    return None


def describe(caps: Optional[dict]) -> str:
    """``visplot, interactive clean`` -- what a kernel (or an environment)
    can run, in words; ``"not known"`` when it was never checked."""
    if not caps:
        return "not known"
    can = [WHAT[a] for a in APPS if caps.get(a)]
    return ", ".join(can) if can else "neither visplot nor interactive clean"


def describe_data(data: Optional[dict], home: Optional[str] = None) -> str:
    """CASA's data as a kernel sees it, in a few words (paths in *home*
    with ``~``)."""
    if not data:
        return "not known"
    where = data.get("measurespath") or "?"
    if home and (where == home or where.startswith(home.rstrip("/") + "/")):
        where = "~" + where[len(home.rstrip("/")):]
    if data.get("ready"):
        return f"in {where}" + (" (auto-updated)" if data.get("auto_update") else "")
    return data.get("why") or "not found"


def kernel_record(kernel_name: str) -> Optional[dict]:
    """Setup's record of the kernel a Jupyter kernel name (``cubevis-NAME``,
    ``cubevis-NAME-NODE`` on another node of a cluster, or NAME) stands for,
    or None (no setup state, or not one of its own)."""
    try:
        from . import state
        from .sessions import setup_kernel_name
        st = state.load()
        kernels = st.section("kernels")
    except Exception:
        return None
    for name in (setup_kernel_name(kernel_name, st), kernel_name):
        if name in kernels:
            return kernels[name]
    return None


class MissingCapability(RuntimeError):
    """The kernel cannot run the application (setup's record says so)."""


def require(kernel_name: str, app: str) -> None:
    """Refuse *kernel_name* for *app* when setup recorded that it cannot run
    it (``MissingCapability``, with what to do).  Silent when setup knows
    nothing about the kernel or its capabilities."""
    k = kernel_record(kernel_name)
    caps = (k or {}).get("capabilities")
    if not caps or caps.get(app) is not False:
        return
    why = ((k.get("capability_errors") or {}).get(app)
           or f"{NEEDS[app]} is not in its environment")
    raise MissingCapability(
        f"kernel {kernel_name!r} cannot run {WHAT[app]}: {why}. "
        f"python -m cubevis.setup HOST (the host it is on) offers to add it to an environment "
        f"setup made; in your own environment, pip install \"cubevis[{app}]\" there.")
