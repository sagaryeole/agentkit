"""MCP tools, against a real MCP server started as a local subprocess. No network."""

import sys
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
    assert registry.select(tags=["mcp"]) == [lookup, store]


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
    assert [t.name for t in tools] == ["glossary_lookup", "glossary_store_entry"]
