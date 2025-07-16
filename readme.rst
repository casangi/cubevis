cubevis - visualization tools for CASA images
=============================================

This is a **beta-release** quality package. This package relies on the
`CASA <https://casadocs.readthedocs.io/en/stable/index.html>`_ data processing system
for radio telescopes as the processing backend while providing control and visualization
using `Bokeh <https://bokeh.org/>`_.

Introduction
------------

These tools are primarily based on `Bokeh <https://bokeh.org/>`_. The GUIs use Python
and CASA to provide image access while a generated JavaScript interface provides a control
front-end.

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

This example provide a summary of how to use interactive clean from Python


For this sample, the test measurement set is
`available <https://casa.nrao.edu/download/devel/casavis/data/refim_twopoints_twochan-ms.tar.gz>`_,
while the `outlierfile` would look something like
