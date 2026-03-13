import logging
from bokeh.core.properties import String, Dict, Any, Nullable, Instance
from bokeh.models.layouts import LayoutDOM
from bokeh.models.ui import UIElement
from bokeh.resources import CDN
from tempfile import TemporaryDirectory
from uuid import uuid4
import unicodedata
import webbrowser
import os
import re

from .. import BokehInit

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
    backend_id = String(default="", help="""
    The backend_id is filled by this class. This is expected to be a unique, non-null
    string. There should be only one backend_id for each cubevis application.
    """ )
    frontend_id = Nullable(String, default=None, help="""
    The frontend_id is filled with the identifier generated for the browser session. It is
    populated by the DataPipe class which establishes communications between the frontend
    and the backend. This is expected to be null. There should be only one frontend_id
    for each cubevis application.
    """ )
    app_state = Dict(String, Any, default={})

    ## Class-level session ID shared across all apps in the same Python session
    _backend_id = None

    @classmethod
    def _create_or_fetch_backend_id(cls):
        """Get or create a session ID for this Python session"""
        if cls._backend_id is None:
            cls._backend_id = str(uuid4())
        return cls._backend_id

    def get_frontend_id(self):
        """Get the current frontend_id value."""
        return self.frontend_id

    def set_frontend_id(self, value):
        """Set the frontend_id value with validation."""
        print( "################################################################################")
        print(f"### existing frontend_id: {self.frontend_id}")
        print(f"###      new frontend_id: {value}")
        print( "################################################################################")
        if self.frontend_id is not None and self.frontend_id != value:
            raise ValueError(
                f"frontend_id is already set to '{self.frontend_id}' and cannot be "
                f"changed to '{value}'. There should be only one frontend_id per "
                f"cubevis application."
            )
        self.frontend_id = value

    def _slugify(self, value, allow_unicode=False):
        """
        Taken from https://github.com/django/django/blob/master/django/utils/text.py
        Convert to ASCII if 'allow_unicode' is False. Convert spaces or repeated
        dashes to single dashes. Remove characters that aren't alphanumerics,
        underscores, or hyphens. Convert to lowercase. Also strip leading and
        trailing whitespace, dashes, and underscores.
        https://stackoverflow.com/a/295466/2903943
        """
        value = str(value)
        if allow_unicode:
            value = unicodedata.normalize('NFKC', value)
        else:
            value = unicodedata.normalize('NFKD', value).encode('ascii', 'ignore').decode('ascii')
        value = re.sub(r'[^\w\s-]', '', value.lower())
        return re.sub(r'[-\s]+', '-', value).strip('-_')

    def __init__( self, ui=None, title=str(uuid4( )), prefix=None, **kwargs ):
        logger.debug(f"\tBokehAppContext::__init__(ui={type(ui).__name__ if ui else None}, {kwargs}): {id(self)}")

        if prefix is None:
            ## create a prefix from the title
            prefix = self._slugify(title)[:10]

        self.__title = title
        self.__workdir = TemporaryDirectory(prefix=prefix)
        self.__htmlpath = os.path.join( self.__workdir.name, f'''{self._slugify(self.__title)}.html''' )

        if ui is not None and 'ui' in kwargs:
            raise RuntimeError( "'ui' supplied as both a positional parameter and a keyword parameter" )

        kwargs['backend_id'] = self._create_or_fetch_backend_id( )
        print( '******backend_id*set****************************************************************************************************' )
        print( f"**** {type(kwargs['backend_id'])}" )
        print( f"**** {kwargs['backend_id']}" )
        print( '************************************************************************************************************************' )

        if 'ui' not in kwargs:
            kwargs['ui'] = ui
        if 'app_id' not in kwargs:
            kwargs['app_id'] = str(uuid4())
        # setting a unique well defined name for BokehAppContext
        # allows this object to be found by name in JavaScript
        kwargs['name'] = "_GLOBAL_APP_CONTEXT_"

        super().__init__(**kwargs)

        # Register this context as the singleton
        BokehInit.set_app_context(self)

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

    def show( self ):
        """Always show plot in a new browser tab without changing output settings.
           Jupyter display is handled by the Showable class. However, at some
           point this function might need to support more than just independent
           browser tab display.
        """
        logger.debug(f"\tBokehAppContext::show( ): {id(self)}")

        from bokeh.plotting import save

        # Save the plot
        save( self, filename=self.__htmlpath, resources=CDN, title=self.__title)

        # Open in browser
        webbrowser.open('file://' + os.path.abspath(self.__htmlpath))
