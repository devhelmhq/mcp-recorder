"""Unit tests for verifier diff and strip logic."""

from __future__ import annotations

from mcp_recorder.verifier import _deep_diff, _strip_volatile


class TestStripVolatile:
    """Tests for _strip_volatile with ignore_fields and ignore_paths."""

    def test_strips_default_volatile_keys(self) -> None:
        obj = {"id": 1, "result": {"_meta": {}, "value": 42}}
        result = _strip_volatile(obj)
        assert result == {"result": {"value": 42}}

    def test_ignore_fields_removes_at_any_depth(self) -> None:
        obj = {
            "result": {
                "timestamp": "2026-01-01",
                "data": {"timestamp": "2026-02-01", "value": 1},
            }
        }
        result = _strip_volatile(obj, ignore_fields=frozenset({"timestamp"}))
        assert result == {"result": {"data": {"value": 1}}}

    def test_ignore_paths_removes_exact_location(self) -> None:
        obj = {
            "result": {
                "metadata": {"scrapeId": "abc123", "other": "keep"},
                "content": [{"text": "hello"}],
            }
        }
        result = _strip_volatile(obj, ignore_paths=frozenset({"$.result.metadata.scrapeId"}))
        assert result == {
            "result": {
                "metadata": {"other": "keep"},
                "content": [{"text": "hello"}],
            }
        }

    def test_ignore_paths_does_not_affect_other_locations(self) -> None:
        """A path targeting one location should not strip the same key elsewhere."""
        obj = {
            "result": {
                "a": {"ts": "v1"},
                "b": {"ts": "v2"},
            }
        }
        result = _strip_volatile(obj, ignore_paths=frozenset({"$.result.a.ts"}))
        assert result == {"result": {"a": {}, "b": {"ts": "v2"}}}

    def test_ignore_paths_with_array_index(self) -> None:
        obj = {"result": {"items": [{"val": 1}, {"val": 2}]}}
        result = _strip_volatile(obj, ignore_paths=frozenset({"$.result.items[0].val"}))
        assert result == {"result": {"items": [{}, {"val": 2}]}}

    def test_both_ignore_fields_and_paths(self) -> None:
        obj = {
            "result": {
                "timestamp": "t1",
                "metadata": {"requestId": "r1", "name": "keep"},
            }
        }
        result = _strip_volatile(
            obj,
            ignore_fields=frozenset({"timestamp"}),
            ignore_paths=frozenset({"$.result.metadata.requestId"}),
        )
        assert result == {"result": {"metadata": {"name": "keep"}}}


class TestDeepDiffJsonInString:
    """Tests for JSON-in-string structural comparison in _deep_diff."""

    def test_identical_json_strings_no_diff(self) -> None:
        expected = '{"key": "value", "count": 1}'
        actual = '{"key": "value", "count": 1}'
        assert _deep_diff(expected, actual) == []

    def test_equivalent_json_strings_different_formatting(self) -> None:
        """Different whitespace/key order but same structure should pass."""
        expected = '{"b": 2, "a": 1}'
        actual = '{"a":1,"b":2}'
        assert _deep_diff(expected, actual) == []

    def test_json_string_structural_diff(self) -> None:
        expected = '{"key": "old", "count": 1}'
        actual = '{"key": "new", "count": 1}'
        diffs = _deep_diff(expected, actual)
        assert len(diffs) > 0
        assert any("old" in d and "new" in d for d in diffs)

    def test_non_json_strings_compared_as_strings(self) -> None:
        diffs = _deep_diff("hello world", "hello mars")
        assert len(diffs) == 1
        assert "hello world" in diffs[0]

    def test_one_json_one_not_compared_as_strings(self) -> None:
        """If only one side parses as JSON, fall back to string comparison."""
        diffs = _deep_diff('{"a": 1}', "not json")
        assert len(diffs) == 1

    def test_json_string_in_nested_structure(self) -> None:
        expected = {"result": {"content": [{"text": '{"status": "ok", "items": [1, 2]}'}]}}
        actual = {"result": {"content": [{"text": '{"status": "ok", "items": [1, 3]}'}]}}
        diffs = _deep_diff(expected, actual)
        assert len(diffs) > 0
        assert any("[1]" in d for d in diffs)

    def test_json_array_strings_compared_structurally(self) -> None:
        expected = "[1, 2, 3]"
        actual = "[1, 2, 4]"
        diffs = _deep_diff(expected, actual)
        assert len(diffs) > 0
        assert any("[2]" in d for d in diffs)

    def test_scalar_json_strings_not_parsed(self) -> None:
        """JSON scalars like '"hello"' or '42' should not trigger structural comparison."""
        diffs = _deep_diff('"hello"', '"world"')
        assert len(diffs) == 1
