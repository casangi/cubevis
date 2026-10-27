"""visplot without CASA, and with a CASA that cannot start (2026-10-26).

Darrell, in a fresh account with casatasks installed but its data not set
up: ``from cubevis import visplot`` failed in casaconfig
(``AutoUpdatesNotAllowed``, an Exception, not an ImportError), and
``visplot(...)`` needed ``casatasks.private.task_logging``.  Now cubevis
never imports CASA to see whether to use it (``cubevis.utils._casa``):
casalog and the task log lines only when ``casatasks`` is loaded already;
casatools for the image code on first use.

Each case runs in a fresh interpreter with a fake ``casatools`` /
``casatasks`` on ``PYTHONPATH``.

    pytest tests/manual/visplot/test_casa_optional.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))

BROKEN = {
    "casatools/__init__.py": """
        import os
        open(os.environ["CASA_TOUCHED"], "a").write("casatools\\n")
        class AutoUpdatesNotAllowed(Exception):
            pass
        raise AutoUpdatesNotAllowed("data_update: path must exist as a directory (fake)")
    """,
    "casatasks/__init__.py": """
        from casatools import logsink as _logsink
    """,
}

WORKING = {
    "casatools/__init__.py": "",
    "casatasks/__init__.py": """
        import os
        LOG = os.environ["CASA_TOUCHED"]
        class _Log:
            def post(self, message, priority="INFO", origin=""):
                open(LOG, "a").write(f"casalog {priority} {message}\\n")
            def filter(self, *a): pass
            def origin(self, *a): pass
        casalog = _Log()
    """,
    "casatasks/private/__init__.py": "",
    "casatasks/private/task_logging.py": """
        import os
        def start_log(tname, arguments):
            open(os.environ["CASA_TOUCHED"], "a").write(f"start {tname}\\n")
            return (0,)
        def end_log(state, tname, result):
            open(os.environ["CASA_TOUCHED"], "a").write(f"end {tname}\\n")
            return result
        def except_log(tname, exc):
            open(os.environ["CASA_TOUCHED"], "a").write(f"except {tname} {type(exc).__name__}\\n")
    """,
}


def _fake(tmp_path, files):
    for rel, text in files.items():
        p = tmp_path / "fake" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(text))
    return str(tmp_path / "fake")


def _run(tmp_path, files, *parts):
    code = "".join(textwrap.dedent(x) for x in parts)
    touched = tmp_path / "touched.txt"
    touched.write_text("")
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([_fake(tmp_path, files), ROOT]),
               CASA_TOUCHED=str(touched))
    r = subprocess.run([sys.executable, "-c", code], capture_output=True,
                       text=True, env=env, cwd=str(tmp_path), timeout=300)
    return r, touched.read_text()


CALL = """
    try:
        visplot(ms="nothing.ms", backend="remote", kernel_name="cubevis-nosuch")
    except Exception as exc:
        print("CALL", type(exc).__name__, exc)
"""


def test_a_casa_that_cannot_start_is_not_touched(tmp_path):
    r, touched = _run(tmp_path, BROKEN, """
        import sys
        from cubevis import visplot
        print("IMPORTED", "casatools" in sys.modules, "casatasks" in sys.modules)
    """, CALL, """
        from cubevis.toolbox.visplot import flag_commit
        print("COMMIT", flag_commit.casatools_available()[0])
    """)
    assert "IMPORTED False False" in r.stdout, r.stdout + r.stderr
    # The kernel name is checked before anything starts, and the message
    # says what to do.
    assert "CALL ValueError no Jupyter kernel named 'cubevis-nosuch'" in r.stdout
    assert "python -m cubevis.setup jupyter KERNEL makes cubevis-KERNEL" in r.stdout
    # Only committing flags (which needs flagdata) tries CASA -- and says no.
    assert "COMMIT False" in r.stdout
    assert touched.count("casatools") == 1


def test_inside_casa_the_task_logs_there(tmp_path):
    """casatasks loaded first (as in CASA): casalog and the task log lines."""
    r, touched = _run(tmp_path, WORKING, """
        import casatasks
        from cubevis import visplot
        from cubevis.utils import get_logger
        get_logger().warning("hello casalog")
    """, CALL)
    assert "CALL ValueError" in r.stdout, r.stdout + r.stderr
    assert "start visplot" in touched and "except visplot ValueError" in touched
    assert "end visplot" in touched and "casalog WARN hello casalog" in touched


def test_without_casa_loaded_nothing_goes_to_it(tmp_path):
    """The same working CASA installed but not imported: left alone."""
    r, touched = _run(tmp_path, WORKING, """
        import sys
        from cubevis import visplot
        from cubevis.utils import get_logger
        get_logger().warning("hello python logging")
    """, CALL, """
        print("LOADED", "casatasks" in sys.modules)
    """)
    assert "CALL ValueError" in r.stdout and "LOADED False" in r.stdout, r.stdout + r.stderr
    assert touched == ""
