"""Unit tests for request matching strategies."""

from __future__ import annotations

from typing import Any

from mcp_recorder._types import CassetteInteraction, InteractionType
from mcp_recorder.matcher import (
    MethodParamsMatcher,
    SequentialMatcher,
    StrictMatcher,
    create_matcher,
)


def _make_interaction(
    method: str = "tools/call",
    params: dict[str, Any] | None = None,
    response_result: dict[str, Any] | None = None,
    request_id: int = 1,
) -> CassetteInteraction:
    request = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        request["params"] = params
    response = {"jsonrpc": "2.0", "id": request_id}
    if response_result is not None:
        response["result"] = response_result
    return CassetteInteraction(
        type=InteractionType.JSONRPC_REQUEST,
        request=request,
        response=response,
        response_status=200,
    )


# ---------------------------------------------------------------------------
# MethodParamsMatcher
# ---------------------------------------------------------------------------


class TestMethodParamsMatcher:
    def test_basic_match(self) -> None:
        interaction = _make_interaction(
            params={"name": "add", "arguments": {"a": 1, "b": 2}},
            response_result={"content": [{"type": "text", "text": "3"}]},
        )
        matcher = MethodParamsMatcher([interaction])

        incoming = {
            "jsonrpc": "2.0",
            "id": 99,
            "method": "tools/call",
            "params": {"name": "add", "arguments": {"a": 1, "b": 2}},
        }
        result = matcher.match(incoming)

        assert result is not None
        assert result.response is not None
        assert result.response["result"]["content"][0]["text"] == "3"

    def test_duplicate_calls_consume_fifo(self) -> None:
        interactions = [
            _make_interaction(
                params={"name": "add", "arguments": {"a": 1, "b": 2}},
                response_result={"value": "first"},
                request_id=1,
            ),
            _make_interaction(
                params={"name": "add", "arguments": {"a": 1, "b": 2}},
                response_result={"value": "second"},
                request_id=2,
            ),
            _make_interaction(
                params={"name": "add", "arguments": {"a": 1, "b": 2}},
                response_result={"value": "third"},
                request_id=3,
            ),
        ]
        matcher = MethodParamsMatcher(interactions)

        incoming = {
            "jsonrpc": "2.0",
            "id": 10,
            "method": "tools/call",
            "params": {"name": "add", "arguments": {"a": 1, "b": 2}},
        }

        r1 = matcher.match(incoming)
        r2 = matcher.match(incoming)
        r3 = matcher.match(incoming)

        assert r1 is not None and r1.response is not None
        assert r2 is not None and r2.response is not None
        assert r3 is not None and r3.response is not None
        assert r1.response["result"]["value"] == "first"
        assert r2.response["result"]["value"] == "second"
        assert r3.response["result"]["value"] == "third"

        assert matcher.match(incoming) is None

    def test_meta_stripped_for_matching(self) -> None:
        interaction = _make_interaction(
            params={"name": "add", "arguments": {"a": 1}, "_meta": {"progressToken": 1}},
            response_result={"value": "ok"},
        )
        matcher = MethodParamsMatcher([interaction])

        incoming = {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {"name": "add", "arguments": {"a": 1}, "_meta": {"progressToken": 99}},
        }
        result = matcher.match(incoming)

        assert result is not None

    def test_no_match_returns_none(self) -> None:
        interaction = _make_interaction(method="tools/list")
        matcher = MethodParamsMatcher([interaction])

        incoming = {"jsonrpc": "2.0", "id": 1, "method": "resources/list"}
        assert matcher.match(incoming) is None

    def test_unmatched_requests_tracked(self) -> None:
        matcher = MethodParamsMatcher([])
        incoming = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        matcher.match(incoming)

        assert len(matcher.unmatched_requests) == 1
        assert matcher.unmatched_requests[0]["method"] == "tools/list"


# ---------------------------------------------------------------------------
# SequentialMatcher
# ---------------------------------------------------------------------------


class TestSequentialMatcher:
    def test_returns_in_order(self) -> None:
        interactions = [
            _make_interaction(method="initialize", response_result={"value": "a"}),
            _make_interaction(method="tools/list", response_result={"value": "b"}),
            _make_interaction(method="tools/call", response_result={"value": "c"}),
        ]
        matcher = SequentialMatcher(interactions)

        r1 = matcher.match({"jsonrpc": "2.0", "id": 1, "method": "anything"})
        r2 = matcher.match({"jsonrpc": "2.0", "id": 2, "method": "whatever"})
        r3 = matcher.match({"jsonrpc": "2.0", "id": 3, "method": "doesnt_matter"})

        assert r1 is not None and r1.response is not None
        assert r2 is not None and r2.response is not None
        assert r3 is not None and r3.response is not None
        assert r1.response["result"]["value"] == "a"
        assert r2.response["result"]["value"] == "b"
        assert r3.response["result"]["value"] == "c"

    def test_exhaustion_returns_none(self) -> None:
        matcher = SequentialMatcher([_make_interaction()])
        matcher.match({"jsonrpc": "2.0", "id": 1, "method": "x"})

        assert matcher.match({"jsonrpc": "2.0", "id": 2, "method": "x"}) is None

    def test_skips_non_request_interactions(self) -> None:
        interactions = [
            CassetteInteraction(
                type=InteractionType.NOTIFICATION,
                request={"method": "notifications/initialized", "jsonrpc": "2.0"},
                response_status=202,
            ),
            _make_interaction(method="tools/list", response_result={"value": "only_request"}),
        ]
        matcher = SequentialMatcher(interactions)

        result = matcher.match({"jsonrpc": "2.0", "id": 1, "method": "x"})
        assert result is not None
        assert result.jsonrpc_method == "tools/list"


# ---------------------------------------------------------------------------
# StrictMatcher
# ---------------------------------------------------------------------------


class TestStrictMatcher:
    def test_meta_included_in_matching(self) -> None:
        interaction = _make_interaction(
            params={"name": "add", "_meta": {"progressToken": 1}},
            response_result={"value": "matched"},
        )
        matcher = StrictMatcher([interaction])

        # Different _meta -> no match (strict includes _meta)
        incoming_different = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "add", "_meta": {"progressToken": 99}},
        }
        assert matcher.match(incoming_different) is None

    def test_exact_match_works(self) -> None:
        interaction = _make_interaction(
            params={"name": "add", "_meta": {"progressToken": 1}},
            response_result={"value": "matched"},
        )
        matcher = StrictMatcher([interaction])

        incoming_exact = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "add", "_meta": {"progressToken": 1}},
        }
        result = matcher.match(incoming_exact)
        assert result is not None


# ---------------------------------------------------------------------------
# Shared properties
# ---------------------------------------------------------------------------


class TestMatcherProperties:
    def test_all_consumed_true_when_empty(self) -> None:
        matcher = MethodParamsMatcher([])
        assert matcher.all_consumed is True

    def test_all_consumed_after_full_consumption(self) -> None:
        interactions = [_make_interaction(), _make_interaction(method="tools/list")]
        matcher = SequentialMatcher(interactions)

        assert matcher.all_consumed is False
        matcher.match({"jsonrpc": "2.0", "id": 1, "method": "x"})
        assert matcher.all_consumed is False
        matcher.match({"jsonrpc": "2.0", "id": 2, "method": "x"})
        assert matcher.all_consumed is True

    def test_create_matcher_factory(self) -> None:
        interactions = [_make_interaction()]
        assert isinstance(create_matcher("method_params", interactions), MethodParamsMatcher)
        assert isinstance(create_matcher("sequential", interactions), SequentialMatcher)
        assert isinstance(create_matcher("strict", interactions), StrictMatcher)

    def test_create_matcher_invalid_strategy(self) -> None:
        import pytest

        with pytest.raises(ValueError, match="Unknown matching strategy"):
            create_matcher("nonexistent", [])
