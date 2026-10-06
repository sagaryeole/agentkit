"""OpenAI, as a preset over the OpenAI-compatible provider."""

from typing import Any

from agentkit.models.base import Capabilities, ModelRequest
from agentkit.models.providers.openai_compat import OpenAICompatProvider

DEFAULT_API_BASE = "https://api.openai.com/v1"
API_KEY_VARS = ("OPENAI_API_KEY",)
DEFAULT_CAPABILITIES = Capabilities(
    prompt_caching=True, context_window=128_000, max_output_tokens=16_384
)


class OpenAIProvider(OpenAICompatProvider):
    name = "openai"

    def _payload(self, req: ModelRequest) -> dict[str, Any]:
        payload = super()._payload(req)
        if "max_tokens" in payload:  # OpenAI's own API has renamed this field
            payload["max_completion_tokens"] = payload.pop("max_tokens")
        return payload
