from bokeh.models import Model
from bokeh.models.layouts import LayoutDOM, UIElement
from bokeh.core.properties import Dict, Any, String, Required

class SharedDict(Model):
    '''Display a tooltip for the child element
    '''

    def __init__(self, *args, **kwargs) -> None:
        if len(args) != 1 and "values" not in kwargs:
            raise ValueError("a 'values' argument must be supplied")
        elif len(args) == 1 and "values" in kwargs:
            raise ValueError("'values' supplied as both a positional argument and a keyword")
        elif len(args) > 1:
            raise ValueError("only one 'values' can be supplied as a positional argument")
        elif len(args) > 0:
            kwargs["values"] = args[0]

        super().__init__(**kwargs)

    values = Required(Dict(String, Any), help="""
    Python dictionary to be shared among multiple JavaScript callbacks.
    """)
