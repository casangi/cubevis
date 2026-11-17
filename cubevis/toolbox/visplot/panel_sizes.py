"""Moved to ``cubevis.toolbox.gridplot.panel_sizes`` (gridplot G1a, 2026-11-10): this
name is the moved module itself, so old imports and monkeypatches reach it."""
import sys
from ..gridplot import panel_sizes as _moved
sys.modules[__name__] = _moved
