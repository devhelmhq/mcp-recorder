"""Shared test fixtures for mcp-recorder."""

from __future__ import annotations

from pathlib import Path

import pytest

from mcp_recorder._types import Cassette

CASSETTES_DIR = Path(__file__).parent / "cassettes"


@pytest.fixture
def cassettes_dir() -> Path:
    return CASSETTES_DIR


@pytest.fixture
def mock_session_path() -> Path:
    return CASSETTES_DIR / "mock_session.json"


@pytest.fixture
def mock_session_cassette(mock_session_path: Path) -> Cassette:
    import json

    raw = json.loads(mock_session_path.read_text())
    return Cassette.model_validate(raw)
