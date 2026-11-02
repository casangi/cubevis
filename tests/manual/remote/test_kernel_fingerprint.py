"""``RemoteReductionContext.kernel_fingerprint`` asks the Jupyter kernel on a
client of its own -- and the link to the worker must go on working after it
(2026-11-01, Darrell's try-010A: visplot hung on the first call after the
check, because the extra client shared the kernel manager's ``Session`` with
the link's client, and the Session's replay check then dropped the worker's
replies).  A local kernel and a real worker; no ssh, no measurement set.
"""
import os

import pytest

pytest.importorskip("jupyter_client")
pytest.importorskip("ipykernel")
pytest.importorskip("bokeh")

from jupyter_client import AsyncKernelManager

from cubevis import _fingerprint
from cubevis.remote import RemoteAppLink, SyncBridge
from cubevis.toolbox.visplot.remote_reduction_context import RemoteReductionContext

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))


def test_the_link_works_after_the_kernel_is_asked_for_its_fingerprint():
    bridge = SyncBridge(name="fp-link-test")
    bridge.start()
    km = AsyncKernelManager(kernel_name="python3")
    bridge.run(km.start_kernel(env={**os.environ, "PYTHONPATH": SRC}), timeout=60)
    try:
        link = bridge.run(RemoteAppLink.open(km, worker_target_name="fp-link-test", timeout=30),
                          timeout=60)
        ctx = bridge.run(link.create_context(
            config={"register_function": "cubevis.remote._test_registrations:register_basic"}),
            timeout=60)
        h = bridge.run(ctx.create_object("Counter", args=[10]), timeout=30)
        assert bridge.run(ctx.call_method(h, "increment", args=[1]), timeout=30) == 11
        # The check, as visplot runs it: same bridge, same kernel manager.
        c = RemoteReductionContext.__new__(RemoteReductionContext)
        c._bridge, c._km = bridge, km
        assert c.kernel_fingerprint() == _fingerprint.fingerprint(root=SRC)
        # ... and the worker still answers (it hung here before the fix).
        assert bridge.run(ctx.call_method(h, "increment", args=[1]), timeout=20) == 12
        assert c.kernel_fingerprint() == _fingerprint.fingerprint(root=SRC)      # again
        assert bridge.run(ctx.call_method(h, "increment", args=[1]), timeout=20) == 13
        bridge.run(link.close(), timeout=20)
    finally:
        bridge.run(km.shutdown_kernel(now=True), timeout=60)
