.. cubevis documentation master file, created by
   sphinx-quickstart on Tue Jun 29 18:33:59 2021.
   You can adapt this file completely to your liking, but it should at least
   contain the root `toctree` directive.

CASA Visualization Environment
==============================
The :code:`cubevis` package provides graphical user interfaces for the :xref:`casa` radio astronomy
package. This new generation of interfaces will be based upon :xref:`bokeh` and will use
web browsers for display. They are implemented in Python.

There are two basic documentation sections. The primary section describes the applications that are
provided by :code:`cubevis`. This section also describes the application programming interface (API)
which can be used to launch and interact with the applications provided by :code:`cubevis`. The
second describes the motivation, the design and the long term goals of :code:`cubevis`.

Applications and API
--------------------

The first application provided by :code:`cubevis` is interactive clean. It has a simple API which
allows users to perform interactive image reconstruction using CASA.


    .. toctree::
       :maxdepth: 3

       applications/intro


Development and Design
-----------------------

The design and development section describes the motivation, choices and architecture for
:code:`cubevis`. It describes the design choice and tradeoffs that have been made. This section
is primarily useful for those users who would like to understand the design of :code:`cubevis`,
develop new :code:`cubevis` applications, or use the tools found within :code:`cubevis` to create
new or different applications.

:xref:`cubevisrepo` is hosted on :xref:`github`. There you can find the :xref:`cubeviswiki` which
is the project management site for ongoing development.

    .. toctree::
       :maxdepth: 3

       design/intro


Index
=======

Automatically generated indexes.

* :ref:`genindex`
* :ref:`modindex`
* :ref:`search`
