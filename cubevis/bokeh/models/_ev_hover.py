from bokeh.models.layouts import LayoutDOM, UIElement
from bokeh.core.properties import Instance, Required
from .. import BokehInit


class EvHover(LayoutDOM, BokehInit):
    """Wrap one child and report the pointer entering and leaving it.

    Triggers the standard ``MouseEnter`` / ``MouseLeave`` events on this
    model when the pointer crosses the wrapper, so::

        wrapped = EvHover(select)
        wrapped.js_on_event(MouseEnter, CustomJS(...))

    works for any control -- a ``Select``, a ``DataTable``, a
    ``CheckboxGroup``, a ``RadioButtonGroup``, or a ``column`` holding a
    control together with its title.  Stock Bokeh widgets do not emit
    these events; ``EvTextInput`` does, for a text input only.  Put the
    wrapper in the layout where the child would have gone.

    Added 2026-10 for visplot's status-area help.  Browser side:
    ``cubevisjs/src/bokeh/models/ev_hover.ts`` (layout handling as
    ``Tip``'s, without the tooltip).
    """

    def __init__(self, *args, **kwargs) -> None:
        if len(args) != 1 and "child" not in kwargs:
            raise ValueError("a 'child' argument must be supplied")
        elif len(args) == 1 and "child" in kwargs:
            raise ValueError("'child' supplied as both a positional argument and a keyword")
        elif len(args) > 1:
            raise ValueError("only one 'child' can be supplied as a positional argument")
        elif len(args) > 0:
            kwargs["child"] = args[0]

        super().__init__(**kwargs)

    child = Required(Instance(UIElement), help="""
    The wrapped component: a widget, or a layout of widgets.
    """)
