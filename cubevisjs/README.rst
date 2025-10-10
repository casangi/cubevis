cubevisjs
=========

This directory contains the build tree for the TypeScript portion of the
``cubevis`` extensions for `Bokeh <https://bokeh.org/>`_. However, it *only*
contains the *TypeScript* build infrastructure, not the actual *TypeScript*
source files. The source files are included with the *Python* files in the
``cubevis`` tree found in the parent directory. This allows the *TypeScript*
and *Python* files to be edited from the same directory and keeps the
``npm`` clutter out of the *Python* package directory.

Build Instructions
==================

``npm`` and ``bokeh`` are used to build these extensions:

1. Ensure that the ``bokeh`` executable is available::

   bash$ type bokeh
   bokeh is hashed (/opt/local/Library/Frameworks/Python.framework/Versions/3.8/bin/bokeh)
   bash$

2. Ensure that the ``npm`` (version 8 or greater) executable is available::

   bash$ type npm
   npm is /opt/local/bin/npm
   bash$

3. The *Bokeh* dependency in the build scripts is maintained by hand. It is **not** automatically
   updated as part of the next command. To update, the *Bokeh* dependency run::

   bash$ npm run sync-bokeh

   If an update has occurred (*and is desired*), then run:

   bash$ npm install


4. Run ``npm run build``::

   bash$ npm run build

   > cubevisjs@0.0.20 prebuild
   > mkdir -p scripts


   > cubevisjs@0.0.20 build
   > bokeh build && npm run copy-versioned

   Using nodejs v24.8.0 and npm 11.6.0
   Working directory: /Users/dschiebel/develop/casa/interactive-clean/integration/cubevis/cubevisjs
   TypeScript lib: /Users/dschiebel/.conda-miniforge3/envs/bokeh-py311/lib/python3.11/site-packages/bokeh/server/static/lib
   Using /Users/dschiebel/develop/casa/interactive-clean/integration/cubevis/cubevisjs/tsconfig.json
   Compiling styles (0 files)
   Compiling TypeScript (20 files)
   Linking modules
   Output written to /Users/dschiebel/develop/casa/interactive-clean/integration/cubevis/cubevisjs/dist
   All done.

   > cubevisjs@0.0.20 copy-versioned
   > node scripts/copy-versioned.js

   Detected Bokeh version: 3.6.1
   Creating versioned copies (v0.0.20, Bokeh 3.6):
   ✓ Minified library: cubevisjs-0.0.20.3.6.min.js
   ✓ Development library: cubevisjs-0.0.20.3.6.js
   ✓ Source map: cubevisjs-0.0.20.3.6.js.map
   ✓ Bokeh model definitions: cubevisjs-0.0.20.3.6.json

   Summary: 4 files copied, 0 errors
      Working directory: /Users/drs/develop/cubevis/kernels/python/cubevisjs
      Using different version of bokeh, rebuilding from scratch.
      Running npm install.

      added 41 packages, and audited 42 packages in 1s

      found 0 vulnerabilities
      Using /Users/drs/develop/cubevis/kernels/python/cubevisjs/tsconfig.json
      Compiling styles
      Compiling TypeScript (2 files)
      Linking modules
      Output written to /Users/drs/develop/cubevis/kernels/python/cubevisjs/dist
      All done.
      bash$

5. ``bokeh`` uses a cache, so if you suspect you are not getting a clean rebuild, try ``bokeh build --rebuild``

6. When the build seems good, publish ``cubevisjs.min.js`` with::

   bash$ BOKEH_VERSION=`bokeh -v | perl -pe 's|^(\d+\.\d+).*|$1|'`
   bash$ mkdir ../cubevis/__js__/bokeh-$BOKEH_VERSION
   bash$ cp dist/cubevisjs.min.js ../cubevis/__js__/bokeh-$BOKEH_VERSION
   bash$

   A version of the *minified* JavaScript library and accompaning files are also created
   for download from the
   `CASA JavaScript download area <https://casa.nrao.edu/download/javascript/cubevis/cubevisjs/>`_.
   These files are **versioned** and include both the ``cubevisjs`` version and the Bokeh
   version, though only the *major* and *minor* part of the Bokeh version number. These
   files should be uploaded to the
   `CASA JavaScript download area <https://casa.nrao.edu/download/javascript/cubevis/cubevisjs/>`_.
