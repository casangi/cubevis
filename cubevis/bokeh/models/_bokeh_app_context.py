import logging
from bokeh.core.properties import String, Dict, Any, Nullable, Instance
from bokeh.models.layouts import LayoutDOM
from bokeh.models.ui import UIElement
from uuid import uuid4

logger = logging.getLogger(__name__)

class BokehAppContext(LayoutDOM):
    """
    Custom Bokeh model that bridges Python AppContext with JavaScript.
    Initializes session-level data structure and app-specific state.
    """
    ui = Nullable(Instance(UIElement), help="""
    A UI element, which can be plots, layouts, widgets, or any other UIElement.
    """)

    app_id = String(default="")
    session_id = String(default="")
    app_state = Dict(String, Any, default={})

    # Class-level session ID shared across all apps in the same Python session
    _session_id = None
    
    @classmethod
    def get_session_id(cls):
        """Get or create a session ID for this Python session"""
        if cls._session_id is None:
            cls._session_id = str(uuid4())
        return cls._session_id

    def __init__( self, ui=None, **kwargs ):
        logger.debug(f"\tBokehAppContext::__init__(ui={type(ui).__name__ if ui else None}, {kwargs}): {id(self)}")

        if ui is not None and 'ui' in kwargs:
            raise RuntimeError( "'ui' supplied as both a positional parameter and a keyword parameter" )

        kwargs['session_id'] = self.get_session_id( )

        if 'ui' not in kwargs:
            kwargs['ui'] = ui
        if 'app_id' not in kwargs:
            kwargs['app_id'] = str(uuid4())
        
        super().__init__(**kwargs)

    def _sphinx_height_hint(self):
        """Delegate height hint to the wrapped UI element"""
        logger.debug(f"\tShowable::_sphinx_height_hint(): {id(self)}")
        if self.ui and hasattr(self.ui, '_sphinx_height_hint'):
            return self.ui._sphinx_height_hint()
        return None

    def update_app_state(self, state_updates):
        """
        Update the application state (will be in the generated HTML/JS)

        Args:
            state_updates: dict of state key-value pairs to update
        """
        current_state = dict(self.app_state)
        current_state.update(state_updates)
        self.app_state = current_state
