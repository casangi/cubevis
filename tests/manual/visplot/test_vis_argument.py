"""
test_vis_argument.py
====================
``vis=`` (2026-10-18): the CASA name for the data argument, taking either
an MSv2 measurement set or an MSv4 / Processing Set; the format is
detected (``data_format.detect_data_format``), on the kernel's host when a
kernel runs the data access.  Exactly one of ``vis``, ``ms`` and ``ps``.

Detection is tested on directories made here; the plotter on the TW Hya
MSv2 / MSv4 (``MS`` / ``PS``, skipped without them) or a small simulated
MSv2; the remote "auto" path on the local ``python3`` kernel.

    pytest test_vis_argument.py
"""
from __future__ import annotations

import os
import warnings

import pytest

from cubevis.toolbox.visplot.data_format import (
    MSV2, MSV4, detect_data_format, resolve_data_args)

warnings.filterwarnings("ignore")


# ---------------------------------------------------------------------- #
# Detection                                                                #
# ---------------------------------------------------------------------- #

def _table(path, ttype="Measurement Set", info=True, subtables=()):
    os.makedirs(path)
    open(os.path.join(path, "table.dat"), "wb").close()
    if info:
        with open(os.path.join(path, "table.info"), "w") as f:
            f.write(f"Type = {ttype}\nSubType = \n\nwhatever\n")
    for s in subtables:
        os.makedirs(os.path.join(path, s))
    return path


def test_msv2_by_table_info(tmp_path):
    assert detect_data_format(_table(str(tmp_path / "a.ms"))) == MSV2
    # Trailing slash (tab completion) and ~ are fine.
    assert detect_data_format(str(tmp_path / "a.ms") + "/") == MSV2


def test_msv2_without_table_info_needs_subtables(tmp_path):
    p = _table(str(tmp_path / "b.ms"), info=False,
               subtables=("ANTENNA", "SPECTRAL_WINDOW"))
    assert detect_data_format(p) == MSV2
    q = _table(str(tmp_path / "c.ms"), info=False)
    with pytest.raises(ValueError, match="not a measurement set"):
        detect_data_format(q)


def test_other_casacore_table_refused(tmp_path):
    p = _table(str(tmp_path / "cal.G"), ttype="Calibration")
    with pytest.raises(ValueError, match="'Calibration'"):
        detect_data_format(p)


@pytest.mark.parametrize("marker", ["zarr.json", ".zgroup", ".zmetadata"])
def test_msv4_by_zarr_marker(tmp_path, marker):
    p = tmp_path / "x.ps.zarr"
    p.mkdir()
    (p / marker).write_text("{}")
    assert detect_data_format(str(p)) == MSV4


def test_missing_file_archive_and_plain_dir(tmp_path):
    with pytest.raises(FileNotFoundError):
        detect_data_format(str(tmp_path / "nope.ms"))
    tgz = tmp_path / "data.ms.tar.gz"
    tgz.write_bytes(b"\x1f\x8b")
    with pytest.raises(ValueError, match="archive"):
        detect_data_format(str(tgz))
    f = tmp_path / "notes.txt"
    f.write_text("x")
    with pytest.raises(ValueError, match="is a file"):
        detect_data_format(str(f))
    d = tmp_path / "empty"
    d.mkdir()
    with pytest.raises(ValueError, match="neither"):
        detect_data_format(str(d))
    with pytest.raises(ValueError, match="empty"):
        detect_data_format("")


def test_resolve_data_args():
    assert resolve_data_args("a", None, None) == ("vis", "a")
    assert resolve_data_args(None, "a", None) == ("ms", "a")
    assert resolve_data_args(None, None, "a") == ("ps", "a")
    # The task layers pass "" for an unset string.
    assert resolve_data_args("", "a", "") == ("ms", "a")
    for args, names in ((("a", "b", None), "vis= and ms="),
                        (("a", None, "b"), "vis= and ps="),
                        ((None, "a", "b"), "ms= and ps="),
                        (("a", "b", "c"), "vis= and ms= and ps=")):
        with pytest.raises(ValueError, match=names):
            resolve_data_args(*args)
    with pytest.raises(ValueError, match="must be supplied"):
        resolve_data_args(None, None, None)


# ---------------------------------------------------------------------- #
# The real data and the plotter                                           #
# ---------------------------------------------------------------------- #

def _simulated(tmp_path_factory):
    sim = pytest.importorskip("xarray_ms.testing.simulator")
    path = str(tmp_path_factory.mktemp("vis") / "sim.ms")
    sim.MSStructureSimulator(ntime=4, nantenna=4, data_description=[(8, ["XX", "YY"])],
                             simulate_data=True).simulate_ms(path)
    return path


@pytest.fixture(scope="module")
def msv2(tmp_path_factory):
    return os.environ.get("MS") or _simulated(tmp_path_factory)


def test_real_data_detected(msv2):
    assert detect_data_format(msv2) == MSV2
    if os.environ.get("PS"):
        assert detect_data_format(os.environ["PS"]) == MSV4


def _plotter(**kw):
    from cubevis.toolbox.visplot import VisibilityPlotter
    if os.environ.get("MS"):
        kw.setdefault("field", "3c279")
    return VisibilityPlotter(headless=True, layout="one", kind="raster", **kw)


def test_plotter_vis_msv2(msv2):
    p = _plotter(vis=msv2)
    assert p._ms_path == msv2 and p._ps_path is None
    assert "ms=" in p.reproducing_call()


def test_plotter_vis_msv4():
    ps = os.environ.get("PS")
    if not ps:
        pytest.skip("PS not set")
    p = _plotter(vis=ps)
    assert p._ps_path == ps and p._ms_path is None
    assert "ps=" in p.reproducing_call()


def test_plotter_more_than_one_refused(msv2):
    from cubevis.toolbox.visplot import VisibilityPlotter
    with pytest.raises(ValueError, match="only one of vis=, ms= or ps="):
        VisibilityPlotter(headless=True, vis=msv2, ms=msv2)
    with pytest.raises(ValueError, match="must be supplied"):
        VisibilityPlotter(headless=True)


def test_task_signature_has_vis():
    import inspect
    from cubevis import visplot
    assert "vis" in inspect.signature(visplot).parameters


def _local_kernel():
    """A kernel on this host, whatever CUBEVIS_TEST_KERNEL says: what is
    tested needs this machine's path, environment and cubevis checkout in
    the worker (a kernel elsewhere runs its own installed cubevis and sees
    neither).  Skips when there is no python3 kernelspec."""
    pytest.importorskip("jupyter_client")
    from jupyter_client.kernelspec import KernelSpecManager, NoSuchKernel
    try:
        KernelSpecManager().get_kernel_spec("python3")
    except NoSuchKernel:
        pytest.skip("no python3 kernelspec on this host")
    return "python3"


def test_remote_auto_detects_on_kernel(msv2):
    """backend_kind="auto": the worker decides the format and reports it.
    On a kernel on this host (python3; see _local_kernel)."""
    kernel = _local_kernel()
    from cubevis.toolbox.visplot.remote_reduction_context import RemoteReductionContext
    # Absolute: the kernel's working directory need not be this one.
    ctx = RemoteReductionContext(os.path.abspath(msv2), kernel, backend_kind="auto",
                                 call_timeout=300)
    try:
        assert ctx.backend_kind == MSV2
        assert ctx.list_fields()
    finally:
        ctx.close()
