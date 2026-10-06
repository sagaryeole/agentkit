"""Tools from Model Context Protocol servers. Needs the `agentkit[mcp]` extra.

Each tool a server offers becomes an ordinary kit `Tool`, so calls go through the same
approval, timeout, tracing and truncation path as any other. A connection is opened for
each call and closed after it: simple and safe across event loops, at the price of
restarting a local server per call and of servers not keeping state between calls.
"""

import asyncio
import concurrent.futures
import json
import os
import re
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import get_default_environment, stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client
from pydantic import BaseModel, ConfigDict, create_model

from agentkit.config.schema import McpServer
from agentkit.errors import AgentKitError
from agentkit.tools.base import Tool, ToolFailure


@asynccontextmanager
async def _session(server: McpServer, debug: bool) -> AsyncIterator[ClientSession]:
    if server.command:
        # A local server sees a minimal environment plus the variables it is granted.
        env = get_default_environment()
        env.update({name: os.environ[name] for name in server.pass_env if name in os.environ})
        params = StdioServerParameters(command=server.command[0], args=server.command[1:], env=env)
        with open(os.devnull, "w") as quiet:
            errlog = sys.stderr if debug else quiet
            async with (
                stdio_client(params, errlog=errlog) as streams,
                ClientSession(streams[0], streams[1]) as session,
            ):
                await session.initialize()
                yield session
    else:
        headers = {name: os.environ.get(var, "") for name, var in server.headers_env.items()}
        async with (
            create_mcp_http_client(headers=headers) as http,
            streamable_http_client(str(server.url), http_client=http) as streams,
            ClientSession(streams[0], streams[1]) as session,
        ):
            await session.initialize()
            yield session


def _tool(server_name: str, server: McpServer, remote: Any, debug: bool) -> Tool:
    remote_name = str(remote.name)

    async def call(**arguments: Any) -> str:
        try:
            async with _session(server, debug) as session:
                result = await session.call_tool(remote_name, arguments)
        except Exception as exc:  # the SDK raises its own and grouped errors
            raise ToolFailure(
                "transient",
                f"The MCP server '{server_name}' could not complete the call ({exc}). Retry.",
                retryable=True,
            ) from exc
        content = getattr(result, "content", None) or []
        text = "\n".join(part.text for part in content if getattr(part, "type", "") == "text")
        structured = getattr(result, "structured_content", None)
        if not text and structured is not None:
            text = json.dumps(structured)
        if getattr(result, "is_error", False):
            raise ToolFailure(
                "internal", f"{remote_name} reported an error: {text or 'no details given'}"
            )
        return text or "(the tool returned no text)"

    name = re.sub(r"[^a-z0-9_]", "_", f"{server_name}_{remote_name}".lower())
    anything: type[BaseModel] = create_model(f"{name}_input", __config__=ConfigDict(extra="allow"))
    return Tool(
        name=name,
        description=remote.description or f"The {remote_name} tool of the {server_name} server.",
        fn=call,
        input_model=anything,
        # Only configuration can say a tool is harmless; a server's own hints are not trusted.
        side_effects=server.side_effects.get(remote_name, "write"),
        idempotent=False,
        timeout_seconds=server.timeout_seconds,
        tags=("mcp", server_name),
        untrusted_output=True,
        wants_ctx=False,
        json_schema=dict(remote.input_schema or {"type": "object"}),
    )


async def list_mcp_tools(server_name: str, server: McpServer, debug: bool = False) -> list[Tool]:
    try:
        async with _session(server, debug) as session:
            listed = await session.list_tools()
    except Exception as exc:
        where = " ".join(server.command) if server.command else server.url
        raise AgentKitError(
            f"The MCP server '{server_name}' ({where}) could not be reached: {exc}. Check "
            f"tools.mcp_servers.{server_name}, or remove it from the config."
        ) from exc
    return [_tool(server_name, server, remote, debug) for remote in listed.tools]


def load_mcp_tools(server_name: str, server: McpServer, debug: bool = False) -> list[Tool]:
    """`list_mcp_tools` for synchronous callers, whether or not an event loop is running."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, list_mcp_tools(server_name, server, debug)).result()
