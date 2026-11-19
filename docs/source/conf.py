# Configuration for the cubevis documentation (Sphinx).
#
# Pages are Markdown (MyST) or reStructuredText; both build side by side.
# See docs/README.md for how to write and build them.
from os.path import dirname, abspath, join
import os
import sys

sys.path.insert(0, abspath(dirname(__file__)))                         # links
sys.path.insert(0, abspath(join(dirname(__file__), 'extensions')))     # xref, pydoc_include, vp_help

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
               'vp_help',            # help/<vp-label>.html for visplot's h key
             ]

source_suffix = { '.rst': 'restructuredtext', '.md': 'markdown' }

# MyST: ::: fences for admonitions, definition lists, and #anchors on headings
myst_enable_extensions = [ 'colon_fence', 'deflist', 'dollarmath' ]
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

# -- Which copy of the documentation this is ----------------------------------
#
# .github/workflows/docs.yml publishes several copies side by side (see
# docs/publish_site.py) and says which one this build is:
#
#   CUBEVIS_DOCS_KIND   latest (main), release (vX.Y), dev (a docs-* branch);
#                       unset for a local or pull-request build
#   CUBEVIS_DOCS_NAME   main, the X.Y version, or the branch name
#
# A release and a preview get a banner pointing at the latest copy, and every
# copy gets the version menu (_static/js/versions.js).

docs_site_url = 'https://casangi.github.io/cubevis/'
docs_site_path = '/cubevis/'
_kind = os.environ.get('CUBEVIS_DOCS_KIND', '')
_name = os.environ.get('CUBEVIS_DOCS_NAME', '')
_current = { 'release': f'v{_name}/',
             'dev': 'dev/' + ''.join(c if c.isalnum() or c in '._-' else '-' for c in _name) + '/',
           }.get(_kind, '')

html_baseurl = docs_site_url          # canonical links name the latest copy
html_js_files = [ ('js/versions.js', { 'data-site': docs_site_path, 'data-current': _current }) ]
html_context = { 'docs_noindex': _kind == 'dev' }
html_theme_options = { }
if _kind == 'release':
    version = release = _name
    html_title = f'cubevis {_name}'
    html_theme_options['announcement'] = (
        f'This is the documentation for cubevis {_name}. '
        f'The <a href="{docs_site_url}">latest documentation</a> describes the current development version.' )
elif _kind == 'dev':
    html_title = f'cubevis ({_name})'
    html_theme_options['announcement'] = (
        f'Preview of the <code>{_name}</code> branch, not yet merged. '
        f'The <a href="{docs_site_url}">published documentation</a> is the one to cite or link to.' )

# The slide decks (devel/slides/<name>/slides.html) are copied into the site
# root, so they stay at <site>/<name>/slides.html whoever builds the docs:
# GitHub Actions, Read the Docs, or `make html` here.
html_extra_path = ['../../devel/slides']
