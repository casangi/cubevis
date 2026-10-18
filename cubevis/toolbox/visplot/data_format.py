"""Tell an MSv2 measurement set from an MSv4 / Processing Set on disk.

Used by ``vis=`` (the CASA-style name for the data argument), which
accepts either format: the plotter calls :func:`detect_data_format` on
the path and opens it as ``ms=`` or ``ps=`` would.  With a remote kernel
the same function runs on the kernel's host (``VisplotRemoteBackend``
with ``backend_kind="auto"``), where the path is.

What is looked at -- only the top directory, nothing is opened:

* **MSv2**: a casacore table directory (``table.dat``) whose
  ``table.info`` says ``Type = Measurement Set``.  A casacore table of
  another type (a calibration table, an image) is refused with its type.
  A table without ``table.info`` is accepted when it has the ``ANTENNA``
  and ``SPECTRAL_WINDOW`` sub-tables every MS has.
* **MSv4**: a Zarr group -- ``zarr.json`` (Zarr v3) or ``.zgroup`` /
  ``.zmetadata`` (Zarr v2) at the top.

Anything else (a missing path, a plain file such as a ``.tar.gz``, a
directory that is neither) raises with a message saying what was found
and what is accepted.
"""
from __future__ import annotations

import os

MSV2 = "msv2"
MSV4 = "msv4"

_ZARR_MARKERS = ("zarr.json", ".zgroup", ".zmetadata")
_ARCHIVE_SUFFIXES = (".tar", ".tar.gz", ".tgz", ".zip", ".tar.bz2", ".tar.xz")


def _table_type(path: str) -> str | None:
    """The ``Type = ...`` line of a casacore table's ``table.info``."""
    try:
        with open(os.path.join(path, "table.info"), encoding="utf-8",
                  errors="replace") as f:
            for line in f:
                if line.strip().lower().startswith("type"):
                    _, _, value = line.partition("=")
                    return value.strip()
    except OSError:
        return None
    return None


def detect_data_format(path: str) -> str:
    """Return ``"msv2"`` or ``"msv4"`` for the data at *path*.

    Raises ``FileNotFoundError`` when *path* does not exist and
    ``ValueError`` when it is not a measurement set of either format.
    """
    if not path:
        raise ValueError("vis= is empty: give the path of an MSv2 "
                         "measurement set or an MSv4 / Processing Set")
    p = os.path.expanduser(str(path)).rstrip("/") or "/"
    if not os.path.exists(p):
        raise FileNotFoundError(f"vis={path!r}: no such file or directory")
    if not os.path.isdir(p):
        if p.lower().endswith(_ARCHIVE_SUFFIXES):
            raise ValueError(
                f"vis={path!r} is an archive; unpack it first (for example "
                f"`tar -xzf {os.path.basename(p)}`) and give the directory "
                "it contains")
        raise ValueError(
            f"vis={path!r} is a file; a measurement set is a directory "
            "(MSv2: a casacore table; MSv4: a Zarr store)")
    if os.path.isfile(os.path.join(p, "table.dat")):
        ttype = _table_type(p)
        if ttype is None:
            if all(os.path.isdir(os.path.join(p, sub))
                   for sub in ("ANTENNA", "SPECTRAL_WINDOW")):
                return MSV2
            raise ValueError(
                f"vis={path!r} is a casacore table but not a measurement "
                "set (no table.info, no ANTENNA / SPECTRAL_WINDOW sub-tables)")
        if ttype.lower().replace(" ", "") == "measurementset":
            return MSV2
        raise ValueError(
            f"vis={path!r} is a casacore table of type {ttype!r}, not a "
            "measurement set")
    if any(os.path.exists(os.path.join(p, m)) for m in _ZARR_MARKERS):
        return MSV4
    raise ValueError(
        f"vis={path!r} is a directory but neither an MSv2 measurement set "
        "(no table.dat) nor an MSv4 / Processing Set (no zarr.json, "
        ".zgroup or .zmetadata)")


def resolve_data_args(vis, ms, ps) -> tuple[str, str]:
    """Check that exactly one of *vis*, *ms*, *ps* is given.

    Returns ``(kind, path)`` with *kind* ``"vis"``, ``"ms"`` or ``"ps"``;
    the caller still has to detect the format of a ``"vis"`` path (on the
    kernel's host when the data are remote).
    """
    given = [(k, v) for k, v in (("vis", vis), ("ms", ms), ("ps", ps))
             if v is not None and v != ""]
    if len(given) > 1:
        names = " and ".join(f"{k}=" for k, _ in given)
        raise ValueError(
            f"Give only one of vis=, ms= or ps= (got {names}). vis= takes "
            "either format; ms= is MSv2, ps= is MSv4 / Processing Set.")
    if not given:
        raise ValueError("One of vis=, ms= or ps= must be supplied (vis= "
                         "takes either an MSv2 or an MSv4 path).")
    return given[0]
