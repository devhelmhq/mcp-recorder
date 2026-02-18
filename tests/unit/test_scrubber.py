"""Unit tests for the explicit secret redaction scrubber."""

from __future__ import annotations

import logging
import os
from typing import Any
from unittest import mock

from mcp_recorder._types import Cassette, CassetteInteraction, CassetteMetadata, InteractionType
from mcp_recorder.scrubber import scrub_cassette


def _make_cassette(
    server_url: str = "http://example.com/secret-key/mcp",
    request_body: dict[str, Any] | None = None,
    response_body: dict[str, Any] | None = None,
) -> Cassette:
    interaction = CassetteInteraction(
        type=InteractionType.JSONRPC_REQUEST,
        request=request_body
        or {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "echo", "arguments": {"message": "hello"}},
        },
        response=response_body
        or {"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": "hello"}]}},
        response_status=200,
    )
    return Cassette(
        metadata=CassetteMetadata(server_url=server_url),
        interactions=[interaction],
    )


class TestNoFlags:
    def test_identity_when_no_flags(self) -> None:
        original = _make_cassette()
        result = scrub_cassette(original)

        assert result.metadata.server_url == original.metadata.server_url
        assert result.interactions[0].response == original.interactions[0].response


class TestRedactServerUrl:
    def test_strips_path(self) -> None:
        cassette = _make_cassette(server_url="https://mcp.firecrawl.dev/fc-abc123/mcp")
        result = scrub_cassette(cassette, redact_server_url=True)

        assert result.metadata.server_url == "https://mcp.firecrawl.dev/[REDACTED]"

    def test_bare_host_unchanged(self) -> None:
        cassette = _make_cassette(server_url="http://localhost:8000")
        result = scrub_cassette(cassette, redact_server_url=True)

        # No path to strip (just empty path)
        assert "localhost:8000" in result.metadata.server_url

    def test_root_path_unchanged(self) -> None:
        cassette = _make_cassette(server_url="http://localhost:8000/")
        result = scrub_cassette(cassette, redact_server_url=True)

        assert result.metadata.server_url == "http://localhost:8000/"


class TestRedactEnv:
    def test_replaces_value_in_responses(self) -> None:
        cassette = _make_cassette(
            response_body={"jsonrpc": "2.0", "id": 1, "result": {"token": "super-secret-value"}},
        )
        with mock.patch.dict(os.environ, {"MY_SECRET": "super-secret-value"}):
            result = scrub_cassette(cassette, redact_env=["MY_SECRET"])

        resp = result.interactions[0].response
        assert resp is not None
        assert resp["result"]["token"] == "[REDACTED]"

    def test_does_not_touch_request_bodies(self) -> None:
        cassette = _make_cassette(
            request_body={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "query", "arguments": {"key": "super-secret-value"}},
            },
            response_body={"jsonrpc": "2.0", "id": 1, "result": {"ok": True}},
        )
        with mock.patch.dict(os.environ, {"MY_SECRET": "super-secret-value"}):
            result = scrub_cassette(cassette, redact_env=["MY_SECRET"])

        req = result.interactions[0].request
        assert req is not None
        assert req["params"]["arguments"]["key"] == "super-secret-value"

    def test_missing_env_var_warns(self, caplog: Any) -> None:
        cassette = _make_cassette()
        with caplog.at_level(logging.WARNING):
            result = scrub_cassette(cassette, redact_env=["NONEXISTENT_VAR_12345"])

        assert "not found in environment" in caplog.text
        assert result.interactions[0].response == cassette.interactions[0].response

    def test_request_body_hit_warns(self, caplog: Any) -> None:
        cassette = _make_cassette(
            request_body={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"key": "the-secret"},
            },
        )
        with mock.patch.dict(os.environ, {"SEC": "the-secret"}), caplog.at_level(logging.WARNING):
            scrub_cassette(cassette, redact_env=["SEC"])

        assert "request body" in caplog.text.lower()


class TestRedactPatterns:
    def test_regex_replaces_in_responses(self) -> None:
        cassette = _make_cassette(
            response_body={
                "jsonrpc": "2.0",
                "id": 1,
                "result": {"city": "London", "note": "Weather in London"},
            },
        )
        result = scrub_cassette(cassette, redact_patterns=["London"])

        resp = result.interactions[0].response
        assert resp is not None
        assert resp["result"]["city"] == "[REDACTED]"
        assert "London" not in resp["result"]["note"]

    def test_invalid_regex_warns(self, caplog: Any) -> None:
        cassette = _make_cassette()
        with caplog.at_level(logging.WARNING):
            result = scrub_cassette(cassette, redact_patterns=["[invalid"])

        assert "invalid regex" in caplog.text
        assert result.interactions[0].response == cassette.interactions[0].response


class TestStructuralKeysPreserved:
    def test_method_jsonrpc_id_never_redacted(self) -> None:
        cassette = _make_cassette(
            request_body={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "echo"},
            },
            response_body={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "result": {"text": "tools/call"},
            },
        )
        # Pattern that matches "tools/call" and "2.0"
        result = scrub_cassette(cassette, redact_patterns=["tools/call", "2\\.0"])

        resp = result.interactions[0].response
        assert resp is not None
        assert resp["jsonrpc"] == "2.0"
        assert resp["id"] == 1
        assert resp["method"] == "tools/call"
        # But the result text SHOULD be redacted
        assert resp["result"]["text"] == "[REDACTED]"
