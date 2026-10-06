"""Fetch a web page, confined to allowed domains and public addresses."""

import asyncio
import ipaddress
import socket
from fnmatch import fnmatch
from html.parser import HTMLParser
from typing import Annotated

import httpx
from pydantic import BaseModel, ConfigDict, Field, HttpUrl

from agentkit.runtime.context import RunContext
from agentkit.tools.base import ToolFailure, tool

MAX_REDIRECTS = 5
TEXT_TYPES = ("text/", "application/json", "application/xml", "application/xhtml+xml")


class HttpFetchSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    allowed_domains: list[str] = []


class FetchResult(BaseModel):
    url: str
    status: int
    content_type: str
    text: str
    truncated: bool


class _TextExtractor(HTMLParser):
    """Keeps the visible text of an HTML page and drops scripts and styles."""

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skipping = 0

    def handle_starttag(self, tag: str, attrs: object) -> None:
        if tag in ("script", "style", "noscript"):
            self._skipping += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "noscript") and self._skipping:
            self._skipping -= 1

    def handle_data(self, data: str) -> None:
        if not self._skipping and data.strip():
            self.parts.append(data.strip())


def domain_allowed(host: str, patterns: list[str]) -> bool:
    """`*.example.com` also matches `example.com`; `*` allows every domain."""
    host = host.lower().rstrip(".")
    for pattern in (p.lower() for p in patterns):
        if fnmatch(host, pattern) or (pattern.startswith("*.") and host == pattern[2:]):
            return True
    return False


def is_public_address(address: str) -> bool:
    ip = ipaddress.ip_address(address.split("%")[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global


async def _resolve(host: str, port: int) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=5.0), follow_redirects=False)


async def _checked_target(url: httpx.URL, allowed: list[str]) -> str:
    """Apply every safety rule to one hop and return the public address to connect to."""
    if url.scheme not in ("http", "https"):
        raise ToolFailure("permission", f"Only http and https URLs can be fetched, not '{url}'.")
    if not domain_allowed(url.host, allowed):
        raise ToolFailure(
            "permission",
            f"The domain '{url.host}' is not in the allowed list "
            "(tools.settings.http_fetch.allowed_domains). Use an allowed domain or tell the "
            "user that this page cannot be fetched.",
        )
    try:
        addresses = await _resolve(url.host, url.port or (443 if url.scheme == "https" else 80))
    except OSError as exc:
        raise ToolFailure(
            "not_found", f"The host '{url.host}' could not be resolved. Check the URL."
        ) from exc
    if not addresses or not all(is_public_address(a) for a in addresses):
        raise ToolFailure(
            "permission",
            f"'{url.host}' resolves to a loopback, link-local or private address, which "
            "cannot be fetched.",
        )
    return addresses[0]


@tool(side_effects="read_external", idempotent=True, timeout_seconds=30, tags=["web"],
      untrusted_output=True)  # fmt: skip
async def http_fetch(
    url: HttpUrl,
    max_bytes: Annotated[int, Field(ge=1, le=2_000_000)] = 200_000,
    *,
    ctx: RunContext,
) -> FetchResult:
    """Fetch a web page and return its text content.

    Use this when you need the current contents of a specific URL.
    Do not use it to search; use `web_search` for that.

    Args:
        url: Absolute http or https URL.
        max_bytes: Truncate the body after this many bytes.
    """
    settings = HttpFetchSettings.model_validate(ctx.config.tools.settings.get("http_fetch", {}))
    current = httpx.URL(str(url))
    async with _client() as client:
        for _ in range(MAX_REDIRECTS + 1):
            address = await _checked_target(current, settings.allowed_domains)
            # Connect to the address that was checked, not to whatever the name resolves to
            # a moment later; the Host header and TLS server name keep the original host.
            headers = {"Host": current.netloc.decode("ascii"), "User-Agent": "agentkit-http-fetch"}
            extensions = {"sni_hostname": current.host} if current.scheme == "https" else {}
            try:
                async with client.stream(
                    "GET", current.copy_with(host=address), headers=headers, extensions=extensions
                ) as response:
                    if response.is_redirect and "location" in response.headers:
                        current = current.join(response.headers["location"])
                        continue
                    content_type = response.headers.get("content-type", "").split(";")[0].strip()
                    if content_type and not content_type.startswith(TEXT_TYPES):
                        raise ToolFailure(
                            "invalid_input",
                            f"{current} is '{content_type}', not text, so it cannot be read.",
                        )
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body += chunk
                        if len(body) > max_bytes:
                            break
                    encoding = response.encoding or "utf-8"
                    status = response.status_code
            except httpx.TransportError as exc:
                raise ToolFailure(
                    "transient", f"Fetching {current} failed ({exc}). Retry.", retryable=True
                ) from exc
            truncated = len(body) > max_bytes
            text = bytes(body[:max_bytes]).decode(encoding, errors="replace")
            if "html" in content_type:
                extractor = _TextExtractor()
                extractor.feed(text)
                text = "\n".join(extractor.parts)
            return FetchResult(
                url=str(current),
                status=status,
                content_type=content_type,
                text=text,
                truncated=truncated,
            )
    raise ToolFailure("invalid_input", f"{url} redirected more than {MAX_REDIRECTS} times.")
