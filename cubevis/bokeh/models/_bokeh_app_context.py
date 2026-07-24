import logging
from typing import TypeAlias, Callable, Union
from bokeh.models import CustomJS
from bokeh.core.properties import String, Dict, Any, Nullable, Instance, List, Tuple
from bokeh.models.layouts import LayoutDOM
from bokeh.models.ui import UIElement
from bokeh.resources import CDN
from tempfile import TemporaryDirectory
from uuid import uuid4
import unicodedata
import webbrowser
import os
import re

### Showable is only needed for type hints
from . import Showable
from ..transport import CommMgr
from .. import BokehInit

logger = logging.getLogger(__name__)

PreflightFunc: TypeAlias = Callable[[], None]

class BokehAppContext(LayoutDOM):
    """
    Custom Bokeh model that bridges Python AppContext with JavaScript.
    Initializes session-level data structure and app-specific state.
    """
    ui = Nullable(Instance(UIElement), help="""
    A UI element, which can be plots, layouts, widgets, or any other UIElement.
    """)

    app_id = String(default="")
    comm_mgr = Nullable(Instance(CommMgr), help="Communications manager for this app")
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

    init_scripts = List(
        Tuple(Instance(CustomJS), String, String),
        default=[],
        help="initialization scripts with associated metadata set with add_init_script(...)"
    )
    
    ## Class-level session ID shared across all apps in the same Python session
    _backend_id = None

    @classmethod
    def _get_backend_id(cls):
        """Get or create a session ID for this Python session"""
        if cls._backend_id is None:
            cls._backend_id = str(uuid4())
        return cls._backend_id

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

    def run_preflight_callables(self):
        callables = list(self._preflight_callables)
        self._preflight_callables.clear()
        for func in callables:
            try:
                func( )
            except Exception as e:
                logger.warning(f"run_preflight_callables: {func} failed: {e}")

    def add_preflight_callable( self, func: PreflightFunc ):
        self._preflight_callables.append(func)
        self._register_auto_drain_if_needed( )

    def _register_auto_drain_if_needed(self):
        """
        If running in a Jupyter/Colab kernel with no Showable present,
        register a one-shot post_execute hook to drain preflight callables
        in the main thread at the end of the current cell.
        """
        # Avoid double-registration
        if getattr(self, '_auto_drain_registered', False):
            return

        try:
            from IPython import get_ipython
            ip = get_ipython()
            if ip is None:
                return  # Not in a kernel, nothing to do
        except ImportError:
            return

        self._auto_drain_registered = True

        def _drain_once():
            # Only drain if Showable hasn't already done it
            if not self._preflight_callables:
                return

            self.run_preflight_callables( )

            # Unregister ourselves after the first drain
            try:
                ip.events.unregister('post_execute', _drain_and_unregister)
            except Exception:
                pass

            self._auto_drain_registered = False  # Allow re-registration next time

        def _drain_and_unregister():
            _drain_once()

        ip.events.register('post_execute', _drain_and_unregister)

    def __init__( self, ui=None, title=str(uuid4( )), prefix=None, **kwargs ):
        logger.debug(f"\tBokehAppContext::__init__(ui={type(ui).__name__ if ui else None}, {kwargs}): {id(self)}")

        if prefix is None:
            ## create a prefix from the title
            prefix = self._slugify(title)[:10]

        self.__title = title
        self.__workdir = TemporaryDirectory(prefix=prefix)
        self.__htmlpath = os.path.join( self.__workdir.name, f'''{self._slugify(self.__title)}.html''' )

        ## list of functions to be called before launching the Bokeh GUI application
        self._preflight_callables: list[PreflightFunc] = [ ]


        if ui is not None and 'ui' in kwargs:
            raise RuntimeError( "'ui' supplied as both a positional parameter and a keyword parameter" )

        ### backend_id is not user settable
        kwargs['backend_id'] = self._get_backend_id( )

        if 'ui' not in kwargs:
            kwargs['ui'] = ui
        if 'app_id' not in kwargs:
            kwargs['app_id'] = str(uuid4())
        # setting a unique well defined name for BokehAppContext
        # allows this object to be found by name in JavaScript
        kwargs['name'] = f"_GLOBAL_APP_CONTEXT_{kwargs['app_id']}"

        super().__init__(**kwargs)

        # Register this context as the singleton
        BokehInit.set_app_context(self)

        if self.comm_mgr:
            self.comm_mgr.registered(self)

    def _sphinx_height_hint(self):
        """Delegate height hint to the wrapped UI element"""
        logger.debug(f"\tShowable::_sphinx_height_hint(): {id(self)}")
        if self.ui and hasattr(self.ui, '_sphinx_height_hint'):
            return self.ui._sphinx_height_hint()
        return None

    def add_init_script(self, code, description='', args=None):
        """
        Helper to append a CustomJS script to the init_scripts list.
        """
        # Create the new CustomJS instance
        new_script = CustomJS(code=code, args=args or {})

        # 1. Access current scripts (default to empty list if None)
        current_scripts = list(self.init_scripts) if self.init_scripts else []

        # 2. Append the new script
        new_entry = (new_script, self.app_id, description)
        current_scripts.append(new_entry)

        # 3. REASSIGN to trigger synchronization
        self.init_scripts = current_scripts

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

        # serialization done, free up application context
        BokehInit.clear_app_context(self)

        # Open in browser
        webbrowser.open('file://' + os.path.abspath(self.__htmlpath))
