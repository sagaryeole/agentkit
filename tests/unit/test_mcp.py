"""MCP tools, against a real MCP server started as a local subprocess. No network."""

import asyncio
import os
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from agentkit.cli.main import app
from agentkit.config import Config
from agentkit.config.schema import McpServer
from agentkit.errors import AgentKitError, ConfigError
from agentkit.guardrails.approvals import AutoApprove
from agentkit.observability.tracer import read_trace
from agentkit.runtime.context import RunContext
from agentkit.runtime.executor import execute_tool
from agentkit.runtime.spec import AgentSpec
from agentkit.tools import ToolRegistry
from agentkit.tools.mcp import close_mcp_connections
from tests.unit.helpers import config, scripted_adapter, tool_calls

SERVER = '''
import os
from mcp.server.mcpserver import MCPServer

server = MCPServer("glossary")


@server.tool()
def lookup(word: str) -> str:
    """Look up a word in the glossary."""
    if word == "missing":
        raise ValueError("no such word")
    return f"{word}: a definition. " + "x" * int(os.environ.get("PADDING", "0"))


@server.tool(name="store-Entry")
def store(key: str, value: int = 1) -> str:
    """Store a value."""
    return f"stored {key}={value} token={os.environ.get('GLOSSARY_TOKEN', 'none')}"


CALLS = 0


@server.tool()
def count() -> str:
    """Count the calls this server process has seen."""
    global CALLS
    CALLS += 1
    return f"{CALLS} in process {os.getpid()}"


@server.tool()
async def nap(seconds: float) -> str:
    """Sleep."""
    import asyncio
    await asyncio.sleep(seconds)
    return "rested"


@server.tool()
def die() -> str:
    """Kill the server."""
    os._exit(1)


if __name__ == "__main__":
    server.run("stdio")
'''


@pytest.fixture
def server(tmp_path: Path) -> dict[str, Any]:
    script = tmp_path / "glossary_server.py"
    script.write_text(SERVER)
    return {"command": [sys.executable, str(script)], "side_effects": {"lookup": "read_external"}}


def with_server(tmp_path: Path, server: dict[str, Any], **tools: Any) -> Config:
    return config(tmp_path, tools={"mcp_servers": {"glossary": server}, **tools})


def test_mcp_tools_join_the_registry_with_safe_defaults(
    tmp_path: Path, server: dict[str, Any]
) -> None:
    registry = ToolRegistry.from_config(with_server(tmp_path, server))
    lookup = registry.get("glossary_lookup")
    store = registry.get("glossary_store_entry")  # names are made snake_case

    assert lookup.description == "Look up a word in the glossary."
    assert lookup.spec.input_schema["required"] == ["word"]
    assert lookup.spec.input_schema["properties"]["word"]["type"] == "string"
    assert lookup.side_effects == "read_external"  # declared in the config
    assert store.side_effects == "write"  # not declared, so treated as risky
    assert lookup.untrusted_output and store.untrusted_output
    assert lookup.tags == ("mcp", "glossary")
    assert registry.select(tags=["mcp"])[:2] == [lookup, store]


async def test_mcp_calls_take_the_same_path_as_any_tool(
    tmp_path: Path, server: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GLOSSARY_TOKEN", "s3cret-token-value")
    monkeypatch.setenv("PADDING", "500")
    server = {**server, "pass_env": ["PADDING"], "timeout_seconds": 30}
    cfg = with_server(tmp_path, server, max_output_chars=200)
    registry = ToolRegistry.from_config(cfg)
    lookup, store = registry.get("glossary_lookup"), registry.get("glossary_store_entry")
    ctx = RunContext(cfg)

    found = await execute_tool(lookup, {"word": "agent"}, ctx)
    assert found.error is None and found.output.startswith("agent: a definition.")
    assert found.truncated  # truncation applies
    assert found.content.startswith('<untrusted_content source="glossary_lookup">')

    # arguments are validated against the server's schema before anything is sent
    invalid = await execute_tool(lookup, {"wrong": 1}, ctx)
    assert invalid.error is not None and invalid.error.kind == "invalid_input"
    assert "'word' is a required property" in invalid.error.message

    failed = await execute_tool(lookup, {"word": "missing"}, ctx)
    assert failed.error is not None and "reported an error" in failed.error.message

    # the undeclared tool needs approval, and is denied in an unattended run
    denied = await execute_tool(store, {"key": "a"}, ctx)
    assert denied.error is not None and denied.error.kind == "permission"
    approved = await execute_tool(
        store, {"key": "a", "value": 2}, RunContext(cfg, approvals=AutoApprove("dev"))
    )
    # the server only sees environment variables it was granted
    assert approved.output == "stored a=2 token=none" and approved.approval == "approved"

    spans = read_trace(Path(ctx.tracer.path or ""))
    assert [s["name"] for s in spans] == ["glossary_lookup"] * 3 + ["glossary_store_entry"]


async def test_an_agent_uses_an_mcp_tool(tmp_path: Path, server: dict[str, Any]) -> None:
    cfg = with_server(tmp_path, server)
    registry = ToolRegistry.from_config(cfg)
    spec = AgentSpec(name="a", description="d", instructions="Define.", tools=["glossary_lookup"])
    script = [tool_calls(("glossary_lookup", {"word": "agent"})), "An agent is a definition."]
    adapter, model = scripted_adapter(script, *registry)
    result = await adapter.run(spec, "Define agent", RunContext(cfg))
    assert result.stop_reason == "completed" and result.tool_calls == 1
    assert "agent: a definition" in model.requests[1].messages[-1].content[0].content  # type: ignore[union-attr]


def test_mcp_tools_show_up_in_the_cli(
    tmp_path: Path, server: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AGENTKIT_ENV", raising=False)
    command = ", ".join(f'"{part}"' for part in server["command"])
    (tmp_path / "agentkit.yaml").write_text(
        "tools:\n  mcp_servers:\n    glossary:\n"
        f"      command: [{command}]\n      side_effects: {{lookup: none}}\n"
    )
    runner = CliRunner()
    listed = runner.invoke(app, ["tools", "list", "--project-dir", str(tmp_path)])
    assert listed.exit_code == 0, listed.output
    assert "glossary_lookup" in listed.output and "glossary_store_entry" in listed.output
    called = runner.invoke(
        app,
        [
            "tools",
            "call",
            "glossary_lookup",
            "--args",
            '{"word": "x"}',
            "--project-dir",
            str(tmp_path),
        ],
    )
    assert called.exit_code == 0 and "x: a definition" in called.output


def test_mcp_configuration_errors(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="exactly one of `command` or `url`"):
        config(tmp_path, tools={"mcp_servers": {"bad": {}}})
    with pytest.raises(ConfigError, match="exactly one"):
        config(tmp_path, tools={"mcp_servers": {"bad": {"command": ["x"], "url": "http://x"}}})

    broken = {"command": [sys.executable, "-c", "raise SystemExit(1)"]}
    with pytest.raises(AgentKitError, match="MCP server 'gone' .* could not be reached"):
        ToolRegistry.from_config(config(tmp_path, tools={"mcp_servers": {"gone": broken}}))

    registry = ToolRegistry()
    with pytest.raises(AgentKitError, match="No MCP server named 'docs' is configured"):
        registry.load_mcp("docs")


def test_a_server_can_be_loaded_in_code(tmp_path: Path, server: dict[str, Any]) -> None:
    registry = ToolRegistry()
    tools = registry.load_mcp("glossary", McpServer(**server))
    assert [t.name for t in tools][:2] == ["glossary_lookup", "glossary_store_entry"]


# persistent connections


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def counted(result: Any) -> tuple[int, int]:
    """(call number, server process id) from the count tool's reply."""
    number, _, pid = str(result.output).partition(" in process ")
    return int(number), int(pid)


def free(server: dict[str, Any]) -> dict[str, Any]:
    harmless = {name: "none" for name in ("lookup", "count", "nap", "die")}
    return {**server, "side_effects": harmless}


def test_one_connection_serves_every_call_across_event_loops(
    tmp_path: Path, server: dict[str, Any]
) -> None:
    cfg = with_server(tmp_path, free(server))
    count = ToolRegistry.from_config(cfg).get("glossary_count")

    async def once() -> tuple[int, int]:
        return counted(await execute_tool(count, {}, RunContext(cfg)))

    # each asyncio.run is a new event loop, as each message of `agentkit chat` is
    first, second, third = (asyncio.run(once()) for _ in range(3))
    assert [first[0], second[0], third[0]] == [1, 2, 3]  # the server kept its state
    assert first[1] == second[1] == third[1]  # and is the same process

    # a second registry for the same server reuses the connection
    again = ToolRegistry.from_config(cfg).get("glossary_count")
    fourth = counted(asyncio.run(execute_tool(again, {}, RunContext(cfg))))
    assert fourth == (4, first[1])

    close_mcp_connections()
    for _ in range(50):
        if not alive(first[1]):
            break
        time.sleep(0.1)
    assert not alive(first[1])  # closing stops the server it started

    reopened = counted(asyncio.run(execute_tool(count, {}, RunContext(cfg))))
    assert reopened[0] == 1 and reopened[1] != first[1]  # usable again, with a new server


async def test_keep_alive_false_reconnects_for_every_call(
    tmp_path: Path, server: dict[str, Any]
) -> None:
    cfg = with_server(tmp_path, {**free(server), "keep_alive": False})
    count = ToolRegistry.from_config(cfg).get("glossary_count")
    ctx = RunContext(cfg)
    first = counted(await execute_tool(count, {}, ctx))
    second = counted(await execute_tool(count, {}, ctx))
    assert first[0] == second[0] == 1 and first[1] != second[1]


async def test_a_dead_server_fails_one_call_and_the_next_reconnects(
    tmp_path: Path, server: dict[str, Any]
) -> None:
    cfg = with_server(tmp_path, free(server))
    registry = ToolRegistry.from_config(cfg)
    ctx = RunContext(cfg)
    before = counted(await execute_tool(registry.get("glossary_count"), {}, ctx))

    killed = await execute_tool(registry.get("glossary_die"), {}, ctx)
    assert killed.error is not None
    assert (killed.error.kind, killed.error.retryable) == ("transient", True)

    after = counted(await execute_tool(registry.get("glossary_count"), {}, ctx))
    assert after[0] == 1 and after[1] != before[1]


async def test_calls_overlap_on_one_connection_and_a_timeout_leaves_it_usable(
    tmp_path: Path, server: dict[str, Any]
) -> None:
    cfg = with_server(tmp_path, {**free(server), "timeout_seconds": 0.5})
    registry = ToolRegistry.from_config(cfg)
    nap, count = registry.get("glossary_nap"), registry.get("glossary_count")
    ctx = RunContext(cfg, budget=None)  # type: ignore[arg-type]

    started = time.monotonic()
    results = await asyncio.gather(*(execute_tool(nap, {"seconds": 0.2}, ctx) for _ in range(4)))
    assert all(r.output == "rested" for r in results)
    assert time.monotonic() - started < 0.7  # together, not one after another

    before = counted(await execute_tool(count, {}, ctx))
    slow = await execute_tool(nap, {"seconds": 5}, ctx)
    assert slow.error is not None and slow.error.kind == "timeout"
    after = counted(await execute_tool(count, {}, ctx))
    assert after == (before[0] + 1, before[1])  # same server, still answering
