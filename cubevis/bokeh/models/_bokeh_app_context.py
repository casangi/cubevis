import logging
from typing import TypeAlias, Callable, Union
from bokeh.models import CustomJS
from bokeh.core.properties import String, Dict, Any, Nullable, Instance, List, Tuple
from bokeh.models.layouts import LayoutDOM
from bokeh.models.ui import UIElement
from bokeh.resources import INLINE, CDN
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
from ...utils import is_interactive_jupyter

logger = logging.getLogger(__name__)

PreflightFunc: TypeAlias = Union[Callable[[Showable], None], Callable[[], None]]

def inline_local_scripts(html_content):
    """
    Finds <script src="file://..."></script> tags and replaces them 
    with the actual file contents to satisfy Chrome's origin policy.
    """
    # Pattern specifically targets your file:/// script tags
    script_pattern = re.compile(r'<script\s+src="file://([^"]+)"></script>')

    def replacer(match):
        file_path = match.group(1)
        # Ensure the path is absolute for the open() call
        abs_path = os.path.abspath(file_path)

        try:
            with open(abs_path, 'r', encoding='utf-8') as f:
                content = f.read()
            return f'<script type="text/javascript">\n/* Inlined: {file_path} */\n{content}\n</script>'
        except Exception as e:
            return f'<!-- ERROR: Could not inline {file_path} - {str(e)} -->'

    return script_pattern.sub(replacer, html_content)

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

    @property
    def preflight_callables(self) -> list[PreflightFunc]:
        return self._preflight_callables.copy( )

    def add_preflight_callable( self, func: PreflightFunc ):
        self._preflight_callables.append(func)

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
        kwargs['name'] = "_GLOBAL_APP_CONTEXT_"

        super().__init__(**kwargs)

        # Register this context as the singleton
        BokehInit.set_app_context(self)
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
        new_entry = (new_script, self.comm_id, description)
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

#    def show(self):
#        from bokeh.plotting import save
#        from bokeh.resources import INLINE
#        import threading, os, socketserver, webbrowser
#        import http.server
#
#        save(self, filename=self.__htmlpath, resources=INLINE, title=self.__title)
#
#        directory = os.path.dirname(os.path.abspath(self.__htmlpath))
#        filename  = os.path.basename(self.__htmlpath)
#
#        class QuietHandler(http.server.SimpleHTTPRequestHandler):
#            def __init__(self, *args, **kwargs):
#                super().__init__(*args, directory=directory, **kwargs)
#            def log_message(self, format, *args):
#                pass
#
#        httpd = socketserver.TCPServer(("", 0), QuietHandler)
#        port  = httpd.server_address[1]
#
#        threading.Thread(target=httpd.serve_forever, daemon=True).start()
#        webbrowser.open(f'http://localhost:{port}/{filename}')


#    def show( self ):
#        """Always show plot in a new browser tab without changing output settings.
#           Jupyter display is handled by the Showable class. However, at some
#           point this function might need to support more than just independent
#           browser tab display.
#        """
#        logger.debug(f"\tBokehAppContext::show( ): {id(self)}")
#
#        from bokeh.io import curdoc
#        from bokeh import plotting
#        from bokeh.plotting import save, show, output_file
#        from bokeh.resources import Resources
#        inline_res = Resources(mode='cdn', root_url=None)
#
#        # Save the plot
#        #output_file(self.__htmlpath, mode="inline")
#        save( self,
#              filename=self.__htmlpath,
#              title=self.__title,
#              resources=inline_res,
#              template=None
#             )
#        # Open in browser
#        webbrowser.open('file://' + os.path.abspath(self.__htmlpath))

    def show(self):
        from bokeh.embed import file_html
        from bokeh.resources import INLINE

        from bokeh.resources import Resources

        # Create a resource object that is strictly inline and has no base for lookups
        strict_inline = Resources(mode='inline', root_url=None)

        # Use this inside your file_html call
        html_content = file_html(self, strict_inline, self.__title)

#        # 1. Generate the initial HTML with BokehJS already inlined
#        # This will still contain your custom file:/// tags from your template
#        raw_html = file_html(self, INLINE, self.__title)
#
#        # 2. Convert those external script tags to internal ones
#        final_html = inline_local_scripts(raw_html)
        # 2. Convert those external script tags to internal ones
        intermediate_html = inline_local_scripts(html_content)

        # Insert this right after the <head> tag starts
#        isolation_fix = """
#<script>
#  // Prevents scripts from trying to traverse origins via the parent window
#  window.parent = window;
#  window.top = window;
#</script>
#"""
#        final_html = intermediate_html.replace("<head>", f"<head>{isolation_fix}")

        # 3. Write the self-contained file to disk
        with open(self.__htmlpath, 'w', encoding='utf-8') as f:
            f.write(intermediate_html)

        # 4. Open safely in Chrome
        import webbrowser
        webbrowser.open('file://' + os.path.abspath(self.__htmlpath))
