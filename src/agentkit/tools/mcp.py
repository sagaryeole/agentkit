"""Tools from Model Context Protocol servers. Needs the `agentkit[mcp]` extra.

Each tool a server offers becomes an ordinary kit `Tool`, so calls go through the same
approval, timeout, tracing and truncation path as any other.

A server is connected once and the connection is kept for the life of the process, so a
local server starts once and can keep state between calls. Connections live on one
background thread with its own event loop, because the code that calls a tool may run on
a different event loop each time (the chat command starts a new one per message), and a
connection can only be closed by the task that opened it.
"""

import asyncio
import atexit
import concurrent.futures
import contextlib
import json
import os
import re
import sys
import threading
from collections.abc import AsyncIterator, Coroutine
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

CLOSE_TIMEOUT_SECONDS = 5.0


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


def _reason(exc: BaseException) -> str:
    """The underlying causes of an error, which the SDK often wraps in exception groups."""
    if isinstance(exc, BaseExceptionGroup):
        return "; ".join(_reason(inner) for inner in exc.exceptions)
    return f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__


class _Connection:
    """One open session. A single task opens it, holds it and closes it."""

    def __init__(self, server: McpServer, debug: bool) -> None:
        self._server = server
        self._debug = debug
        self._closing = asyncio.Event()
        self._ready: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self.session: ClientSession | None = None
        self._task = asyncio.get_running_loop().create_task(self._hold())

    async def _hold(self) -> None:
        try:
            async with _session(self._server, self._debug) as session:
                self.session = session
                self._ready.set_result(None)
                await self._closing.wait()
        except Exception as exc:  # the SDK raises its own and grouped errors
            if not self._ready.done():
                self._ready.set_exception(exc)
        finally:
            self.session = None
            if not self._ready.done():
                self._ready.cancel()

    @property
    def alive(self) -> bool:
        return self.session is not None and not self._task.done()

    async def opened(self) -> ClientSession:
        await self._ready
        assert self.session is not None
        return self.session

    async def close(self) -> None:
        self._closing.set()
        _, pending = await asyncio.wait([self._task], timeout=CLOSE_TIMEOUT_SECONDS)
        for task in pending:
            task.cancel()


class _Connections:
    """Every open MCP connection of this process, on one background event loop."""

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._start = threading.Lock()
        # Touched only from the background loop:
        self._open: dict[str, _Connection] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def submit(self, work: Coroutine[Any, Any, Any]) -> concurrent.futures.Future[Any]:
        """Run `work` on the background loop, starting the loop on first use."""
        with self._start:
            if self._loop is None:
                self._loop = asyncio.new_event_loop()
                self._thread = threading.Thread(
                    target=self._loop.run_forever, name="agentkit-mcp", daemon=True
                )
                self._thread.start()
        return asyncio.run_coroutine_threadsafe(work, self._loop)

    async def request(
        self, name: str, server: McpServer, debug: bool, method: str, *args: Any
    ) -> Any:
        """Call a session method on the server's connection, connecting if needed.
        A failed call drops the connection, so the next call starts a fresh one."""
        key = f"{name}:{server.model_dump_json()}"
        async with self._locks.setdefault(key, asyncio.Lock()):
            connection = self._open.get(key)
            if connection is None or not connection.alive:
                connection = self._open[key] = _Connection(server, debug)
            try:
                session = await connection.opened()
            except BaseException:
                self._open.pop(key, None)
                raise
        try:
            return await getattr(session, method)(*args)
        except Exception:
            await self._drop(key)
            raise
        finally:
            if not server.keep_alive:
                await self._drop(key)

    async def _drop(self, key: str) -> None:
        connection = self._open.pop(key, None)
        if connection is not None:
            await connection.close()

    async def _close_all(self) -> None:
        for key in list(self._open):
            await self._drop(key)

    def close(self) -> None:
        """Close every connection and stop the background loop. Safe to call twice."""
        with self._start:
            loop, thread = self._loop, self._thread
            self._loop = self._thread = None
        if loop is None or thread is None:
            return
        # Shutting down: a server that will not close cleanly is left to the operating system.
        with contextlib.suppress(Exception):
            asyncio.run_coroutine_threadsafe(self._close_all(), loop).result(
                timeout=2 * CLOSE_TIMEOUT_SECONDS
            )
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=CLOSE_TIMEOUT_SECONDS)
        self._open.clear()
        self._locks.clear()


_CONNECTIONS = _Connections()
atexit.register(_CONNECTIONS.close)


def close_mcp_connections() -> None:
    """Close every MCP connection now, stopping the local servers this process started.
    They are also closed when the process exits."""
    _CONNECTIONS.close()


def _tool(server_name: str, server: McpServer, remote: Any, debug: bool) -> Tool:
    remote_name = str(remote.name)

    async def call(**arguments: Any) -> str:
        work = _CONNECTIONS.request(server_name, server, debug, "call_tool", remote_name, arguments)
        try:
            # Awaiting through wrap_future means a timeout here also cancels the call there.
            result = await asyncio.wrap_future(_CONNECTIONS.submit(work))
        except Exception as exc:
            raise ToolFailure(
                "transient",
                f"The MCP server '{server_name}' could not complete the call ({_reason(exc)}). "
                "Retry.",
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


def load_mcp_tools(server_name: str, server: McpServer, debug: bool = False) -> list[Tool]:
    """Connect to a server and return its tools. Callable with or without a running loop."""
    work = _CONNECTIONS.request(server_name, server, debug, "list_tools")
    try:
        listed = _CONNECTIONS.submit(work).result()
    except Exception as exc:
        where = " ".join(server.command) if server.command else server.url
        raise AgentKitError(
            f"The MCP server '{server_name}' ({where}) could not be reached: {_reason(exc)}. Check "
            f"tools.mcp_servers.{server_name}, or remove it from the config."
        ) from exc
    return [_tool(server_name, server, remote, debug) for remote in listed.tools]
