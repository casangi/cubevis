"""kernel_name= without the packages that start a kernel: a message saying
what to install, not "No module named 'jupyter_client'" (a CASA without the
``remote`` packages, Darrell 2026-11-10).

    pytest tests/manual/remote/test_kernel_packages.py
"""
from __future__ import annotations

import importlib.util
import json
import sys

import pytest

from cubevis.remote import (MissingKernelPackages, RemoteObjectConnection,
                            require_kernel_packages)


def _hide(monkeypatch, *names):
    real = importlib.util.find_spec
    monkeypatch.setattr(importlib.util, "find_spec",
                        lambda n, *a, **k: None if n in names else real(n, *a, **k))


def test_without_jupyter_client_the_connection_says_what_to_install(monkeypatch):
    _hide(monkeypatch, "jupyter_client")
    with pytest.raises(MissingKernelPackages) as e:
        RemoteObjectConnection("somehost", "Anything", register_function="f", app="iclean")
    msg = str(e.value)
    assert 'kernel_name="somehost"' in msg and "jupyter_client" in msg
    assert f'{sys.executable} -m pip install jupyter_client "sshpyk>=1.23"' in msg
    assert "leave out kernel_name=" in msg
    assert isinstance(e.value, ImportError)          # what callers already catch


def _spec(tmp_path, monkeypatch, provisioner):
    d = tmp_path / "kernels" / "k-ssh"
    d.mkdir(parents=True)
    meta = {"kernel_provisioner": {"provisioner_name": provisioner, "config": {}}} if provisioner else {}
    (d / "kernel.json").write_text(json.dumps({"argv": [sys.executable, "-c", "pass"],
                                               "display_name": "k", "language": "python",
                                               "metadata": meta}))
    monkeypatch.setenv("JUPYTER_PATH", str(tmp_path))


def test_a_kernel_over_ssh_without_sshpyk(tmp_path, monkeypatch):
    _spec(tmp_path, monkeypatch, "sshpyk-provisioner")
    _hide(monkeypatch, "sshpyk")
    require_kernel_packages("k-ssh", "visplot")                     # before resolving: fine
    with pytest.raises(MissingKernelPackages) as e:
        require_kernel_packages("k-ssh", "visplot", resolved=True)
    assert "sshpyk" in str(e.value) and '"sshpyk>=1.23"' in str(e.value)


def test_a_local_kernel_needs_no_sshpyk(tmp_path, monkeypatch):
    _spec(tmp_path, monkeypatch, None)
    _hide(monkeypatch, "sshpyk")
    require_kernel_packages("k-ssh", "iclean", resolved=True)
    require_kernel_packages("no-such-kernel", "iclean", resolved=True)   # left to jupyter_client
