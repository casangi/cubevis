"""grid_app.py -- a grid view on its own page.

The smallest application around a ``GridView``: for the demo, the browser
tests, and as the shape calplot's application starts from.  Same protocol
as visplot and iclean (``cubevis.exe``): ``app(exec_context, app_id)``
returns ``(app_context, task)``; ``show()`` puts it in a notebook cell.

    from cubevis.toolbox.gridplot.grid_app import GridApp
    from cubevis.toolbox.gridplot.grid_source import SyntheticGridSource
    GridApp(SyntheticGridSource(45), rows=4, cols=4).show()
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional, Tuple
from uuid import uuid4

log = logging.getLogger(__name__)


class GridApp:
    """A page holding one ``GridView`` of *source*.  Keyword arguments go
    to ``GridView`` (rows, cols, pool, cell, theme, on_drill)."""

    def __init__(self, source, *, title: str = "grid", **view_kw) -> None:
        from cubevis.bokeh import BokehInit
        from cubevis.bokeh.models import BokehAppContext
        from cubevis.bokeh.transport import CommMgr
        from .grid_view import GridView

        self._result_future: Optional[asyncio.Future] = None

        def _shutdown(reason, description):
            self._stop()
            BokehInit.clear_app_context(self._app_context)

        self._app_context = BokehAppContext(
            comm_mgr=CommMgr(on_shutdown=_shutdown),
            app_state={"name": "GridApp", "initialized": True},
            title=title)
        self._comm_mgr = self._app_context.comm_mgr
        self._comm_mgr.set_connection_closed_callback(lambda *a: None)
        self._comm_mgr.resend_inflight_on_reconnect = True
        self._comm_mgr.reconnect_timeout = None
        self.view = GridView(source, self._comm_mgr, **view_kw)
        self._app_context.ui = self.view.layout

    # -- the cubevis application protocol ---------------------------------
    def __call__(self, exec_context=None, task_id=None):
        from cubevis import exe
        if exec_context is None:
            raise TypeError("GridApp(...) needs an execution context (or use show())")
        return self._app_context, exe.Task(self._task_server)

    def show(self):
        """Display in a Jupyter notebook cell."""
        from bokeh.io.state import curstate
        from cubevis import exe
        from cubevis.bokeh.models import Showable
        if not curstate().notebook:
            from bokeh.io import output_notebook
            output_notebook()
        app_id = uuid4()
        context = exe.Context(exe.Mode.THREAD)
        app_context, task = self(context, app_id)
        holder = [None]

        def startup():
            holder[0] = context.execute(task, app_id)

        def get_future():
            if holder[0] is None:
                raise RuntimeError("GridApp has not been launched yet")
            return holder[0]
        return Showable(app_context, startup, get_future, name="grid-app-jpy")

    async def _task_server(self):
        import websockets
        self._result_future = asyncio.Future()
        if self._comm_mgr.address:
            async with websockets.serve(self._comm_mgr.process_messages,
                                        self._comm_mgr.address[0], self._comm_mgr.address[1],
                                        ping_interval=20, ping_timeout=10, close_timeout=5):
                await self._result_future
        else:
            await self._comm_mgr.process_messages()
        return None

    def _stop(self, _=None):
        if self._result_future is not None and not self._result_future.done():
            self._result_future.set_result(None)
