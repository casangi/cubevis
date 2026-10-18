"""
test_memory_guard.py
====================
Scatter memory guard (2026-10-18), opt-in.  By default nothing is refused
(a large selection is only logged); ``CUBEVIS_VISPLOT_MEMORY_GUARD=<MB>``
refuses, before reading, a scatter estimated to need more
(``reader.check_scatter_memory``).

* no limit by default; the environment variable sets one;
* both backends refuse through ``query_columns`` with the message;
* a headless export raises; the GUI's start-up render leaves the panel
  empty and puts the reason in the status line; Plot reports it;
* a headless One-panel export no longer renders slot B at all.

The limit is forced small with the environment variable so the tests need
little memory.  Uses ``MS`` / ``PS`` (TW Hya), else a simulated MSv2.
"""
from __future__ import annotations

import asyncio
import os
import warnings

import pytest

from cubevis.toolbox.visplot.data import reader as R

warnings.filterwarnings("ignore")


@pytest.fixture
def tiny_budget(monkeypatch):
    monkeypatch.setenv(R.MEMORY_GUARD_ENV, "1")      # 1 MB
    yield


def test_budget_and_override(monkeypatch):
    monkeypatch.delenv(R.MEMORY_GUARD_ENV, raising=False)
    assert R.scatter_memory_budget() is None
    R.check_scatter_memory(10 ** 12, 2)               # default: never refuses
    monkeypatch.setenv(R.MEMORY_GUARD_ENV, "250")
    assert R.scatter_memory_budget() == 250 << 20
    for off in ("off", "OFF", "0", "no"):
        monkeypatch.setenv(R.MEMORY_GUARD_ENV, off)
        assert R.scatter_memory_budget() is None
        R.check_scatter_memory(10 ** 12, 2)           # no check
    monkeypatch.setenv(R.MEMORY_GUARD_ENV, "nonsense")
    assert R.scatter_memory_budget() is None


def test_message(monkeypatch):
    monkeypatch.setenv(R.MEMORY_GUARD_ENV, "100")
    R.check_scatter_memory(100_000, 2)                # 11 MB: fits
    with pytest.raises(R.SelectionTooLargeError) as ei:
        R.check_scatter_memory(67_000_000, 2)
    msg = str(ei.value)
    assert "67.0 M samples in 2 layers" in msg
    assert "Narrow the selection" in msg and R.MEMORY_GUARD_ENV in msg
    assert isinstance(ei.value, ValueError)


def _simulated(tmp_path_factory):
    sim = pytest.importorskip("xarray_ms.testing.simulator")
    path = str(tmp_path_factory.mktemp("mg") / "sim.ms")
    sim.MSStructureSimulator(ntime=20, nantenna=8, data_description=[(64, ["XX", "YY"])],
                             simulate_data=True).simulate_ms(path)
    return path


@pytest.fixture(scope="module")
def data(tmp_path_factory):
    if os.environ.get("MS"):
        return {"ms": os.environ["MS"]}, "3c279"
    return {"ms": _simulated(tmp_path_factory)}, None


def _kw(data, **kw):
    src, field = data
    if field:
        kw.setdefault("field", field)
    return {**src, **kw}


@pytest.mark.parametrize("fmt", ["ms", "ps"])
def test_backends_refuse(data, tiny_budget, fmt):
    from cubevis.toolbox.visplot import VisibilityPlotter
    if fmt == "ps":
        if not os.environ.get("PS"):
            pytest.skip("PS not set")
        src = {"ps": os.environ["PS"]}
        d = (src, "3c279")
    else:
        d = data
    with pytest.raises(R.SelectionTooLargeError, match="M samples"):
        VisibilityPlotter(headless=True, layout="scatter", **_kw(d))


def test_raster_unaffected(data, tiny_budget):
    from cubevis.toolbox.visplot import VisibilityPlotter
    vp = VisibilityPlotter(headless=True, layout="raster", **_kw(data))
    vp.close()


def test_headless_one_panel_does_not_render_slot_b(data, monkeypatch):
    from cubevis.toolbox.visplot import VisibilityPlotter
    vp = VisibilityPlotter(headless=True, layout="raster", **_kw(data))
    try:
        b = vp._slots[1]
        assert b.kind == "scatter"
        assert all(img is None for img in b.scatter._layer_images)
    finally:
        vp.close()


def test_gui_startup_keeps_going_and_says_why(data, tiny_budget):
    from cubevis.toolbox.visplot import VisibilityPlotter
    vp = VisibilityPlotter(**_kw(data))               # GUI, side by side
    try:
        b = vp._slots[1]
        assert b.kind == "scatter" and b.scatter._render_error
        text = vp._status_text()
        assert "Panel B" in text and "M samples" in text
    finally:
        vp.close()


def test_plot_reports_refusal(data, monkeypatch):
    """Built within the limit, then the limit drops: Plot answers with the
    message instead of reading."""
    from cubevis.toolbox.visplot import VisibilityPlotter
    vp = VisibilityPlotter(**_kw(data))
    try:
        vp._build_layout()
        monkeypatch.setenv(R.MEMORY_GUARD_ENV, "1")
        vp._cache_generation += 1
        W = vp._panel_axis_widgets
        msg = {"panels": {
                   "A": {"kind": "raster",
                         "y": W["A"]["raster"]["y_sel"].value,
                         "x": W["A"]["raster"]["x_sel"].value,
                         "qty": W["A"]["raster"]["q_sel"].value},
                   "B": {"kind": "scatter", "x": "CHANNEL",
                         "y": W["B"]["scatter"]["y_sel"].value,
                         "colorize": [None, None]}},
               "reload": True}
        rep = asyncio.run(vp._handle_plot(msg))
        assert rep["status"] == "error" and "M samples" in rep["notify_text"]
    finally:
        vp.close()


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


def test_remote_refusal_is_the_same_error(data, tiny_budget):
    """Through a kernel on this host (python3; see _local_kernel): the
    worker's refusal comes back as SelectionTooLargeError (no remote
    traceback in the message), the GUI start-up leaves the panel empty, and
    a failed construction closes the kernel at once instead of hanging in
    __del__ at exit."""
    kernel = _local_kernel()
    from cubevis.toolbox.visplot import VisibilityPlotter
    # Absolute: the kernel's working directory need not be this one.
    src, field = data
    data = ({k: os.path.abspath(v) for k, v in src.items()}, field)
    with pytest.raises(R.SelectionTooLargeError) as ei:
        VisibilityPlotter(headless=True, layout="scatter", kernel_name=kernel, **_kw(data))
    assert "remote traceback" not in str(ei.value)
    vp = VisibilityPlotter(kernel_name=kernel, **_kw(data))
    try:
        assert vp._slots[1].scatter._render_error
    finally:
        vp.close()
