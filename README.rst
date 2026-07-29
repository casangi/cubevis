cubevis - visualization tools for CASA images
=============================================

``cubevis`` is a visualization toolkit for radio astronomy image and
visibility data, built on `Bokeh <https://bokeh.org/>`_ for the front-end and
`CASA <https://casadocs.readthedocs.io/en/stable/index.html>`_ for the
processing backend. Python and CASA drive image/data access while a
generated JavaScript interface provides an interactive control front-end,
usable from JupyterLab, Google Colab, or plain Python scripts.

``cubevis`` currently provides two applications:

- **Interactive Clean** (``iclean``) - **released**. Visually observe and
  control CASA's image reconstruction (``tclean``/``deconvolve``). See
  `Interactive Clean`_ below.
- **Visibility Plotter** (``visplot``) - **in development**. Interactive
  visibility-domain plotting supporting both legacy CASA Measurement Sets
  (MSv2) and the newer AstroVIPER Measurement Set v4 format, via
  `xarray-ms <https://xarray-ms.readthedocs.io>`_. ``visplot`` has no stable
  end-user API yet; it is not ready for general use.

This is a **beta-release** quality package.

Installation
------------

``cubevis`` uses optional extras so that installing it doesn't pull in more
than a given application needs. The extras you want depend on which
application you're using:

.. code-block:: bash

   # Interactive Clean (released)
   pip install cubevis[iclean]

   # Visibility Plotter (in development - no stable API yet)
   pip install cubevis[visplot]

   # add Jupyter/anywidget support for either, if working in a notebook
   pip install cubevis[iclean,notebook]

``iclean`` depends on CASA6's ``casatasks``. ``visplot`` depends on
`xarray-ms <https://xarray-ms.readthedocs.io>`_ / ``arcae`` for **both** its
MSv2 and MSv4 backends, so it does not require ``casatasks`` even when
working with legacy Measurement Sets.

``iclean`` and ``visplot`` are convenience aliases for the underlying
``casa6`` and ``xarray`` extras, respectively; those backend-named extras
are also available directly for anyone assembling a custom environment.
Installing ``casa6`` and ``xarray`` together in the same plain ``pip``
environment is not currently supported, since the two backends can require
mutually incompatible native library versions (notably ``libprotobuf``) that
``pip`` has no mechanism to reconcile - use a `pixi <https://pixi.sh>`_ or
conda environment for combined installs.

Interactive Clean
-----------------

Interactive clean is the primary application provided by this package. It allows for
visualizally observing and controlling the image reconstruction performed by
`CASA <https://casadocs.readthedocs.io/en/stable/index.html>`_. The primary CASA
`tasks <https://casadocs.readthedocs.io/en/stable/api/casatasks.html>`_ used to
perform the image reconstruction are
`tclean <https://casadocs.readthedocs.io/en/stable/api/tt/casatasks.imaging.tclean.html>`_ and
`deconvolve <https://casadocs.readthedocs.io/en/stable/api/tt/casatasks.imaging.deconvolve.html>`_.

Usage
^^^^^

This example provide a summary of how to use interactive clean from Python:

.. code-block:: python

   from cubevis import iclean

   iclean( vis='refim_twopoints_twochan.ms', imagename='test',
           imsize=100, cell='8.0arcsec',
           phasecenter="J2000 19:59:28.500 +40.44.01.50",
           outlierfile='test_outlier.txt',
           niter=50, cycleniter=10, deconvolver='hogbom',
           specmode='mfs', spw='0:0' )


For this sample, the test measurement set is
`available <https://casa.nrao.edu/download/devel/casavis/data/refim_twopoints_twochan-ms.tar.gz>`_,
while the `outlierfile` would look something like::

  imagename=try_multifield_1
  imsize=[80,80]
  cell=[8.0arcsec,8.0arcsec]
  phasecenter=J2000 19:58:41.095 +40.56.01.043

Visibility Plotter
------------------

``visplot`` is under active development and does not yet have a stable
end-user API - it isn't ready for general use, and this README will be
updated with usage instructions once one is available.
