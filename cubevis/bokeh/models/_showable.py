import logging
from bokeh.models.layouts import LayoutDOM
from bokeh.models.ui import UIElement
from bokeh.core.properties import Instance, String

from bokeh.io import curdoc
from .. import BokehInit
from ...utils import is_colab

logger = logging.getLogger(__name__)

class Showable(LayoutDOM,BokehInit):
    """Wrap a UIElement to make any Bokeh UI component showable with show()

    This class works by acting as a simple container that delegates to its UI element.
    For Jupyter notebook display, use show(showable) - automatic display via _repr_mimebundle_
    is not reliably supported by Bokeh's architecture.
    """

    ### _usage_mode is needed to prevent mixing "bokeh.plotting.show(showable)" with
    ### "showable.show( )" or just evaluating "showable". This is required because the
    ### latter use "bokeh.embed.components" to create the HTML that is rendered while
    ### "bokeh.plotting.show(showable)" uses internal Bokeh rendering that is
    ### incompatable with "bokeh.embed.components" usage. For this reason, the user
    ### can use either one, but not both.
    _usage_mode = None

    @property
    def document(self):
        """Get the document this model is attached to."""
        return getattr(self, '_document', None)

    @document.setter
    def document(self, doc):
        """
        Intercept when Bokeh tries to attach us to a document.
        This is called by bokeh.plotting.show() when it adds us to a document.
        """

        def get_caller_class_name(frame):
            """Attempt to find the name of the class the calling method belongs to."""
            # Check if 'self' is in the caller's local variables (conventional for instance methods)
            if 'self' in frame.f_locals:
                return frame.f_locals['self'].__class__.__name__
            # Check for 'cls' (conventional for class methods)
            elif 'cls' in frame.f_locals:
                return frame.f_locals['cls'].__name__
            else:
                # It might be a regular function or static method without explicit 'self'/'cls'
                return None

        # Allow None (detaching from document) without any further checking
        if doc is None:
            self._document = None
            return

        from bokeh.io.state import curstate
        state = curstate( )

        # Validate environment (only one OUTPUT mode)
        active_modes = []
        if state.file: active_modes.append('file')
        if state.notebook: active_modes.append('notebook')

        # only allow a single GUI to be displayed since there is a backend
        # this could be relaxed if the backend can manage events from two
        # different GUIs
        if len(active_modes) > 1:
            raise RuntimeError(
                f"{self.__class__.__name__} can only be displayed in a single Bokeh\n"
                f"display mode. Either file or notebook, but not both."
            )

        # For notebook display, fixed sizing is required. This selects between the
        # fixed, notebook dimensions and the default browser dimensions based on
        # the Bokeh output that has been selected.
        if 'notebook' in active_modes and self._display_config['notebook']['mode'] == 'fixed':
            self.sizing_mode = None
            self.width = self._display_config['notebook']['width']
            self.height = self._display_config['notebook']['height']
        else:
            self.sizing_mode = self._display_config['browser']['mode']
            self.width = self._display_config['browser']['width']
            self.height = self._display_config['browser']['height']

        # Now set the document
        self._document = doc

        # Start backend if needed
        if not hasattr(self, '_backend_started') or not self._backend_started:
            self._start_backend()
            self._backend_started = True

    def to_serializable(self, *args, **kwargs):
        if self._display_context:
            self._display_context.on_to_serializable( )

        # Call parent's to_serializable
        return super().to_serializable(*args, **kwargs)

    def __init__( self, ui_element=None, backend_func=None,
                  result_retrieval=None,
                  notebook_width=1200, notebook_height=800,
                  notebook_sizing='fixed',
                  display_context=None,
                  **kwargs ):
        logger.debug(f"\tShowable::__init__(ui_element={type(ui_element).__name__ if ui_element else None}, {kwargs}): {id(self)}")

        self._display_context = display_context

        # Set default sizing if not provided
        sizing_params = {'sizing_mode', 'width', 'height'}
        provided_sizing_params = set(kwargs.keys()) & sizing_params
        if not provided_sizing_params:
            kwargs['sizing_mode'] = 'stretch_both'

        # CRITICAL FIX: Don't call _ensure_in_document during __init__
        # Let Bokeh handle document management through the normal flow
        super().__init__(**kwargs)

        # Set the UI element
        if ui_element is not None:
            self.ui = ui_element

        # Keep track of defaults based on display mode
        ### self._notebook_width = notebook_width
        ### self._notebook_height = notebook_height
        ### self._notebook_sizing = notebook_sizing  # 'fixed' or 'stretch'
        self._display_config = {
            'notebook': { 'mode': notebook_sizing, 'width': notebook_width, 'height': notebook_height },
            'browser': { 'mode': self.sizing_mode, 'width': self.width, 'height': self.height }
        }

        # Set the function to be called upon display
        if backend_func is not None:
            self._backend_startup_callback = backend_func
        # function to be called to fetch the Showable GUI
        # result (if one is/will be available)...
        self._result_retrieval = result_retrieval

        self._notebook_rendering = None

    ui = Instance(UIElement, help="""
    A UI element, which can be plots, layouts, widgets, or any other UIElement.
    """)
    ###
    ### when 'disabled' is set to true, this message should be displayed over
    ### a grey-obscured GUI...
    ###
    ### May need to adjust message formatting to allow for more flexiblity,
    ### i.e. all of the message does not need to be in big green print. May
    ### need to allow something like:
    ###
    ###   <div style="font-size: 24px; font-weight: bold; color: #4CAF50; margin-bottom: 10px;">
    ###      Interaction Complete ✓
    ###   </div>
    ###   <div style="font-size: 14px; color: #666;">
    ###      You can now close this GUI or continue working in your notebook
    ###   </div>
    ###
    ### currently the message is displayed in showable.ts like:
    ###
    ###   <div style="font-size: 24px; font-weight: bold; color: #4CAF50; margin-bottom: 10px;">
    ###      ${this.model.disabled_message}
    ###   </div>
    ###
    disabled_message = String(default="Interaction Complete ✓", help="""
    Message to show when disabled
    """)

    # FIXED: Remove the children property override
    # Let LayoutDOM handle its own children management
    # The TypeScript side will handle the UI element rendering

    def _sphinx_height_hint(self):
        """Delegate height hint to the wrapped UI element"""
        logger.debug(f"\tShowable::_sphinx_height_hint(): {id(self)}")
        if self.ui and hasattr(self.ui, '_sphinx_height_hint'):
            return self.ui._sphinx_height_hint()
        return None

    def _ensure_in_document(self):
        """Ensure this Showable is in the current document"""
        from bokeh.io import curdoc
        current_doc = curdoc()

        # FIXED: More careful document management
        # Only add to document if we're not already in the right one
        if self.document is None:
            current_doc.add_root(self)
            logger.debug(f"\tShowable::_ensure_in_document(): Added {id(self)} to document {id(current_doc)}")
        elif self.document is not current_doc:
            # Remove from old document first
            if self in self.document.roots:
                self.document.remove_root(self)
            current_doc.add_root(self)
            logger.debug(f"\tShowable::_ensure_in_document(): Moved {id(self)} to document {id(current_doc)}")

        # HOOK: Backend startup when added to document
        # This catches both direct show() calls and Bokeh's show() function
        if not hasattr(self, '_backend_started'):
            self._start_backend( )
            self._backend_started = True

    def get_future(self):
        if self._result_retrieval is None:
            raise RuntimeError( f"{self.name if self.name else 'this showable'} does not return a result" )
        else:
            return self._result_retrieval( )

    def get_result(self):
        if self._result_retrieval is None:
            raise RuntimeError( f"{self.name if self.name else 'this showable'} does not return a result" )
        else:
            return self._result_retrieval( ).result( )

    def _start_backend(self):
        """Hook to start backend services when showing"""
        # Override this in subclasses or set a callback
        if hasattr(self, '_backend_startup_count'):
            ### backend has already been started
            ### must figure out what is the proper way to handle this case
            logger.debug(f"\tShowable::_start_backend(): backend already started for {id(self)} [{self._backend_startup_count}]")
            self._backend_startup_count += 1
            return

        if hasattr(self, '_backend_startup_callback'):
            try:
                self._backend_startup_callback()
                logger.debug(f"\tShowable::_start_backend(): Executed startup callback for {id(self)}")
                self._backend_startup_count = 1
            except Exception as e:
                logger.error(f"\tShowable::_start_backend(): Error in startup callback: {e}")

        # Example: Start asyncio backend
        # if hasattr(self, '_backend_manager'):
        #     self._backend_manager.start()

        logger.debug(f"\tShowable::_start_backend(): Backend startup hook called for {id(self)}")

    def set_backend_startup_callback(self, callback):
        """Set a callback to be called when show() is invoked"""
        if not callable(callback):
            raise ValueError("Backend startup callback must be callable")
        self._backend_startup_callback = callback
        logger.debug(f"\tShowable::set_backend_startup_callback(): Set callback for {id(self)}")

    def _stop_backend(self):
        """Hook to stop backend services - override in subclasses"""
        if hasattr(self, '_backend_cleanup_callback'):
            try:
                self._backend_cleanup_callback()
                logger.debug(f"\tShowable::_stop_backend(): Executed cleanup callback for {id(self)}")
            except Exception as e:
                logger.error(f"\tShowable::_stop_backend(): Error in cleanup callback: {e}")

        logger.debug(f"\tShowable::_stop_backend(): Backend cleanup hook called for {id(self)}")

    def set_backend_cleanup_callback(self, callback):
        """Set a callback to be called when cleaning up backend"""
        if not callable(callback):
            raise ValueError("Backend cleanup callback must be callable")
        self._backend_cleanup_callback = callback
        logger.debug(f"\tShowable::set_backend_cleanup_callback(): Set callback for {id(self)}")

    def __del__(self):
        """Cleanup when Showable is destroyed"""
        if hasattr(self, '_backend_startup_callback') and self._backend_startup_callback:
            self._stop_backend()

    def _get_notebook_html(self, start_backend=True):
        """
        Common logic for generating HTML in notebook environments.
        Returns the HTML string to display, or None if not in a notebook.
        """
        from bokeh.embed import components, json_item
        from bokeh.io.state import curstate
        from bokeh.resources import CDN
        import sys
        import json as json_lib

        state = curstate()

        if not state.notebook:
            return None

        if self.ui is None:
            return '<div style="color: red; padding: 10px; border: 1px solid red;">Showable object with no UI set</div>'

        if self._display_context:
            self._display_context.on_show()

        if self._notebook_rendering:
            # Return a lightweight reference instead of re-rendering the full GUI
            return f'''
            <div style="padding: 10px; background: #f0f8f0; border-left: 4px solid #4CAF50; margin: 5px 0;">
                <strong>→ iclean GUI active above</strong>
                <small style="color: #666; display: block; margin-top: 5px;">
                    Showable ID: {self.id[-8:]} | Backend: Running
                </small>
            </div>
            '''

        if is_colab( ):
            # Get all JS paths from the existing function
            # This returns paths in the correct order:
            # [casalib, bokeh-core, bokeh-widgets, bokeh-tables, cubevisjs]
            from cubevis.bokeh import get_bokeh_js_paths
            js_paths = get_bokeh_js_paths( )

            # Build script tags for all libraries in order
            all_scripts = '\n'.join([
                f'<script type="text/javascript" src="{url}"></script>'
                for url in js_paths
            ])

            # Use json_item approach which is more reliable in iframes
            item = json_item(self, target=f"bokeh-{self.id}")
            item_json = json_lib.dumps(item)

            # Build complete HTML with proper loading sequence
            # get_bokeh_js_paths() already returns libs in the correct order:
            # 1. casalib (third-party libs for CustomJS)
            # 2. bokeh-core
            # 3. bokeh-widgets
            # 4. bokeh-tables
            # 5. cubevisjs (custom Bokeh models)
            html = f'''
            {f'<link href="{CDN.css_files[0]}" rel="stylesheet" type="text/css">' if CDN.css_files else ""}
            <div id="bokeh-{self.id}" class="bk-root"></div>
            {all_scripts}
            <script type="text/javascript">
            (function() {{
                var item = {item_json};

                function embedWhenReady() {{
                    // Check if all required libraries are loaded
                    if (typeof Bokeh !== 'undefined' && Bokeh.embed) {{
                        var target = document.getElementById("bokeh-{self.id}");
                        if (target) {{
                            try {{
                                Bokeh.embed.embed_item(item);
                                console.log("Bokeh plot embedded successfully");
                            }} catch(e) {{
                                console.error("Error embedding Bokeh plot:", e);
                            }}
                        }} else {{
                            console.error("Target element not found");
                            setTimeout(embedWhenReady, 50);
                        }}
                    }} else {{
                        setTimeout(embedWhenReady, 50);
                    }}
                }}

                if (document.readyState === 'loading') {{
                    document.addEventListener('DOMContentLoaded', embedWhenReady);
                }} else {{
                    embedWhenReady();
                }}
            }})();
            </script>
            '''

            if start_backend:
                self._start_backend()

            self._notebook_rendering = html
            return html
        else:
            # In Jupyter Lab/Classic, use components() as before
            script, div = components(self)
            if start_backend:
                self._start_backend()
            self._notebook_rendering = f'{script}\n{div}'
            return self._notebook_rendering

    def _repr_mimebundle_(self, include=None, exclude=None):
        """
        MIME bundle representation for Jupyter display.

        This is called by Bokeh's show() function and IPython's display system.
        By implementing this, we ensure consistent display whether the object
        is displayed via:
        - Automatic display (just evaluating the object)
        - IPython display(showable)
        - Bokeh show(showable)
        - showable.show()
        """
        if self.__class__._usage_mode is None:
            self.__class__._usage_mode = "custom"
        from bokeh.io.state import curstate

        state = curstate()

        if state.notebook:
            html = self._get_notebook_html(start_backend=True)
            if html:
                return {
                    'text/html': html
                }

        # Fall back to default Bokeh behavior for non-notebook environments
        # Return None to let Bokeh handle it
        return None

    def show(self, start_backend=True):
        """Explicitly show this Showable using inline display in Jupyter"""
        if self.__class__._usage_mode is None:
            self.__class__._usage_mode = "custom"

        from bokeh.io.state import curstate

        self._ensure_in_document()

        state = curstate()

        if state.notebook:
            # In Jupyter, display directly using IPython.display
            from IPython.display import display, HTML

            html = self._get_notebook_html(start_backend)
            if html:
                display(HTML(html))
                return

        # Fall back to standard Bokeh show for non-notebook environments
        from bokeh.io import show as bokeh_show
        if start_backend:
            self._start_backend()
        bokeh_show(self)

    def __str__(self):
        """String conversion"""
        name = f", name='{self.name}'" if self.name else ""
        return f"{self.__class__.__name__}(id='{self.id}'{name} ...)"

    def __repr__(self):
        """String representation from repr(...)"""
        ui_type = type(self.ui).__name__ if self.ui else "None"
        doc_info = f"doc='{id(self.document)}'" if self.document else "doc=None"
        backend_info = f"backend='{'started' if getattr(self, '_backend_startup_count', 0) else 'not-started'}'"
        return f"{self.__class__.__name__}(id='{self.id}', name='{self.name}', ui='{ui_type}', {doc_info}, {backend_info})"
