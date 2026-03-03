"""Verify engine: replay cassette requests against a live server and compare responses."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from mcp_recorder._types import Cassette, InteractionType

if TYPE_CHECKING:
    from mcp_recorder.transport import Transport

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


async def _verify_with_transport(
    cassette: Cassette,
    transport: Transport,
    *,
    ignore_fields: frozenset[str] = frozenset(),
    ignore_paths: frozenset[str] = frozenset(),
) -> VerifyResult:
    """Core verification loop using a :class:`Transport`."""
    results: list[InteractionResult] = []

    async with transport:
        for idx, interaction in enumerate(cassette.interactions):
            method_name = interaction.jsonrpc_method or interaction.http_method or "unknown"

            # Lifecycle interactions are HTTP-specific — skip for non-HTTP transports,
            # pass through for HttpTransport (which exposes send_lifecycle).
            if interaction.type == InteractionType.LIFECYCLE:
                if hasattr(transport, "send_lifecycle"):
                    http_method = interaction.http_method or "DELETE"
                    http_path = interaction.http_path or "/mcp"
                    status = await transport.send_lifecycle(http_method, http_path)
                    results.append(
                        InteractionResult(
                            index=idx + 1,
                            method=f"{interaction.http_method} {interaction.http_path}",
                            passed=True,
                        )
                    )
                    logger.info("[%d] %s -> %d (lifecycle)", idx + 1, method_name, status)
                else:
                    results.append(
                        InteractionResult(
                            index=idx + 1,
                            method=f"{interaction.http_method} {interaction.http_path}",
                            passed=True,
                        )
                    )
                    logger.info("[%d] %s (lifecycle, skipped)", idx + 1, method_name)
                continue

            if interaction.type == InteractionType.NOTIFICATION:
                try:
                    await transport.send_notification(interaction.request or {})
                except Exception as exc:
                    results.append(
                        InteractionResult(
                            index=idx + 1,
                            method=method_name,
                            passed=False,
                            diff=[f"  transport error: {exc}"],
                        )
                    )
                    logger.info("[%d] %s -> FAIL (transport error)", idx + 1, method_name)
                    continue

                results.append(InteractionResult(index=idx + 1, method=method_name, passed=True))
                logger.info("[%d] %s -> pass (notification)", idx + 1, method_name)
                continue

            # JSON-RPC request.
            try:
                actual_body = await transport.send_request(interaction.request or {})
            except Exception as exc:
                results.append(
                    InteractionResult(
                        index=idx + 1,
                        method=method_name,
                        passed=False,
                        diff=[f"  transport error: {exc}"],
                    )
                )
                logger.info("[%d] %s -> FAIL (transport error)", idx + 1, method_name)
                continue

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


async def verify_cassette(
    cassette: Cassette,
    target_url: str | None = None,
    *,
    transport: Transport | None = None,
    ignore_fields: frozenset[str] = frozenset(),
    ignore_paths: frozenset[str] = frozenset(),
) -> VerifyResult:
    """Replay all interactions from a cassette and compare responses.

    Provide either *target_url* (HTTP) or *transport* (e.g. StdioTransport).
    """
    if target_url is None and transport is None:
        raise ValueError("Either target_url or transport must be provided")

    if transport is None:
        from mcp_recorder.transport import HttpTransport

        assert target_url is not None
        transport = HttpTransport(target_url)

    return await _verify_with_transport(
        cassette, transport, ignore_fields=ignore_fields, ignore_paths=ignore_paths
    )


def run_verify(
    cassette: Cassette,
    target_url: str | None = None,
    *,
    transport: Transport | None = None,
    ignore_fields: frozenset[str] = frozenset(),
    ignore_paths: frozenset[str] = frozenset(),
) -> VerifyResult:
    """Synchronous wrapper around verify_cassette."""
    return asyncio.run(
        verify_cassette(
            cassette,
            target_url,
            transport=transport,
            ignore_fields=ignore_fields,
            ignore_paths=ignore_paths,
        )
    )
