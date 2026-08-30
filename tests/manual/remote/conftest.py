import os
import sys

# Sandbox-only fallback: if cubevis isn't already importable (e.g. via an
# editable install), fall back to the local src/ layout this was built
# and tested against. Harmless no-op if that path doesn't exist -- see
# the package README.
_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)
