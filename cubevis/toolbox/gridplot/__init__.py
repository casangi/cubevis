"""``cubevis.toolbox.gridplot`` -- what a plot is, apart from any one application.

Shared by visplot, the grid view and calplot
(``devel/docs/gridplot/grid_and_calplot_plan.md``, section 3.2; stage G1a,
2026-11-10, moved here from ``cubevis.toolbox.visplot``):

``panel_spec``
    ``PanelSpec`` / ``RenderedPanel``: a panel as data (axes, ticks,
    colour bands, legend) and its RGBA image -- what a grid cell is.
``png_export``
    ``export_png``: one or many ``RenderedPanel`` composited into a PNG,
    as an M x N grid with shared or per-cell colour bars and legend.
``tick_format``, ``palettes``, ``colormap_scaling``, ``panel_sizes``,
``raster_grid``
    tick labels and units; palettes and backgrounds; colour scaling;
    panel size arguments; where a raster's cells are.

Nothing here imports Bokeh, CASA or a data backend: a remote worker, a test
or a script can use it without the GUI.  The old names
(``cubevis.toolbox.visplot.png_export`` and the rest) remain as aliases of
these modules.
"""
