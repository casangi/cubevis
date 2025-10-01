import logging
from bokeh.models.layouts import LayoutDOM
from bokeh.models.ui import UIElement
from bokeh.core.properties import Instance
from bokeh.io import curdoc
from .. import BokehInit

logger = logging.getLogger(__name__)

class Showable(LayoutDOM,BokehInit):
    """Wrap a UIElement to make any Bokeh UI component showable with show()
    
    This class works by acting as a simple container that delegates to its UI element.
    For Jupyter notebook display, use show(showable) - automatic display via _repr_mimebundle_
    is not reliably supported by Bokeh's architecture.
    """

    def __init__(self, ui_element=None, backend_func=None, **kwargs):
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

        # Set the function to be called upon display
        if backend_func is not None:
            self._backend_startup_callback = backend_func

    ui = Instance(UIElement, help="""
    A UI element, which can be plots, layouts, widgets, or any other UIElement.
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

    def show(self,start_backend=True):
        """Explicitly show this Showable using Bokeh's show function"""
        # Ensure we're in the current document before showing
        self._ensure_in_document()

        from bokeh.io import show
        if start_backend: self._start_backend( )
        return show(self)
    
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

    def _repr_html_(self,start_backend=True):
        """
        HTML representation for Jupyter display.
        
        Note: Bokeh doesn't reliably support automatic display via _repr_mimebundle_.
        This provides a helpful message directing users to use show().
        """
        logger.debug(f"\tShowable::_repr_html_(): {id(self)}")
        
        if self.ui is None:
            return '<div style="color: red; padding: 10px; border: 1px solid red;">Showable object with no UI set</div>'
        
        # Check if we're in a notebook environment  
        from bokeh.embed import components
        from bokeh.io.state import curstate
        state = curstate()
        
        if state.notebook:
            script, div = components(self)
            if start_backend: self._start_backend( )
            return script + div
        else:
            return f"<!-- error: non-notebook environment{' in ' + self.name if self.name else ''} -->" + '''
            <div style="padding: 15px; border: 2px solid #4CAF50; border-radius: 5px; background: #f9fff9; margin: 10px 0;">
                <strong>📊 Showable Widget Ready</strong><br>
                <em>Notebook display is not enabled, run:</em>
                        <p><pre>
    from bokeh.io import output_notebook
    output_notebook()</pre>
                        <p><em>and try again.</em>
                <hr>
                <small>Contains: {}</small>
            </div>
            '''.format(type(self.ui).__name__ if self.ui else "None")

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


# Enhanced debugging and examples
class ShowableManager:
    """Helper class to manage Showable instances and debug document issues"""
    
    @staticmethod
    def ensure_notebook_setup():
        """Ensure notebook output is properly configured"""
        from bokeh.io import output_notebook
        from bokeh.io.state import curstate
        
        state = curstate()
        if not state.notebook:
            print("Setting up notebook output...")
            output_notebook()
        else:
            print("Notebook output already configured")
        
        return state.notebook
    
    @staticmethod
    def debug_document_state(obj, name="object"):
        """Enhanced debugging with document tracking"""
        from bokeh.io import curdoc
        
        print(f"=== {name} ===")
        print(f"  Type: {type(obj).__name__}")
        print(f"  ID: {getattr(obj, 'id', 'No ID')}")
        print(f"  Object doc: {id(obj.document) if obj.document else None}")
        print(f"  Current doc: {id(curdoc())}")
        print(f"  In current doc: {obj.document is curdoc()}")
        print(f"  In doc roots: {obj in curdoc().roots if obj.document else False}")
        
        if hasattr(obj, 'ui') and obj.ui:
            print(f"  UI type: {type(obj.ui).__name__}")
            print(f"  UI doc: {id(obj.ui.document) if obj.ui.document else None}")
            print(f"  UI in current doc: {obj.ui.document is curdoc() if obj.ui.document else False}")

        if hasattr(obj, '_backend_startup_count'):
            print(f"  Backend start count: {obj._backend_startup_count}")

        if hasattr(obj, '_backend_startup_callback'):
            print(f"  Has startup callback: {callable(obj._backend_startup_callback)}")
    
    @staticmethod
    def create_safe_example():
        """Create a Showable that works with show() function"""
        from bokeh.plotting import figure
        from bokeh.models import Button, DataTable, TableColumn, ColumnDataSource
        from bokeh.layouts import column
        
        # Ensure notebook is set up
        ShowableManager.ensure_notebook_setup()
        
        # Create fresh components with DataTable to test the fix
        plot = figure(title="Example Plot", width=400, height=300)
        plot.scatter([1, 2, 3, 4], [1, 4, 2, 3], size=15, color='blue', alpha=0.6)
        
        # Create a DataTable similar to your use case
        source = ColumnDataSource({
            'labels': ['Mean', 'Std', 'Min', 'Max'],
            'values': [2.5, 1.2, 1.0, 4.0]
        })
        columns = [
            TableColumn(field='labels', title='Statistics', width=75),
            TableColumn(field='values', title='Values')
        ]
        table = DataTable(source=source, columns=columns, index_position=None)

        button = Button(label="Click me!", button_type="success")
        layout = column(button, table, plot)
        
        # Create Showable with backend callback
        showable = Showable(ui_element=layout)
        
        # Add a demo backend startup callback
        def demo_backend_startup():
            print("🚀 Demo backend starting up!")
            print("   - Initializing async services...")
            print("   - Ready to handle GUI interactions!")

        showable.set_backend_startup_callback(demo_backend_startup)

        return showable

    @staticmethod
    def demonstrate_backend_hooks():
        """Demonstrate the backend startup hooks"""
        print("=== Backend Hooks Demo ===")

        # Create example with backend hooks
        showable = ShowableManager.create_safe_example()
        
        print("\n=== Showable Created ===")
        ShowableManager.debug_document_state(showable, "Showable with Backend")
        
        print("\n=== Usage Examples ===")
        print("1. The backend will start automatically when you call:")
        print("   show(showable)  # Backend starts before GUI appears")
        print()
        print("2. You can also set cleanup callbacks:")
        print("   showable.set_backend_cleanup_callback(cleanup_func)")
        print()
        print("3. For custom backends, subclass and override _start_backend():")
        print("   class MyShowable(Showable):")
        print("       def _start_backend(self):")
        print("           # Custom backend startup logic")
        
        return showable


# Example backend integration patterns
class AsyncShowable(Showable):
    """Example of Showable with built-in async backend support"""
    
    def __init__(self, ui_element, backend_manager=None, **kwargs):
        super().__init__(ui_element, **kwargs)
        self.backend_manager = backend_manager
        self._backend_thread = None
        
    def _start_backend(self):
        """Start async backend in a separate thread"""
        super()._start_backend()  # Call parent for callbacks
        
        if self.backend_manager and not self._backend_thread:
            import threading
            import asyncio

            def run_async_backend():
                try:
                    # Create new event loop for this thread
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)

                    # Run the backend manager
                    loop.run_until_complete(self.backend_manager.start())
                except Exception as e:
                    logger.error(f"Error in async backend: {e}")
                finally:
                    loop.close()

            self._backend_thread = threading.Thread(target=run_async_backend, daemon=True)
            self._backend_thread.start()
            logger.info("Started async backend thread")

    def _stop_backend(self):
        """Stop async backend"""
        super()._stop_backend()  # Call parent for callbacks
        
        if self.backend_manager:
            # Signal backend to stop (implementation depends on your backend)
            # self.backend_manager.stop()
            pass


# Convenience function for creating Showables
def make_showable(ui_element, backend_callback=None, **kwargs):
    """Convenience function to create a Showable from any Bokeh UI element"""
    showable = Showable(ui_element=ui_element, **kwargs)
    if backend_callback:
        showable.set_backend_startup_callback(backend_callback)
    return showable


# Example usage
if __name__ == "__main__":
    print("=== Showable Class - With Backend Hooks ===\n")
    
    # Demonstrate backend hooks
    my_showable = ShowableManager.demonstrate_backend_hooks()
    
    print(f"\nExample created: {repr(my_showable)}")
    print("\nTo see the widget with backend startup, run: show(my_showable)")
