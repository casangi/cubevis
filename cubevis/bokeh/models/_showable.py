import logging
from bokeh.models.layouts import LayoutDOM
from bokeh.models.ui import UIElement
from bokeh.core.properties import Instance, String

from bokeh.io import curdoc
from .. import BokehInit

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

        # Detect Bokeh usage mode (i.e. self.__class__._usage_mode unset)
        calling_mode = None
        if self.__class__._usage_mode is None:
            self.__class__._usage_mode = "bokeh"
            calling_mode = "bokeh"

        # Allow None (detaching from document) without any further checking
        if doc is None:
            self._document = None
            return

        if calling_mode is None:
            import inspect
            stack_frames = inspect.stack( )
            try:
                for frame in stack_frames[1:]:
                    if frame.function == '_repr_mimebundle_' or frame.function == 'show':
                        if get_caller_class_name(frame.frame) == self.__class__.__name__:
                            calling_mode = "custom"
                            break
            finally:
                # Essential to delete stack frames to avoid reference cycles
                del stack_frames

        if calling_mode != self.__class__._usage_mode:
            ###  THIS CATCHES:   using Bokeh show after Showable display methods
            ###                  using Showable.show after Bokeh show
            raise RuntimeError(
                f"\n{'='*70}\n" +
                ( (self._usage_error['custom'] % self.__class__.__name__) if calling_mode == 'custom' else
                  (self._usage_error['bokeh'] % 'bokeh.plotting.show') ) +
                f"\n{'='*70}\n" )

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

    def __init__( self, ui_element=None, backend_func=None,
                  result_retrieval=None,
                  notebook_width=1200, notebook_height=800,
                  notebook_sizing='fixed', **kwargs):
        logger.debug(f"\tShowable::__init__(ui_element={type(ui_element).__name__ if ui_element else None}, {kwargs}): {id(self)}")

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

        # Error messages included in RuntimeErrors
        self._usage_error = {
            'custom': "❌ Cannot use %s display methods:\n\n" \
                      "Reason: bokeh.plotting.show() has already been used for display\n" \
                      "        of this class. Mixing display methods within a single notebook\n" \
                      "        corrupts Bokeh display within the notebook\n",
            'bokeh':  "❌ Cannot use %s display method:\n\n" \
                      "Reason: Showable display methods have already been used for display\n" \
                      "        of this class. Mixing display methods within a single notebook\n" \
                      "        corrupts Bokeh display within the notebook\n" }


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
        from bokeh.embed import components
        from bokeh.io.state import curstate

        if self.__class__._usage_mode != "custom":
            ###  THIS CATCHES:   Showable display via evaluation ( "ic" ) after Bokeh show
            raise RuntimeError(
                f"\n{'='*70}\n" +
                (self._usage_error['custom'] % self.__class__.__name__) +
                f"\n{'='*70}\n" )

        state = curstate()

        if not state.notebook:
            return None

        if self.ui is None:
            return '<div style="color: red; padding: 10px; border: 1px solid red;">Showable object with no UI set</div>'

        if self._notebook_rendering:
            # Return a lightweight reference instead of re-rendering the full GUI
            return f'''
            <div style="padding: 10px; background: #f0f8f0; border-left: 4px solid #4CAF50; margin: 5px 0;">
                <strong>↑ iclean GUI active above</strong>
                <small style="color: #666; display: block; margin-top: 5px;">
                    Showable ID: {self.id[-8:]} | Backend: Running
                </small>
            </div>
            '''

        # Apply notebook sizing for Jupyter context
        ###if self._notebook_sizing == 'fixed':
        ###    self.sizing_mode = None
        ###    self.width = self._notebook_width
        ###    self.height = self._notebook_height

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
