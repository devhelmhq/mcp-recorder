# mcp-recorder

**VCR.py for MCP servers.** Record, replay, and verify Model Context Protocol interactions for deterministic testing.

[![PyPI version](https://img.shields.io/pypi/v/mcp-recorder.svg)](https://pypi.org/project/mcp-recorder/)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://python.org)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![CI](https://github.com/devhelmhq/mcp-recorder/actions/workflows/check.yml/badge.svg)](https://github.com/devhelmhq/mcp-recorder/actions)

MCP servers break silently. Tool schemas change, prompts drift, responses shift. Without wire-level regression tests, you find out from your users. mcp-recorder captures the full protocol exchange into a cassette file and lets you test from both sides.

## Record. Replay. Verify.

```bash
# 1. Record cassettes from a scenarios file (zero code)
mcp-recorder record-scenarios scenarios.yml

# 2. Replay as a mock server — test your client without the real server
mcp-recorder replay --cassette cassettes/golden.json

# 3. Verify your server hasn't regressed — compare responses to the recording
mcp-recorder verify --cassette cassettes/golden.json --target http://localhost:8000
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

## Scenarios (zero-code recording)

Define what to test in a YAML file. No Python scripts, no boilerplate — works with MCP servers written in any language.

```yaml
schema_version: "1.0"

target: http://localhost:3000

redact:
  server_url: true
  env:
    - API_KEY

scenarios:
  tools_and_schemas:
    description: "Discover tools and call search"
    actions:
      - list_tools
      - call_tool:
          name: search
          arguments:
            query: "test"

  error_handling:
    description: "Invalid inputs return proper errors"
    actions:
      - call_tool:
          name: search
          arguments: {}
```

Record all scenarios at once:

```bash
mcp-recorder record-scenarios scenarios.yml
```

This produces `cassettes/tools_and_schemas.json` and `cassettes/error_handling.json`. Each scenario gets its own cassette. Protocol handshake (`initialize` + `notifications/initialized`) is handled automatically.

Supported actions:

| Action | Description |
|---|---|
| `list_tools` | Call `tools/list` |
| `call_tool` | Call `tools/call` with `name` and `arguments` |
| `list_prompts` | Call `prompts/list` |
| `get_prompt` | Call `prompts/get` with `name` and optional `arguments` |
| `list_resources` | Call `resources/list` |
| `read_resource` | Call `resources/read` with `uri` |

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

## Python API

For programmatic recording from Python code:

```python
from mcp_recorder import RecordSession

async with RecordSession(
    target="http://localhost:8000",
    output="golden.json",
) as client:
    await client.list_tools()
    await client.call_tool("add", {"a": 2, "b": 3})
```

`RecordSession` starts a recording proxy, runs `initialize` automatically, and saves the cassette on exit. Supports all redaction options (`redact_server_url`, `redact_env`, `redact_patterns`).

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

### Recording (interactive)

For manual recording, start the proxy pointing at your MCP server:

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

For automated recording from a scenarios file, see [Scenarios](#scenarios-zero-code-recording) above.

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

When a change is intentional, update the cassette:

```bash
mcp-recorder verify --cassette golden.json --target http://localhost:8000 --update
```

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
- **YAML scenarios** — define test actions declaratively, no code required
- **Python API** — `RecordSession` context manager for programmatic recording
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

### `mcp-recorder record-scenarios`

| Argument / Option | Default | Description |
|---|---|---|
| `SCENARIOS_FILE` | *(required)* | Path to YAML scenarios file |
| `--output-dir` | `cassettes/` next to file | Output directory for cassettes |
| `--verbose` | — | Log full request/response details to stderr |

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
| `--ignore-fields` | — | Response fields to ignore during comparison. Repeatable |
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

In scenarios files, redaction is configured in the `redact` block and applies to all cassettes from that file.

HTTP headers (Authorization, Cookie, etc.) are not stored in cassettes — the proxy only captures JSON-RPC message bodies, so header secrets never reach the cassette file.

## Scenarios Format

Scenarios files use YAML with a versioned schema:

```yaml
schema_version: "1.0"

target: http://localhost:3000

redact:
  server_url: true
  env:
    - API_KEY
  patterns:
    - "sk-[a-zA-Z0-9]+"

scenarios:
  basic_flow:
    description: "Tool discovery and invocation"
    actions:
      - list_tools
      - call_tool:
          name: search
          arguments:
            query: "example"

  resources:
    description: "List and read resources"
    actions:
      - list_resources
      - read_resource:
          uri: file:///config.json
```

Each scenario key becomes the cassette filename (`basic_flow` -> `basic_flow.json`). The `schema_version` field is validated on load — incompatible versions produce a clear error.

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

Using scenarios and verify (recommended for any language):

```yaml
steps:
  - uses: actions/checkout@v4
  - uses: actions/setup-python@v5
    with:
      python-version: "3.12"
  - run: pip install mcp-recorder

  # Start your MCP server
  - run: npm start &
  - run: sleep 5

  # Verify cassettes against the live server
  - run: |
      mcp-recorder verify \
        --cassette integration/cassettes/tools_and_schemas.json \
        --target http://localhost:3000
```

With the pytest plugin (Python projects):

```yaml
steps:
  - uses: actions/checkout@v4
  - uses: actions/setup-python@v5
    with:
      python-version: "3.12"
  - run: pip install mcp-recorder
  - run: pytest
```

Cassettes committed to the repo are replayed automatically. No server needed in CI for replay mode.

## Roadmap

- [ ] `stdio` transport — subprocess wrapping for local MCP servers
- [ ] WebSocket transport
- [ ] `mcp-recorder diff` — compare two cassettes for breaking changes
- [ ] TypeScript/JS cassette support — same JSON format, Vitest/Jest plugin

## Contributing

```bash
git clone https://github.com/devhelmhq/mcp-recorder.git
cd mcp-recorder
uv sync --group dev
uv run pytest
```

## License

MIT — see [LICENSE](LICENSE) for details.
