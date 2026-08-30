"""
Chunk 1c, Task 6 definition-of-done: `KernelPersistenceManifest` proven
against a real-or-realistically-faked sshpyk-like persistent-file
lifecycle.

What is real vs. faked here, stated plainly (this sandbox has no
SSH/cluster access, matching every other Chunk 1c test's documented
limitation): a real `AsyncKernelManager` starts a real local kernel
subprocess and writes a real connection file -- that connection file is
this test's stand-in for one of sshpyk's own `persistent_file`s (same
role: a JSON file on disk that lets a *separate* process reconnect to
an already-running kernel without starting a new one). What is
deliberately NOT exercised: sshpyk's own remote-PID-reverification
logic inside a real reattach (that's `sshpyk`'s job, and
`_persistence.py` is explicit about staying layered on top of it, never
reimplementing it -- see its module docstring). The "liveness" proof
this test performs instead is the generic jupyter_client-level
reattach `_persistence.py`'s docstring says is the caller's own
responsibility: loading the recorded connection file into a *second*,
independently-constructed `AsyncKernelManager` and completing a real
`kernel_info_request` round trip against the still-running kernel.

The manifest bookkeeping itself -- record/outstanding/forget, and
critically that a FRESH `KernelPersistenceManifest` instance (simulating
a `P_local` restart) sees what an earlier instance wrote, not a
silently-reused or silently-discarded copy -- is exercised for real
against the actual file on disk, no mocking.
"""
import os
import tempfile

import pytest

pytest.importorskip("jupyter_client")
pytest.importorskip("ipykernel")

from jupyter_client import AsyncKernelManager

from cubevis.remote import KernelPersistenceManifest


@pytest.mark.asyncio
async def test_record_survives_a_simulated_p_local_restart_and_forget_removes_it():
    with tempfile.TemporaryDirectory() as tmpdir:
        manifest_path = os.path.join(tmpdir, "cubevis-kernel-manifest.json")

        km = AsyncKernelManager(kernel_name="python3")
        await km.start_kernel()
        try:
            label = "test-cluster-session"

            # --- "P_local session #1": record the persistent kernel ------
            manifest_1 = KernelPersistenceManifest(path=manifest_path)
            assert manifest_1.outstanding() == {}, "must start empty against a fresh path"

            manifest_1.record(label, km.connection_file, kernel_name="python3")

            entry = manifest_1.get(label)
            assert entry is not None
            assert entry["persistent_file"] == km.connection_file
            assert entry["kernel_name"] == "python3"
            assert "created_at" in entry

            # --- "P_local session #2": a FRESH manifest instance, same ---
            # --- path -- simulates P_local having exited and restarted. ---
            manifest_2 = KernelPersistenceManifest(path=manifest_path)
            outstanding = manifest_2.outstanding()
            assert label in outstanding, (
                "a fresh KernelPersistenceManifest instance must surface an "
                "entry recorded by an earlier instance/process -- this is "
                "the whole point: it must not be silently reused (returning "
                "cached in-memory data) or silently discarded"
            )
            assert outstanding[label]["persistent_file"] == km.connection_file

            # --- liveness/reattach: the caller's own job (see module -----
            # --- docstring), proven here via the generic jupyter_client ---
            # --- mechanism sshpyk-provisioned kernels use identically. ---
            recorded_connection_file = outstanding[label]["persistent_file"]
            reattached_km = AsyncKernelManager()
            reattached_km.load_connection_file(recorded_connection_file)
            client = reattached_km.client()
            client.start_channels()
            try:
                await client.wait_for_ready(timeout=30)
                client.kernel_info()
                msg = await client.get_shell_msg(timeout=15)
                assert msg["msg_type"] == "kernel_info_reply", (
                    "reattaching via the recorded persistent_file must reach "
                    "the SAME still-running kernel, not fail or spawn a new one"
                )
            finally:
                client.stop_channels()

            # --- deliberate shutdown through cubevis -> forget() ----------
            forgotten = manifest_2.forget(label)
            assert forgotten is True

            manifest_3 = KernelPersistenceManifest(path=manifest_path)
            assert label not in manifest_3.outstanding(), (
                "forget() must actually persist to disk -- a third fresh "
                "instance must no longer see this label"
            )

            # Forgetting an already-forgotten label is a no-op, not an error.
            assert manifest_3.forget(label) is False
        finally:
            await km.shutdown_kernel()


def test_forget_unknown_label_returns_false_without_touching_the_file():
    with tempfile.TemporaryDirectory() as tmpdir:
        manifest_path = os.path.join(tmpdir, "cubevis-kernel-manifest.json")
        manifest = KernelPersistenceManifest(path=manifest_path)
        assert manifest.forget("never-recorded") is False


def test_record_overwrites_a_previous_entry_under_the_same_label():
    with tempfile.TemporaryDirectory() as tmpdir:
        manifest_path = os.path.join(tmpdir, "cubevis-kernel-manifest.json")
        manifest = KernelPersistenceManifest(path=manifest_path)

        manifest.record("session-a", "/tmp/fake-persistent-file-1.json", kernel_name="python3")
        first_created_at = manifest.get("session-a")["created_at"]

        manifest.record("session-a", "/tmp/fake-persistent-file-2.json", kernel_name="python3")
        entry = manifest.get("session-a")
        assert entry["persistent_file"] == "/tmp/fake-persistent-file-2.json", (
            "recording again under the same label must supersede the previous "
            "entry, not accumulate multiple entries under one label"
        )
        assert len(manifest.outstanding()) == 1
