"""
Chunk 1c test scaffolding -- NOT part of the framework's public API.

`worker_main.py`'s `configure` handler resolves `register_function` as a
dotted import path (see `_load_dotted`), which means any test that wants
to exercise real worker configuration needs something importable *from
inside the freshly spawned worker subprocess*. That subprocess only ever
sees `cubevis` on `sys.path` (via the supervisor kernel's own
PYTHONPATH) -- it does not see pytest's own `tests/` collection
directory, which isn't a real package (no `__init__.py`, by pytest
convention). So these fixtures live here, inside the installable
package, purely so `"cubevis.remote._test_registrations:register_basic"`
resolves correctly wherever `cubevis` itself is importable.

Two independent registration functions, deliberately registering
non-overlapping class names, so a test can configure two execution
contexts differently and confirm each only has access to what *it* was
configured with (Chunk 1c, Task 3's worker-configuration/isolation
requirement).
"""
from __future__ import annotations


class Counter:
    """A tiny stateful object -- proves create_object/call_method reach
    a real instance with real, mutating state across multiple calls,
    not just a stateless function dispatch."""

    def __init__(self, start: int = 0):
        self.value = start

    def increment(self, by: int = 1) -> int:
        self.value += by
        return self.value

    def get(self) -> int:
        return self.value


class NumpyEcho:
    """Round-trips a non-trivial (numpy-array-typed) argument through a
    real method call and back, so the object-registry path is proven
    against real `cubevis.utils.serialize`/`deserialize`, not just JSON
    -native scalars."""

    def double(self, arr):
        return arr * 2

    def shape_of(self, arr):
        return list(arr.shape)


def register_basic(comm, registry, **kwargs) -> None:
    """The 'main' fixture registration: Counter + NumpyEcho."""
    registry.register_class("Counter", Counter)
    registry.register_class("NumpyEcho", NumpyEcho)


class OnlyInAlt:
    """Deliberately registered ONLY by `register_alt`, under a name
    `register_basic` never uses -- the isolation test's proof that a
    context configured with `register_basic` cannot reach this class,
    and vice versa."""

    def ping(self) -> str:
        return "alt"


def register_alt(comm, registry, **kwargs) -> None:
    """An alternate fixture registration, disjoint from `register_basic`,
    for the worker-configuration-isolation test."""
    registry.register_class("OnlyInAlt", OnlyInAlt)
