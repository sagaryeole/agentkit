import sys
from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Run every test in its own directory, so traces and workspaces never land in the repo."""
    monkeypatch.chdir(tmp_path)


@pytest.fixture(autouse=True)
def closed_mcp_connections() -> Iterator[None]:
    """Stop any MCP server a test started, so none outlives the test that needed it."""
    yield
    module = sys.modules.get("agentkit.tools.mcp")
    if module is not None:
        module.close_mcp_connections()
