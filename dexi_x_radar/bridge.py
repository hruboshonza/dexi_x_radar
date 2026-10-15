"""stdio bridge: Claude Code <-stdio-> this process <-HTTPS JSON-RPC-> the deployed radar.

Why it exists: on the owner's PC something (Norton is running) inspects MCP traffic and holds
back responses. It keys on the `/mcp` path and the `mcp-protocol-version` header, and it holds
write tools (radar_skip, radar_save_drafts) while reads pass. Over stdio nothing crosses the
network on the Claude side.

Toward the radar, the bridge speaks plain JSON-RPC without that header. The radar runs
stateless with JSON responses, so `tools/list` and `tools/call` work as single POSTs and need no
session or initialize step.
"""
import itertools

import httpx
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

_ids = itertools.count(1)


class RemoteError(RuntimeError):
    pass


async def rpc(http: httpx.AsyncClient, url: str, token: str, method: str, params: dict) -> dict:
    resp = await http.post(
        url,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json, text/event-stream",
                 "Content-Type": "application/json"},
        json={"jsonrpc": "2.0", "id": next(_ids), "method": method, "params": params},
    )
    if resp.status_code != 200:
        raise RemoteError(f"radar answered HTTP {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    if "error" in data:
        raise RemoteError(f"radar error: {data['error'].get('message')}")
    return data["result"]


async def run_bridge(url: str, token: str) -> None:
    server = Server("dexi-x-radar")

    async with httpx.AsyncClient(timeout=60) as http:

        @server.list_tools()
        async def list_tools() -> list[types.Tool]:
            result = await rpc(http, url, token, "tools/list", {})
            return [types.Tool.model_validate(t) for t in result["tools"]]

        # Input is validated by the radar itself; the bridge only forwards.
        @server.call_tool(validate_input=False)
        async def call_tool(name: str, arguments: dict) -> types.CallToolResult:
            result = await rpc(http, url, token, "tools/call", {"name": name, "arguments": arguments})
            return types.CallToolResult.model_validate(result)

        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())
