"""Request matching strategies for cassette replay."""

from __future__ import annotations

import hashlib
import json
from abc import ABC, abstractmethod
from collections import deque
from typing import Any

from mcp_recorder._types import CassetteInteraction, InteractionType


def normalize_params(params: dict[str, Any] | None) -> dict[str, Any] | None:
    """Strip volatile fields from params before matching.

    Removes ``_meta`` (contains progressToken which changes every run).
    """
    if params is None:
        return None
    return {k: v for k, v in params.items() if k != "_meta"}


def stable_hash(obj: Any) -> str:
    """Produce a deterministic hash of a JSON-serializable object."""
    serialized = json.dumps(obj, sort_keys=True, ensure_ascii=True, default=str)
    return hashlib.sha256(serialized.encode()).hexdigest()[:16]


def match_key_for(request: dict[str, Any]) -> str:
    """Compute the match key for a JSON-RPC request body.

    Key format: ``method::hash(normalized_params)``
    """
    method = request.get("method", "")
    params = request.get("params")
    normalized = normalize_params(params) if isinstance(params, dict) else params
    return f"{method}::{stable_hash(normalized)}"


class Matcher(ABC):
    """Base class for replay matching strategies."""

    def __init__(self, interactions: list[CassetteInteraction]) -> None:
        self._all_interactions = interactions
        self._total = sum(1 for i in interactions if i.type == InteractionType.JSONRPC_REQUEST)
        self._matched_count = 0
        self._unmatched_requests: list[dict[str, Any]] = []

    @abstractmethod
    def match(self, request_body: dict[str, Any]) -> CassetteInteraction | None:
        """Find the matching interaction for a request. Returns None if no match."""

    @property
    def all_consumed(self) -> bool:
        """True if every JSON-RPC request interaction has been consumed."""
        return self._matched_count >= self._total

    @property
    def unmatched_requests(self) -> list[dict[str, Any]]:
        """Requests that came in but found no match."""
        return self._unmatched_requests

    def _record_match(self) -> None:
        self._matched_count += 1

    def _record_miss(self, request_body: dict[str, Any]) -> None:
        self._unmatched_requests.append(request_body)


class MethodParamsMatcher(Matcher):
    """Match by JSON-RPC method + normalized params. FIFO consumption for duplicates."""

    def __init__(self, interactions: list[CassetteInteraction]) -> None:
        super().__init__(interactions)
        self._index: dict[str, deque[CassetteInteraction]] = {}
        for interaction in interactions:
            if interaction.type != InteractionType.JSONRPC_REQUEST:
                continue
            if interaction.request is None:
                continue
            key = match_key_for(interaction.request)
            self._index.setdefault(key, deque()).append(interaction)

    def match(self, request_body: dict[str, Any]) -> CassetteInteraction | None:
        key = match_key_for(request_body)
        bucket = self._index.get(key)
        if bucket:
            interaction = bucket.popleft()
            self._record_match()
            return interaction
        self._record_miss(request_body)
        return None


class SequentialMatcher(Matcher):
    """Return the next unconsumed JSON-RPC request interaction in recorded order."""

    def __init__(self, interactions: list[CassetteInteraction]) -> None:
        super().__init__(interactions)
        self._queue: deque[CassetteInteraction] = deque(
            i for i in interactions if i.type == InteractionType.JSONRPC_REQUEST
        )

    def match(self, request_body: dict[str, Any]) -> CassetteInteraction | None:
        if self._queue:
            self._record_match()
            return self._queue.popleft()
        self._record_miss(request_body)
        return None


class StrictMatcher(Matcher):
    """Match by full JSON-RPC body equality (including _meta). FIFO consumption."""

    def __init__(self, interactions: list[CassetteInteraction]) -> None:
        super().__init__(interactions)
        self._index: dict[str, deque[CassetteInteraction]] = {}
        for interaction in interactions:
            if interaction.type != InteractionType.JSONRPC_REQUEST:
                continue
            if interaction.request is None:
                continue
            # Strict: hash the full params (no normalization)
            method = interaction.request.get("method", "")
            params = interaction.request.get("params")
            key = f"{method}::{stable_hash(params)}"
            self._index.setdefault(key, deque()).append(interaction)

    def match(self, request_body: dict[str, Any]) -> CassetteInteraction | None:
        method = request_body.get("method", "")
        params = request_body.get("params")
        key = f"{method}::{stable_hash(params)}"
        bucket = self._index.get(key)
        if bucket:
            interaction = bucket.popleft()
            self._record_match()
            return interaction
        self._record_miss(request_body)
        return None


def create_matcher(strategy: str, interactions: list[CassetteInteraction]) -> Matcher:
    """Factory to create a matcher by strategy name."""
    matchers: dict[str, type[Matcher]] = {
        "method_params": MethodParamsMatcher,
        "sequential": SequentialMatcher,
        "strict": StrictMatcher,
    }
    cls = matchers.get(strategy)
    if cls is None:
        raise ValueError(f"Unknown matching strategy: {strategy!r}. Choose from: {list(matchers)}")
    return cls(interactions)
