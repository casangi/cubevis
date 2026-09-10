from cubevis.toolbox.visplot.remote_reduction_context import RemoteReductionContext
from cubevis.toolbox.visplot.axes import Axis
from cubevis.toolbox.visplot.selection import SelectionSpec

ctx = RemoteReductionContext(
    "/home/zuul06-2/dschieb/casa/visplot/sis14_twhya_calibrated_flagged.ms",
    "zuul06_python312",
)
agg, xr_, yr_, dec = ctx.query_raster(
    y_dim=Axis.BASELINE, x_dim=Axis.TIME, quantity=Axis.AMPLITUDE,
    selection=SelectionSpec(), max_cells=500_000,
)
print(type(agg), agg.shape)
ctx.close()
