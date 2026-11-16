"""``cubevis.xarray`` -- xarray backends for CASA tables, read with arcae.

Nothing here imports Bokeh or CASA (the ``cubevis.imaging`` rule), so a
remote worker, a test or another package can use it without the GUI.
arcae and xarray are imported when a table is opened, not at import.

* ``caltab`` -- engine ``"casa-caltab"``: a CASA calibration table as an
  ``xarray.DataTree`` in the shape of xradio's draft calibration schema::

      import xarray as xr
      dt = xr.open_datatree("cal.G", engine="casa-caltab")

  or, without the entry point, ``cubevis.xarray.caltab.open_caltab("cal.G")``.

Plan: ``devel/docs/gridplot/grid_and_calplot_plan.md`` section 10.
Record: ``devel/notes/drs/calplot/calplot_notes.md``.
"""
