"""Where the GUI is shown decides the transport, not where Python runs
(2026-11-25).

``visplot(...)`` run in a notebook cell opened its GUI in a browser tab
(``BokehAppContext.show``: ``webbrowser.open('file://...')``) while its
``CommMgr``, made in a Jupyter kernel with transport 'auto', chose Jupyter
comms.  A ``file://`` tab cannot reach the kernel's comms, so the GUI showed
its first (embedded) view and then waited forever on its first request
(stepping to the first Antenna).  The visplot and iclean tasks now ask for
'websocket', and ``BokehAppContext.show`` refuses a comms ``CommMgr`` instead
of hanging.

    MS=/path/to/small.ms pytest tests/manual/visplot/test_tab_transport.py
"""
import inspect
import os
from types import SimpleNamespace

import pytest

from cubevis.bokeh.transport import CommMgr
from cubevis.bokeh.transport import _environment
from cubevis.bokeh.models import BokehAppContext

MS = os.environ.get("MS", "/home/claude/data/live.ms")


@pytest.fixture
def in_kernel(monkeypatch):
    """Make CommMgr's transport detection see a Jupyter kernel."""
    monkeypatch.setattr(_environment, "is_jupyter_kernel", lambda: True)


def test_auto_in_a_kernel_is_comms(in_kernel):
    assert CommMgr().transport_type == "jupyter"


def test_websocket_asked_for_is_kept_in_a_kernel(in_kernel):
    assert CommMgr(transport_type="websocket").transport_type == "websocket"


def test_tab_show_refuses_a_comms_comm_mgr():
    fake = SimpleNamespace(comm_mgr=SimpleNamespace(transport_type="jupyter"))
    with pytest.raises(RuntimeError, match="browser tab cannot reach"):
        BokehAppContext.show(fake)


def test_iclean_tab_asks_for_websocket_notebook_for_auto():
    from cubevis.toolbox.iclean import InteractiveClean, InteractiveCleanNotebook
    assert InteractiveClean._TRANSPORT == "websocket"
    assert InteractiveCleanNotebook._TRANSPORT == "auto"
    from cubevis.toolbox.iclean import InteractiveCleanUI
    assert inspect.signature(InteractiveCleanUI.__init__).parameters["transport"].default == "auto"


def test_visplot_task_layers_ask_for_websocket():
    from cubevis.toolbox.visplot.visibility_plotter import VisibilityPlotter
    assert inspect.signature(VisibilityPlotter.__init__).parameters["transport"].default == "auto"
    import cubevis.private.casatasks.visplot as task
    assert "transport = 'websocket'" in inspect.getsource(task._visplot_t)


@pytest.mark.skipif(not os.path.isdir(MS), reason="no MS (MS=)")
def test_visplot_websocket_in_a_kernel(in_kernel):
    from cubevis.toolbox.visplot.visibility_plotter import VisibilityPlotter
    with VisibilityPlotter(ms=MS, transport="websocket") as vp:
        assert vp._comm_mgr.transport_type == "websocket"
        assert vp._comm_mgr.address            # _task_server serves it
    with pytest.raises(ValueError, match="transport must be"):
        VisibilityPlotter(ms=MS, transport="tab")
