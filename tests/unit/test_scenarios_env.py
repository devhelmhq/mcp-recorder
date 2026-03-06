"""Unit tests for environment variable interpolation in scenarios YAML."""

from __future__ import annotations

import os
from pathlib import Path
from textwrap import dedent

import pytest

from mcp_recorder.scenarios import (
    StdioTargetConfig,
    _expand_env_vars,
    load_scenarios_file,
)


class TestExpandEnvVars:
    """Low-level tests for the _expand_env_vars helper."""

    def test_plain_string_unchanged(self) -> None:
        assert _expand_env_vars("hello world") == "hello world"

    def test_non_string_types_unchanged(self) -> None:
        assert _expand_env_vars(42) == 42
        assert _expand_env_vars(3.14) == 3.14
        assert _expand_env_vars(True) is True
        assert _expand_env_vars(None) is None

    def test_simple_substitution(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MY_VAR", "replaced")
        assert _expand_env_vars("${MY_VAR}") == "replaced"

    def test_substitution_with_surrounding_text(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HOST", "example.com")
        assert _expand_env_vars("https://${HOST}/api") == "https://example.com/api"

    def test_multiple_vars_in_one_string(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SCHEME", "https")
        monkeypatch.setenv("HOST", "example.com")
        assert _expand_env_vars("${SCHEME}://${HOST}") == "https://example.com"

    def test_default_value_when_unset(self) -> None:
        key = "_MCP_TEST_UNSET_FOR_DEFAULT"
        os.environ.pop(key, None)
        assert _expand_env_vars(f"${{{key}:-fallback}}") == "fallback"

    def test_default_empty_string(self) -> None:
        key = "_MCP_TEST_UNSET_FOR_EMPTY"
        os.environ.pop(key, None)
        assert _expand_env_vars(f"${{{key}:-}}") == ""

    def test_set_var_ignores_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MY_VAR", "actual")
        assert _expand_env_vars("${MY_VAR:-fallback}") == "actual"

    def test_missing_var_no_default_raises(self) -> None:
        key = "_MCP_TEST_MISSING_NO_DEFAULT"
        os.environ.pop(key, None)
        with pytest.raises(ValueError, match=f"Environment variable '{key}' is not set"):
            _expand_env_vars(f"${{{key}}}")

    def test_dict_values_expanded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SECRET", "s3cret")
        result = _expand_env_vars({"key": "${SECRET}", "plain": "no-change"})
        assert result == {"key": "s3cret", "plain": "no-change"}

    def test_dict_keys_not_expanded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("K", "expanded")
        result = _expand_env_vars({"${K}": "value"})
        assert result == {"${K}": "value"}

    def test_list_items_expanded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("A", "1")
        monkeypatch.setenv("B", "2")
        assert _expand_env_vars(["${A}", "${B}", "literal"]) == ["1", "2", "literal"]

    def test_nested_structure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TOKEN", "abc123")
        data = {"outer": {"inner": [{"deep": "${TOKEN}"}]}}
        assert _expand_env_vars(data) == {"outer": {"inner": [{"deep": "abc123"}]}}

    def test_dollar_brace_literal_without_closing(self) -> None:
        assert _expand_env_vars("${NOT_CLOSED") == "${NOT_CLOSED"

    def test_dollar_sign_without_brace_unchanged(self) -> None:
        assert _expand_env_vars("$PLAIN_VAR") == "$PLAIN_VAR"


class TestLoadScenariosFileEnvExpansion:
    """Integration tests: load a YAML file with env var references."""

    def test_env_in_target_url(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MCP_HOST", "my-server.example.com")
        f = tmp_path / "scenarios.yml"
        f.write_text(
            dedent("""\
                schema_version: "1.0"
                target: "https://${MCP_HOST}/mcp"
                scenarios:
                  s1:
                    actions:
                      - list_tools
            """)
        )
        sf = load_scenarios_file(f)
        assert sf.target == "https://my-server.example.com/mcp"

    def test_env_in_stdio_target_env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("REAL_API_KEY", "key-from-env")
        f = tmp_path / "scenarios.yml"
        f.write_text(
            dedent("""\
                schema_version: "1.0"
                target:
                  command: node
                  args: ["server.js"]
                  env:
                    API_KEY: "${REAL_API_KEY}"
                scenarios:
                  s1:
                    actions:
                      - list_tools
            """)
        )
        sf = load_scenarios_file(f)
        assert isinstance(sf.target, StdioTargetConfig)
        assert sf.target.env == {"API_KEY": "key-from-env"}

    def test_env_in_tool_arguments(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SEARCH_KEY", "sk-test-123")
        f = tmp_path / "scenarios.yml"
        f.write_text(
            dedent("""\
                schema_version: "1.0"
                target: http://localhost:3000
                scenarios:
                  s1:
                    actions:
                      - call_tool:
                          name: search
                          arguments:
                            api_key: "${SEARCH_KEY}"
                            query: "test"
            """)
        )
        sf = load_scenarios_file(f)
        action = sf.scenarios["s1"].actions[0]
        assert isinstance(action, dict)
        assert action["call_tool"]["arguments"]["api_key"] == "sk-test-123"

    def test_env_with_default_in_yaml(self, tmp_path: Path) -> None:
        key = "_MCP_TEST_UNSET_FOR_YAML_DEFAULT"
        os.environ.pop(key, None)
        f = tmp_path / "scenarios.yml"
        f.write_text(
            dedent(f"""\
                schema_version: "1.0"
                target: "http://${{{key}:-localhost}}:3000"
                scenarios:
                  s1:
                    actions:
                      - list_tools
            """)
        )
        sf = load_scenarios_file(f)
        assert sf.target == "http://localhost:3000"

    def test_missing_env_in_yaml_raises(self, tmp_path: Path) -> None:
        key = "_MCP_TEST_DEFINITELY_UNSET"
        os.environ.pop(key, None)
        f = tmp_path / "scenarios.yml"
        f.write_text(
            dedent(f"""\
                schema_version: "1.0"
                target: "http://${{{key}}}/mcp"
                scenarios:
                  s1:
                    actions:
                      - list_tools
            """)
        )
        with pytest.raises(ValueError, match="is not set"):
            load_scenarios_file(f)

    def test_no_env_refs_still_works(self, tmp_path: Path) -> None:
        f = tmp_path / "scenarios.yml"
        f.write_text(
            dedent("""\
                schema_version: "1.0"
                target: http://localhost:3000
                scenarios:
                  s1:
                    description: "plain scenario"
                    actions:
                      - list_tools
            """)
        )
        sf = load_scenarios_file(f)
        assert sf.target == "http://localhost:3000"
        assert sf.scenarios["s1"].description == "plain scenario"
