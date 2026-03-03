"""Unit tests for CLI flag validation and parsing helpers."""

from __future__ import annotations

import pytest
from click import BadParameter
from click.testing import CliRunner

from mcp_recorder.cli import _parse_target_env, _validate_target, record, verify


class TestTargetValidation:
    def test_both_target_and_target_stdio_errors(self) -> None:
        runner = CliRunner()
        result = runner.invoke(
            record,
            [
                "--target",
                "http://localhost:3000",
                "--target-stdio",
                "node server.js",
                "--output",
                "out.json",
            ],
        )
        assert result.exit_code != 0
        assert "mutually exclusive" in result.output

    def test_neither_target_nor_target_stdio_errors(self) -> None:
        runner = CliRunner()
        result = runner.invoke(record, ["--output", "out.json"])
        assert result.exit_code != 0
        assert "Provide either" in result.output

    def test_verify_both_targets_errors(self) -> None:
        runner = CliRunner()
        result = runner.invoke(
            verify,
            [
                "--cassette",
                "nonexistent.json",
                "--target",
                "http://localhost:3000",
                "--target-stdio",
                "node server.js",
            ],
        )
        assert result.exit_code != 0
        assert "mutually exclusive" in result.output

    def test_verify_neither_target_errors(self) -> None:
        runner = CliRunner()
        result = runner.invoke(verify, ["--cassette", "nonexistent.json"])
        assert result.exit_code != 0
        assert "Provide either" in result.output


class TestParseTargetEnv:
    def test_valid_pairs(self) -> None:
        result = _parse_target_env(("FOO=bar", "BAZ=qux"))
        assert result == {"FOO": "bar", "BAZ": "qux"}

    def test_value_with_equals(self) -> None:
        result = _parse_target_env(("TOKEN=abc=def",))
        assert result == {"TOKEN": "abc=def"}

    def test_empty_value(self) -> None:
        result = _parse_target_env(("KEY=",))
        assert result == {"KEY": ""}

    def test_invalid_format_raises(self) -> None:
        with pytest.raises(BadParameter, match="KEY=VALUE"):
            _parse_target_env(("NOEQUALSSIGN",))

    def test_empty_tuple(self) -> None:
        assert _parse_target_env(()) == {}


class TestValidateTarget:
    def test_http_only_passes(self) -> None:
        _validate_target("http://localhost:3000", None)

    def test_stdio_only_passes(self) -> None:
        _validate_target(None, "node server.js")
