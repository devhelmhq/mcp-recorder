"""Transport abstraction for MCP server communication.

Defines a common interface for sending JSON-RPC messages to MCP servers,
with implementations for HTTP (Streamable HTTP / SSE) and stdio (subprocess).
"""

from __future__ import annotations

import abc
import asyncio
import contextlib
import json
import logging
import os
import signal
import sys
from typing import Any

import httpx

from mcp_recorder._utils import parse_sse_response

logger = logging.getLogger("mcp_recorder.transport")

_SUBPROCESS_SHUTDOWN_TIMEOUT = 5.0
_REQUEST_TIMEOUT = 120.0


class Transport(abc.ABC):
    """Abstract base for MCP transports."""

    @abc.abstractmethod
    async def connect(self) -> None:
        """Establish connection (spawn subprocess, open HTTP client, etc.)."""

    @abc.abstractmethod
    async def send_request(self, request: dict[str, Any]) -> dict[str, Any] | None:
        """Send a JSON-RPC request and return the parsed response."""

    @abc.abstractmethod
    async def send_notification(self, notification: dict[str, Any]) -> None:
        """Send a JSON-RPC notification (no response expected)."""

    @abc.abstractmethod
    async def close(self) -> None:
        """Release resources (kill subprocess, close HTTP client, etc.)."""

    async def __aenter__(self) -> Transport:
        await self.connect()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()


# ---------------------------------------------------------------------------
# HTTP transport
# ---------------------------------------------------------------------------


class HttpTransport(Transport):
    """HTTP/SSE transport wrapping httpx.AsyncClient.

    Handles mcp-session-id management and SSE response parsing, matching
    the existing behaviour in verifier.py and mcp_client.py.
    """

    def __init__(self, url: str, *, timeout: float = _REQUEST_TIMEOUT) -> None:
        self._base_url = url.rstrip("/")
        self._mcp_url = (
            f"{self._base_url}/mcp" if not self._base_url.endswith("/mcp") else self._base_url
        )
        self._timeout = timeout
        self._client: httpx.AsyncClient | None = None
        self._session_id: str | None = None

    async def connect(self) -> None:
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(self._timeout, connect=30.0),
        )

    def _headers(self) -> dict[str, str]:
        h: dict[str, str] = {
            "content-type": "application/json",
            "accept": "application/json, text/event-stream",
        }
        if self._session_id:
            h["mcp-session-id"] = self._session_id
        return h

    def _update_session(self, resp: httpx.Response) -> None:
        sid = resp.headers.get("mcp-session-id")
        if sid:
            self._session_id = sid

    async def send_request(self, request: dict[str, Any]) -> dict[str, Any] | None:
        assert self._client is not None, "Transport not connected"
        body = json.dumps(request).encode()
        resp = await self._client.post(self._mcp_url, content=body, headers=self._headers())
        self._update_session(resp)

        content_type = resp.headers.get("content-type", "")
        if "text/event-stream" in content_type:
            return parse_sse_response(resp.text)
        try:
            return resp.json()  # type: ignore[no-any-return]
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None

    async def send_notification(self, notification: dict[str, Any]) -> None:
        assert self._client is not None, "Transport not connected"
        body = json.dumps(notification).encode()
        resp = await self._client.post(self._mcp_url, content=body, headers=self._headers())
        self._update_session(resp)

    async def send_lifecycle(self, method: str = "DELETE", path: str = "/mcp") -> int:
        """Send an HTTP lifecycle request (DELETE/GET). Returns the status code."""
        assert self._client is not None, "Transport not connected"
        url = f"{self._base_url}{path}"
        resp = await self._client.request(method=method, url=url, headers=self._headers())
        self._update_session(resp)
        return resp.status_code

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None


# ---------------------------------------------------------------------------
# Stdio transport
# ---------------------------------------------------------------------------


class StdioTransport(Transport):
    """Stdio transport: JSON-RPC over stdin/stdout of a subprocess.

    Spawns the MCP server as a child process, writes newline-delimited JSON
    to its stdin, and reads newline-delimited JSON responses from stdout.
    Responses are routed to callers by matching on JSON-RPC ``id``.
    """

    def __init__(
        self,
        command: str,
        args: list[str] | None = None,
        *,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
    ) -> None:
        self._command = command
        self._args = args or []
        self._extra_env = env or {}
        self._cwd = cwd

        self._process: asyncio.subprocess.Process | None = None
        self._pending: dict[int | str, asyncio.Future[dict[str, Any]]] = {}
        self._write_lock = asyncio.Lock()
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._closed = False

    # -- lifecycle -----------------------------------------------------------

    async def connect(self) -> None:
        merged_env = {**os.environ, **self._extra_env}
        self._process = await asyncio.create_subprocess_exec(
            self._command,
            *self._args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=merged_env,
            cwd=self._cwd,
        )
        logger.info(
            "Spawned stdio server: %s %s (pid=%d)",
            self._command,
            " ".join(self._args),
            self._process.pid,
        )
        self._reader_task = asyncio.create_task(self._read_stdout())
        self._stderr_task = asyncio.create_task(self._read_stderr())

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True

        # Cancel pending futures so callers don't hang.
        for fut in self._pending.values():
            if not fut.done():
                fut.cancel()
        self._pending.clear()

        if self._process is None:
            return

        # 1. Close stdin to signal EOF.
        if self._process.stdin and not self._process.stdin.is_closing():
            self._process.stdin.close()

        # 2. Wait for graceful exit.
        try:
            await asyncio.wait_for(
                self._process.wait(),
                timeout=_SUBPROCESS_SHUTDOWN_TIMEOUT,
            )
        except TimeoutError:
            logger.warning(
                "Stdio server did not exit, sending SIGTERM (pid=%d)",
                self._process.pid,
            )
            try:
                self._process.send_signal(signal.SIGTERM)
                await asyncio.wait_for(
                    self._process.wait(),
                    timeout=_SUBPROCESS_SHUTDOWN_TIMEOUT,
                )
            except (TimeoutError, ProcessLookupError):
                logger.warning(
                    "Stdio server still alive, sending SIGKILL (pid=%d)",
                    self._process.pid,
                )
                try:
                    self._process.kill()
                    await self._process.wait()
                except ProcessLookupError:
                    pass

        for task in (self._reader_task, self._stderr_task):
            if task and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

        logger.info("Stdio server stopped (pid=%d)", self._process.pid)

    # -- messaging -----------------------------------------------------------

    async def send_request(self, request: dict[str, Any]) -> dict[str, Any] | None:
        self._assert_alive()
        msg_id = request.get("id")
        if msg_id is None:
            raise ValueError("send_request requires a JSON-RPC message with an 'id' field")

        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[msg_id] = future

        await self._write(request)

        try:
            return await asyncio.wait_for(future, timeout=_REQUEST_TIMEOUT)
        except TimeoutError:
            self._pending.pop(msg_id, None)
            raise TimeoutError(
                f"Stdio server did not respond to request id={msg_id} within {_REQUEST_TIMEOUT}s"
            ) from None
        except asyncio.CancelledError:
            self._pending.pop(msg_id, None)
            raise ConnectionError("Stdio server process exited unexpectedly") from None

    async def send_notification(self, notification: dict[str, Any]) -> None:
        self._assert_alive()
        await self._write(notification)

    # -- internals -----------------------------------------------------------

    async def _write(self, message: dict[str, Any]) -> None:
        assert self._process is not None and self._process.stdin is not None
        line = json.dumps(message, separators=(",", ":")) + "\n"
        async with self._write_lock:
            self._process.stdin.write(line.encode("utf-8"))
            await self._process.stdin.drain()

    async def _read_stdout(self) -> None:
        """Background task: read stdout lines, route responses by id."""
        assert self._process is not None and self._process.stdout is not None
        try:
            while True:
                raw = await self._process.stdout.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    logger.debug("Non-JSON line from stdout: %s", line[:200])
                    continue

                msg_id = msg.get("id")
                if msg_id is not None and msg_id in self._pending:
                    self._pending.pop(msg_id).set_result(msg)
                elif msg_id is not None:
                    logger.debug("Unexpected response id=%s (no pending request)", msg_id)
                else:
                    # Server-initiated notification — log and discard.
                    method = msg.get("method", "unknown")
                    logger.debug("Server notification: %s", method)
        except asyncio.CancelledError:
            return
        finally:
            # Process exited or stdout closed — fail any pending futures.
            for fut in self._pending.values():
                if not fut.done():
                    fut.cancel()

    async def _read_stderr(self) -> None:
        """Background task: forward subprocess stderr to our stderr."""
        assert self._process is not None and self._process.stderr is not None
        try:
            while True:
                raw = await self._process.stderr.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").rstrip()
                # Forward to stderr so the user can see server logs during recording.
                print(line, file=sys.stderr)
        except asyncio.CancelledError:
            return

    def _assert_alive(self) -> None:
        if self._closed:
            raise ConnectionError("Transport is closed")
        if self._process is None:
            raise ConnectionError("Transport not connected — call connect() first")
        if self._process.returncode is not None:
            raise ConnectionError(f"Stdio server exited with code {self._process.returncode}")
