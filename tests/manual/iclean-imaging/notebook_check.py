"""Run the cells of tests/manual/jupyter/iclean-jupyterlab-*.ipynb in a real
kernel, without a browser: construct, show, get_result, construct and show
again.  Prints the lines that show whether the notebook bridge was displayed
(at the end of the constructor cell) and whether show() could connect.
With no browser the handshake itself never completes; what this checks is
everything up to it.

    CUBEVIS_DEBUG=1 python notebook_check.py REPO MS WORKDIR [KERNEL]

REPO: the cubevis checkout to import; KERNEL: a kernelspec name (default
python3) whose environment has casatasks, ipykernel, nbclient and anywidget.
"""
import sys, os, nbformat
from nbclient import NotebookClient

repo, ms, work = sys.argv[1:4]
kernel = sys.argv[4] if len(sys.argv) > 4 else 'python3'
os.makedirs(work, exist_ok=True)
cells = [
    f"import sys, logging; sys.path.insert(0, {repo!r})\n"
    "logging.basicConfig(level=logging.DEBUG, stream=sys.stdout, force=True, format='[%(name)s] %(levelname)s: %(message)s')\n"
    "for n in ('asyncio','bokeh','casatools','websockets','matplotlib','PIL','parso','comm'): logging.getLogger(n).setLevel(logging.WARNING)",
    "import cubevis; print('cubevis from', cubevis.__file__)\nfrom cubevis import iclean",
    f"ic = iclean.notebook( vis={ms!r}, imagename='test', imsize=128, niter=50, cycleniter=10, cell='12.0arcsec',\n"
    "    specmode='cube', interpolation='nearest', nchan=5, start='1.0GHz', width='0.2GHz', pblimit=-1e-05,\n"
    "    deconvolver='hogbom', threshold='0.001Jy', cyclefactor=3 )",
    "ic.show( )",
    "import time; time.sleep(3)",
    "print('RESULT', ic.get_result( ))",
    "import subprocess; subprocess.run('rm -rf test.*', shell=True, cwd=%r)" % work,
    f"ic = iclean.notebook( vis={ms!r}, imagename='test', imsize=128, niter=50, cycleniter=10, cell='12.0arcsec',\n"
    "    specmode='cube', interpolation='nearest', nchan=5, start='1.0GHz', width='0.2GHz', pblimit=-1e-05,\n"
    "    deconvolver='hogbom', threshold='0.001Jy', cyclefactor=3 )",
    "ic.show( )",
    "import time; time.sleep(3)",
]
nb = nbformat.v4.new_notebook(cells=[nbformat.v4.new_code_cell(c) for c in cells])
client = NotebookClient(nb, kernel_name=kernel, timeout=600, resources={'metadata': {'path': work}},
                        allow_errors=True, record_timing=False)
client.execute()
for i, c in enumerate(nb.cells):
    for o in c.get('outputs', []):
        text = o.get('text') or o.get('evalue') or ''
        if not text and 'data' in o:
            text = '<display %s>' % ','.join(o['data'].keys())
        for line in str(text).splitlines():
            if any(k in line for k in ('display_bridge', 'connect', 'preflight', 'Error', 'ERROR',
                                       'app_context', 'starting backend', 'cubevis from', 'Traceback',
                                       '<display', 'Showable', 'CommsTransport', 'Application state', 'RESULT', 'process_messages', 'context')):
                print(f'[cell {i}] {line[:220]}')
