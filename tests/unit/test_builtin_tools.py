import math
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

from agentkit.config import load_config
from agentkit.runtime.context import RunContext
from agentkit.runtime.executor import execute_tool
from agentkit.tools.base import ToolResult
from agentkit.tools.builtin import BUILTIN_TOOLS
from agentkit.tools.builtin.http_fetch import domain_allowed, is_public_address

# The package re-exports the tool under the same name, so fetch the module itself.
http_fetch_module = sys.modules["agentkit.tools.builtin.http_fetch"]


def context(tmp_path: Path, **settings: Any) -> RunContext:
    settings.setdefault("filesystem", {"root": str(tmp_path / "workspace")})
    overrides = {"tools": {"settings": settings}}
    return RunContext(load_config(tmp_path, environ={}, overrides=overrides))


async def call(name: str, ctx: RunContext, **arguments: Any) -> ToolResult:
    return await execute_tool(BUILTIN_TOOLS[name], arguments, ctx)


def test_every_builtin_declares_its_side_effects() -> None:
    assert {name: t.side_effects for name, t in BUILTIN_TOOLS.items()} == {
        "calculator": "none",
        "current_time": "none",
        "read_file": "none",
        "list_files": "none",
        "http_fetch": "read_external",
    }


# calculator


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("1717 * 2323", 3988591),
        ("(2 + 3) ** 2 / 5", 5.0),
        ("-7 // 2", -4),
        ("sqrt(16) + abs(-2)", 6.0),
        ("round(pi, 2)", 3.14),
        ("max(1, 2, 3) % 2", 1),
    ],
)
async def test_calculator_computes(tmp_path: Path, expression: str, expected: float) -> None:
    result = await call("calculator", context(tmp_path), expression=expression)
    assert result.output == pytest.approx(expected)


@pytest.mark.parametrize(
    "expression",
    [
        "__import__('os').system('true')",
        "open('/etc/passwd').read()",
        "(1).__class__",
        "[x for x in range(3)]",
        "'a' * 3",
        "lambda: 1",
        "pi.real",
        "9 ** 9 ** 9",
        "1 / 0",
        "sqrt(-1)",
        "2 +",
    ],
)
async def test_calculator_refuses_anything_but_arithmetic(tmp_path: Path, expression: str) -> None:
    result = await call("calculator", context(tmp_path), expression=expression)
    assert result.error is not None and result.error.kind == "invalid_input"


def test_calculator_constants() -> None:
    assert BUILTIN_TOOLS["calculator"].fn("e") == math.e


# current_time


async def test_current_time_is_zone_aware(tmp_path: Path) -> None:
    ctx = context(tmp_path)
    kolkata = await call("current_time", ctx, timezone="Asia/Kolkata")
    assert kolkata.output["iso"].endswith("+05:30")
    assert (await call("current_time", ctx)).output["timezone"] == "UTC"
    unknown = await call("current_time", ctx, timezone="Mars/Olympus")
    assert unknown.error is not None and unknown.error.kind == "invalid_input"


# files


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    (root / "docs").mkdir(parents=True)
    (root / "notes.txt").write_text("0123456789", encoding="utf-8")
    (root / "docs" / "a.md").write_text("# A", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("TOP SECRET", encoding="utf-8")
    return root


async def test_read_file_returns_content_in_pieces(tmp_path: Path, workspace: Path) -> None:
    ctx = context(tmp_path)
    first = await call("read_file", ctx, path="notes.txt", max_chars=4)
    assert first.output == {
        "path": "notes.txt",
        "content": "0123",
        "total_chars": 10,
        "next_offset": 4,
    }
    rest = await call("read_file", ctx, path="notes.txt", offset=4)
    assert (rest.output["content"], rest.output["next_offset"]) == ("456789", None)
    assert first.content.startswith('<untrusted_content source="read_file">')


async def test_list_files_lists_one_directory(tmp_path: Path, workspace: Path) -> None:
    result = await call("list_files", context(tmp_path))
    assert result.output == [
        {"name": "docs", "type": "directory", "size": None},
        {"name": "notes.txt", "type": "file", "size": 10},
    ]
    nested = await call("list_files", context(tmp_path), path="docs")
    assert [e["name"] for e in nested.output] == ["a.md"]


@pytest.mark.parametrize("path", ["../secret.txt", "docs/../../secret.txt", "{abs}"])
async def test_paths_outside_the_root_are_refused(
    tmp_path: Path, workspace: Path, path: str
) -> None:
    path = path.replace("{abs}", str(tmp_path / "secret.txt"))
    for name in ("read_file", "list_files"):
        result = await call(name, context(tmp_path), path=path)
        assert result.error is not None and result.error.kind == "permission", name
        assert "TOP SECRET" not in result.content


async def test_symlinks_cannot_lead_out_of_the_root(tmp_path: Path, workspace: Path) -> None:
    (workspace / "link.txt").symlink_to(tmp_path / "secret.txt")
    (workspace / "up").symlink_to(tmp_path)
    ctx = context(tmp_path)
    for name, path in (
        ("read_file", "link.txt"),
        ("read_file", "up/secret.txt"),
        ("list_files", "up"),
    ):
        result = await call(name, ctx, path=path)
        assert result.error is not None and result.error.kind == "permission", path
        assert "TOP SECRET" not in result.content

    inside = workspace / "alias.txt"
    inside.symlink_to(workspace / "notes.txt")
    assert (await call("read_file", ctx, path="alias.txt")).output["content"] == "0123456789"


async def test_missing_files_and_missing_root_are_not_found(tmp_path: Path) -> None:
    no_root = await call("read_file", context(tmp_path), path="notes.txt")
    assert no_root.error is not None and no_root.error.kind == "not_found"
    (tmp_path / "workspace").mkdir()
    missing = await call("read_file", context(tmp_path), path="nope.txt")
    assert missing.error is not None and "list_files" in missing.error.message


# http_fetch


class Web:
    """Fake DNS and fake servers for http_fetch."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.dns: dict[str, list[str]] = {}
        self.pages: dict[str, httpx.Response] = {}
        self.requests: list[httpx.Request] = []

        async def resolve(host: str, port: int) -> list[str]:
            if host not in self.dns:
                raise OSError("unknown host")
            return self.dns[host]

        def handle(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            url = f"{request.url.scheme}://{request.headers['host']}{request.url.path}"
            return self.pages.get(url, httpx.Response(404, text="not found"))

        monkeypatch.setattr(http_fetch_module, "_resolve", resolve)
        monkeypatch.setattr(
            http_fetch_module,
            "_client",
            lambda: httpx.AsyncClient(transport=httpx.MockTransport(handle)),
        )


@pytest.fixture
def web(monkeypatch: pytest.MonkeyPatch) -> Web:
    return Web(monkeypatch)


def allowing(tmp_path: Path, *domains: str) -> RunContext:
    return context(tmp_path, http_fetch={"allowed_domains": list(domains)})


async def test_http_fetch_returns_page_text_and_connects_to_the_checked_address(
    tmp_path: Path, web: Web
) -> None:
    web.dns["docs.example.com"] = ["93.184.216.34"]
    html = "<html><style>p{}</style><body><h1>Title</h1><script>x()</script><p>Body</p></body>"
    web.pages["https://docs.example.com/page"] = httpx.Response(
        200, text=html, headers={"content-type": "text/html; charset=utf-8"}
    )
    result = await call(
        "http_fetch", allowing(tmp_path, "*.example.com"), url="https://docs.example.com/page"
    )
    assert result.error is None
    assert result.output["text"] == "Title\nBody"
    assert (result.output["status"], result.output["truncated"]) == (200, False)
    sent = web.requests[0]
    assert sent.url.host == "93.184.216.34"
    assert sent.headers["host"] == "docs.example.com"
    assert sent.extensions["sni_hostname"] == "docs.example.com"
    assert result.content.startswith('<untrusted_content source="http_fetch">')


async def test_http_fetch_truncates_at_max_bytes(tmp_path: Path, web: Web) -> None:
    web.dns["example.com"] = ["93.184.216.34"]
    web.pages["http://example.com/big"] = httpx.Response(
        200, text="a" * 5000, headers={"content-type": "text/plain"}
    )
    result = await call(
        "http_fetch", allowing(tmp_path, "example.com"), url="http://example.com/big", max_bytes=100
    )
    assert result.output["text"] == "a" * 100 and result.output["truncated"] is True


async def test_nothing_is_fetchable_until_a_domain_is_allowed(tmp_path: Path, web: Web) -> None:
    web.dns["example.com"] = ["93.184.216.34"]
    result = await call("http_fetch", context(tmp_path), url="https://example.com/")
    assert result.error is not None and result.error.kind == "permission"
    assert "allowed_domains" in result.error.message
    assert web.requests == []


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.5",
        "172.16.3.4",
        "192.168.1.1",
        "169.254.169.254",
        "0.0.0.0",
        "::1",
        "fe80::1",
        "fd00::1",
        "::ffff:10.0.0.1",
    ],
)
async def test_private_loopback_and_link_local_addresses_are_refused(
    tmp_path: Path, web: Web, address: str
) -> None:
    web.dns["internal.example.com"] = [address]
    result = await call(
        "http_fetch", allowing(tmp_path, "*"), url="http://internal.example.com/admin"
    )
    assert result.error is not None and result.error.kind == "permission"
    assert "private address" in result.error.message
    assert web.requests == []


async def test_a_name_with_any_private_address_is_refused(tmp_path: Path, web: Web) -> None:
    web.dns["mixed.example.com"] = ["93.184.216.34", "127.0.0.1"]
    result = await call("http_fetch", allowing(tmp_path, "*"), url="http://mixed.example.com/")
    assert result.error is not None and result.error.kind == "permission"


@pytest.mark.parametrize("url", ["http://127.0.0.1:8080/", "http://[::1]/", "http://localhost/"])
async def test_literal_local_urls_are_refused(tmp_path: Path, web: Web, url: str) -> None:
    web.dns.update({"127.0.0.1": ["127.0.0.1"], "::1": ["::1"], "localhost": ["127.0.0.1"]})
    result = await call("http_fetch", allowing(tmp_path, "*"), url=url)
    assert result.error is not None and result.error.kind == "permission"
    assert web.requests == []


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://example.com/x", "gopher://x"])
async def test_non_http_schemes_are_refused(tmp_path: Path, web: Web, url: str) -> None:
    result = await call("http_fetch", allowing(tmp_path, "*"), url=url)
    assert result.error is not None and result.error.kind == "invalid_input"
    assert web.requests == []


async def test_redirects_are_followed_only_to_allowed_public_targets(
    tmp_path: Path, web: Web
) -> None:
    web.dns.update(
        {
            "example.com": ["93.184.216.34"],
            "www.example.com": ["93.184.216.35"],
            "evil.test": ["93.184.216.36"],
            "rebind.example.com": ["192.168.0.10"],
        }
    )

    def redirect(to: str) -> httpx.Response:
        return httpx.Response(302, headers={"location": to})

    web.pages["http://example.com/ok"] = redirect("http://www.example.com/final")
    web.pages["http://www.example.com/final"] = httpx.Response(
        200, text="arrived", headers={"content-type": "text/plain"}
    )
    web.pages["http://example.com/out"] = redirect("http://evil.test/steal")
    web.pages["http://example.com/in"] = redirect("http://rebind.example.com/router")
    web.pages["http://example.com/file"] = redirect("file:///etc/passwd")
    web.pages["http://example.com/loop"] = redirect("/loop")
    ctx = allowing(tmp_path, "*.example.com")

    ok = await call("http_fetch", ctx, url="http://example.com/ok")
    assert ok.output["text"] == "arrived" and ok.output["url"] == "http://www.example.com/final"

    for path in ("out", "in", "file"):
        refused = await call("http_fetch", ctx, url=f"http://example.com/{path}")
        assert refused.error is not None and refused.error.kind == "permission", path
    assert all(r.headers["host"] != "evil.test" for r in web.requests)

    loop = await call("http_fetch", ctx, url="http://example.com/loop")
    assert loop.error is not None and "redirected more than 5 times" in loop.error.message


async def test_unknown_hosts_binary_content_and_network_errors(
    tmp_path: Path, web: Web, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = allowing(tmp_path, "*")
    unknown = await call("http_fetch", ctx, url="http://nowhere.example.com/")
    assert unknown.error is not None and unknown.error.kind == "not_found"

    web.dns["example.com"] = ["93.184.216.34"]
    web.pages["http://example.com/logo.png"] = httpx.Response(
        200, content=b"\x89PNG", headers={"content-type": "image/png"}
    )
    binary = await call("http_fetch", ctx, url="http://example.com/logo.png")
    assert binary.error is not None and "not text" in binary.error.message

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection reset")

    monkeypatch.setattr(
        http_fetch_module,
        "_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(refuse)),
    )
    down = await call("http_fetch", ctx, url="http://example.com/")
    assert down.error is not None
    assert (down.error.kind, down.error.retryable) == ("transient", True)


def test_domain_patterns() -> None:
    assert domain_allowed("docs.example.com", ["*.example.com"])
    assert domain_allowed("Example.com.", ["*.example.com"])
    assert domain_allowed("anything.test", ["*"])
    assert not domain_allowed("example.com.evil.test", ["*.example.com"])
    assert not domain_allowed("notexample.com", ["*.example.com"])
    assert not domain_allowed("example.com", [])


def test_public_address_check() -> None:
    assert is_public_address("93.184.216.34") and is_public_address("2606:4700::1111")
    assert not is_public_address("100.64.0.1")  # carrier-grade NAT
    assert not is_public_address("fe80::1%en0")
