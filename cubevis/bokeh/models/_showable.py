from bokeh.models.layouts import LayoutDOM
from bokeh.models.ui import UIElement
from bokeh.core.properties import Instance, Required

class Showable(LayoutDOM):
    """Wrap a UIElement to make any Bokeh UI component showable with show()"""

    def __init__(self, *args, **kwargs) -> None:
        if len(args) == 1 and "ui" not in kwargs:
            kwargs["ui"] = args[0]
        elif len(args) == 1 and "ui" in kwargs:
            raise ValueError("'ui' supplied as both a positional argument and a keyword")
        elif len(args) > 1:
            raise ValueError("only one 'ui' can be supplied as a positional argument")

        super().__init__(**kwargs)

    ui = Required(Instance(UIElement), help="""
    A UI element, which can be plots, layouts, widgets, or any other UIElement.
    """)

    def _sphinx_height_hint(self):
        """Delegate height hint to the wrapped UI element"""
        if hasattr(self.ui, '_sphinx_height_hint'):
            hint = self.ui._sphinx_height_hint()
            return hint
        return None
