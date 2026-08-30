"""
Chunk 1c, Task 4 definition-of-done: a fast unit test against
`_object_registry.py` directly, no subprocess/transport involved --
`ObjectRegistry` is a plain, dependency-free class specifically so this
is possible (see its module docstring). The real end-to-end path
(through a worker subprocess, over the wire, with a numpy-array-typed
argument) is covered separately in test_object_registry_e2e.py.
"""
import pytest

from cubevis.remote._object_registry import (
    ObjectRegistry,
    UnknownClassError,
    UnknownHandleError,
)


class Counter:
    def __init__(self, start=0):
        self.value = start

    def increment(self, by=1):
        self.value += by
        return self.value


def test_create_call_dispose_round_trip():
    registry = ObjectRegistry()
    registry.register_class("Counter", Counter)
    assert registry.registered_class_names() == ["Counter"]

    handle = registry.create_object("Counter", args=[10])
    assert isinstance(handle, str) and handle
    assert len(registry) == 1

    assert registry.call_method(handle, "increment", args=[5]) == 15
    assert registry.call_method(handle, "increment", kwargs={"by": 1}) == 16

    assert registry.dispose_object(handle) is True
    assert len(registry) == 0
    # Disposing an already-gone handle is a no-op, not an error.
    assert registry.dispose_object(handle) is False


def test_create_object_unknown_class_raises():
    registry = ObjectRegistry()
    with pytest.raises(UnknownClassError):
        registry.create_object("NoSuchClass")


def test_call_method_unknown_handle_raises():
    registry = ObjectRegistry()
    with pytest.raises(UnknownHandleError):
        registry.call_method("nonexistent-handle", "whatever")


def test_two_instances_of_same_class_are_independent():
    registry = ObjectRegistry()
    registry.register_class("Counter", Counter)
    h1 = registry.create_object("Counter")
    h2 = registry.create_object("Counter")
    assert h1 != h2

    registry.call_method(h1, "increment", args=[100])
    assert registry.call_method(h2, "increment", args=[1]) == 1, (
        "each create_object call must produce an independent instance, "
        "not share state through the class registry"
    )
