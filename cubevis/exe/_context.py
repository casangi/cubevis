import asyncio
import logging
import threading
from typing import Optional, Union, Any, Callable
from concurrent.futures import ThreadPoolExecutor, Future

from . import Mode, Task

logger = logging.getLogger(__name__)

class Context:
    """Unified interface for different execution contexts with lifecycle management."""

    def __init__(self, mode: Mode, executor: Optional[ThreadPoolExecutor] = None):
        logger.debug( f"\tContext::__init__({mode},{executor} ): {id(self)}" )
        self.mode = mode
        self.executor = executor or ThreadPoolExecutor(max_workers=4)
        self._running_tasks = []
        self._stop_events = {}

    def create_stop_condition( self, task_id: Optional[str] = None,
                               prefer_asyncio: bool = False ):
        """Create appropriate stop condition for the execution mode."""
        logger.debug( f"\tContext::create_stop_condition({task_id}): {id(self)}" )
        if task_id is None:
            task_id = f"task_{len(self._stop_events)}"

        if prefer_asyncio or self.mode in [ExecutionMode.ASYNC_RUN, ExecutionMode.ASYNC_TASK]:
            # For async contexts, use asyncio primitives
            event = asyncio.Event()
        else:
            # ====>> if self.mode in [Mode.SYNC, Mode.THREAD]: <<====
            # For threading contexts, always use threading primitives
            condition = threading.Condition()
            condition.stop_requested = False

            def stop_task():
                with condition:
                    condition.stop_requested = True
                    condition.notify_all()

            condition.stop_task = stop_task
            event = condition

        self._stop_events[task_id] = event
        return event, task_id

    def stop_task(self, task_id: str):
        """Signal a task to stop."""
        if task_id in self._stop_events:
            condition_or_event = self._stop_events[task_id]
            if isinstance(condition_or_event, threading.Condition):
                logger.debug( f"\tContext::stop_task({task_id}): {id(self)} - condition" )
                condition_or_event.stop_task()
            elif hasattr(condition_or_event, 'set'):  # Both threading.Event and asyncio.Event have set()
                logger.debug( f"\tContext::stop_task({task_id}): {id(self)} - event" )
                condition_or_event.set()
            else:
                # Fallback for other types
                logger.debug(f"Warning: Unknown stop condition type: {type(condition_or_event)}")

    def stop_all_tasks(self):
        """Signal all tasks to stop."""
        logger.debug( f"\tContext::stop_all_task( ): {id(self)}" )
        for condition_or_event in self._stop_events.values():
            if isinstance(condition_or_event, threading.Condition):
                condition_or_event.stop_task()
            elif hasattr(condition_or_event, 'set'):  # Both threading.Event and asyncio.Event have set()
                condition_or_event.set()
            else:
                logger.debug(f"Warning: Unknown stop condition type: {type(condition_or_event)}")

    def execute(self, task: Task, task_id: Optional[str] = None) -> Union[Any, Future, asyncio.Task]:
        """Execute Bokeh task according to the configured mode."""

        if self.mode == Mode.SYNC:
            logger.debug( f"\tContext::execute({task},{task_id} ): {id(self)} - sync" )
            return task.run_sync()

        elif self.mode == Mode.ASYNC_RUN:
            logger.debug( f"\tContext::execute({task},{task_id} ): {id(self)} - async" )
            if asyncio.iscoroutinefunction(task.server_func):
                return asyncio.run(task.run_async())
            else:
                async def async_wrapper():
                    return await task.run_async()
                return asyncio.run(async_wrapper())

        elif self.mode == Mode.ASYNC_TASK:
            logger.debug( f"\tContext::execute({task},{task_id} ): {id(self)} - task" )
            try:
                loop = asyncio.get_running_loop()
                async_task = loop.create_task(task.run_async())
                if task_id:
                    self._running_tasks.append((task_id, async_task))
                return async_task
            except RuntimeError:
                raise RuntimeError("ASYNC_TASK mode requires an active event loop")

        elif self.mode == Mode.THREAD:
            logger.debug( f"\tContext::execute({task},{task_id} ): {id(self)} - thread" )
            future = self.executor.submit(task.run_sync)
            if task_id:
                self._running_tasks.append((task_id, future))
            return future

    async def execute_async(self, task: Task, task_id: Optional[str] = None) -> Any:
        """Async version that always returns the result."""

        if self.mode == Mode.SYNC:
            logger.debug( f"\tContext::execute_async({task},{task_id} ): {id(self)} - sync" )
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(self.executor, task.run_sync)

        elif self.mode == Mode.ASYNC_RUN:
            logger.debug( f"\tContext::execute_async({task},{task_id} ): {id(self)} - async" )
            return await task.run_async()

        elif self.mode == Mode.ASYNC_TASK:
            logger.debug( f"\tContext::execute_async({task},{task_id} ): {id(self)} - task" )
            return await task.run_async()

        elif self.mode == Mode.THREAD:
            logger.debug( f"\tContext::execute_async({task},{task_id} ): {id(self)} - thread" )
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(self.executor, task.run_sync)
