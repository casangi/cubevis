"""
Chunk 1, Task 6 -- fast, in-process coverage of `ensure_remote_worker`'s
idempotency contract, independent of any real kernel process (that part
is covered separately in test_worker_start_reattach_real_kernel.py).
Unchanged by Chunk 1c -- `ensure_remote_worker` itself is untouched by
this chunk (see _worker.py's module docstring); the pool object it
bootstraps (Chunk 1c) is exactly the kind of opaque return value this
mechanism was already designed to keep alive across a reattach.
"""
import pytest

from cubevis.bokeh.transport import CommMgr
from cubevis.remote._worker import ensure_remote_worker, _NAMESPACE_KEY


def test_first_call_builds_worker_and_stashes_marker():
    ns = {}
    calls = []
    mgrs_seen = []

    def build_worker(mgr):
        calls.append(1)
        mgrs_seen.append(mgr)
        return object()

    comm_mgr_id = ensure_remote_worker(build_worker, target_name="t1", namespace=ns)

    assert len(calls) == 1
    # build_worker must receive the freshly-constructed CommMgr, not None
    # or something else -- this is what lets it register its own comm
    # handlers as part of construction (see the docstring in _worker.py).
    assert len(mgrs_seen) == 1
    assert isinstance(mgrs_seen[0], CommMgr)
    assert mgrs_seen[0].comm_mgr_id == comm_mgr_id
    assert _NAMESPACE_KEY in ns
    assert ns[_NAMESPACE_KEY].comm_mgr_id == comm_mgr_id
    assert ns[_NAMESPACE_KEY].target_name == "t1"


def test_second_call_in_same_namespace_does_not_rebuild():
    """The core start-vs-reattach guarantee: repeated bootstrap calls
    against a namespace that already has a worker must not touch
    build_worker again, and must return the same comm_mgr_id."""
    ns = {}
    calls = []

    def build_worker(mgr):
        calls.append(1)
        return object()

    first_id = ensure_remote_worker(build_worker, target_name="t1", namespace=ns)
    second_id = ensure_remote_worker(build_worker, target_name="t1", namespace=ns)
    third_id = ensure_remote_worker(build_worker, target_name="t1", namespace=ns)

    assert len(calls) == 1, "build_worker must only run on the first (bootstrapping) call"
    assert first_id == second_id == third_id


def test_build_worker_failure_leaves_no_marker_behind():
    """If construction fails, a later call must retry rather than seeing
    a half-built worker and treating it as healthy."""
    ns = {}
    attempts = []

    def flaky_build_worker(mgr):
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("MS failed to open")
        return object()

    with pytest.raises(RuntimeError, match="MS failed to open"):
        ensure_remote_worker(flaky_build_worker, target_name="t1", namespace=ns)

    assert _NAMESPACE_KEY not in ns, "a failed build must not leave a marker behind"

    # A later, successful attempt should proceed normally.
    comm_mgr_id = ensure_remote_worker(flaky_build_worker, target_name="t1", namespace=ns)
    assert len(attempts) == 2
    assert _NAMESPACE_KEY in ns
    assert ns[_NAMESPACE_KEY].comm_mgr_id == comm_mgr_id


def test_different_namespaces_are_independent():
    """Sanity check that the marker is scoped to the namespace passed in,
    not some module-level global -- otherwise two kernels sharing this
    module's import (impossible in practice, but worth pinning down)
    could bleed state into each other."""
    ns_a, ns_b = {}, {}
    calls = []

    def build_worker(mgr):
        calls.append(1)
        return object()

    id_a = ensure_remote_worker(build_worker, target_name="t1", namespace=ns_a)
    id_b = ensure_remote_worker(build_worker, target_name="t1", namespace=ns_b)

    assert len(calls) == 2, "each fresh namespace should trigger its own build"
    assert id_a != id_b
