"""The builtin tools added after the first five, the finish tool, and state stores."""

import sqlite3
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

from agentkit.config import load_config
from agentkit.guardrails.approvals import ApprovalDecision, ApprovalRequest, CallbackApproval
from agentkit.memory.state import InMemoryStateStore, SqliteStateStore, StateStore
from agentkit.runtime.context import RunContext
from agentkit.runtime.executor import execute_tool
from agentkit.runtime.spec import AgentSpec
from agentkit.tools.base import ToolResult
from agentkit.tools.builtin import BUILTIN_TOOLS
from tests.unit.helpers import scripted_adapter, tool_calls

web_search_module = sys.modules["agentkit.tools.builtin.web_search"]


def context(tmp_path: Path, **settings: Any) -> RunContext:
    settings.setdefault("filesystem", {"root": str(tmp_path / "workspace")})
    tools = {"settings": settings, "approval": {"overrides": {"write_file": "allow"}}}
    return RunContext(load_config(tmp_path, environ={}, overrides={"tools": tools}))


async def call(name: str, ctx: RunContext, **arguments: Any) -> ToolResult:
    return await execute_tool(BUILTIN_TOOLS[name], arguments, ctx)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    (root / "docs").mkdir(parents=True)
    (root / "notes.txt").write_text("Launch is on Friday.\nBudget approved.\n")
    (root / "docs" / "plan.md").write_text("# Plan\nThe LAUNCH needs a venue.\n")
    (root / "image.bin").write_bytes(b"\xff\xfe\x00launch")
    (tmp_path / "secret.txt").write_text("launch codes")
    return root


# search_files and write_file


async def test_search_files_finds_lines_case_insensitively(tmp_path: Path, workspace: Path) -> None:
    result = await call("search_files", context(tmp_path), query="launch")
    assert result.output == [
        {"path": "docs/plan.md", "line": 2, "text": "The LAUNCH needs a venue."},
        {"path": "notes.txt", "line": 1, "text": "Launch is on Friday."},
    ]
    one = await call("search_files", context(tmp_path), query="launch", max_results=1)
    assert len(one.output) == 1
    scoped = await call("search_files", context(tmp_path), query="launch", path="notes.txt")
    assert [m["path"] for m in scoped.output] == ["notes.txt"]
    assert (await call("search_files", context(tmp_path), query="zebra")).output == []


async def test_search_files_stays_inside_the_root(tmp_path: Path, workspace: Path) -> None:
    (workspace / "link.txt").symlink_to(tmp_path / "secret.txt")
    (workspace / "up").symlink_to(tmp_path)
    result = await call("search_files", context(tmp_path), query="codes")
    assert result.output == []
    outside = await call("search_files", context(tmp_path), query="codes", path="..")
    assert outside.error is not None and outside.error.kind == "permission"


async def test_write_file_writes_appends_and_stays_inside_the_root(
    tmp_path: Path, workspace: Path
) -> None:
    ctx = context(tmp_path)
    written = await call("write_file", ctx, path="out/report.txt", content="one")
    assert written.output == {"path": "out/report.txt", "chars_written": 3}
    await call("write_file", ctx, path="out/report.txt", content=" two", append=True)
    assert (workspace / "out" / "report.txt").read_text() == "one two"
    await call("write_file", ctx, path="out/report.txt", content="fresh")
    assert (workspace / "out" / "report.txt").read_text() == "fresh"

    for path in ("../escaped.txt", str(tmp_path / "escaped.txt")):
        refused = await call("write_file", ctx, path=path, content="x")
        assert refused.error is not None and refused.error.kind == "permission"
    assert not (tmp_path / "escaped.txt").exists()
    directory = await call("write_file", ctx, path="docs", content="x")
    assert directory.error is not None and directory.error.kind == "invalid_input"


async def test_write_file_needs_approval_by_default(tmp_path: Path, workspace: Path) -> None:
    seen: list[ApprovalRequest] = []

    async def approve(request: ApprovalRequest) -> ApprovalDecision:
        seen.append(request)
        return ApprovalDecision(outcome="approve")

    settings = {"filesystem": {"root": str(workspace)}}
    cfg = load_config(tmp_path, environ={}, overrides={"tools": {"settings": settings}})
    denied = await execute_tool(
        BUILTIN_TOOLS["write_file"], {"path": "a.txt", "content": "x"}, RunContext(cfg)
    )
    assert denied.error is not None and denied.error.kind == "permission"
    assert not (workspace / "a.txt").exists()

    ctx = RunContext(cfg, approvals=CallbackApproval(approve))
    result = await execute_tool(BUILTIN_TOOLS["write_file"], {"path": "a.txt", "content": "x"}, ctx)
    assert result.approval == "approved" and (workspace / "a.txt").read_text() == "x"
    assert seen[0].side_effects == "write"


# web_search


async def test_web_search_queries_searxng(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[httpx.Request] = []
    status = [200]

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        results = [
            {"title": f"Result {i}", "url": f"https://example.com/{i}", "content": "snippet"}
            for i in range(10)
        ]
        return httpx.Response(status[0], json={"results": results})

    monkeypatch.setattr(
        web_search_module,
        "_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    )
    ctx = context(tmp_path, web_search={"base_url": "http://search.internal:8080/"})
    result = await call("web_search", ctx, query="agent evals", max_results=3)
    assert result.output == [
        {"title": f"Result {i}", "url": f"https://example.com/{i}", "snippet": "snippet"}
        for i in range(3)
    ]
    sent = requests[0].url
    assert (sent.host, sent.path) == ("search.internal", "/search")
    assert dict(sent.params) == {"q": "agent evals", "format": "json"}
    assert result.content.startswith('<untrusted_content source="web_search">')

    status[0] = 403
    forbidden = await call("web_search", ctx, query="x")
    assert forbidden.error is not None and "search.formats" in forbidden.error.message
    status[0] = 503
    down = await call("web_search", ctx, query="x")
    assert down.error is not None and down.error.retryable


async def test_web_search_must_be_configured(tmp_path: Path) -> None:
    unset = await call("web_search", context(tmp_path), query="x")
    assert unset.error is not None and "web_search.base_url" in unset.error.message
    other = context(tmp_path, web_search={"backend": "altavista", "base_url": "http://x"})
    unknown = await call("web_search", other, query="x")
    assert unknown.error is not None and "one of: searxng" in unknown.error.message


# sql_query


@pytest.fixture
def database(tmp_path: Path) -> Path:
    path = tmp_path / "shop.db"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE orders (id INTEGER, status TEXT)")
    db.executemany("INSERT INTO orders VALUES (?, ?)", [(i, "shipped") for i in range(1, 6)])
    db.commit()
    db.close()
    return path


async def test_sql_query_returns_rows_with_limits(tmp_path: Path, database: Path) -> None:
    ctx = context(tmp_path, sql_query={"database": str(database), "max_rows": 4})
    result = await call("sql_query", ctx, query="SELECT id, status FROM orders ORDER BY id;")
    assert result.output["columns"] == ["id", "status"]
    assert result.output["rows"] == [[i, "shipped"] for i in range(1, 5)]  # capped by settings
    assert result.output["truncated"] is True
    few = await call("sql_query", ctx, query="select id from orders", max_rows=2)
    assert few.output["rows"] == [[1], [2]]
    cte = await call("sql_query", ctx, query="WITH t AS (SELECT 1 AS n) SELECT n FROM t")
    assert cte.output == {"columns": ["n"], "rows": [[1]], "truncated": False}


@pytest.mark.parametrize(
    "query",
    [
        "DELETE FROM orders",
        "UPDATE orders SET status = 'x'",
        "DROP TABLE orders",
        "INSERT INTO orders VALUES (9, 'x')",
        "SELECT 1; DELETE FROM orders",
        "PRAGMA writable_schema = 1",
        "ATTACH DATABASE 'x.db' AS x",
        "WITH t AS (SELECT 1) DELETE FROM orders",
    ],
)
async def test_sql_query_refuses_everything_but_a_single_select(
    tmp_path: Path, database: Path, query: str
) -> None:
    ctx = context(tmp_path, sql_query={"database": str(database)})
    result = await call("sql_query", ctx, query=query)
    assert result.error is not None and result.error.kind == "invalid_input"
    db = sqlite3.connect(database)
    assert db.execute("SELECT count(*) FROM orders").fetchone() == (5,)
    db.close()


async def test_sql_query_errors_and_time_limit(tmp_path: Path, database: Path) -> None:
    unset = await call("sql_query", context(tmp_path), query="SELECT 1")
    assert unset.error is not None and "sql_query.database" in unset.error.message
    ctx = context(tmp_path, sql_query={"database": str(database), "timeout_seconds": 0.05})
    bad = await call("sql_query", ctx, query="SELECT nope FROM orders")
    assert bad.error is not None and "no such column" in bad.error.message
    endless = (
        "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n) SELECT count(*) FROM n"
    )
    slow = await call("sql_query", ctx, query=endless)
    assert slow.error is not None and slow.error.kind == "timeout"


# ask_human, remember, recall, finish


async def test_ask_human_reaches_the_operator_or_says_nobody_is_there(tmp_path: Path) -> None:
    async def decide(request: ApprovalRequest) -> ApprovalDecision:
        return ApprovalDecision(outcome="deny")

    async def answer(question: str) -> str:
        return f"answer to: {question}"

    ctx = context(tmp_path)
    ctx.approvals = CallbackApproval(decide, answer)
    asked = await call("ask_human", ctx, question="Which region?")
    assert asked.output == "answer to: Which region?"

    unattended = await call("ask_human", context(tmp_path), question="Which region?")
    assert unattended.error is not None and unattended.error.kind == "permission"
    assert "No operator is available" in unattended.error.message


async def test_remember_and_recall_are_scoped_to_the_session(tmp_path: Path) -> None:
    store = InMemoryStateStore()
    ctx = context(tmp_path)
    ctx.session_id, ctx.state = "s1", store
    saved = await call("remember", ctx, key="name", value="Ada")
    assert saved.error is None  # allowed without approval by the shipped config
    await call("remember", ctx, key="plan", value="pro")
    assert (await call("recall", ctx, key="name")).output == {"name": "Ada"}
    assert (await call("recall", ctx)).output == {"name": "Ada", "plan": "pro"}
    missing = await call("recall", ctx, key="age")
    assert missing.error is not None and "Saved keys: name, plan" in missing.error.message

    other = context(tmp_path)
    other.session_id, other.state = "s2", store
    assert (await call("recall", other)).output == {}


async def test_finish_ends_the_run_with_a_structured_result(tmp_path: Path) -> None:
    from pydantic import BaseModel

    class Report(BaseModel):
        total: int

    finish = BUILTIN_TOOLS["finish"]
    spec = AgentSpec(name="a", description="d", instructions="Do it.", tools=["finish"])
    done = tool_calls(("finish", {"summary": "Counted.", "result": {"total": 3}}))
    adapter, model = scripted_adapter([done, "never reached"], finish)
    ctx = RunContext(load_config(tmp_path, environ={}))
    result = await adapter.run(spec, "count", ctx)
    assert (result.stop_reason, result.output) == ("completed", {"total": 3})
    assert len(model.requests) == 1 and result.messages[-1].role == "tool"

    adapter, _ = scripted_adapter([done], finish)
    typed = await adapter.run(spec.model_copy(update={"output_schema": Report}), "count", ctx)
    assert typed.output == Report(total=3)

    adapter, _ = scripted_adapter([tool_calls(("finish", {"summary": "All done."}))], finish)
    assert (
        await adapter.run(spec, "go", RunContext(load_config(tmp_path, environ={})))
    ).output == ("All done.")


# state stores


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
def test_state_stores(tmp_path: Path, kind: str) -> None:
    def make() -> StateStore:
        return InMemoryStateStore() if kind == "memory" else SqliteStateStore(tmp_path / "s.db")

    store = make()
    store.set("session", "s1", "b", {"n": 1})
    store.set("session", "s1", "a", "text")
    store.set("user", "s1", "a", "other scope")
    store.set("session", "s2", "a", "other owner")
    assert store.get("session", "s1", "b") == {"n": 1}
    assert store.list("session", "s1") == ["a", "b"]
    store.set("session", "s1", "a", "replaced")
    assert store.get("session", "s1", "a") == "replaced"
    store.delete("session", "s1", "a")
    assert store.get("session", "s1", "a") is None
    assert store.get("user", "s1", "a") == "other scope"
    assert store.list("run", "nobody") == []
    if kind == "sqlite":
        assert make().get("session", "s1", "b") == {"n": 1}  # survives a new connection
