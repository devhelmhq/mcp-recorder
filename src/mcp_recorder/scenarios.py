"""YAML scenario parsing, validation, and execution for record-scenarios."""

from __future__ import annotations

import asyncio
import logging
import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, model_validator

from mcp_recorder._types import Cassette, CassetteMetadata
from mcp_recorder._utils import UvicornServer, find_free_port, save_cassette
from mcp_recorder.mcp_client import McpClient
from mcp_recorder.proxy import create_proxy_app
from mcp_recorder.scrubber import scrub_cassette
from mcp_recorder.transport import StdioTransport

logger = logging.getLogger("mcp_recorder.scenarios")

SCENARIOS_FORMAT_VERSION = "1.0"

# ---------------------------------------------------------------------------
# Environment variable interpolation
# ---------------------------------------------------------------------------

# Matches ${VAR} and ${VAR:-default}
_ENV_VAR_PATTERN = re.compile(r"\$\{([^}:]+)(?::-((?:[^}\\]|\\.)*)?)?\}")


def _expand_env_vars(obj: Any) -> Any:
    """Recursively expand ``${VAR}`` and ``${VAR:-default}`` in string values."""
    if isinstance(obj, str):

        def _replace(m: re.Match[str]) -> str:
            name = m.group(1).strip()
            default = m.group(2)
            value = os.environ.get(name)
            if value is None and default is None:
                raise ValueError(
                    f"Environment variable '{name}' is not set "
                    f"and no default was provided (referenced as ${{{name}}})"
                )
            return value if value is not None else default

        return _ENV_VAR_PATTERN.sub(_replace, obj)
    if isinstance(obj, dict):
        return {k: _expand_env_vars(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_expand_env_vars(item) for item in obj]
    return obj


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


class StdioTargetConfig(BaseModel):
    """Stdio target configuration for scenarios YAML."""

    command: str
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    cwd: str | None = None


class Scenario(BaseModel):
    description: str = ""
    actions: list[str | dict[str, Any]]


class ScenariosFile(BaseModel):
    schema_version: str = SCENARIOS_FORMAT_VERSION
    target: str | StdioTargetConfig
    redact: RedactConfig = Field(default_factory=RedactConfig)
    scenarios: dict[str, Scenario]

    @model_validator(mode="before")
    @classmethod
    def _coerce_target(cls, data: Any) -> Any:
        """Distinguish HTTP URL strings from stdio config dicts."""
        if isinstance(data, dict) and "target" in data:
            target = data["target"]
            if isinstance(target, dict) and "command" in target:
                data["target"] = StdioTargetConfig.model_validate(target)
        return data

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


def _resolve_target(
    target: str | StdioTargetConfig,
) -> tuple[str, str | None, StdioTransport | None]:
    """Return ``(server_url, target_url_or_none, transport_or_none)``."""
    if isinstance(target, StdioTargetConfig):
        cmd_display = f"{target.command} {' '.join(target.args)}".strip()
        server_url = f"stdio://{cmd_display}"
        transport = StdioTransport(
            command=target.command,
            args=target.args,
            env=target.env or None,
            cwd=target.cwd,
        )
        return server_url, None, transport
    return target, target, None


async def _run_single_scenario(
    name: str,
    scenario: Scenario,
    target: str | StdioTargetConfig,
    output_path: Path,
    redact: RedactConfig,
    verbose: bool,
) -> int:
    """Record one scenario. Returns the number of interactions captured."""
    server_url, target_url, transport = _resolve_target(target)

    metadata = CassetteMetadata(
        server_url=server_url,
        transport_type="stdio" if transport else "http",
    )
    cassette = Cassette(metadata=metadata)

    if transport:
        app = create_proxy_app(cassette=cassette, transport=transport, verbose=verbose)
    else:
        app = create_proxy_app(cassette=cassette, target_url=target_url, verbose=verbose)

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
    """Parse and validate a YAML scenarios file.

    ``${VAR}`` and ``${VAR:-default}`` references in string values are expanded
    from the current environment before validation.
    """
    raw = yaml.safe_load(path.read_text())
    if not isinstance(raw, dict):
        raise ValueError(f"Scenarios file must be a YAML mapping, got {type(raw).__name__}")
    raw = _expand_env_vars(raw)
    return ScenariosFile.model_validate(raw)


def run_scenarios(
    scenarios_file: ScenariosFile,
    output_dir: Path,
    *,
    scenario_names: tuple[str, ...] = (),
    verbose: bool = False,
) -> dict[str, int]:
    """Run scenarios and return {name: interaction_count} for each.

    If scenario_names is non-empty, only the named scenarios are recorded.
    """
    to_run = scenarios_file.scenarios
    if scenario_names:
        unknown = set(scenario_names) - set(to_run)
        if unknown:
            available = ", ".join(sorted(to_run))
            raise ValueError(
                f"Unknown scenario(s): {', '.join(sorted(unknown))}. Available: {available}"
            )
        to_run = {k: v for k, v in to_run.items() if k in scenario_names}

    results: dict[str, int] = {}
    for name, scenario in to_run.items():
        output_path = output_dir / f"{name}.json"
        count = asyncio.run(
            _run_single_scenario(
                name=name,
                scenario=scenario,
                target=scenarios_file.target,
                output_path=output_path,
                redact=scenarios_file.redact,
                verbose=verbose,
            )
        )
        results[name] = count
    return results
