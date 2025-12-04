import logging
import asyncio
import threading
from typing import Callable, Optional, Union, Any

logger = logging.getLogger(__name__)

class Task:
    """A long-running Bokeh backend task that runs until completion or stop signal.
    """

    def __init__(self,
                 server_func: Callable,
                 *args,
                 stop_condition: Optional[Union[threading.Event, asyncio.Event, threading.Condition, Callable[[], bool]]] = None,
                 **kwargs):
        """
        Args:
            server_func: Function that starts the Bokeh server (sync or async)
            stop_condition: Event or callable that signals when to stop
                - threading.Event: for thread-based execution
                - asyncio.Event: for async execution
                - threading.Condition: for complex condition-based stopping (most flexible)
                - Callable[[], bool]: function returning True when should stop
                - None: runs until server_func returns naturally
        """
        logger.debug( f"\tTask::__init__({server_func},{args},{stop_condition},{kwargs}): {id(self)}" )
        self.server_func = server_func
        self.args = args
        self.kwargs = kwargs
        self.stop_condition = stop_condition
        self._result = None
        self._exception = None

    def _convert_asyncio_event_to_threading(self, asyncio_event):
        """Convert asyncio.Event to threading.Event for sync contexts."""
        threading_event = threading.Event()
        logger.debug( f"\tTask::_convert_asyncio_event_to_threading({asyncio_event}): {id(self)}" )
        
        # Create a bridge between asyncio.Event and threading.Event
        # We'll use a background thread with its own event loop to monitor the asyncio event
        def bridge_events():
            # Create a new event loop for this thread
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                
                async def wait_and_set():
                    await asyncio_event.wait()
                    threading_event.set()
                
                loop.run_until_complete(wait_and_set())
            except Exception as e:
                # If bridging fails, fall back to a simple timeout approach
                print(f"Warning: Event bridging failed: {e}")
                time.sleep(0.1)  # Small delay to prevent tight loop
                threading_event.set()  # Set it anyway to unblock
            finally:
                try:
                    loop.close()
                except:
                    pass
        
        # Start the bridge in a daemon thread
        bridge_thread = threading.Thread(target=bridge_events, daemon=True)
        bridge_thread.start()
        
        return threading_event

    def _run_coroutine_sync(self, coro):
        """
        Helper to run a coroutine synchronously, handling both CLI and Jupyter contexts.
        """
        try:
            # Check if there's already a running event loop (Jupyter/IPython)
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No running loop - safe to use asyncio.run() (CLI context)
            return asyncio.run(coro)
        else:
            # Running loop exists (Jupyter context)
            try:
                import nest_asyncio
                nest_asyncio.apply()
                return asyncio.run(coro)
            except ImportError:
                # Fallback: run in a new thread with its own event loop
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    return pool.submit(asyncio.run, coro).result()

    def run_sync(self) -> Any:
        """Run synchronously until completion or stop signal."""
        if self.stop_condition is None:
            # Handle async functions in sync context
            if asyncio.iscoroutinefunction(self.server_func):
                logger.debug(f"\tTask::run_sync( ): {id(self)} - asyncio no stop condition {self.server_func}")
                return self._run_coroutine_sync(self.server_func(*self.args, **self.kwargs))
            else:
                logger.debug(f"\tTask::run_sync( ): {id(self)} - no stop condition {self.server_func}")
                return self.server_func(*self.args, **self.kwargs)

        # Handle asyncio.Event in sync context by converting to threading.Event
        stop_condition = self.stop_condition
        if isinstance(self.stop_condition, asyncio.Event):
            logger.debug(f"\tTask::run_sync( ): {id(self)} - asyncio bridge {self.server_func}")
            # Convert asyncio.Event to threading.Event for sync execution
            stop_condition = self._convert_asyncio_event_to_threading(self.stop_condition)

        if callable(stop_condition):
            # Poll-based stopping
            logger.debug(f"\tTask::run_sync( ): {id(self)} - polling {self.server_func}")
            while not stop_condition():
                try:
                    if asyncio.iscoroutinefunction(self.server_func):
                        result = self._run_coroutine_sync(self.server_func(*self.args, **self.kwargs))
                    else:
                        result = self.server_func(*self.args, **self.kwargs)
                    if result is not None:  # Server function completed
                        return result
                except Exception as e:
                    self._exception = e
                    raise

        elif isinstance(stop_condition, threading.Event):
            # Event-based stopping for threads
            logger.debug(f"\tTask::run_sync( ): {id(self)} - threading event {self.server_func}")
            def run_with_check():
                while not stop_condition.is_set():
                    try:
                        if asyncio.iscoroutinefunction(self.server_func):
                            result = self._run_coroutine_sync(self.server_func(*self.args, **self.kwargs))
                        else:
                            result = self.server_func(*self.args, **self.kwargs)
                        if result is not None:
                            return result
                    except Exception as e:
                        self._exception = e
                        raise
            return run_with_check()

        elif isinstance(stop_condition, threading.Condition):
            logger.debug(f"\tTask::run_sync( ): {id(self)} - threading condition {self.server_func}")
            # Condition-based stopping - most flexible and efficient
            def run_with_condition():
                with stop_condition:
                    while True:
                        try:
                            # Pass the condition to the server function if it accepts it
                            import inspect
                            sig = inspect.signature(self.server_func)
                            if 'stop_condition' in sig.parameters:
                                if asyncio.iscoroutinefunction(self.server_func):
                                    result = self._run_coroutine_sync(
                                        self.server_func(*self.args, stop_condition=stop_condition, **self.kwargs)
                                    )
                                else:
                                    result = self.server_func(*self.args, stop_condition=stop_condition, **self.kwargs)
                            else:
                                if asyncio.iscoroutinefunction(self.server_func):
                                    result = self._run_coroutine_sync(self.server_func(*self.args, **self.kwargs))
                                else:
                                    result = self.server_func(*self.args, **self.kwargs)

                            if result is not None:
                                return result

                            # If function returns None, wait for condition change
                            stop_condition.wait()

                        except Exception as e:
                            self._exception = e
                            raise

            return run_with_condition()

        else:
            raise ValueError(f"Invalid stop_condition ({type(stop_condition)}) for sync execution")

    async def run_async(self) -> Any:
        """Run asynchronously until completion or stop signal."""
        if asyncio.iscoroutinefunction(self.server_func):
            if self.stop_condition is None:
                logger.debug( f"\tTask::run_async( ): {id(self)} - no stop condition {self.server_func}" )
                return await self.server_func(*self.args, **self.kwargs)

            if callable(self.stop_condition):
                # Poll-based stopping
                logger.debug( f"\tTask::run_async( ): {id(self)} - polling {self.server_func}" )
                while not self.stop_condition():
                    try:
                        # Run server function as task so we can check stop condition
                        task = asyncio.create_task(self.server_func(*self.args, **self.kwargs))

                        # Check stop condition periodically
                        while not task.done() and not self.stop_condition():
                            await asyncio.sleep(0.1)

                        if self.stop_condition():
                            task.cancel()
                            try:
                                await task
                            except asyncio.CancelledError:
                                pass
                            return None

                        return await task
                    except Exception as e:
                        self._exception = e
                        raise

            elif isinstance(self.stop_condition, asyncio.Event):
                logger.debug( f"\tTask::run_async( ): {id(self)} - asyncio event {self.server_func}" )
                # Event-based stopping for async
                server_task = asyncio.create_task(self.server_func(*self.args, **self.kwargs))
                stop_task = asyncio.create_task(self.stop_condition.wait())

                done, pending = await asyncio.wait(
                    [server_task, stop_task],
                    return_when=asyncio.FIRST_COMPLETED
                )

                # Cancel remaining tasks
                for task in pending:
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass

                if server_task in done:
                    return server_task.result()
                else:
                    return None  # Stopped by event

            elif isinstance(self.stop_condition, threading.Condition):
                logger.debug( f"\tTask::run_async( ): {id(self)} - threading condition {self.server_func}" )
                # For async context with threading.Condition, we need to run in executor
                # since threading.Condition is synchronous
                loop = asyncio.get_event_loop()
                return await loop.run_in_executor(None, self.run_sync)

            else:
                raise ValueError("Invalid stop_condition for async execution")

        else:
            logger.debug( f"\tTask::run_async( ): {id(self)} - until completion" )
            # Sync function in async context - run in thread pool
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(None, self.run_sync)
