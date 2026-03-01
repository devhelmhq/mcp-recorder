"""mcp-recorder: Record and replay MCP server interactions for deterministic testing."""

from importlib.metadata import version

from mcp_recorder.mcp_client import McpClient, RecordSession

__all__ = ["McpClient", "RecordSession", "__version__"]
__version__ = version("mcp-recorder")
