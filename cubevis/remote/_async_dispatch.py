"""
Chunk 1b, Task 4. Unchanged by Chunk 1c -- _supervisor.py now owns one
JobRegistry per execution context (instead of one, singular) but the
registry class itself needed no changes to support that.

Two things live here, deliberately together:

1. A **background-dispatch primitive**: schedules a `request()` to the
   worker as a background `asyncio.Task` instead of awaiting it inline,
   and returns a `job_id` immediately. This exists because of a real
   constraint traced through `_comm_mgr.py`: every `TransportBase.run()`
   loop awaits its message callback inline before reading the next
   incoming message (`_handle_request` awaits the handler's result
   before sending a reply and returning control to the loop). A P_local-
   facing handler that does `return await request(worker_comm, ...)`
   directly for a long command therefore stalls that CommMgr's entire
   receive loop for the duration -- P_local's status-check would sit
   unread until the long command finally finishes, which is exactly
   backwards from Task 4's "the remote kernel should remain lively"
   goal. Splitting comm categories doesn't help either: there's still
   only one underlying transport/socket per CommMgr feeding one receive
   loop, regardless of which `comm_id` messages target.

   Fast commands (metadata, `probe_*`, anything that returns promptly)
   should keep using the plain direct-await proxy -- unaffected here.

2. The **status vocabulary** itself: died/working/stuck/completed,
   combining IPC status pushes from the worker when it can send them,
   and `proc.returncode is None` as the supervisor-side fallback when it
   can't. "died" and "completed" are provable end-to-end against a real
   subprocess (see the tests). "stuck" is documented as a heuristic
   (time-since-last-update while the process is still alive) rather than
   proven -- it genuinely isn't provable without cooperation from
   whatever's running inside the worker, and pretending otherwise would
   be dishonest about what this layer can actually guarantee.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, Optional

from ._bridge import request

logger = logging.getLogger(__name__)


class JobStatus(str, Enum):
    WORKING = "working"      # dispatched, no result yet, worker process still alive
    COMPLETED = "completed"  # worker replied; `result` holds its return value
    DIED = "died"            # worker process exited (or its comm closed) before replying
    STUCK = "stuck"          # heuristic only -- see module docstring


@dataclass
class JobRecord:
    job_id: str
    message_id: str
    status: JobStatus = JobStatus.WORKING
    result: Any = None
    error: Optional[str] = None
    started_at: float = field(default_factory=time.monotonic)
    updated_at: float = field(default_factory=time.monotonic)
    task: Optional[asyncio.Task] = None


class JobRegistry:
    """
    In-memory job table, owned by the supervisor's per-worker dispatch
    layer. All lookups are synchronous dict access -- deliberately, so
    status polls are always fast enough to never themselves become a
    liveness problem.
    """

    def __init__(self, stuck_after: Optional[float] = 30.0,
                 is_worker_alive: Optional[Callable[[], bool]] = None):
        """
        ``stuck_after``: seconds of no update, while the worker process
        is still alive, before `status()` reports `STUCK` instead of
        `WORKING`. `None` disables the heuristic entirely (always report
        `WORKING` until it actually resolves).

        ``is_worker_alive``: callable returning whether the worker
        process is still alive (normally `transport.is_connected`, or
        `lambda: transport.returncode is None`). If not given, "died"
        can still be reported when the dispatched request itself fails
        with a connection error, just not proactively between polls.
        """
        self._jobs: Dict[str, JobRecord] = {}
        self._stuck_after = stuck_after
        self._is_worker_alive = is_worker_alive

    def dispatch(self, comm, message_id: str, payload: Dict[str, Any],
                 job_id: str) -> JobRecord:
        """Schedule `request(comm, message_id, payload)` as a background
        task and return immediately with a `JobRecord` in `WORKING`
        status. Never awaits the worker -- this is what keeps the
        caller's own receive loop free."""
        record = JobRecord(job_id=job_id, message_id=message_id)
        self._jobs[job_id] = record

        async def _run():
            try:
                result = await request(comm, message_id, payload)
            except Exception as e:
                logger.warning(f"JobRegistry: job {job_id} ({message_id}) failed: {e}")
                record.status = JobStatus.DIED
                record.error = str(e)
            else:
                record.status = JobStatus.COMPLETED
                record.result = result
            finally:
                record.updated_at = time.monotonic()

        record.task = asyncio.ensure_future(_run())
        return record

    def status(self, job_id: str) -> Dict[str, Any]:
        """Fast, synchronous status lookup -- safe to call as often as
        P_local likes without ever blocking on the worker."""
        record = self._jobs.get(job_id)
        if record is None:
            return {"job_id": job_id, "status": "unknown"}

        status = record.status
        if status == JobStatus.WORKING:
            worker_alive = True if self._is_worker_alive is None else self._is_worker_alive()
            if not worker_alive:
                status = JobStatus.DIED
                record.status = JobStatus.DIED
                record.error = record.error or "worker process is no longer alive"
            elif (self._stuck_after is not None
                  and time.monotonic() - record.updated_at > self._stuck_after):
                status = JobStatus.STUCK

        payload = {
            "job_id": job_id,
            "status": status.value,
            "elapsed": time.monotonic() - record.started_at,
        }
        if status == JobStatus.COMPLETED:
            payload["result"] = record.result
        if status == JobStatus.DIED and record.error is not None:
            payload["error"] = record.error
        return payload
