# mcp-recorder

Record and replay MCP server interactions for deterministic testing.

[![PyPI version](https://img.shields.io/pypi/v/mcp-recorder.svg)](https://pypi.org/project/mcp-recorder/)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://python.org)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![CI](https://github.com/devhelm/mcp-recorder/actions/workflows/ci.yml/badge.svg)](https://github.com/devhelm/mcp-recorder/actions)

## Record. Replay. Test.

```bash
# 1. Record interactions from a live MCP server
mcp-recorder record --target http://localhost:8000 --output cassette.json

# 2. Replay them — no real server, no credentials, no network
mcp-recorder replay --cassette cassette.json

# 3. Run your tests against the replay
pytest
```

No code changes to your server. No live credentials in CI. No flaky tests.

## Install

```bash
pip install mcp-recorder
```

Or with [uv](https://docs.astral.sh/uv/):

```bash
uv add mcp-recorder
```

## How It Works

mcp-recorder is a transparent HTTP proxy that sits between your MCP client and server.

```
Record:  Client -> mcp-recorder (proxy) -> Real Server -> cassette.json
Replay:  Client -> mcp-recorder (mock)  -> cassette.json
```

In **record** mode, every JSON-RPC interaction is captured — `initialize` handshake, tool calls, notifications — and saved to a cassette file. In **replay** mode, recorded responses are served back. Deterministic. Fast. Offline.

- **MCP-aware** — understands the full JSON-RPC lifecycle, not just raw HTTP
- **Zero code changes** — point your client at the proxy, everything else stays the same
- **Flexible matching** — match by method + params, sequential order, or strict equality
- **Secret redaction** — auto-scrubs tokens and credentials before writing cassettes
- **CI-ready** — exits non-zero on mismatches, fully headless
- **JSON cassettes** — human-readable, git-diff-friendly, optional YAML output

## Quick Start

### Recording

Start the proxy pointing at your MCP server:

```bash
mcp-recorder record \
  --target http://localhost:8000 \
  --port 5555 \
  --output cassettes/github.json
```

Point your MCP client at `http://localhost:5555` and interact normally. Press `Ctrl+C` when done — the cassette is saved.

### Replaying

Serve recorded responses without the real server:

```bash
mcp-recorder replay --cassette cassettes/github.json
```

A mock server starts on port `5555`. No network, no credentials, same responses.

### Testing with pytest

Use the built-in pytest integration:

```python
@pytest.mark.mcp_cassette("cassettes/github.json")
def test_search_tool(mcp_client):
    result = mcp_client.call_tool("search", {"query": "protocol"})
    assert result["content"][0]["text"] == "expected output"
```

First run records (cassette doesn't exist). Subsequent runs replay. Override with:

```bash
MCP_RECORDER_MODE=record pytest    # Force re-record
MCP_RECORDER_MODE=replay pytest    # Fail if no cassette
```

## CLI Reference

### `mcp-recorder record`

| Option | Default | Description |
|---|---|---|
| `--target` | *(required)* | URL of the real MCP server |
| `--port` | `5555` | Local proxy port |
| `--output` | `recording.json` | Output cassette file path |
| `--format` | `json` | Cassette format: `json` or `yaml` |
| `--no-redact` | — | Disable automatic secret redaction |
| `--redact-patterns` | — | Additional regex patterns to redact |

### `mcp-recorder replay`

| Option | Default | Description |
|---|---|---|
| `--cassette` | *(required)* | Path to cassette file |
| `--port` | `5555` | Local server port |
| `--match` | `method_params` | Matching strategy (see below) |
| `--simulate-latency` | `false` | Replay with original recorded timing |

### `mcp-recorder inspect`

Pretty-print a cassette summary:

```bash
mcp-recorder inspect cassettes/session.json
```

```
Cassette: cassettes/session.json
Recorded: 2026-02-16T12:00:00Z
Server:   http://localhost:8000

Interactions (12):
  1. initialize                  <- 200  (42ms)
  2. notifications/initialized
  3. tools/list                  <- 200  (15ms)
  4. tools/call [search]         <- 200  (183ms)
  ...
```

## Cassette Format

Cassettes store the raw JSON-RPC messages as they appear on the wire:

```json
{
  "version": "1.0",
  "metadata": {
    "recorded_at": "2026-02-16T12:00:00Z",
    "server_url": "http://localhost:8000",
    "transport": "http"
  },
  "interactions": [
    {
      "request": {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
          "protocolVersion": "2025-03-26",
          "capabilities": {},
          "clientInfo": { "name": "test-client", "version": "1.0" }
        }
      },
      "response": {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
          "protocolVersion": "2025-03-26",
          "capabilities": { "tools": { "listChanged": true } },
          "serverInfo": { "name": "my-server", "version": "0.1.0" }
        }
      },
      "timestamp": "2026-02-16T12:00:01.000Z",
      "latency_ms": 42
    }
  ]
}
```

## Configuration

### Matching Strategies

| Strategy | Flag | Description |
|---|---|---|
| **Method + Params** | `method_params` | Match on JSON-RPC `method` and `params` (default) |
| **Sequential** | `sequential` | Return next unmatched interaction in order |
| **Strict** | `strict` | Full structural equality of the request body |

### Secret Redaction

By default, values matching environment variables with sensitive names (`*_TOKEN`, `*_KEY`, `*_SECRET`, `*_PASSWORD`) are replaced with `<REDACTED>` in cassettes.

Add custom patterns:

```bash
mcp-recorder record --target http://localhost:8000 --redact-patterns "sk-[a-zA-Z0-9]+"
```

## CI Integration

### GitHub Actions

```yaml
steps:
  - uses: actions/checkout@v4
  - uses: actions/setup-python@v5
    with:
      python-version: "3.12"

  - run: pip install mcp-recorder
  - run: |
      mcp-recorder replay --cassette cassettes/search.json --port 5555 &
      sleep 1
      pytest --timeout=30
```

Or with the pytest plugin (manages the server lifecycle automatically):

```yaml
  - run: pytest tests/ -m mcp_cassette
```

## Roadmap

- [ ] `stdio` transport — subprocess wrapping for local MCP servers
- [ ] WebSocket transport
- [ ] `mcp-recorder diff` — compare cassettes for breaking changes
- [ ] Cassette auto-update mode
- [ ] Visual session debugger

## Contributing

```bash
git clone https://github.com/devhelm/mcp-recorder.git
cd mcp-recorder
uv sync --group dev
uv run pytest
```

## License

MIT — see [LICENSE](LICENSE) for details.
