"""Scripted MCP client that exercises the full protocol lifecycle through the proxy."""

import asyncio
import sys

from fastmcp import Client


async def main() -> None:
    proxy_url = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:5555/mcp"
    print(f"Connecting to {proxy_url}\n")

    async with Client(proxy_url) as client:
        # initialize already happened inside the context manager
        server_info = client.initialize_result.serverInfo
        print(f"Server: {server_info.name}")
        print(f"Capabilities: {client.initialize_result.capabilities}\n")

        # List tools
        tools = await client.list_tools()
        print(f"Available tools ({len(tools)}): {[t.name for t in tools]}\n")

        # Call each tool
        r1 = await client.call_tool("add", {"a": 2, "b": 3})
        print(f"add(2, 3) = {r1.data}")

        r2 = await client.call_tool("multiply", {"a": 3.5, "b": 2.0})
        print(f"multiply(3.5, 2.0) = {r2.data}")

        r3 = await client.call_tool("echo", {"message": "hello mcp-recorder"})
        print(f"echo('hello mcp-recorder') = {r3.data}")

        r4 = await client.call_tool("get_weather", {"city": "London"})
        print(f"get_weather('London') = {r4.data}")

    print("\nDone. All interactions completed.")


if __name__ == "__main__":
    asyncio.run(main())
