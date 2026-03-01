"""Shared utilities used across mcp-recorder modules."""

from __future__ import annotations

import json
import socket
import threading
import time
from pathlib import Path
from typing import Any

import uvicorn

from mcp_recorder._types import Cassette


def parse_sse_response(text: str) -> dict[str, Any] | None:
    """Extract the first JSON-RPC message from an SSE response body."""
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("data:"):
            continue
        payload = stripped[len("data:") :].strip()
        if not payload:
            continue
        try:
            return json.loads(payload)  # type: ignore[no-any-return]
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
    return None


def find_free_port() -> int:
    """Bind to port 0 and return the OS-assigned port number."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


def load_cassette(path: Path) -> Cassette:
    """Load and validate a cassette from a JSON file."""
    raw = json.loads(path.read_text())
    return Cassette.model_validate(raw)


def save_cassette(cassette: Cassette, path: Path) -> None:
    """Serialize a cassette to a JSON file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = cassette.model_dump(mode="json")
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")


class UvicornServer:
    """Manages a uvicorn server running in a daemon thread.

    Used by the scenario runner (proxy) and pytest plugin (replay).
    """

    def __init__(self, app: Any, port: int, *, log_level: str = "warning") -> None:
        self.port = port
        self._config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level=log_level)
        self._server = uvicorn.Server(self._config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)

    def start(self, timeout: float = 10.0) -> None:
        self._thread.start()
        deadline = time.monotonic() + timeout
        while not self._server.started:
            if time.monotonic() > deadline:
                raise RuntimeError(f"Server failed to start on port {self.port} within {timeout}s")
            time.sleep(0.05)

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=5.0)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/mcp"
