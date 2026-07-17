"""Concurrent JSONL stdio bridge for Oculai tools.

Each request is dispatched in its own asyncio task. Responses are serialized
through one writer lock so stdout remains valid one-object-per-line JSONL even
when tools complete out of order. The reserved ``$cancel`` method cancels an
in-flight request by id; it is handled by the transport and is never exposed to
the LLM tool registry.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
import traceback
from collections.abc import Awaitable, Callable
from typing import Any

# Ensure the package root is on sys.path for imports
_package_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _package_dir not in sys.path:
    sys.path.insert(0, _package_dir)

from oculai_mcp.tools.errors import OculaiError

_shutdown_requested = False
_DEFAULT_DRAIN_TIMEOUT_SECONDS = 30.0

ResponseWriter = Callable[[dict[str, Any]], Awaitable[None]]
ToolLookup = Callable[[str], Callable[[dict[str, Any]], Any] | None]
LineReader = Callable[[], Awaitable[bytes]]


def _handle_signal(signum: int, _frame: Any) -> None:
    """Signal-safe shutdown request; the event loop performs cleanup."""
    del signum
    global _shutdown_requested
    _shutdown_requested = True


class JsonlRequestServer:
    """Owns in-flight request tasks for one stdin/stdout connection."""

    def __init__(
        self,
        get_tool: ToolLookup,
        write_response: ResponseWriter,
        *,
        drain_timeout: float = _DEFAULT_DRAIN_TIMEOUT_SECONDS,
    ) -> None:
        self._get_tool = get_tool
        self._write_response = write_response
        self._drain_timeout = drain_timeout
        self._tasks: dict[str, asyncio.Task[None]] = {}

    @property
    def in_flight(self) -> int:
        return len(self._tasks)

    async def accept(self, request: Any) -> None:
        """Validate and schedule one decoded request without blocking the reader."""
        if not isinstance(request, dict):
            await self._write_response({
                "id": None,
                "ok": False,
                "error": {"code": "INVALID_REQUEST", "message": "Request must be a JSON object"},
            })
            return

        req_id_value = request.get("id")
        req_id = str(req_id_value) if req_id_value is not None else ""
        method = request.get("method")
        params = request.get("params", {})

        if not req_id or not isinstance(method, str) or not isinstance(params, dict):
            await self._write_response({
                "id": req_id_value,
                "ok": False,
                "error": {
                    "code": "INVALID_REQUEST",
                    "message": "Request requires a non-empty id, string method, and object params",
                },
            })
            return

        if method == "$cancel":
            target_id = str(params.get("request_id", ""))
            target = self._tasks.get(target_id)
            cancelled = bool(target and not target.done())
            if cancelled and target:
                target.cancel()
            await self._write_response({
                "id": req_id,
                "ok": True,
                "result": {"request_id": target_id, "cancelled": cancelled},
            })
            return

        if req_id in self._tasks:
            await self._write_response({
                "id": req_id,
                "ok": False,
                "error": {"code": "DUPLICATE_ID", "message": f"Request id '{req_id}' is active"},
            })
            return

        task = asyncio.create_task(self._dispatch(req_id, method, params))
        self._tasks[req_id] = task

        def remove_completed(completed: asyncio.Task[None]) -> None:
            self._remove_task(req_id, completed)

        task.add_done_callback(remove_completed)

    def _remove_task(self, req_id: str, task: asyncio.Task[None]) -> None:
        if self._tasks.get(req_id) is task:
            self._tasks.pop(req_id, None)

    async def _dispatch(self, req_id: str, method: str, params: dict[str, Any]) -> None:
        handler = self._get_tool(method)
        if handler is None:
            await self._write_response({
                "id": req_id,
                "ok": False,
                "error": {"code": "UNKNOWN_TOOL", "message": f"Tool '{method}' not found"},
            })
            return

        try:
            result = handler(params)
            if asyncio.iscoroutine(result) or isinstance(result, Awaitable):
                result = await result
            await self._write_response({"id": req_id, "ok": True, "result": result})
        except asyncio.CancelledError:
            # A cancelled tool gets its own terminal response so the host can
            # settle the original pending promise deterministically.
            await asyncio.shield(self._write_response({
                "id": req_id,
                "ok": False,
                "error": {"code": "CANCELLED", "message": "Tool request was cancelled"},
            }))
            raise
        except OculaiError as exc:
            error_payload: dict[str, Any] = {"code": exc.code, "message": exc.message}
            if exc.details is not None:
                error_payload["details"] = exc.details
            await self._write_response({"id": req_id, "ok": False, "error": error_payload})
        except Exception as exc:
            await self._write_response({
                "id": req_id,
                "ok": False,
                "error": {
                    "code": "TOOL_ERROR",
                    "message": str(exc),
                    "traceback": traceback.format_exc(),
                },
            })

    async def drain(self) -> None:
        """Wait for active tools, then cancel any that exceed the drain bound."""
        active = list(self._tasks.values())
        if not active:
            return
        _done, pending = await asyncio.wait(active, timeout=self._drain_timeout)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    async def cancel_all(self) -> None:
        active = list(self._tasks.values())
        for task in active:
            task.cancel()
        if active:
            await asyncio.gather(*active, return_exceptions=True)


async def _read_request_lines(readline: LineReader, server: JsonlRequestServer) -> None:
    while not _shutdown_requested:
        line = await readline()
        if not line:
            break
        line_str = line.decode("utf-8").strip()
        if not line_str:
            continue
        try:
            request = json.loads(line_str)
        except json.JSONDecodeError as exc:
            await server._write_response({  # transport parse failure, no request id available
                "id": None,
                "ok": False,
                "error": {"code": "PARSE_ERROR", "message": str(exc)},
            })
            continue
        await server.accept(request)


async def _read_requests(reader: asyncio.StreamReader, server: JsonlRequestServer) -> None:
    await _read_request_lines(reader.readline, server)


async def main() -> None:
    """Run the concurrent JSONL server on stdin/stdout."""
    # JSONL is a byte protocol between runtimes, not a console UI.  Frozen
    # Windows executables otherwise inherit the active ANSI code page and can
    # emit non-UTF-8 bytes for Chinese source metadata.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="strict")

    from oculai_mcp.tool_registry import TOOL_REGISTRY, get_tool

    _emit_system({"type": "ready", "tools": len(TOOL_REGISTRY), "pid": os.getpid()})

    writer_lock = asyncio.Lock()

    async def write_response(message: dict[str, Any]) -> None:
        async with writer_lock:
            _emit_response(message)

    drain_timeout = float(os.getenv("OCULAI_SIDECAR_DRAIN_TIMEOUT", _DEFAULT_DRAIN_TIMEOUT_SECONDS))
    server = JsonlRequestServer(get_tool, write_response, drain_timeout=drain_timeout)

    try:
        if os.name == "nt":
            # ProactorEventLoop.connect_read_pipe() is unreliable for the
            # inherited stdin handle of a PyInstaller one-file child on
            # Windows.  A blocking readline isolated in asyncio's worker pool
            # preserves concurrent request execution without registering that
            # handle with IOCP.
            async def threaded_readline() -> bytes:
                return await asyncio.to_thread(sys.stdin.buffer.readline)

            await _read_request_lines(threaded_readline, server)
        else:
            loop = asyncio.get_running_loop()
            reader = asyncio.StreamReader()
            protocol = asyncio.StreamReaderProtocol(reader)
            await loop.connect_read_pipe(lambda: protocol, sys.stdin)
            await _read_requests(reader, server)
        if _shutdown_requested:
            _emit_system({"type": "shutdown", "reason": "signal received"})
            await server.cancel_all()
        else:
            _emit_system({"type": "shutdown", "reason": "stdin closed"})
            await server.drain()
    except asyncio.CancelledError:
        _emit_system({"type": "shutdown", "reason": "cancelled"})
        await server.cancel_all()
        raise


def _emit_system(msg: dict[str, Any]) -> None:
    sys.stderr.write(json.dumps(msg, ensure_ascii=False) + "\n")
    sys.stderr.flush()


def _emit_response(msg: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(msg, ensure_ascii=False, default=str) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    asyncio.run(main())
