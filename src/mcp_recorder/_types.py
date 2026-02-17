"""Data models for raw interaction capture."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field


class RawInteraction(BaseModel):
    """A single captured HTTP exchange between MCP client and server."""

    request_method: str
    request_path: str
    request_headers: dict[str, str]
    request_body: dict[str, Any] | list[Any] | None = None

    response_status: int
    response_headers: dict[str, str]
    response_body: dict[str, Any] | list[Any] | None = None

    response_is_sse: bool = False
    response_sse_events: list[dict[str, Any]] = Field(default_factory=list)

    timestamp: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    latency_ms: int = 0

    @property
    def jsonrpc_method(self) -> str | None:
        """Extract the JSON-RPC method name from the request body, if present."""
        if isinstance(self.request_body, dict):
            return self.request_body.get("method")
        return None

    @property
    def summary(self) -> str:
        """One-line summary for console logging."""
        method = self.jsonrpc_method or self.request_method
        sse_tag = " SSE" if self.response_is_sse else ""
        sse_detail = ""
        if self.response_is_sse and self.response_sse_events:
            sse_detail = f" ({len(self.response_sse_events)} events)"
        return (
            f"{self.request_method} {self.request_path} -> "
            f"{self.response_status}{sse_tag}{sse_detail} "
            f"({self.latency_ms}ms) {method}"
        )


class RawRecording(BaseModel):
    """Complete dump of all interactions from a recording session."""

    captured_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    target: str
    interaction_count: int = 0
    interactions: list[RawInteraction] = Field(default_factory=list)
