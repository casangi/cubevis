import logging
from bokeh.models.layouts import LayoutDOM
from bokeh.models.ui import UIElement
from bokeh.core.properties import Instance
from bokeh.io import curdoc

logger = logging.getLogger(__name__)

class Showable(LayoutDOM):
    """Wrap a UIElement to make any Bokeh UI component showable with show()
    
    This class works by acting as a simple container that delegates to its UI element.
    For Jupyter notebook display, use show(showable) - automatic display via _repr_mimebundle_
    is not reliably supported by Bokeh's architecture.
    """

    def __init__(self, ui_element=None, **kwargs):
        logger.debug(f"\tShowable::__init__(ui_element={type(ui_element).__name__ if ui_element else None}, {kwargs}): {id(self)}")
        
        # Set default sizing if not provided
        sizing_params = {'sizing_mode', 'width', 'height'}
        provided_sizing_params = set(kwargs.keys()) & sizing_params
        if not provided_sizing_params:
            kwargs['sizing_mode'] = 'stretch_both'

        super().__init__(**kwargs)
        
        # Set the UI element
        if ui_element is not None:
            self.ui = ui_element
            
        # Ensure this gets added to the current document
        self._ensure_in_document()

    ui = Instance(UIElement, help="""
    A UI element, which can be plots, layouts, widgets, or any other UIElement.
    """)

    # Override children property to include our UI element
    @property  
    def children(self):
        """Return the UI as children so it gets rendered by Bokeh's layout system"""
        if self.ui is not None:
            return [self.ui]
        return []

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
        
        # If we're not in any document or in the wrong document, add to current
        if self.document is None or self.document is not current_doc:
            if self.document is not None and self in self.document.roots:
                self.document.remove_root(self)
            current_doc.add_root(self)
            logger.debug(f"\tShowable::_ensure_in_document(): Added {id(self)} to document {id(current_doc)}")

    def show(self):
        """Explicitly show this Showable using Bokeh's show function"""
        # Ensure we're in the current document before showing
        self._ensure_in_document()
        from bokeh.io import show
        return show(self)
    
    def _repr_html_(self):
        """
        HTML representation for Jupyter display.
        
        Note: Bokeh doesn't reliably support automatic display via _repr_mimebundle_.
        This provides a helpful message directing users to use show().
        """
        logger.debug(f"\tShowable::_repr_html_(): {id(self)}")
        
        if self.ui is None:
            return '<div style="color: red; padding: 10px; border: 1px solid red;">Showable object with no UI set</div>'
        
        # Check if we're in a notebook environment  
        from bokeh.io.state import curstate
        state = curstate()
        
        if state.notebook:
            return '''
            <div style="padding: 15px; border: 2px solid #4CAF50; border-radius: 5px; background: #f9fff9; margin: 10px 0;">
                <strong>📊 Showable Widget Ready</strong><br>
                <em>Use <code>show(this_showable)</code> to display the Bokeh widget inline.</em><br>
                <small>Contains: {}</small>
            </div>
            '''.format(type(self.ui).__name__ if self.ui else "None")
        else:
            return '''
            <div style="padding: 15px; border: 2px solid #2196F3; border-radius: 5px; background: #f0f8ff; margin: 10px 0;">
                <strong>📊 Showable Widget</strong><br>
                <em>Use <code>show(this_showable)</code> to display in browser.</em><br>
                <small>Contains: {}</small>
            </div>
            '''.format(type(self.ui).__name__ if self.ui else "None")

    def __repr__(self):
        """String representation"""
        ui_type = type(self.ui).__name__ if self.ui else "None"
        doc_info = f"doc={id(self.document)}" if self.document else "doc=None"
        return f"{self.__class__.__name__}(ui={ui_type}, {doc_info})"


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
            
        # Check children
        if hasattr(obj, 'children'):
            print(f"  Children: {[type(child).__name__ for child in obj.children]}")
    
    @staticmethod
    def create_safe_example():
        """Create a Showable that works with show() function"""
        from bokeh.plotting import figure
        from bokeh.models import Button
        from bokeh.layouts import column
        
        # Ensure notebook is set up
        ShowableManager.ensure_notebook_setup()
        
        # Create fresh components - fix the deprecation warning
        plot = figure(title="Example Plot", width=400, height=300)
        plot.scatter([1, 2, 3, 4], [1, 4, 2, 3], size=15, color='blue', alpha=0.6)
        
        button = Button(label="Click me!", button_type="success")
        layout = column(button, plot)
        
        # Create Showable with the simpler constructor
        showable = Showable(ui_element=layout)
        
        return showable
    
    @staticmethod
    def demonstrate_usage():
        """Demonstrate the correct usage patterns"""
        print("=== Creating Showable Example ===")
        showable = ShowableManager.create_safe_example()
        
        print("\n=== Debug Document State ===")
        ShowableManager.debug_document_state(showable, "Example Showable")
        
        print("\n=== Correct Usage Patterns ===")
        print("1. In Jupyter notebook:")
        print("   show(showable)  # This will display inline")
        print("   # OR")
        print("   showable.show()  # Convenience method")
        print()
        print("2. From Python CLI:")
        print("   show(showable)  # This will open in browser")
        print()
        print("3. What WON'T work reliably:")
        print("   showable  # Automatic display - Bokeh doesn't support this consistently")
        print()
        print("The reason: Bokeh's architecture requires explicit show() calls.")
        print("The show() function handles all the document management and display logic.")
        
        return showable
    
    @staticmethod
    def test_show_methods():
        """Test the show methods"""
        showable = ShowableManager.create_safe_example()
        
        print("=== Testing Show Methods ===")
        print("Created showable:", repr(showable))
        
        # Debug state before ensuring document integration
        print("\n--- Before ensuring document integration ---")
        ShowableManager.debug_document_state(showable, "Before Integration")
        
        # Ensure it's in the document
        showable._ensure_in_document()
        
        # Debug state after ensuring document integration
        print("\n--- After ensuring document integration ---")
        ShowableManager.debug_document_state(showable, "After Integration")
        
        print("\nTo display this showable, run one of:")
        print("show(showable)")
        print("showable.show()")
        
        return showable


# Convenience function for creating Showables
def make_showable(ui_element, **kwargs):
    """Convenience function to create a Showable from any Bokeh UI element"""
    return Showable(ui_element=ui_element, **kwargs)


# Example usage
if __name__ == "__main__":
    print("=== Showable Class - Correct Usage Patterns ===\n")
    
    # Demonstrate the correct way to use Showable
    my_showable = ShowableManager.demonstrate_usage()
    
    print(f"\nExample created: {repr(my_showable)}")
    print("\nTo see the actual widget, run: show(my_showable)")
