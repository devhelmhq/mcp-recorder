"""Minimal FastMCP server for manual and integration testing."""

from fastmcp import FastMCP

mcp = FastMCP("Test Calculator")


@mcp.tool
def add(a: int, b: int) -> int:
    """Add two numbers."""
    return a + b


@mcp.tool
def multiply(a: float, b: float) -> float:
    """Multiply two numbers."""
    return a * b


@mcp.tool
def echo(message: str) -> str:
    """Echo back the input message."""
    return message


@mcp.tool
def get_weather(city: str) -> dict:
    """Get fake weather data for a city."""
    return {"city": city, "temp_c": 22, "condition": "sunny", "humidity": 45}


if __name__ == "__main__":
    mcp.run(transport="http", host="127.0.0.1", port=8000)
