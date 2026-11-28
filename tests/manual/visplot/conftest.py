"""visplot tests see the built-in defaults, not the developer's own
preferences (``~/.config/cubevis/prefs.json``; prefs.py, issue 6 of
``devel/notes/drs/review/issue35_spinoffs.md``).  Set CUBEVIS_PREFS
before running to test with a preference file."""
import os

os.environ.setdefault("CUBEVIS_PREFS", "none")
