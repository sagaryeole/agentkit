"""Models served on the developer's machine by LM Studio, Ollama or a compatible server.

A thin preset over the OpenAI-compatible provider: default base URLs, discovery,
and a start-up check that names the cause when the server or model is missing.
"""

from collections.abc import AsyncIterator

import httpx

from agentkit.errors import ModelError
from agentkit.models.base import Capabilities, ModelRequest, ModelResponse, StreamEvent
from agentkit.models.providers.openai_compat import OpenAICompatProvider

DEFAULT_API_BASES = {
    "lmstudio": "http://127.0.0.1:1234/v1",
    "ollama": "http://127.0.0.1:11434/v1",
}
START_HINTS = {
    "lmstudio": "Start it from LM Studio's Developer tab or with `lms server start`",
    "ollama": "Start it with `ollama serve`",
    "custom": "Start the server",
}

# Conservative: local models differ widely, so config declares what a model really supports.
DEFAULT_CAPABILITIES = Capabilities(
    tools=True,
    parallel_tool_calls=False,
    structured_output=False,
    vision=False,
    context_window=8192,
    max_output_tokens=4096,
)


async def list_local_models(
    api_base: str,
    *,
    server: str = "custom",
    api_key: str = "local",
    transport: httpx.AsyncBaseTransport | None = None,
) -> list[str]:
    """Model IDs the server offers. Raises ModelError naming why it could not answer."""
    try:
        async with httpx.AsyncClient(timeout=5.0, transport=transport) as client:
            resp = await client.get(
                f"{api_base.rstrip('/')}/models", headers={"Authorization": f"Bearer {api_key}"}
            )
        if resp.status_code == 200:
            return [str(model["id"]) for model in resp.json()["data"]]
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        pass

    url = httpx.URL(api_base)
    if url.port is None or not url.path.rstrip("/").endswith("/v1"):
        example = DEFAULT_API_BASES.get(server, DEFAULT_API_BASES["lmstudio"])
        raise ModelError(
            f"The local base URL '{api_base}' must include the port and end in '/v1', "
            f"for example {example}. Fix LOCAL_API_BASE or models.local.api_base."
        )
    raise ModelError(
        f"No {server} model server is answering at {api_base}. {START_HINTS[server]}, "
        "or set LOCAL_API_BASE if it listens somewhere else."
    )


class LocalProvider(OpenAICompatProvider):
    name = "local"

    def __init__(
        self,
        model_id: str,
        *,
        api_base: str,
        server: str = "custom",
        api_key: str = "local",
        capabilities: Capabilities = DEFAULT_CAPABILITIES,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        super().__init__(
            model_id,
            api_base=api_base,
            api_key=api_key,
            capabilities=capabilities,
            transport=transport,
        )
        self.server = server
        self._checked = False

    async def _preflight(self) -> None:
        """Before the first call, check the server is up and the model is on it."""
        if self._checked:
            return
        available = await list_local_models(
            self.api_base, server=self.server, api_key=self.api_key, transport=self._transport
        )
        offered = ", ".join(available) or "none"
        if not self.model_id:
            raise ModelError(
                f"No local model is selected. Set LOCAL_MODEL_ID to one of the IDs the "
                f"{self.server} server at {self.api_base} offers: {offered}."
            )
        if self.model_id not in available:
            raise ModelError(
                f"Model '{self.model_id}' is not available on the {self.server} server at "
                f"{self.api_base}. Available: {offered}. Load the model, or set LOCAL_MODEL_ID "
                "to one of these."
            )
        self._checked = True

    def _connection_error(self, exc: Exception) -> ModelError:
        return ModelError(
            f"Lost the connection to the {self.server} server at {self.api_base} ({exc}). "
            f"{START_HINTS[self.server]} and retry.",
            retryable=True,
        )

    async def generate(self, req: ModelRequest) -> ModelResponse:
        await self._preflight()
        return await super().generate(req)

    async def stream(self, req: ModelRequest) -> AsyncIterator[StreamEvent]:
        await self._preflight()
        async for event in super().stream(req):
            yield event
