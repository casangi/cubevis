"""Write t.ipynb: one cell that opens visplot on $MS (see README.md).

    python make_notebook.py DIR
"""
import os, sys
import nbformat as nbf

d = sys.argv[1] if len(sys.argv) > 1 else "."
ms = os.environ.get("MS")
if not ms:
    sys.exit("set MS to a measurement set")
nb = nbf.v4.new_notebook()
nb.cells = [nbf.v4.new_code_cell(
    "import cubevis.bokeh.state._initialize as _ini\n"
    "# Load scripts from their CDN URLs, which drive.py answers with local copies\n"
    "# (a page served over http may not load file:// scripts).  have_network is\n"
    "# forced because a sandbox may block cubevis's connectivity check.\n"
    "_ini.have_network = lambda: True\n"
    "_ini.set_js_loading(\"network\")\n"
    "from cubevis.toolbox.visplot import VisibilityPlotter\n"
    f"vp = VisibilityPlotter(ms={ms!r})\n"
    "vp.show()")]
nb.metadata["kernelspec"] = {"name": "python3", "display_name": "Python 3", "language": "python"}
nbf.write(nb, os.path.join(d, "t.ipynb"))
