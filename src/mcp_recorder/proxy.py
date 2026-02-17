"""HTTP reverse proxy that captures MCP interactions."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import AsyncGenerator
from typing import Any

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse
from starlette.routing import Route

from mcp_recorder._types import RawInteraction, RawRecording

logger = logging.getLogger("mcp_recorder.proxy")

# Headers that should not be forwarded between client and upstream.
HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
    }
)


def _forward_headers(headers: httpx.Headers | dict[str, str], target_host: str) -> dict[str, str]:
    """Filter hop-by-hop headers and rewrite Host."""
    out: dict[str, str] = {}
    raw = headers.items()
    for key, value in raw:
        lower = key.lower()
        if lower in HOP_BY_HOP:
            continue
        if lower == "host":
            out[key] = target_host
            continue
        out[key] = value
    return out


def _parse_json_body(raw: bytes) -> dict[str, Any] | list[Any] | None:
    """Try to parse bytes as JSON. Return None on failure."""
    if not raw:
        return None
    try:
        return json.loads(raw)  # type: ignore[no-any-return]
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


def _parse_sse_line(line: str) -> dict[str, Any] | None:
    """Parse a single SSE data line into a JSON object if possible."""
    stripped = line.strip()
    if stripped.startswith("data:"):
        payload = stripped[len("data:") :].strip()
        if not payload:
            return None
        try:
            return json.loads(payload)  # type: ignore[no-any-return]
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {"_raw": payload}
    return None


def create_proxy_app(target_url: str, recording: RawRecording) -> Starlette:
    """Create a Starlette app that proxies all requests to target_url.

    Every interaction is appended to ``recording.interactions`` in-memory.
    """
    target_url = target_url.rstrip("/")
    target_host = httpx.URL(target_url).host

    client = httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=30.0))

    interaction_counter = 0

    async def _proxy(request: Request) -> Response:
        nonlocal interaction_counter
        interaction_counter += 1
        idx = interaction_counter

        req_body_bytes = await request.body()
        req_body_parsed = _parse_json_body(req_body_bytes)
        req_headers = _forward_headers(dict(request.headers), target_host)

        upstream_url = f"{target_url}{request.url.path}"
        if request.url.query:
            upstream_url += f"?{request.url.query}"

        start = time.monotonic()

        try:
            upstream_resp = await client.request(
                method=request.method,
                url=upstream_url,
                headers=req_headers,
                content=req_body_bytes,
            )
        except httpx.HTTPError as exc:
            logger.error("[%d] Upstream error: %s", idx, exc)
            return Response(
                content=json.dumps({"error": f"Upstream error: {exc}"}),
                status_code=502,
                media_type="application/json",
            )

        latency_ms = int((time.monotonic() - start) * 1000)

        content_type = upstream_resp.headers.get("content-type", "")
        is_sse = "text/event-stream" in content_type

        if is_sse:
            return await _handle_sse(
                request,
                req_body_bytes,
                req_body_parsed,
                req_headers,
                upstream_url,
                upstream_resp,
                idx,
                start,
            )

        resp_body_parsed = _parse_json_body(upstream_resp.content)
        resp_headers = _forward_headers(upstream_resp.headers, target_host)

        interaction = RawInteraction(
            request_method=request.method,
            request_path=request.url.path,
            request_headers=dict(request.headers),
            request_body=req_body_parsed,
            response_status=upstream_resp.status_code,
            response_headers=dict(upstream_resp.headers),
            response_body=resp_body_parsed,
            latency_ms=latency_ms,
        )
        recording.interactions.append(interaction)
        recording.interaction_count = len(recording.interactions)

        logger.info("[%d] %s", idx, interaction.summary)

        return Response(
            content=upstream_resp.content,
            status_code=upstream_resp.status_code,
            headers=resp_headers,
        )

    async def _handle_sse(
        request: Request,
        req_body_bytes: bytes,
        req_body_parsed: dict[str, Any] | list[Any] | None,
        req_headers: dict[str, str],
        upstream_url: str,
        initial_resp: httpx.Response,
        idx: int,
        start_time: float,
    ) -> Response:
        """Handle an SSE response: stream to client while buffering events."""
        sse_events: list[dict[str, Any]] = []

        async def stream_and_capture() -> AsyncGenerator[bytes, None]:
            try:
                async with client.stream(
                    method=request.method,
                    url=upstream_url,
                    headers=req_headers,
                    content=req_body_bytes,
                ) as stream:
                    async for line_bytes in stream.aiter_lines():
                        yield (line_bytes + "\n").encode("utf-8")
                        parsed = _parse_sse_line(line_bytes)
                        if parsed is not None:
                            sse_events.append(parsed)
            finally:
                total_latency = int((time.monotonic() - start_time) * 1000)
                interaction = RawInteraction(
                    request_method=request.method,
                    request_path=request.url.path,
                    request_headers=dict(request.headers),
                    request_body=req_body_parsed,
                    response_status=initial_resp.status_code,
                    response_headers=dict(initial_resp.headers),
                    response_body=None,
                    response_is_sse=True,
                    response_sse_events=sse_events,
                    latency_ms=total_latency,
                )
                recording.interactions.append(interaction)
                recording.interaction_count = len(recording.interactions)
                logger.info("[%d] %s", idx, interaction.summary)

        resp_headers = _forward_headers(initial_resp.headers, target_host)
        # Close the initial non-streaming response since we re-issue as streaming
        await initial_resp.aclose()

        return StreamingResponse(
            stream_and_capture(),
            status_code=initial_resp.status_code,
            headers=resp_headers,
            media_type="text/event-stream",
        )

    async def shutdown() -> None:
        await client.aclose()

    app = Starlette(
        routes=[Route("/{path:path}", _proxy, methods=["GET", "POST", "PUT", "DELETE", "PATCH"])],
        on_shutdown=[shutdown],
    )

    return app
