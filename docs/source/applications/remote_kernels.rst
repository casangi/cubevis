Remote Kernels
==============

.. currentmodule:: applications

``visplot`` and interactive clean can run their data side -- reading the
measurement set, running :code:`tclean`, reading and writing the images
and masks -- in a Jupyter kernel on the computer that holds the data,
while the window stays on your own computer.  Only what the window shows
crosses the network.  The kernel is named with ``kernel_name=``::

   from cubevis import visplot, iclean

   visplot(vis="sis14_twhya_calibrated_flagged.ms", kernel_name="cvpost106-visplot")
   iclean(vis="sis14_twhya_calibrated_flagged.ms", imagename="twhya", ...,
          kernel_name="cvpost106-iclean")

Paths (``vis``, ``imagename``) are the kernel's: relative paths are found
in, and images written to, the kernel's working directory on that computer.

Setting up a kernel
-------------------

``python -m cubevis.setup`` sets up kernels, once per computer you want to
use, from your own computer::

   pip install "cubevis[remote]"          # on your computer: ssh-started kernels
   python -m cubevis.setup cvpost106      # the computer that holds the data

It asks before each change, says what it will change, and tests each step.
On your computer it writes only under ``~/.config/cubevis``; on the remote
computer, the kernels' launchers and, when it makes one, its own Python
environment.  It works out how to reach the computer from your ssh
configuration (a login host in between, passwords or keys, two-factor
logins), finds the Python environments there that have cubevis, and makes
one when there is none (a conda environment with
``cubevis[visplot,iclean]``).  A computer that is one of a cluster's nodes
(``cvpost001``, ``cvpost002``, ...) is set up once for all of them.

A kernel is an environment, the directory and variables it starts with,
and a name.  At the end, setup prints how to use it::

   kernel cvpost106-iclean (on cvpost106) runs visplot, interactive clean:

     from cubevis import iclean
     iclean(vis="...", imagename="...", ..., kernel_name="cvpost106-iclean")

What a kernel can run
---------------------

Each application needs its own packages in the kernel's environment:

* ``visplot``: ``cubevis[visplot]`` (xarray-ms, zarr, dask, distributed,
  datashader);
* interactive clean: ``cubevis[iclean]`` (casatasks and casatools), and
  CASA's data (below).

Setup checks this when it tests a kernel and records it
(``python -m cubevis.setup status`` shows it), and each connection checks it
again: a kernel whose environment lacks an application's packages says so
when you start the application, with the ``pip install`` that adds them.
An environment setup made itself can have interactive clean added to it: a
rerun of ``python -m cubevis.setup HOST`` offers it.  Setup never installs
into an environment of yours.

CASA's data
-----------

Interactive clean needs CASA's data (the measures).  When the kernel's
environment finds it -- the ``measurespath`` in your ``~/.casa/config.py``, a site
configuration, or ``~/.casa/data`` -- setup asks whether to use it or to give
the environment a private copy (the ``casadata`` package, about 850 MB,
never updated), so that cleaning there does not change when your own data is
updated.  When none is found, an environment setup made gets the private
copy; for an environment of yours setup says what to do.

Changing a kernel
-----------------

Run setup again at any time; nothing that passes is repeated::

   python -m cubevis.setup cvpost106                 # check its kernels; offer another one
   python -m cubevis.setup cvpost106 --kernel NAME   # add kernel NAME (its environment is asked)
   python -m cubevis.setup kernel NAME --workdir ~/casa/other
   python -m cubevis.setup kernel NAME --env "OMP_NUM_THREADS=8"     # 'none' clears them
   python -m cubevis.setup kernel NAME --environment ENV
   python -m cubevis.setup status                    # what is set up
   python -m cubevis.setup check                     # test the logins and kernels
   python -m cubevis.setup --help                    # every command

Interactive clean on a remote kernel
------------------------------------

* The parameters are checked on the kernel's computer, as :code:`tclean`'s
  task checks them: a path that is not there (relative to the kernel's
  working directory) is reported with that computer's name and directory.
* The images are best written in the kernel's working directory (a
  relative ``imagename``): with current casatasks, ``tclean``'s
  interactive driver looks for the residual and mask there, and an
  ``imagename`` elsewhere fails at the first cycle.
* While a cycle runs, the status line shows its progress, read from
  :code:`tclean`'s log on the kernel's computer: the elapsed time, the
  minor or major cycle, channels, iterations and the peak residual (the
  whole text is its tooltip).
* Stop takes effect after the running cycle; :code:`tclean` cannot be
  interrupted within one.
* When the window is closed, or Python ends, the remote session is closed:
  the kernel and its worker stop.  The images stay on that computer.
