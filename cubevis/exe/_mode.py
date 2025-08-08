from enum import Enum

class Mode(Enum):
    SYNC = "sync"
    ASYNC_RUN = "async_run"
    ASYNC_TASK = "async_task"
    THREAD = "thread"
