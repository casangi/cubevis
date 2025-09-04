from enum import Enum
from typing import FrozenSet
from bokeh import __version__ as _bokeh_version
from bokeh.io.state import curstate

class BokehMode(Enum):
    NOTEBOOK = "jupyter_notebook"
    FILE = "file_output"
    BROWSER = "browser_display"
    SERVER = "bokeh_server"

class CurrentBokehState:

    @staticmethod
    def version( ) -> str:
        return _bokeh_version

    @staticmethod
    def mode( ) -> FrozenSet[str]:
        state = curstate( )

        notebook = getattr(state, '_notebook', False)
        file = getattr(state, '_file', None) is not None
        server = getattr(state, '_server_enabled', False)
        browser = file or not (notebook or server)

        return frozenset(
            mode for mode, condition in [
                (BokehMode.NOTEBOOK, notebook),
                (BokehMode.FILE, file),
                (BokehMode.SERVER, server),
                (BokehMode.BROWSER, browser),
            ] if condition
        )
