"""visplot's check of the kernel it starts (``visibility_plotter.kernel_code_check``):
the kernel-side fingerprint decides, not the version (2026-11-01: a checkout
used against a 1.0.102 environment with the same code was warned about as
"cubevis 1.0.102 differs from local unknown").  The versions are compared
only when the kernel's fingerprint cannot be had.

    pytest tests/manual/setup/test_kernel_check.py

The real round trip (``RemoteReductionContext.kernel_fingerprint`` over
sshpyk) is in ``test_cli.py`` (the rig).
"""
from __future__ import annotations

import pytest

pytest.importorskip("bokeh")
from cubevis import _fingerprint as F                                   # noqa: E402
from cubevis.toolbox.visplot import visibility_plotter as VP            # noqa: E402

HERE = F.fingerprint()


class Remote:
    def __init__(self, fp, versions=(("cubevis", "1.0.102"),)):
        self._fp, self._versions = fp, list(versions)

    def kernel_fingerprint(self):
        return self._fp

    def software_versions(self):
        return self._versions


def test_same_code_under_another_version_is_not_warned_about():
    versions, warning = VP.kernel_code_check(Remote(HERE))
    assert warning is None and versions == [("cubevis", "1.0.102")]


def test_other_code_is_warned_about_by_its_fingerprint():
    _v, warning = VP.kernel_code_check(Remote(F.ALGORITHM + "-0000000000"))
    assert "its kernel-side code differs (there " in warning and HERE in warning
    assert "python -m cubevis.setup HOST" in warning


def test_without_the_kernels_fingerprint_the_versions_decide(monkeypatch):
    from cubevis.toolbox.visplot import plot_summary
    monkeypatch.setattr(plot_summary, "software_versions", lambda: [("cubevis", "1.0.103")])
    _v, warning = VP.kernel_code_check(Remote(None))
    assert "kernel cubevis 1.0.102 differs from local 1.0.103" in warning

    class Old:                                   # a context from before kernel_fingerprint
        def software_versions(self):
            return [("cubevis", "1.0.103")]
    assert VP.kernel_code_check(Old()) == ([("cubevis", "1.0.103")], None)
