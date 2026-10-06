"""Web search behind one interface, so the backend can be swapped in configuration."""

from typing import Annotated, Protocol

import httpx
from pydantic import BaseModel, ConfigDict, Field

from agentkit.runtime.context import RunContext
from agentkit.tools.base import ToolFailure, tool


class SearchHit(BaseModel):
    title: str
    url: str
    snippet: str


class WebSearchSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    backend: str = "searxng"
    base_url: str | None = None  # address of the search service


class SearchBackend(Protocol):
    async def search(self, query: str, max_results: int) -> list[SearchHit]: ...


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=5.0))


class SearxngBackend:
    """A SearXNG instance with its JSON output format enabled."""

    def __init__(self, base_url: str) -> None:
        self._base_url = base_url.rstrip("/")

    async def search(self, query: str, max_results: int) -> list[SearchHit]:
        try:
            async with _client() as client:
                response = await client.get(
                    f"{self._base_url}/search", params={"q": query, "format": "json"}
                )
        except httpx.TransportError as exc:
            raise ToolFailure(
                "transient", f"The search service did not answer ({exc}). Retry.", retryable=True
            ) from exc
        if response.status_code == 403:
            raise ToolFailure(
                "internal",
                "The SearXNG instance refused the JSON format. The operator must add `json` to "
                "search.formats in its settings.yml.",
            )
        if response.status_code != 200:
            raise ToolFailure(
                "transient",
                f"The search service answered HTTP {response.status_code}. Retry later.",
                retryable=response.status_code >= 500 or response.status_code == 429,
            )
        results = response.json().get("results") or []
        return [
            SearchHit(
                title=str(r.get("title") or ""),
                url=str(r.get("url") or ""),
                snippet=str(r.get("content") or "")[:500],
            )
            for r in results[:max_results]
        ]


BACKENDS = {"searxng": SearxngBackend}


@tool(side_effects="read_external", idempotent=True, timeout_seconds=30, tags=["web"],
      untrusted_output=True)  # fmt: skip
async def web_search(
    query: str, max_results: Annotated[int, Field(ge=1, le=20)] = 5, *, ctx: RunContext
) -> list[SearchHit]:
    """Search the web and return titles, URLs and short snippets.

    Use this to find pages about a topic or to check recent facts.
    Do not use it when you already have the URL; use `http_fetch` for that.

    Args:
        query: What to search for, as you would type it into a search engine.
        max_results: How many results to return.
    """
    settings = WebSearchSettings.model_validate(ctx.config.tools.settings.get("web_search", {}))
    if settings.backend not in BACKENDS:
        raise ToolFailure(
            "internal",
            f"The search backend '{settings.backend}' is not available. The operator must set "
            f"tools.settings.web_search.backend to one of: {', '.join(BACKENDS)}.",
        )
    if not settings.base_url:
        raise ToolFailure(
            "internal",
            "Web search is not configured. The operator must set "
            "tools.settings.web_search.base_url to the address of the search service.",
        )
    return await BACKENDS[settings.backend](settings.base_url).search(query, max_results)
