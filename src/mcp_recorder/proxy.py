"""HTTP reverse proxy that captures MCP interactions into cassette format.

Supports two upstream modes:
- **HTTP** (default): forwards requests via httpx to a remote MCP server.
- **Transport**: delegates to a :class:`~mcp_recorder.transport.Transport`
  instance (e.g. :class:`StdioTransport`), enabling subprocess-based servers.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, Any

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse
from starlette.routing import Route

from mcp_recorder._types import Cassette, CassetteInteraction, InteractionType

if TYPE_CHECKING:
    from mcp_recorder.transport import Transport

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


def create_proxy_app(
    *,
    cassette: Cassette,
    target_url: str | None = None,
    transport: Transport | None = None,
    verbose: bool = False,
) -> Starlette:
    """Create a Starlette proxy that captures MCP interactions into *cassette*.

    Exactly one of *target_url* or *transport* must be provided:

    - **target_url** (HTTP mode): forward via httpx to a remote MCP server.
    - **transport** (generic mode): delegate to any
      :class:`~mcp_recorder.transport.Transport` (e.g. stdio subprocess).
    """
    if target_url is None and transport is None:
        raise ValueError("Either target_url or transport must be provided")
    if target_url is not None and transport is not None:
        raise ValueError("target_url and transport are mutually exclusive")

    if transport is not None:
        return _create_transport_proxy(cassette=cassette, transport=transport, verbose=verbose)
    assert target_url is not None
    return _create_http_proxy(cassette=cassette, target_url=target_url, verbose=verbose)


# ---------------------------------------------------------------------------
# Transport-based proxy (stdio / generic)
# ---------------------------------------------------------------------------


def _create_transport_proxy(
    *,
    cassette: Cassette,
    transport: Transport,
    verbose: bool,
) -> Starlette:
    """Proxy that delegates to a :class:`Transport` for upstream communication."""
    interaction_counter = 0

    async def _proxy(request: Request) -> Response:
        nonlocal interaction_counter
        interaction_counter += 1
        idx = interaction_counter

        req_body_bytes = await request.body()
        req_body = _parse_json(req_body_bytes)

        if verbose:
            logger.info("[%d] -> %s %s", idx, request.method, request.url.path)
            if req_body:
                logger.info("[%d]    Body: %s", idx, json.dumps(req_body, indent=2))

        interaction_type = _classify_interaction(request.method, req_body)

        # Lifecycle (DELETE/GET) is HTTP-only — acknowledge without forwarding.
        if interaction_type == InteractionType.LIFECYCLE:
            logger.info(
                "[%d] %s %s (lifecycle, skipped for transport)",
                idx,
                request.method,
                request.url.path,
            )
            return Response(status_code=200, media_type="application/json")

        start = time.monotonic()

        if interaction_type == InteractionType.NOTIFICATION:
            try:
                await transport.send_notification(req_body)  # type: ignore[arg-type]
            except Exception as exc:
                logger.error("[%d] Transport error: %s", idx, exc)
                return Response(
                    content=json.dumps({"error": f"Transport error: {exc}"}),
                    status_code=502,
                    media_type="application/json",
                )
            latency_ms = int((time.monotonic() - start) * 1000)
            interaction = CassetteInteraction(
                type=InteractionType.NOTIFICATION,
                request=req_body if isinstance(req_body, dict) else None,
                response_is_sse=False,
                response_status=202,
                latency_ms=latency_ms,
            )
            cassette.add_interaction(interaction)
            logger.info("[%d] %s", idx, interaction.summary)
            return Response(status_code=202, media_type="application/json")

        # JSON-RPC request (has "id").
        try:
            resp_body = await transport.send_request(req_body)  # type: ignore[arg-type]
        except Exception as exc:
            logger.error("[%d] Transport error: %s", idx, exc)
            return Response(
                content=json.dumps({"error": f"Transport error: {exc}"}),
                status_code=502,
                media_type="application/json",
            )

        latency_ms = int((time.monotonic() - start) * 1000)
        interaction = CassetteInteraction(
            type=InteractionType.JSONRPC_REQUEST,
            request=req_body if isinstance(req_body, dict) else None,
            response=resp_body if isinstance(resp_body, dict) else None,
            response_is_sse=False,
            response_status=200,
            latency_ms=latency_ms,
        )
        cassette.add_interaction(interaction)
        logger.info("[%d] %s", idx, interaction.summary)

        if verbose and resp_body:
            logger.info("[%d] <- %s", idx, json.dumps(resp_body, indent=2)[:500])

        content = json.dumps(resp_body) if resp_body else ""
        return Response(content=content, status_code=200, media_type="application/json")

    async def startup() -> None:
        await transport.connect()

    async def shutdown() -> None:
        await transport.close()

    return Starlette(
        routes=[Route("/{path:path}", _proxy, methods=["GET", "POST", "PUT", "DELETE", "PATCH"])],
        on_startup=[startup],
        on_shutdown=[shutdown],
    )


# ---------------------------------------------------------------------------
# HTTP proxy (existing behaviour, unchanged)
# ---------------------------------------------------------------------------


def _create_http_proxy(
    *,
    cassette: Cassette,
    target_url: str,
    verbose: bool,
) -> Starlette:
    """Proxy that forwards via httpx to an HTTP MCP server."""
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

    return Starlette(
        routes=[Route("/{path:path}", _proxy, methods=["GET", "POST", "PUT", "DELETE", "PATCH"])],
        on_shutdown=[shutdown],
    )
