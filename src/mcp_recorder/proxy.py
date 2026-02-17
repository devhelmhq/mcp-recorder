"""HTTP reverse proxy that captures MCP interactions into cassette format."""

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

from mcp_recorder._types import Cassette, CassetteInteraction, InteractionType

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
    for key, value in headers.items():
        lower = key.lower()
        if lower in HOP_BY_HOP:
            continue
        if lower == "host":
            out[key] = target_host
            continue
        out[key] = value
    return out


def _parse_json(raw: bytes) -> dict[str, Any] | list[Any] | None:
    """Try to parse bytes as JSON. Return None on failure."""
    if not raw:
        return None
    try:
        return json.loads(raw)  # type: ignore[no-any-return]
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


def _parse_sse_data(line: str) -> dict[str, Any] | None:
    """Extract and parse a JSON object from an SSE data: line."""
    stripped = line.strip()
    if not stripped.startswith("data:"):
        return None
    payload = stripped[len("data:") :].strip()
    if not payload:
        return None
    try:
        return json.loads(payload)  # type: ignore[no-any-return]
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


def _classify_interaction(method: str, body: dict[str, Any] | list[Any] | None) -> InteractionType:
    """Determine interaction type from HTTP method and JSON-RPC body."""
    if method in ("DELETE", "GET"):
        return InteractionType.LIFECYCLE
    if isinstance(body, dict) and "id" not in body:
        return InteractionType.NOTIFICATION
    return InteractionType.JSONRPC_REQUEST


def create_proxy_app(target_url: str, cassette: Cassette, verbose: bool = False) -> Starlette:
    """Create a Starlette app that proxies all requests to target_url.

    Every interaction is captured directly as a CassetteInteraction.
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
        req_body = _parse_json(req_body_bytes)
        req_headers = _forward_headers(dict(request.headers), target_host)

        if verbose:
            logger.info("[%d] -> %s %s", idx, request.method, request.url.path)
            logger.info("[%d]    Headers: %s", idx, dict(request.headers))
            if req_body:
                logger.info("[%d]    Body: %s", idx, json.dumps(req_body, indent=2))

        upstream_url = f"{target_url}{request.url.path}"
        if request.url.query:
            upstream_url += f"?{request.url.query}"

        interaction_type = _classify_interaction(request.method, req_body)

        # Lifecycle interactions (DELETE, GET) — no JSON-RPC body expected
        if interaction_type == InteractionType.LIFECYCLE:
            return await _handle_lifecycle(request, req_body_bytes, req_headers, upstream_url, idx)

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
            return await _handle_sse_response(
                request,
                req_body,
                req_body_bytes,
                req_headers,
                upstream_url,
                upstream_resp,
                idx,
                start,
                interaction_type,
            )

        # Plain JSON response (e.g., notifications returning 202)
        resp_body = _parse_json(upstream_resp.content)
        resp_headers = _forward_headers(upstream_resp.headers, target_host)

        interaction = CassetteInteraction(
            type=interaction_type,
            request=req_body if isinstance(req_body, dict) else None,
            response=resp_body if isinstance(resp_body, dict) else None,
            response_is_sse=False,
            response_status=upstream_resp.status_code,
            latency_ms=latency_ms,
        )
        cassette.add_interaction(interaction)
        logger.info("[%d] %s", idx, interaction.summary)

        if verbose:
            logger.info(
                "[%d] <- %d %s", idx, upstream_resp.status_code, dict(upstream_resp.headers)
            )

        return Response(
            content=upstream_resp.content,
            status_code=upstream_resp.status_code,
            headers=resp_headers,
        )

    async def _handle_sse_response(
        request: Request,
        req_body: dict[str, Any] | list[Any] | None,
        req_body_bytes: bytes,
        req_headers: dict[str, str],
        upstream_url: str,
        initial_resp: httpx.Response,
        idx: int,
        start_time: float,
        interaction_type: InteractionType,
    ) -> Response:
        """Stream SSE response to client while extracting JSON-RPC for the cassette."""
        sse_events: list[dict[str, Any]] = []

        async def stream_and_capture() -> AsyncGenerator[bytes, None]:
            try:
                async with client.stream(
                    method=request.method,
                    url=upstream_url,
                    headers=req_headers,
                    content=req_body_bytes,
                ) as stream:
                    async for line in stream.aiter_lines():
                        yield (line + "\n").encode("utf-8")
                        parsed = _parse_sse_data(line)
                        if parsed is not None:
                            sse_events.append(parsed)
            finally:
                total_latency = int((time.monotonic() - start_time) * 1000)

                # Take the first SSE event as the JSON-RPC response
                response_body = sse_events[0] if sse_events else None

                interaction = CassetteInteraction(
                    type=interaction_type,
                    request=req_body if isinstance(req_body, dict) else None,
                    response=response_body,
                    response_is_sse=True,
                    response_status=initial_resp.status_code,
                    latency_ms=total_latency,
                )
                cassette.add_interaction(interaction)
                logger.info("[%d] %s", idx, interaction.summary)

        resp_headers = _forward_headers(initial_resp.headers, target_host)
        await initial_resp.aclose()

        return StreamingResponse(
            stream_and_capture(),
            status_code=initial_resp.status_code,
            headers=resp_headers,
            media_type="text/event-stream",
        )

    async def _handle_lifecycle(
        request: Request,
        req_body_bytes: bytes,
        req_headers: dict[str, str],
        upstream_url: str,
        idx: int,
    ) -> Response:
        """Handle DELETE/GET lifecycle requests."""
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

        # GET /mcp can return an SSE keep-alive stream
        if is_sse:
            interaction = CassetteInteraction(
                type=InteractionType.LIFECYCLE,
                http_method=request.method,
                http_path=request.url.path,
                response_is_sse=True,
                response_status=upstream_resp.status_code,
                latency_ms=latency_ms,
            )
            cassette.add_interaction(interaction)
            logger.info("[%d] %s", idx, interaction.summary)

            resp_headers = _forward_headers(upstream_resp.headers, target_host)
            await upstream_resp.aclose()

            async def empty_sse() -> AsyncGenerator[bytes, None]:
                # Keep-alive: just yield nothing and let the client disconnect
                return
                yield  # noqa: RUF100 # make it an async generator

            return StreamingResponse(
                empty_sse(),
                status_code=upstream_resp.status_code,
                headers=resp_headers,
                media_type="text/event-stream",
            )

        # DELETE /mcp — plain response
        resp_headers = _forward_headers(upstream_resp.headers, target_host)

        interaction = CassetteInteraction(
            type=InteractionType.LIFECYCLE,
            http_method=request.method,
            http_path=request.url.path,
            response_is_sse=False,
            response_status=upstream_resp.status_code,
            latency_ms=latency_ms,
        )
        cassette.add_interaction(interaction)
        logger.info("[%d] %s", idx, interaction.summary)

        return Response(
            content=upstream_resp.content,
            status_code=upstream_resp.status_code,
            headers=resp_headers,
        )

    async def shutdown() -> None:
        await client.aclose()

    app = Starlette(
        routes=[Route("/{path:path}", _proxy, methods=["GET", "POST", "PUT", "DELETE", "PATCH"])],
        on_shutdown=[shutdown],
    )

    return app
