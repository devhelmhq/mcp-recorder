"""Cassette data models for MCP interaction recording."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, model_validator

CASSETTE_FORMAT_VERSION = "1.0"


class InteractionType(StrEnum):
    """Classification of an MCP interaction."""

    JSONRPC_REQUEST = "jsonrpc_request"
    NOTIFICATION = "notification"
    LIFECYCLE = "lifecycle"


class CassetteInteraction(BaseModel):
    """A single MCP interaction (request + response pair).

    For jsonrpc_request: request and response are JSON-RPC message dicts.
    For notification: request is the notification body, response is None.
    For lifecycle: http_method/http_path describe the HTTP operation.
    """

    type: InteractionType
    request: dict[str, Any] | None = None
    response: dict[str, Any] | None = None
    response_is_sse: bool = False
    response_status: int = 200
    latency_ms: int = 0

    # Only used for lifecycle interactions (DELETE, GET)
    http_method: str | None = None
    http_path: str | None = None

    @property
    def jsonrpc_method(self) -> str | None:
        """Extract the JSON-RPC method from the request."""
        if isinstance(self.request, dict):
            return self.request.get("method")
        return None

    @property
    def tool_name(self) -> str | None:
        """Extract the tool name for tools/call interactions."""
        if self.jsonrpc_method == "tools/call" and isinstance(self.request, dict):
            params = self.request.get("params", {})
            if isinstance(params, dict):
                return params.get("name")
        return None

    @property
    def summary(self) -> str:
        """One-line summary for console logging."""
        if self.type == InteractionType.LIFECYCLE:
            return (
                f"{self.http_method} {self.http_path} ->"
                f"{self.response_status} ({self.latency_ms}ms)"
            )

        method = self.jsonrpc_method or "unknown"
        tool = f" [{self.tool_name}]" if self.tool_name else ""
        sse = " SSE" if self.response_is_sse else ""
        return f"{method}{tool} -> {self.response_status}{sse} ({self.latency_ms}ms)"


class CassetteMetadata(BaseModel):
    """Metadata about the recording session."""

    recorded_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    server_url: str = ""
    protocol_version: str | None = None
    server_info: dict[str, Any] | None = None


class Cassette(BaseModel):
    """A complete recorded MCP session."""

    version: str = CASSETTE_FORMAT_VERSION
    metadata: CassetteMetadata = Field(default_factory=CassetteMetadata)
    interactions: list[CassetteInteraction] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_format_version(self) -> Cassette:
        expected_major = CASSETTE_FORMAT_VERSION.split(".")[0]
        actual_major = self.version.split(".")[0]
        if actual_major != expected_major:
            raise ValueError(
                f"Incompatible cassette format version '{self.version}' "
                f"(expected {expected_major}.x). "
                f"Re-record the cassette with the current version of mcp-recorder."
            )
        return self

    def add_interaction(self, interaction: CassetteInteraction) -> None:
        """Append an interaction and extract metadata if this is the initialize response."""
        self.interactions.append(interaction)
        if interaction.jsonrpc_method == "initialize" and interaction.response is not None:
            result = interaction.response.get("result", {})
            self.metadata.protocol_version = result.get("protocolVersion")
            self.metadata.server_info = result.get("serverInfo")
