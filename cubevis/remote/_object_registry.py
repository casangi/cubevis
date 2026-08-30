"""
Chunk 1c, Task 4 -- object registry and handle table.

Lives inside a single worker process (one instance per execution
context, since each execution context is its own OS process -- see
_supervisor.py and the design doc §2f). Generic: no application classes
are baked in here. A name->class registry, populated entirely via the
worker's own configuration hook (Task 3's "configure" wire message,
handled in worker_main.py), plus a handle->instance table populated by
`create_object`.

Deliberately a plain, dependency-free class (no cubevis imports) so it
can be unit-tested in isolation, without a real subprocess/transport --
see test_object_registry_unit.py.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional
from uuid import uuid4

__all__ = ["ObjectRegistry", "UnknownClassError", "UnknownHandleError"]


class UnknownClassError(KeyError):
    """Raised by `create_object` when `class_name` was never registered
    in this execution context (see `register_class`)."""


class UnknownHandleError(KeyError):
    """Raised by `call_method`/`get_object`/`dispose_object` when
    `handle` doesn't name a currently-live object (never created, or
    already disposed)."""


class ObjectRegistry:
    """
    Two tables:

    - `_classes`: name -> class, populated once at worker configuration
      time by whichever application-specific registration function Task
      3's "configure" handler loaded (e.g. `registry.register_class(
      "ReductionContext", SomeReductionContext)`). Never touched by
      `create_object`/`call_method` themselves.
    - `_objects`: handle -> live instance, populated by `create_object`
      and consumed by `call_method`/`dispose_object`. Handles are
      generated (UUIDs), not caller-supplied, matching the same
      generated-id convention used at every other layer of this
      framework (`comm_mgr_id`, `execution_context_id`) -- see the
      design doc §2f/§2e.
    """

    def __init__(self) -> None:
        self._classes: Dict[str, type] = {}
        self._objects: Dict[str, Any] = {}

    # -- configuration-time: populating the class registry -------------
    def register_class(self, name: str, cls: type) -> None:
        self._classes[name] = cls

    def registered_class_names(self) -> List[str]:
        return sorted(self._classes)

    # -- create_object / call_method / dispose_object -------------------
    def create_object(self, class_name: str, args: Optional[List[Any]] = None,
                       kwargs: Optional[Dict[str, Any]] = None) -> str:
        try:
            cls = self._classes[class_name]
        except KeyError:
            raise UnknownClassError(
                f"no class registered under {class_name!r} in this execution "
                f"context (registered here: {self.registered_class_names()})"
            ) from None
        instance = cls(*(args or []), **(kwargs or {}))
        handle = str(uuid4())
        self._objects[handle] = instance
        return handle

    def get_object(self, handle: str) -> Any:
        try:
            return self._objects[handle]
        except KeyError:
            raise UnknownHandleError(
                f"no live object for handle {handle!r} in this execution context "
                f"(never created here, or already disposed)"
            ) from None

    def call_method(self, handle: str, method: str, args: Optional[List[Any]] = None,
                     kwargs: Optional[Dict[str, Any]] = None) -> Any:
        instance = self.get_object(handle)
        fn = getattr(instance, method)
        return fn(*(args or []), **(kwargs or {}))

    def dispose_object(self, handle: str) -> bool:
        """Returns True if `handle` was live and has now been removed
        (freeing the instance for garbage collection); False if it was
        already gone (disposing twice is not an error)."""
        return self._objects.pop(handle, None) is not None

    def __len__(self) -> int:
        return len(self._objects)
