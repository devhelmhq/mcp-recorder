# mcp-recorder

Record, replay, and verify MCP server interactions for deterministic testing.

[![PyPI version](https://img.shields.io/pypi/v/mcp-recorder.svg)](https://pypi.org/project/mcp-recorder/)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://python.org)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![CI](https://github.com/devhelm/mcp-recorder/actions/workflows/ci.yml/badge.svg)](https://github.com/devhelm/mcp-recorder/actions)

## Record. Replay. Verify.

```bash
# Record a golden interaction session from a live MCP server
mcp-recorder record --target http://localhost:8000 --output golden.json

# Replay as a mock server — test your client without the real server
mcp-recorder replay --cassette golden.json

# Verify your server hasn't regressed — replay recorded requests, compare responses
mcp-recorder verify --cassette golden.json --target http://localhost:8000
```

One cassette. Three modes. Full coverage for both client and server testing.

## Install

```bash
pip install mcp-recorder
```

Or with [uv](https://docs.astral.sh/uv/):

```bash
uv add mcp-recorder
```

For YAML cassette support:

```bash
pip install mcp-recorder[yaml]
```

## How It Works

mcp-recorder is a transparent HTTP proxy that captures the full MCP exchange — requests and responses — into a cassette file. That single recording unlocks two testing directions:

```
Record:   Client -> mcp-recorder (proxy) -> Real Server -> cassette.json

Replay:   Client -> mcp-recorder (mock)  -> cassette.json     (test your client)
Verify:   mcp-recorder (client mock) -> Real Server            (test your server)
```

**Replay** serves recorded responses back to your client. No real server, no credentials, no network. Use this to test client code against a frozen server.

**Verify** sends recorded requests to your (updated) server and compares the actual responses to the golden recording. Use this to catch regressions after changing tools, schemas, or prompts.

Both modes use the same cassette. Record once, test from both sides.

## Features

- **MCP-aware** — captures the full JSON-RPC lifecycle: `initialize`, capabilities, tool calls, notifications
- **Two-sided testing** — mock the server (replay) or mock the client (verify) from the same recording
- **Zero code changes** — swap the server URL to record, that's it
- **Flexible matching** — method + params, sequential order, or strict equality
- **Explicit secret redaction** — opt-in flags to scrub URLs, env var values, and custom patterns
- **CI-ready** — exits non-zero on mismatches, fully headless
- **JSON cassettes** — human-readable, git-diff-friendly, optional YAML output

## Quick Start

### Recording

Start the proxy pointing at your MCP server (local or remote):

```bash
mcp-recorder record \
  --target http://localhost:8000 \
  --port 5555 \
  --output golden.json
```

Point your MCP client at `http://localhost:5555` and interact normally. Press `Ctrl+C` when done — the cassette is saved.

Works with remote servers too:

```bash
mcp-recorder record --target https://mcp.example.com/v1/mcp --output golden.json
```

### Replaying (client testing)

Serve recorded responses without the real server:

```bash
mcp-recorder replay --cassette golden.json
```

A mock server starts on port `5555`. Point your client at it and run your tests. No network, no credentials, same responses every time.

### Verifying (server regression testing)

After making changes to your server, verify nothing broke:

```bash
mcp-recorder verify --cassette golden.json --target http://localhost:8000
```

```
Verifying golden.json against http://localhost:8000...

  1. initialize             OK
  2. tools/list             OK
  3. tools/call [search]    DIFF
     - result.content[0].text: "old output" != "new output"
  4. tools/call [analyze]   OK

3/4 passed, 1 failed
```

Exit code is non-zero on any diff — plug it straight into CI.

### Testing with pytest

The plugin activates automatically when `mcp-recorder` is installed. Mark tests with a cassette path and request the `mcp_replay_url` fixture:

```python
import pytest
from fastmcp import Client

@pytest.mark.mcp_cassette("cassettes/golden.json")
async def test_search_tool(mcp_replay_url):
    async with Client(mcp_replay_url) as client:
        result = await client.call_tool("search", {"query": "protocol"})
        assert result.content[0].text == "expected output"
```

A replay server starts automatically on a random port, serves the cassette, and shuts down after the test. No manual server management.

For server regression testing, use `mcp_verify_result`:

```python
@pytest.mark.mcp_cassette("cassettes/golden.json")
def test_no_regression(mcp_verify_result):
    assert mcp_verify_result.failed == 0, mcp_verify_result.results
```

```bash
# Run with --mcp-target pointing at your live server
pytest --mcp-target http://localhost:8000

# Control replay mode
pytest --mcp-record-mode=replay   # default: serve from cassette
pytest --mcp-record-mode=auto     # replay if cassette exists, skip if not
```

## CLI Reference

### `mcp-recorder record`

Record interactions from a live MCP server.

| Option | Default | Description |
|---|---|---|
| `--target` | *(required)* | URL of the real MCP server |
| `--port` | `5555` | Local proxy port |
| `--output` | `recording.json` | Output cassette file path |
| `--verbose` | — | Log full headers and bodies to stderr |
| `--redact-server-url` | `true` | Strip URL path from metadata (keeps scheme + host) |
| `--redact-env VAR` | — | Redact named env var value from metadata + responses. Repeatable |
| `--redact-patterns REGEX` | — | Redact regex matches from metadata + responses. Repeatable |

### `mcp-recorder replay`

Start a mock server from a recorded cassette.

| Option | Default | Description |
|---|---|---|
| `--cassette` | *(required)* | Path to cassette file |
| `--port` | `5555` | Local server port |
| `--match` | `method_params` | Matching strategy (see below) |
| `--verbose` | — | Log every matched request to stderr |

### `mcp-recorder verify`

Replay recorded requests against a server and compare responses to the cassette.

| Option | Default | Description |
|---|---|---|
| `--cassette` | *(required)* | Path to golden cassette file |
| `--target` | *(required)* | URL of the server to verify |
| `--ignore-fields` | — | Response fields to ignore during comparison |
| `--update` | — | Update the cassette with new responses (snapshot update) |
| `--verbose` | — | Show full diff for each failing interaction |

### `mcp-recorder inspect`

Pretty-print a cassette summary.

```bash
mcp-recorder inspect golden.json
```

```
Cassette: golden.json
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

Cassettes store raw JSON-RPC messages as they appear on the wire:

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

Redaction is explicit — no magic scanning, no hidden behavior. You control exactly what gets scrubbed.

**`--redact-server-url`** (enabled by default)

Strips the URL path from `metadata.server_url`, keeping only the scheme and host. This is the most common case: API keys embedded in URLs like `https://mcp.firecrawl.dev/<key>/mcp`.

```bash
# Cassette metadata will show: https://mcp.firecrawl.dev/[REDACTED]
mcp-recorder record --target https://mcp.firecrawl.dev/$FIRECRAWL_KEY/mcp

# Disable if you want the full URL preserved
mcp-recorder record --target http://localhost:8000 --no-redact-server-url
```

**`--redact-env VAR_NAME`**

Reads the value of the named environment variable and replaces every occurrence in **metadata and response bodies**. Request bodies are never modified — this preserves replay and verify integrity.

```bash
export FIRECRAWL_KEY=fc-abc123
mcp-recorder record \
  --target https://mcp.firecrawl.dev/$FIRECRAWL_KEY/mcp \
  --redact-env FIRECRAWL_KEY
```

If a redacted value is also found in a request body, a warning is printed but the request is left intact. This is a deliberate tradeoff: redacting request bodies would break replay matching and verify.

**`--redact-patterns REGEX`**

For values not in environment variables. Same scope as `--redact-env` (metadata + responses only).

```bash
mcp-recorder record --target http://localhost:8000 \
  --redact-patterns "sk-[a-zA-Z0-9]+" \
  --redact-patterns "session-[0-9a-f]{32}"
```

**What about HTTP headers?**

Headers (Authorization, Cookie, etc.) are not stored in cassettes. The proxy only captures JSON-RPC message bodies and protocol metadata, so header secrets never reach the cassette file.

## CI Integration

### GitHub Actions

```yaml
steps:
  - uses: actions/checkout@v4
  - uses: actions/setup-python@v5
    with:
      python-version: "3.12"

  - run: pip install mcp-recorder

  # Option A: Replay mock server + run client tests
  - run: |
      mcp-recorder replay --cassette golden.json --port 5555 &
      sleep 1
      pytest

  # Option B: Verify server hasn't regressed
  - run: mcp-recorder verify --cassette golden.json --target http://localhost:8000
```

Or with the pytest plugin (manages the server lifecycle automatically):

```yaml
  - run: pytest tests/ -m mcp_cassette
```

## Roadmap

- [ ] `stdio` transport — subprocess wrapping for local MCP servers
- [ ] WebSocket transport
- [ ] `mcp-recorder diff` — compare two cassettes for breaking changes
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
