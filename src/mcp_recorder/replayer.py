"""Replay server that serves recorded MCP interactions from a cassette."""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import AsyncGenerator
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse
from starlette.routing import Route

from mcp_recorder._types import Cassette
from mcp_recorder.matcher import Matcher

logger = logging.getLogger("mcp_recorder.replayer")


def _parse_json(raw: bytes) -> dict[str, Any] | None:
    """Try to parse bytes as JSON."""
    if not raw:
        return None
    try:
        return json.loads(raw)  # type: ignore[no-any-return]
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


def _rewrite_id(response: dict[str, Any], request_id: Any) -> dict[str, Any]:
    """Return a copy of the response with the id field set to match the request."""
    out = dict(response)
    if "id" in out:
        out["id"] = request_id
    return out


def _jsonrpc_error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    """Build a JSON-RPC error response."""
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def _make_sse_body(data: dict[str, Any]) -> bytes:
    """Format a JSON-RPC message as an SSE event."""
    payload = json.dumps(data, ensure_ascii=False)
    return f"event: message\ndata: {payload}\n\n".encode()


def _notification_response(session_id: str) -> Response:
    """Return 202 Accepted for notifications."""
    return Response(
        content=b"",
        status_code=202,
        headers={
            "content-type": "application/json",
            "mcp-session-id": session_id,
        },
    )


def _mcp_headers(session_id: str) -> dict[str, str]:
    """Standard MCP response headers."""
    return {
        "mcp-session-id": session_id,
        "cache-control": "no-cache, no-transform",
    }


def create_replay_app(cassette: Cassette, matcher: Matcher) -> Starlette:
    """Create a Starlette app that serves recorded responses from a cassette."""
    session_id = uuid.uuid4().hex
    interaction_counter = 0

    async def _handle_post(request: Request) -> Response:
        nonlocal interaction_counter
        interaction_counter += 1
        idx = interaction_counter

        body_bytes = await request.body()
        body = _parse_json(body_bytes)

        if not isinstance(body, dict):
            error = _jsonrpc_error(None, -32700, "Parse error: invalid JSON")
            return Response(
                content=json.dumps(error),
                status_code=200,
                media_type="application/json",
                headers=_mcp_headers(session_id),
            )

        method = body.get("method", "")
        request_id = body.get("id")
        has_id = "id" in body

        # Notifications (no id) — return 202
        if not has_id:
            logger.info("[%d] %s -> 202 (notification)", idx, method)
            return _notification_response(session_id)

        # JSON-RPC request — match against cassette
        matched = matcher.match(body)

        if matched is None:
            msg = f"No matching interaction for {method}"
            params = body.get("params")
            if isinstance(params, dict) and "name" in params:
                msg += f" [{params['name']}]"
            logger.warning("[%d] %s -> NO MATCH", idx, method)
            error = _jsonrpc_error(request_id, -32600, msg)
            return Response(
                content=json.dumps(error),
                status_code=200,
                media_type="application/json",
                headers=_mcp_headers(session_id),
            )

        # Rewrite the response id to match the incoming request
        response_body = matched.response
        if response_body is not None:
            response_body = _rewrite_id(response_body, request_id)

        tool = matched.tool_name
        tool_tag = f" [{tool}]" if tool else ""
        sse_tag = " SSE" if matched.response_is_sse else ""
        logger.info(
            "[%d] %s%s -> %d%s (replayed)", idx, method, tool_tag, matched.response_status, sse_tag
        )

        # Return as SSE if the original response was SSE
        if matched.response_is_sse and response_body is not None:
            sse_bytes = _make_sse_body(response_body)

            async def sse_stream() -> AsyncGenerator[bytes, None]:
                yield sse_bytes

            return StreamingResponse(
                sse_stream(),
                status_code=matched.response_status,
                media_type="text/event-stream",
                headers=_mcp_headers(session_id),
            )

        # Plain JSON response
        content = json.dumps(response_body).encode() if response_body else b""
        return Response(
            content=content,
            status_code=matched.response_status,
            media_type="application/json",
            headers=_mcp_headers(session_id),
        )

    async def _handle_delete(request: Request) -> Response:
        """Session teardown."""
        logger.info("[*] DELETE /mcp -> 200 (session teardown)")
        return Response(
            content=b"",
            status_code=200,
            headers={
                "content-type": "application/json",
                "mcp-session-id": session_id,
            },
        )

    async def _handle_get(request: Request) -> Response:
        """Server-push SSE listener — return an empty keep-alive stream."""
        logger.info("[*] GET /mcp -> SSE keep-alive")

        async def keepalive() -> AsyncGenerator[bytes, None]:
            # Yield nothing — the client holds the connection open
            return
            yield  # noqa: RUF100 # make it an async generator

        return StreamingResponse(
            keepalive(),
            status_code=200,
            media_type="text/event-stream",
            headers=_mcp_headers(session_id),
        )

    app = Starlette(
        routes=[
            Route("/{path:path}", _handle_post, methods=["POST"]),
            Route("/{path:path}", _handle_delete, methods=["DELETE"]),
            Route("/{path:path}", _handle_get, methods=["GET"]),
        ],
    )

    return app
