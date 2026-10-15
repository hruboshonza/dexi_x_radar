"""stdio bridge: Claude Code <-stdio-> this process <-HTTPS-> the deployed radar.

Some antivirus web filters cut off HTTP(S) responses to MCP requests from certain programs
(see README, Troubleshooting). Over stdio nothing crosses the network on the Claude side,
and this Python process reaches the radar normally. Each call opens a fresh connection, so a
redeploy of the radar never leaves the bridge holding a dead session.
"""
from contextlib import asynccontextmanager

from mcp import ClientSession, types
from mcp.client.streamable_http import streamablehttp_client
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server


@asynccontextmanager
async def _remote(url: str, token: str):
    async with streamablehttp_client(url, headers={"Authorization": f"Bearer {token}"}) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


async def run_bridge(url: str, token: str) -> None:
    server = Server("dexi-x-radar")

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        async with _remote(url, token) as remote:
            return (await remote.list_tools()).tools

    # Input is validated by the radar itself; the bridge only forwards.
    @server.call_tool(validate_input=False)
    async def call_tool(name: str, arguments: dict) -> types.CallToolResult:
        async with _remote(url, token) as remote:
            return await remote.call_tool(name, arguments)

    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())
