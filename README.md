# mcp-recorder

**VCR.py for MCP servers.** Record, replay, and verify Model Context Protocol interactions for deterministic testing.

[![PyPI version](https://img.shields.io/pypi/v/mcp-recorder.svg)](https://pypi.org/project/mcp-recorder/)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://python.org)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![CI](https://github.com/devhelm/mcp-recorder/actions/workflows/check.yml/badge.svg)](https://github.com/devhelm/mcp-recorder/actions)

MCP servers break silently. Tool schemas change, prompts drift, responses shift. Without wire-level regression tests, you find out from your users. mcp-recorder captures the full protocol exchange into a cassette file and lets you test from both sides.

## Record. Replay. Verify.

```bash
# 1. Record a session from a live MCP server
mcp-recorder record --target http://localhost:8000 --output golden.json

# 2. Replay as a mock server — test your client without the real server
mcp-recorder replay --cassette golden.json

# 3. Verify your server hasn't regressed — compare responses to the recording
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

## Testing with pytest

The pytest plugin activates automatically on install. Mark tests with a cassette and use the `mcp_replay_url` fixture:

```python
import pytest
from fastmcp import Client

@pytest.mark.mcp_cassette("cassettes/golden.json")
async def test_tool_call(mcp_replay_url):
    """A replay server starts automatically, serves the cassette, shuts down after the test."""
    async with Client(mcp_replay_url) as client:
        result = await client.call_tool("add", {"a": 2, "b": 3})
        assert result.content[0].text == "5"
```

For server regression testing, use `mcp_verify_result`:

```python
@pytest.mark.mcp_cassette("cassettes/golden.json")
def test_no_regression(mcp_verify_result):
    assert mcp_verify_result.failed == 0, mcp_verify_result.results
```

```bash
pytest                                    # replay from cassettes (default)
pytest --mcp-target http://localhost:8000  # verify against live server
pytest --mcp-record-mode=auto             # replay if cassette exists, skip if not
```

No manual server management. No boilerplate. 20 cassettes in one file works fine — each test gets an isolated server on a random port.

## How It Works

mcp-recorder is a transparent HTTP proxy that captures the full MCP exchange into a cassette file. That single recording unlocks two testing directions:

```
Record:   Client -> mcp-recorder (proxy) -> Real Server -> cassette.json

Replay:   Client -> mcp-recorder (mock)  -> cassette.json     (test your client)
Verify:   mcp-recorder (client mock) -> Real Server            (test your server)
```

**Replay** serves recorded responses back to your client. No real server, no credentials, no network.

**Verify** sends recorded requests to your (updated) server and compares the actual responses to the golden recording. Catches regressions after changing tools, schemas, or prompts.

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
mcp-recorder record \
  --target https://mcp.example.com/v1/mcp \
  --redact-env API_KEY \
  --output golden.json
```

### Replaying (client testing)

Serve recorded responses without the real server:

```bash
mcp-recorder replay --cassette golden.json
```

A mock server starts on port `5555`. Point your client at it. No network, no credentials, same responses every time.

### Verifying (server regression testing)

After making changes to your server, verify nothing broke:

```bash
mcp-recorder verify --cassette golden.json --target http://localhost:8000
```

```
Verifying golden.json against http://localhost:8000

  1. initialize          [PASS]
  2. tools/list          [PASS]
  3. tools/call [search] [FAIL]
       $.result.content[0].text: "old output" != "new output"
  4. tools/call [analyze] [PASS]

Result: 3/4 passed, 1 failed
```

Exit code is non-zero on any diff — plug it straight into CI.

### Inspecting a cassette

```bash
mcp-recorder inspect golden.json
```

```
golden.json
  Recorded: 2026-02-17 20:25:23
  Server:   Test Calculator v2.14.5
  Protocol: 2025-11-25
  Target:   http://127.0.0.1:8000

  Interactions (9):
    1. initialize -> 200 SSE (7ms)
    2. notifications/initialized -> 202 (1ms)
    3. tools/list -> 200 SSE (22ms)
    4. tools/call [add] -> 200 SSE (18ms)
    ...

  Summary: 6 requests, 1 notification, 2 lifecycle
```

## Features

- **MCP-aware** — captures the full JSON-RPC lifecycle: `initialize`, capabilities, tool calls, notifications
- **Two-sided testing** — mock the server (replay) or mock the client (verify) from one recording
- **pytest plugin** — auto-activating fixtures for replay and verify, zero config
- **Zero code changes** — swap the server URL to record, that's it
- **Flexible matching** — method + params, sequential order, or strict equality
- **Explicit secret redaction** — opt-in flags to scrub URLs, env var values, and regex patterns
- **CI-ready** — exits non-zero on verify mismatches, fully headless
- **JSON cassettes** — human-readable, git-diff-friendly

## CLI Reference

### `mcp-recorder record`

| Option | Default | Description |
|---|---|---|
| `--target` | *(required)* | URL of the real MCP server |
| `--port` | `5555` | Local proxy port |
| `--output` | `recording.json` | Output cassette file path |
| `--verbose` | — | Log full headers and bodies to stderr |
| `--redact-server-url / --no-redact-server-url` | `true` | Strip URL path from metadata (keeps scheme + host) |
| `--redact-env VAR` | — | Redact named env var value from metadata + responses. Repeatable |
| `--redact-patterns REGEX` | — | Redact regex matches from metadata + responses. Repeatable |

### `mcp-recorder replay`

| Option | Default | Description |
|---|---|---|
| `--cassette` | *(required)* | Path to cassette file |
| `--port` | `5555` | Local server port |
| `--match` | `method_params` | Matching strategy (see below) |
| `--verbose` | — | Log every matched request to stderr |

### `mcp-recorder verify`

| Option | Default | Description |
|---|---|---|
| `--cassette` | *(required)* | Path to golden cassette file |
| `--target` | *(required)* | URL of the server to verify |
| `--ignore-fields` | — | Response fields to ignore during comparison |
| `--update` | — | Update the cassette with new responses (snapshot update) |
| `--verbose` | — | Show full diff for each failing interaction |

### `mcp-recorder inspect`

| Argument | Description |
|---|---|
| `CASSETTE` | Path to cassette file to inspect |

## Configuration

### Matching Strategies

| Strategy | Flag | Description |
|---|---|---|
| **Method + Params** | `method_params` | Match on JSON-RPC `method` and `params`, ignoring `_meta` (default) |
| **Sequential** | `sequential` | Return next unmatched interaction in recorded order |
| **Strict** | `strict` | Full structural equality of the request body including `_meta` |

### Secret Redaction

Redaction is explicit — no magic scanning, no hidden behavior. You control exactly what gets scrubbed.

**`--redact-server-url`** (enabled by default)

Strips the URL path from `metadata.server_url`, keeping only the scheme and host. Handles the common case of API keys in URLs like `https://mcp.firecrawl.dev/<key>/mcp`.

```bash
mcp-recorder record --target https://mcp.firecrawl.dev/$FIRECRAWL_KEY/mcp
# metadata shows: https://mcp.firecrawl.dev/[REDACTED]

mcp-recorder record --target http://localhost:8000 --no-redact-server-url
# metadata shows full URL
```

**`--redact-env VAR_NAME`**

Reads the named env var's value and replaces it in **metadata and response bodies**. Request bodies are never modified — this preserves replay and verify integrity.

```bash
mcp-recorder record \
  --target https://mcp.firecrawl.dev/$FIRECRAWL_KEY/mcp \
  --redact-env FIRECRAWL_KEY
```

**`--redact-patterns REGEX`**

For values not in environment variables. Same scope (metadata + responses only).

```bash
mcp-recorder record --target http://localhost:8000 \
  --redact-patterns "sk-[a-zA-Z0-9]+" \
  --redact-patterns "session-[0-9a-f]{32}"
```

HTTP headers (Authorization, Cookie, etc.) are not stored in cassettes — the proxy only captures JSON-RPC message bodies, so header secrets never reach the cassette file.

## Cassette Format

Cassettes store JSON-RPC messages at the protocol level:

```json
{
  "version": "1.0",
  "metadata": {
    "recorded_at": "2026-02-17T20:25:23Z",
    "server_url": "http://127.0.0.1:8000",
    "protocol_version": "2025-11-25",
    "server_info": { "name": "Test Calculator", "version": "2.14.5" }
  },
  "interactions": [
    {
      "type": "jsonrpc_request",
      "request": {
        "jsonrpc": "2.0", "id": 0, "method": "initialize",
        "params": { "protocolVersion": "2025-11-25", "capabilities": {} }
      },
      "response": {
        "jsonrpc": "2.0", "id": 0,
        "result": {
          "protocolVersion": "2025-11-25",
          "capabilities": { "tools": { "listChanged": true } },
          "serverInfo": { "name": "Test Calculator", "version": "2.14.5" }
        }
      },
      "response_is_sse": true,
      "response_status": 200,
      "latency_ms": 7
    }
  ]
}
```

## CI Integration

### GitHub Actions

With the pytest plugin (recommended):

```yaml
steps:
  - uses: actions/checkout@v4
  - uses: actions/setup-python@v5
    with:
      python-version: "3.12"
  - run: pip install mcp-recorder
  - run: pytest
```

Cassettes committed to the repo are replayed automatically. No server needed in CI.

For server regression testing:

```yaml
  - run: pip install mcp-recorder
  - run: mcp-recorder verify --cassette golden.json --target ${{ secrets.MCP_SERVER_URL }}
```

## Roadmap

- [ ] `stdio` transport — subprocess wrapping for local MCP servers
- [ ] WebSocket transport
- [ ] `mcp-recorder diff` — compare two cassettes for breaking changes
- [ ] TypeScript/JS cassette support — same JSON format, Vitest/Jest plugin

## Contributing

```bash
git clone https://github.com/devhelm/mcp-recorder.git
cd mcp-recorder
uv sync --group dev
uv run pytest
```

## License

MIT — see [LICENSE](LICENSE) for details.
