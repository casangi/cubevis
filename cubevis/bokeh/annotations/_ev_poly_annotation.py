from bokeh.models import PolyAnnotation
from .. import BokehInit

class EvPolyAnnotation(PolyAnnotation,BokehInit):

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
