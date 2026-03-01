"""Verify engine: replay cassette requests against a live server and compare responses."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

from mcp_recorder._types import Cassette, CassetteInteraction, InteractionType
from mcp_recorder._utils import parse_sse_response

logger = logging.getLogger("mcp_recorder.verifier")

# Fields stripped before comparison because they change every session.
_VOLATILE_KEYS = frozenset({"id", "_meta"})


@dataclass
class InteractionResult:
    """Outcome of verifying a single interaction."""

    index: int
    method: str
    passed: bool
    expected: dict[str, Any] | None = None
    actual: dict[str, Any] | None = None
    diff: list[str] = field(default_factory=list)


@dataclass
class VerifyResult:
    """Aggregate verification outcome."""

    total: int
    passed: int
    failed: int
    results: list[InteractionResult] = field(default_factory=list)


def _strip_volatile(
    obj: Any,
    ignore_fields: frozenset[str] = frozenset(),
    ignore_paths: frozenset[str] = frozenset(),
    _current_path: str = "$",
) -> Any:
    """Recursively strip volatile and user-ignored fields from a JSON structure.

    ignore_fields: key names stripped at any depth (e.g. "timestamp")
    ignore_paths:  exact dot-paths stripped only at that location
                   (e.g. "$.result.content[0].text.metadata.scrapeId")
    """
    if isinstance(obj, dict):
        result: dict[str, Any] = {}
        for k, v in obj.items():
            if k in _VOLATILE_KEYS or k in ignore_fields:
                continue
            child_path = f"{_current_path}.{k}"
            if child_path in ignore_paths:
                continue
            result[k] = _strip_volatile(v, ignore_fields, ignore_paths, child_path)
        return result
    if isinstance(obj, list):
        return [
            _strip_volatile(item, ignore_fields, ignore_paths, f"{_current_path}[{i}]")
            for i, item in enumerate(obj)
        ]
    return obj


def _deep_diff(expected: Any, actual: Any, path: str = "$") -> list[str]:
    """Produce human-readable diff lines between two JSON-like structures."""
    diffs: list[str] = []

    if type(expected) is not type(actual):
        diffs.append(f"  {path}: type {type(expected).__name__} != {type(actual).__name__}")
        diffs.append(f"    expected: {json.dumps(expected, default=str)}")
        diffs.append(f"    actual:   {json.dumps(actual, default=str)}")
        return diffs

    if isinstance(expected, dict):
        all_keys = set(expected) | set(actual)
        for key in sorted(all_keys):
            child_path = f"{path}.{key}"
            if key not in actual:
                diffs.append(f"  {child_path}: missing in actual")
                diffs.append(f"    expected: {json.dumps(expected[key], default=str)}")
            elif key not in expected:
                diffs.append(f"  {child_path}: unexpected key in actual")
                diffs.append(f"    actual: {json.dumps(actual[key], default=str)}")
            else:
                diffs.extend(_deep_diff(expected[key], actual[key], child_path))
        return diffs

    if isinstance(expected, list):
        if len(expected) != len(actual):
            diffs.append(f"  {path}: list length {len(expected)} != {len(actual)}")
        for i in range(min(len(expected), len(actual))):
            diffs.extend(_deep_diff(expected[i], actual[i], f"{path}[{i}]"))
        return diffs

    if expected != actual:
        # When both values are strings, try parsing as JSON for structural comparison.
        # Handles MCP tools that return JSON-as-string in content[0].text.
        if isinstance(expected, str) and isinstance(actual, str):
            try:
                parsed_expected = json.loads(expected)
                parsed_actual = json.loads(actual)
                if isinstance(parsed_expected, dict | list) and isinstance(
                    parsed_actual, dict | list
                ):
                    return _deep_diff(parsed_expected, parsed_actual, path)
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass

        diffs.append(
            f"  {path}: {json.dumps(expected, default=str)} != {json.dumps(actual, default=str)}"
        )

    return diffs


async def _send_request(
    client: httpx.AsyncClient,
    url: str,
    interaction: CassetteInteraction,
    session_id: str | None,
) -> tuple[dict[str, Any] | None, int, str | None]:
    """Send a single interaction request and return (response_body, status, session_id)."""
    headers: dict[str, str] = {
        "content-type": "application/json",
        "accept": "application/json, text/event-stream",
    }
    if session_id:
        headers["mcp-session-id"] = session_id

    if interaction.type == InteractionType.LIFECYCLE:
        method = interaction.http_method or "DELETE"
        resp = await client.request(method=method, url=url, headers=headers)
        new_sid = resp.headers.get("mcp-session-id", session_id)
        return None, resp.status_code, new_sid

    body = json.dumps(interaction.request).encode() if interaction.request else b""
    resp = await client.post(url, content=body, headers=headers)

    new_sid = resp.headers.get("mcp-session-id", session_id)
    content_type = resp.headers.get("content-type", "")

    if "text/event-stream" in content_type:
        parsed = parse_sse_response(resp.text)
        return parsed, resp.status_code, new_sid

    try:
        return resp.json(), resp.status_code, new_sid
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None, resp.status_code, new_sid


async def verify_cassette(
    cassette: Cassette,
    target_url: str,
    *,
    ignore_fields: frozenset[str] = frozenset(),
    ignore_paths: frozenset[str] = frozenset(),
) -> VerifyResult:
    """Replay all interactions from a cassette against the target and compare responses."""
    target_url = target_url.rstrip("/")
    mcp_url = f"{target_url}/mcp" if not target_url.endswith("/mcp") else target_url

    results: list[InteractionResult] = []
    session_id: str | None = None

    async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=30.0)) as client:
        for idx, interaction in enumerate(cassette.interactions):
            method_name = interaction.jsonrpc_method or interaction.http_method or "unknown"

            if interaction.type == InteractionType.LIFECYCLE:
                actual_body, status, session_id = await _send_request(
                    client, mcp_url, interaction, session_id
                )
                results.append(
                    InteractionResult(
                        index=idx + 1,
                        method=f"{interaction.http_method} {interaction.http_path}",
                        passed=True,
                    )
                )
                logger.info("[%d] %s -> %d (lifecycle)", idx + 1, method_name, status)
                continue

            if interaction.type == InteractionType.NOTIFICATION:
                actual_body, status, session_id = await _send_request(
                    client, mcp_url, interaction, session_id
                )
                passed = status == interaction.response_status
                diff_lines: list[str] = []
                if not passed:
                    diff_lines.append(
                        f"  status: expected {interaction.response_status}, got {status}"
                    )
                results.append(
                    InteractionResult(
                        index=idx + 1,
                        method=method_name,
                        passed=passed,
                        diff=diff_lines,
                    )
                )
                logger.info(
                    "[%d] %s -> %d (%s)",
                    idx + 1,
                    method_name,
                    status,
                    "pass" if passed else "FAIL",
                )
                continue

            # JSON-RPC request
            actual_body, status, session_id = await _send_request(
                client, mcp_url, interaction, session_id
            )

            expected_clean = _strip_volatile(interaction.response, ignore_fields, ignore_paths)
            actual_clean = _strip_volatile(actual_body, ignore_fields, ignore_paths)

            diff_lines = _deep_diff(expected_clean, actual_clean)
            passed = len(diff_lines) == 0

            tool = interaction.tool_name
            tool_tag = f" [{tool}]" if tool else ""
            logger.info(
                "[%d] %s%s -> %s", idx + 1, method_name, tool_tag, "pass" if passed else "FAIL"
            )
            if diff_lines:
                for line in diff_lines:
                    logger.info("  %s", line)

            results.append(
                InteractionResult(
                    index=idx + 1,
                    method=f"{method_name}{tool_tag}",
                    passed=passed,
                    expected=interaction.response,
                    actual=actual_body,
                    diff=diff_lines,
                )
            )

    passed_count = sum(1 for r in results if r.passed)
    return VerifyResult(
        total=len(results),
        passed=passed_count,
        failed=len(results) - passed_count,
        results=results,
    )


def run_verify(
    cassette: Cassette,
    target_url: str,
    *,
    ignore_fields: frozenset[str] = frozenset(),
    ignore_paths: frozenset[str] = frozenset(),
) -> VerifyResult:
    """Synchronous wrapper around verify_cassette."""
    return asyncio.run(
        verify_cassette(
            cassette, target_url, ignore_fields=ignore_fields, ignore_paths=ignore_paths
        )
    )
