"""Moved to ``cubevis.toolbox.gridplot.png_export`` (gridplot G1a, 2026-11-10): this
name is the moved module itself, so old imports and monkeypatches reach it."""
import sys
from ..gridplot import png_export as _moved
sys.modules[__name__] = _moved
