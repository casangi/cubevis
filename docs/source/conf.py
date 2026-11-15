# Configuration for the cubevis documentation (Sphinx).
#
# Pages are Markdown (MyST) or reStructuredText; both build side by side.
# See docs/README.md for how to write and build them.
from os.path import dirname, abspath, join
import os
import sys

sys.path.insert(0, abspath(dirname(__file__)))                         # links
sys.path.insert(0, abspath(join(dirname(__file__), 'extensions')))     # xref, pydoc_include

# -- Project information -----------------------------------------------------

project = 'cubevis'
copyright = '2021-2026 Associated Universities, Inc. Washington DC, USA.'
author = 'CASA visualization team'

# -- Links for the :xref: role (docs/source/links/*.py) -----------------------

from links.link import *
from links import *

rst_prolog = """
 .. include:: <s5defs.txt>
 """

# -- General configuration ---------------------------------------------------

extensions = [ 'sphinx.ext.todo',
               'sphinx.ext.intersphinx',
               'myst_parser',
               'xref',
               'pydoc_include',
             ]

source_suffix = { '.rst': 'restructuredtext', '.md': 'markdown' }

# MyST: ::: fences for admonitions, definition lists, and #anchors on headings
myst_enable_extensions = [ 'colon_fence', 'deflist' ]
myst_heading_anchors = 3

# Where the "from the Python source" labels and slide "source" links point.
# GitHub Actions sets GITHUB_REF_NAME, so a branch build links to its branch.
_ref = os.environ.get('GITHUB_REF_NAME') or 'main'
if os.environ.get('GITHUB_EVENT_NAME') == 'pull_request':
    _ref = os.environ.get('GITHUB_HEAD_REF') or 'main'
pydoc_source_url = f'https://github.com/casangi/cubevis/blob/{_ref}'

templates_path = ['_templates']

# The casagui API stubs and notebooks are kept with the historical
# reference but are not built.
exclude_patterns = [ 'historical/python', 'historical/notebooks' ]

# -- Options for HTML output -------------------------------------------------

html_theme = 'furo'
html_title = 'cubevis'
html_static_path = ['_static']
html_css_files = ['css/s4defs-roles.css', 'css/cubevis-docs.css']

# The slide decks (devel/slides/<name>/slides.html) are copied into the site
# root, so they stay at <site>/<name>/slides.html whoever builds the docs:
# GitHub Actions, Read the Docs, or `make html` here.
html_extra_path = ['../../devel/slides']
