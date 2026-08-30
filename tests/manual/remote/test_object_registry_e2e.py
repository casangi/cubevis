"""
Chunk 1c, Task 4 definition-of-done: the real end-to-end path for
generalized remote object creation/invocation -- a genuine worker
subprocess, configured via the opening `configure` message with a real
`register_function` (`cubevis.remote._test_registrations:register_basic`,
see that module's docstring for why fixtures live inside the package
rather than under tests/), driven through `ExecutionContext.create_object`/
`call_method`/`dispose_object`. Includes a non-trivial numpy-array-typed
argument AND return value, so the real serializer
(`cubevis.utils.serialize`/`deserialize`, Bokeh's own
`Serializer`/`Deserializer`) is actually exercised on the wire, not just
JSON-native scalars.
"""
import os

import numpy as np
import pytest

pytest.importorskip("jupyter_client")
pytest.importorskip("ipykernel")

from jupyter_client import AsyncKernelManager

from cubevis.remote import RemoteAppLink


@pytest.mark.asyncio
async def test_create_object_call_method_dispose_against_real_worker():
    src_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
    km = AsyncKernelManager(kernel_name="python3")
    await km.start_kernel(env={**os.environ, "PYTHONPATH": src_path})
    try:
        link = await RemoteAppLink.open(km, worker_target_name="object-registry-e2e", timeout=30)
        try:
            ctx = await link.create_context(
                config={"register_function": "cubevis.remote._test_registrations:register_basic"}
            )

            # -- a plain stateful object, exercised across multiple calls --
            handle = await ctx.create_object("Counter", args=[10])
            assert isinstance(handle, str) and handle

            assert await ctx.call_method(handle, "increment", args=[5]) == 15
            assert await ctx.call_method(handle, "increment", kwargs={"by": 1}) == 16

            assert await ctx.dispose_object(handle) is True

            # `call_method`'s documented contract is "the raw return
            # value, unwrapped" (see _link.py) -- a handler-side
            # exception (here, ObjectRegistry.UnknownHandleError) never
            # raises on the caller's side; CommMgr._handle_request turns
            # it into an {'error': ..., 'traceback': ...} reply instead
            # (the same convention test_worker_process_transport.py's
            # crash-diagnosability test relies on), so the failure shows
            # up as that dict, not a raised exception.
            reply = await ctx.dispatch_fast("call_method",
                                             {"handle": handle, "method": "increment",
                                              "args": [], "kwargs": {}})
            assert "error" in reply and "handle" in reply["error"].lower(), (
                f"expected an 'unknown handle' error after disposal, got {reply}"
            )

            # -- unknown class name: create_object's wrapper does
            # `reply["handle"]`, which raises KeyError against an
            # {'error': ...} reply -- so go through dispatch_fast
            # directly here too, for the same reason as above.
            reply = await ctx.dispatch_fast("create_object",
                                             {"class_name": "NoSuchClass", "args": [], "kwargs": {}})
            assert "error" in reply and "NoSuchClass" in reply["error"], (
                f"expected an 'unknown class' error naming the class, got {reply}"
            )

            # -- real numpy-array round trip through the real serializer --
            echo_handle = await ctx.create_object("NumpyEcho")
            original = np.arange(6, dtype="float64").reshape(2, 3)

            doubled = await ctx.call_method(echo_handle, "double", args=[original])
            doubled_arr = np.asarray(doubled)
            assert doubled_arr.shape == (2, 3)
            assert np.array_equal(doubled_arr, original * 2), (
                "a numpy array argument/return value must round-trip correctly "
                "through cubevis.utils.serialize/deserialize over the real wire, "
                "not just JSON-native scalars"
            )

            shape = await ctx.call_method(echo_handle, "shape_of", args=[original])
            assert tuple(shape) == (2, 3)

            await ctx.dispose_object(echo_handle)
        finally:
            await link.close(timeout=20)
    finally:
        await km.shutdown_kernel()
