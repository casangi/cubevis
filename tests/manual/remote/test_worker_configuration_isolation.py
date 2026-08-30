"""
Chunk 1c, Task 3 definition-of-done: two execution contexts under the
same supervisor kernel, configured with two different
`register_function`s via the opening `configure` message, each only
having access to what it was itself configured with. Isolation here is
"for free" in the strongest possible sense -- each execution context is
its own OS process (see the design doc §2f), so there is no shared
Python-level state to accidentally leak between them; this test proves
that in practice rather than just asserting it by construction.
"""
import os

import pytest

pytest.importorskip("jupyter_client")
pytest.importorskip("ipykernel")

from jupyter_client import AsyncKernelManager

from cubevis.remote import RemoteAppLink


@pytest.mark.asyncio
async def test_two_contexts_configured_differently_are_isolated():
    src_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
    km = AsyncKernelManager(kernel_name="python3")
    await km.start_kernel(env={**os.environ, "PYTHONPATH": src_path})
    try:
        link = await RemoteAppLink.open(km, worker_target_name="config-isolation-test", timeout=30)
        try:
            ctx_basic = await link.create_context(
                config={"register_function": "cubevis.remote._test_registrations:register_basic"}
            )
            ctx_alt = await link.create_context(
                config={"register_function": "cubevis.remote._test_registrations:register_alt"}
            )
            assert ctx_basic.pid != ctx_alt.pid, (
                "two contexts must be genuinely separate OS processes"
            )

            # ctx_basic can create a Counter (its own registration)...
            handle = await ctx_basic.create_object("Counter", args=[1])
            assert isinstance(handle, str) and handle

            # ...but NOT an OnlyInAlt -- that class was never registered
            # in ctx_basic's process, only in ctx_alt's.
            reply = await ctx_basic.dispatch_fast(
                "create_object", {"class_name": "OnlyInAlt", "args": [], "kwargs": {}}
            )
            assert "error" in reply and "OnlyInAlt" in reply["error"], (
                f"ctx_basic should not have access to OnlyInAlt (registered only "
                f"in ctx_alt's separate process), got {reply}"
            )

            # And symmetrically: ctx_alt can create OnlyInAlt...
            alt_handle = await ctx_alt.create_object("OnlyInAlt")
            assert await ctx_alt.call_method(alt_handle, "ping") == "alt"

            # ...but NOT a Counter -- that was only registered in
            # ctx_basic's separate process.
            reply = await ctx_alt.dispatch_fast(
                "create_object", {"class_name": "Counter", "args": [], "kwargs": {}}
            )
            assert "error" in reply and "Counter" in reply["error"], (
                f"ctx_alt should not have access to Counter (registered only "
                f"in ctx_basic's separate process), got {reply}"
            )

            # Also true at the eval/exec layer: each context's namespace
            # only has the _registry it was configured with.
            names_basic = await ctx_basic.eval_code("_registry.registered_class_names()")
            names_alt = await ctx_alt.eval_code("_registry.registered_class_names()")
            assert names_basic == ["Counter", "NumpyEcho"]
            assert names_alt == ["OnlyInAlt"]
        finally:
            await link.close(timeout=20)
    finally:
        await km.shutdown_kernel()
