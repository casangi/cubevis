"""Shared by the setup tests: what the account's own ssh configuration (and
the system's ``/etc/ssh/ssh_config``) says never leaks into them -- setup
reads it through ``ssh -G`` for defaults, the route, and the login options
the generated configuration copies (``cubevis.setup.sshopts``).  A test that
wants a configuration sets ``CUBEVIS_SETUP_USER_SSH_CONFIG`` itself.  And
PyPI is not asked which release has this code (``cubevis.setup.releases``):
``CUBEVIS_SETUP_PYPI=off`` unless a test sets it."""
import pytest


@pytest.fixture(autouse=True)
def _no_user_ssh_config(monkeypatch):
    import os
    if "CUBEVIS_SETUP_USER_SSH_CONFIG" not in os.environ:
        monkeypatch.setenv("CUBEVIS_SETUP_USER_SSH_CONFIG", "/dev/null")
    if "CUBEVIS_SETUP_PYPI" not in os.environ:
        monkeypatch.setenv("CUBEVIS_SETUP_PYPI", "off")
