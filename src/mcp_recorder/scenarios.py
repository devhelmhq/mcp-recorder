"""YAML scenario parsing, validation, and execution for record-scenarios."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, model_validator

from mcp_recorder._types import Cassette, CassetteMetadata
from mcp_recorder._utils import UvicornServer, find_free_port, save_cassette
from mcp_recorder.mcp_client import McpClient
from mcp_recorder.proxy import create_proxy_app
from mcp_recorder.scrubber import scrub_cassette

logger = logging.getLogger("mcp_recorder.scenarios")

SCENARIOS_FORMAT_VERSION = "1.0"

# ---------------------------------------------------------------------------
# YAML schema models
# ---------------------------------------------------------------------------


class ToolCallAction(BaseModel):
    name: str
    arguments: dict[str, Any] = {}


class PromptGetAction(BaseModel):
    name: str
    arguments: dict[str, Any] = {}


class ResourceReadAction(BaseModel):
    uri: str


class RedactConfig(BaseModel):
    server_url: bool = True
    env: list[str] = Field(default_factory=list)
    patterns: list[str] = Field(default_factory=list)


class Scenario(BaseModel):
    description: str = ""
    actions: list[str | dict[str, Any]]


class ScenariosFile(BaseModel):
    schema_version: str = SCENARIOS_FORMAT_VERSION
    target: str
    redact: RedactConfig = Field(default_factory=RedactConfig)
    scenarios: dict[str, Scenario]

    @model_validator(mode="after")
    def _check_schema_version(self) -> ScenariosFile:
        expected_major = SCENARIOS_FORMAT_VERSION.split(".")[0]
        actual_major = self.schema_version.split(".")[0]
        if actual_major != expected_major:
            raise ValueError(
                f"Incompatible scenarios schema version '{self.schema_version}' "
                f"(expected {expected_major}.x). "
                f"Update mcp-recorder or fix the schema_version field."
            )
        return self


# ---------------------------------------------------------------------------
# Supported actions
# ---------------------------------------------------------------------------

_SIMPLE_ACTIONS = frozenset({"list_tools", "list_prompts", "list_resources"})
_PARAMETERIZED_ACTIONS = frozenset({"call_tool", "get_prompt", "read_resource"})
_ALL_ACTIONS = _SIMPLE_ACTIONS | _PARAMETERIZED_ACTIONS


async def _execute_action(client: McpClient, action: str | dict[str, Any]) -> None:
    """Execute a single scenario action against the MCP client."""
    if isinstance(action, str):
        if action not in _SIMPLE_ACTIONS:
            supported = ", ".join(sorted(_ALL_ACTIONS))
            raise ValueError(f"Unknown action '{action}'. Supported actions: {supported}")
        await getattr(client, action)()
        return

    if not isinstance(action, dict) or len(action) != 1:
        raise ValueError(f"Parameterized action must be a single-key dict, got: {action}")

    action_name = next(iter(action))
    params = action[action_name]

    if action_name not in _PARAMETERIZED_ACTIONS:
        supported = ", ".join(sorted(_ALL_ACTIONS))
        raise ValueError(f"Unknown action '{action_name}'. Supported actions: {supported}")

    if action_name == "call_tool":
        parsed = ToolCallAction.model_validate(params)
        await client.call_tool(parsed.name, parsed.arguments)
    elif action_name == "get_prompt":
        parsed_prompt = PromptGetAction.model_validate(params)
        await client.get_prompt(parsed_prompt.name, parsed_prompt.arguments)
    elif action_name == "read_resource":
        parsed_resource = ResourceReadAction.model_validate(params)
        await client.read_resource(parsed_resource.uri)


# ---------------------------------------------------------------------------
# Scenario runner
# ---------------------------------------------------------------------------


async def _run_single_scenario(
    name: str,
    scenario: Scenario,
    target_url: str,
    output_path: Path,
    redact: RedactConfig,
    verbose: bool,
) -> int:
    """Record one scenario. Returns the number of interactions captured."""
    cassette = Cassette(metadata=CassetteMetadata(server_url=target_url))
    app = create_proxy_app(target_url=target_url, cassette=cassette, verbose=verbose)

    port = find_free_port()
    server = UvicornServer(app, port)
    server.start()

    try:
        proxy_url = f"http://127.0.0.1:{port}"
        async with McpClient(proxy_url) as client:
            await client.initialize()
            for action in scenario.actions:
                await _execute_action(client, action)
    finally:
        server.stop()

    cassette = scrub_cassette(
        cassette,
        redact_server_url=redact.server_url,
        redact_env=tuple(redact.env),
        redact_patterns=tuple(redact.patterns),
    )

    save_cassette(cassette, output_path)

    count = len(cassette.interactions)
    logger.info("  %s -> %s (%d interactions)", name, output_path.name, count)
    return count


def load_scenarios_file(path: Path) -> ScenariosFile:
    """Parse and validate a YAML scenarios file."""
    raw = yaml.safe_load(path.read_text())
    if not isinstance(raw, dict):
        raise ValueError(f"Scenarios file must be a YAML mapping, got {type(raw).__name__}")
    return ScenariosFile.model_validate(raw)


def run_scenarios(
    scenarios_file: ScenariosFile,
    output_dir: Path,
    *,
    verbose: bool = False,
) -> dict[str, int]:
    """Run all scenarios and return {name: interaction_count} for each."""
    results: dict[str, int] = {}
    for name, scenario in scenarios_file.scenarios.items():
        output_path = output_dir / f"{name}.json"
        count = asyncio.run(
            _run_single_scenario(
                name=name,
                scenario=scenario,
                target_url=scenarios_file.target,
                output_path=output_path,
                redact=scenarios_file.redact,
                verbose=verbose,
            )
        )
        results[name] = count
    return results
