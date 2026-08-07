from bokeh.models import Span
from bokeh.core.properties import Bool, Instance, Nullable

class EditSpan(Span):

    dragging = Bool(default=False, help="""
    True while the span is being actively dragged (set True on pan
    start, False on pan end).

    Property-change alternative to the LODStart/LODEnd (PlotEvent)
    approach previously used to signal drag start/end: those events are
    documented (bokeh.events.PlotEvent) as applying to Plot models, and
    in practice never dispatched to any js_on_event listener when
    triggered from a non-Plot origin like this Span -- confirmed via
    live console output across two separate debugging rounds. Listen
    via ``span.js_on_change('dragging', callback)`` and act when
    ``cb_obj.dragging`` becomes ``False`` (drag just ended); this uses
    the same well-understood property-change dispatch machinery already
    relied on for ``location`` itself.
    """)

    # Deferred self-reference -- EditSpan isn't fully defined yet at
    # class-body execution time, so `Instance(EditSpan)` directly would
    # be a NameError. A plain string also doesn't work here: Bokeh's
    # Object.instance_type only accepts a *dotted module path* string
    # (`module, name = self._instance_type.rsplit(".", 1)`), not a bare
    # class name -- confirmed by testing (raises "not enough values to
    # unpack"). The documented-working option is a zero-arg callable,
    # evaluated lazily the first time .instance_type is accessed (by
    # which point the class body has finished executing) -- verified
    # against this installed Bokeh version before landing this.
    sibling = Nullable(Instance(lambda: EditSpan), default=None, help="""
    Reference to a paired EditSpan (e.g. a min/max pair on the same
    figure), used client-side (edit_span.ts's interactive_hit override)
    to correctly hand a click to whichever of the two spans is actually
    closer. Bokeh's own pan-gesture dispatch (ui_events.ts) has no
    distance awareness at all -- it hands the gesture to the first
    candidate, in reversed add-order, whose hit-test succeeds, so
    without this a later-added sibling always wins whenever both spans'
    hit-test tolerance zones overlap at the click point, regardless of
    which one the user actually meant to grab.
    """)

    # explicit __init__ to support Init signatures
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
