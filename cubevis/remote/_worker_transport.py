"""
Chunk 1b, Task 1 + Task 3.

`WorkerProcessTransport` (supervisor side) / `WorkerCommTransport`
(worker side) -- a `TransportBase` pair one level deeper than Chunk 1's
`KernelClientTransport`/`KernelCommTransport`, over a real OS subprocess
instead of the Jupyter Comm protocol. Length-prefixed framing (4-byte
big-endian length + JSON payload) via `cubevis.utils.serialize`/
`deserialize`, matching the wire format used elsewhere for consistency.

Spawned via `asyncio.create_subprocess_exec(sys.executable, "-m",
"cubevis.remote.worker_main", ...)` -- deliberately NOT
`multiprocessing`'s default `fork()`: fork-without-exec in a
multi-threaded parent is a documented hazard, and an ipykernel process
is multi-threaded (its own event loop plus whatever thread pools
`SyncBridge` instances have spun up).

Unchanged by Chunk 1c -- the pool (see _supervisor.py) spawns one of
these per execution context instead of exactly one per kernel, but the
transport itself needed no changes to support that.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import struct
import sys
from typing import Any, Callable, Dict, List, Optional

from cubevis.bokeh.transport import TransportBase
from cubevis.utils import serialize, deserialize

logger = logging.getLogger(__name__)

_HEADER = struct.Struct(">I")  # 4-byte big-endian length prefix


def _dbg(msg: str) -> None:
    # Temporary diagnostic (2026-09-05) -- see _wire_types.py's own
    # _encode_dataarray for why this writes straight to a file rather
    # than through logging. PID-prefixed since this file is shared by
    # both worker and supervisor processes. Remove once root-caused.
    with open("/tmp/cubevis_frame_debug.log", "a") as f:
        f.write(f"[pid={os.getpid()}] {msg}\n")
        f.flush()


async def _write_frame(writer: asyncio.StreamWriter, message: Dict[str, Any]) -> None:
    _dbg(f"_write_frame: START, message_id={message.get('message_id')!r}")
    payload = serialize(message).encode("utf-8")
    digest = hashlib.md5(payload).hexdigest()
    _dbg(f"_write_frame: serialize() done, {len(payload)} bytes, md5={digest}")
    writer.write(_HEADER.pack(len(payload)))
    writer.write(payload)
    _dbg("_write_frame: write() calls issued, awaiting drain()")
    await writer.drain()
    _dbg(f"_write_frame: drain() complete -- {len(payload)} bytes (md5={digest}) "
          f"are with the OS/pipe now")


async def _read_frame(reader: asyncio.StreamReader) -> Optional[Dict[str, Any]]:
    """Returns None on clean EOF (peer closed its write side)."""
    _dbg("_read_frame: waiting for 4-byte header ...")
    try:
        header = await reader.readexactly(4)
    except asyncio.IncompleteReadError:
        _dbg("_read_frame: EOF while reading header")
        return None
    (n,) = _HEADER.unpack(header)
    _dbg(f"_read_frame: header says {n} bytes; reading payload in chunks "
          f"(temporary: replacing readexactly with a progress-visible loop) ...")

    # Temporary diagnostic: readexactly() gives no visibility into partial
    # progress -- it either returns everything or never returns at all,
    # which is exactly the blind spot in this investigation right now.
    # This manual loop reports how many bytes actually arrive and when,
    # even if it ultimately stalls the same way readexactly() did.
    chunks = []
    remaining = n
    while remaining > 0:
        chunk = await reader.read(min(4096, remaining))
        if not chunk:
            _dbg(f"_read_frame: EOF mid-payload -- got {n - remaining} of {n} "
                  f"bytes before the peer closed its write side")
            return None
        chunks.append(chunk)
        remaining -= len(chunk)
        _dbg(f"_read_frame: chunk of {len(chunk)} bytes arrived, "
              f"{n - remaining}/{n} total so far")
    payload = b"".join(chunks)
    digest = hashlib.md5(payload).hexdigest()
    _dbg(f"_read_frame: payload read OK ({n} bytes, md5={digest}); deserializing ...")
    result = deserialize(payload.decode("utf-8"))
    _dbg("_read_frame: deserialize() done")
    return result


# ============================================================================
# Supervisor side
# ============================================================================
class WorkerProcessTransport(TransportBase):
    """
    Spawns and owns the worker subprocess. `send_message()`/incoming
    frames go over the subprocess's stdin/stdout; stderr is captured and
    relayed to this module's logger line-by-line rather than discarded
    -- Chunk 1's own hard-learned lesson about a plain script's logging
    defaults swallowing diagnostics and turning a real startup failure
    into an opaque error.
    """

    def __init__(self, comm_mgr_id: str,
                 worker_module: str = "cubevis.remote.worker_main",
                 extra_args: Optional[List[str]] = None,
                 env: Optional[Dict[str, str]] = None,
                 abort: Optional[Callable] = None):
        super().__init__(comm_mgr_id, abort=abort)
        self._worker_module = worker_module
        self._extra_args = extra_args or []
        self._env = env
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._callback: Optional[Callable[[Dict[str, Any]], Any]] = None
        self._connected = False
        self._stderr_task: Optional[asyncio.Task] = None
        self._last_stderr_lines: List[str] = []  # small ring buffer for diagnosis

    @property
    def pid(self) -> Optional[int]:
        return self._proc.pid if self._proc is not None else None

    @property
    def returncode(self) -> Optional[int]:
        return self._proc.returncode if self._proc is not None else None

    async def connect(self) -> None:
        self._proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", self._worker_module,
            "--comm-mgr-id", self._comm_mgr_id,
            *self._extra_args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._env,
        )
        self._connected = True
        self._stderr_task = asyncio.ensure_future(self._relay_stderr())
        logger.debug(f"WorkerProcessTransport: spawned pid={self._proc.pid} "
                      f"module={self._worker_module!r}")

    async def _relay_stderr(self) -> None:
        assert self._proc is not None and self._proc.stderr is not None
        try:
            while True:
                line = await self._proc.stderr.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip("\n")
                self._last_stderr_lines.append(text)
                if len(self._last_stderr_lines) > 200:
                    self._last_stderr_lines.pop(0)
                logger.warning(f"[worker pid={self.pid}] {text}")
        except Exception:
            logger.exception("WorkerProcessTransport: error relaying worker stderr")

    async def send_message(self, message: Dict[str, Any]) -> None:
        if self._proc is None or self._proc.stdin is None or not self._connected:
            return
        try:
            await _write_frame(self._proc.stdin, message)
        except (BrokenPipeError, ConnectionResetError):
            logger.warning(f"WorkerProcessTransport: worker pid={self.pid} pipe closed "
                            f"while sending; recent stderr: {self._last_stderr_lines[-10:]}")
            self._connected = False

    def set_message_callback(self, callback: Callable[[Dict[str, Any]], Any]) -> None:
        self._callback = callback

    async def run(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        while self._connected:
            message = await _read_frame(self._proc.stdout)
            if message is None:
                # Worker's stdout closed -- process died or is exiting.
                self._connected = False
                break
            if self._callback is not None:
                await self._callback(message)

    async def close(self, timeout: float = 10.0) -> None:
        self._connected = False
        if self._proc is None:
            return

        if self._proc.stdin is not None:
            try:
                self._proc.stdin.close()
            except Exception:
                pass

        try:
            await asyncio.wait_for(self._proc.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning(f"WorkerProcessTransport: pid={self.pid} did not exit within "
                            f"{timeout}s after stdin close; terminating")
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                logger.warning(f"WorkerProcessTransport: pid={self.pid} ignored SIGTERM; killing")
                self._proc.kill()
                await self._proc.wait()

        if self._stderr_task is not None:
            try:
                await asyncio.wait_for(self._stderr_task, timeout=2.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._stderr_task.cancel()

    def is_connected(self) -> bool:
        return self._connected


# ============================================================================
# Worker side (runs inside the subprocess, via worker_main)
# ============================================================================
class WorkerCommTransport(TransportBase):
    """Worker-side leg. Reads/writes its own `sys.stdin.buffer`/
    `sys.stdout.buffer` directly -- the worker process has no other job."""

    def __init__(self, comm_mgr_id: str, abort: Optional[Callable] = None):
        super().__init__(comm_mgr_id, abort=abort)
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._callback: Optional[Callable[[Dict[str, Any]], Any]] = None
        self._connected = False

    async def connect(self) -> None:
        loop = asyncio.get_running_loop()

        reader = asyncio.StreamReader()
        reader_protocol = asyncio.StreamReaderProtocol(reader)
        await loop.connect_read_pipe(lambda: reader_protocol, sys.stdin.buffer)

        writer_transport, writer_protocol = await loop.connect_write_pipe(
            asyncio.streams.FlowControlMixin, sys.stdout.buffer
        )
        writer = asyncio.StreamWriter(writer_transport, writer_protocol, reader, loop)

        self._reader = reader
        self._writer = writer
        self._connected = True

    async def send_message(self, message: Dict[str, Any]) -> None:
        if self._writer is None or not self._connected:
            return
        try:
            await _write_frame(self._writer, message)
        except (BrokenPipeError, ConnectionResetError):
            self._connected = False

    def set_message_callback(self, callback: Callable[[Dict[str, Any]], Any]) -> None:
        self._callback = callback

    async def run(self) -> None:
        assert self._reader is not None
        while self._connected:
            message = await _read_frame(self._reader)
            if message is None:
                self._connected = False
                break
            if self._callback is not None:
                await self._callback(message)

    async def close(self) -> None:
        self._connected = False
        if self._writer is not None:
            try:
                self._writer.close()
            except Exception:
                pass

    def is_connected(self) -> bool:
        return self._connected
