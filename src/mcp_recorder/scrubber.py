"""Explicit secret redaction for cassettes.

Design principles:
- No auto-detection magic. Every redaction is triggered by an explicit CLI flag.
- --redact-server-url: strips the URL path from metadata (safe, never affects matching)
- --redact-env VAR: replaces the value of a named env var in metadata + response bodies
- --redact-patterns REGEX: replaces regex matches in metadata + response bodies
- Request bodies are never modified (preserves replay/verify integrity).
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Sequence
from typing import Any
from urllib.parse import urlparse, urlunparse

from mcp_recorder._types import Cassette

logger = logging.getLogger("mcp_recorder.scrubber")

_PLACEHOLDER = "[REDACTED]"


def _redact_url_path(url: str) -> str:
    """Replace the path component of a URL with [REDACTED], keeping scheme + host."""
    parsed = urlparse(url)
    if not parsed.path or parsed.path == "/":
        return url
    return urlunparse(parsed._replace(path=f"/{_PLACEHOLDER}", query="", fragment=""))


def _compile_patterns(
    *,
    env_vars: Sequence[str],
    regex_patterns: Sequence[str],
) -> list[re.Pattern[str]]:
    """Build regex patterns from explicit env var names and raw regex strings."""
    patterns: list[re.Pattern[str]] = []

    for var_name in env_vars:
        value = os.environ.get(var_name)
        if value is None:
            logger.warning("--redact-env %s: variable not found in environment, skipping", var_name)
            continue
        if not value:
            logger.warning("--redact-env %s: variable is empty, skipping", var_name)
            continue
        patterns.append(re.compile(re.escape(value)))

    for raw in regex_patterns:
        try:
            patterns.append(re.compile(raw))
        except re.error as exc:
            logger.warning("--redact-patterns %r: invalid regex (%s), skipping", raw, exc)

    return patterns


def _redact_string(value: str, patterns: list[re.Pattern[str]]) -> str:
    """Replace all pattern matches in a string."""
    for pat in patterns:
        value = pat.sub(_PLACEHOLDER, value)
    return value


def _walk(obj: Any, patterns: list[re.Pattern[str]]) -> Any:
    """Recursively walk a JSON-like structure and redact matching strings.

    Skips JSON-RPC structural keys (method, jsonrpc, id) to avoid breaking protocol.
    """
    if isinstance(obj, dict):
        return {
            k: (obj[k] if k in ("method", "jsonrpc", "id") else _walk(v, patterns))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_walk(item, patterns) for item in obj]
    if isinstance(obj, str):
        return _redact_string(obj, patterns)
    return obj


def scrub_cassette(
    cassette: Cassette,
    *,
    redact_server_url: bool = False,
    redact_env: Sequence[str] = (),
    redact_patterns: Sequence[str] = (),
) -> Cassette:
    """Return a new Cassette with explicit redactions applied.

    - redact_server_url: strips the URL path from metadata.server_url
    - redact_env: list of env var NAMES whose values are redacted from metadata + responses
    - redact_patterns: list of regex patterns redacted from metadata + responses

    Request bodies are never modified to preserve replay/verify integrity.
    """
    has_value_patterns = bool(redact_env or redact_patterns)

    if not redact_server_url and not has_value_patterns:
        return cassette

    data = cassette.model_dump(mode="json")

    # 1. URL path redaction (metadata only)
    if redact_server_url:
        data["metadata"]["server_url"] = _redact_url_path(data["metadata"]["server_url"])

    # 2. Value-based redaction (metadata + response bodies only)
    if has_value_patterns:
        patterns = _compile_patterns(env_vars=redact_env, regex_patterns=redact_patterns)

        if patterns:
            # Redact metadata string fields
            data["metadata"]["server_url"] = _redact_string(
                data["metadata"]["server_url"], patterns
            )

            # Redact response bodies only — never touch request bodies
            request_hits = 0
            for interaction in data["interactions"]:
                if interaction.get("response") is not None:
                    interaction["response"] = _walk(interaction["response"], patterns)

                # Scan request bodies for matches and warn (but don't redact)
                if interaction.get("request") is not None:
                    req_json = re.sub(r"\s+", " ", str(interaction["request"]))
                    for pat in patterns:
                        if pat.search(req_json):
                            request_hits += 1
                            break

            if request_hits > 0:
                logger.warning(
                    "Redacted values found in %d request body(ies). "
                    "Request bodies are NOT redacted to preserve replay/verify. "
                    "Review cassette manually if needed.",
                    request_hits,
                )

    return Cassette.model_validate(data)
