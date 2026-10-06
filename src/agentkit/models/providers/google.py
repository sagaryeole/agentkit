"""Google Gemini, through the OpenAI-compatible endpoint Google provides for it."""

from agentkit.models.base import Capabilities
from agentkit.models.providers.openai_compat import OpenAICompatProvider

DEFAULT_API_BASE = "https://generativelanguage.googleapis.com/v1beta/openai"
API_KEY_VARS = ("GOOGLE_API_KEY", "GEMINI_API_KEY")
DEFAULT_CAPABILITIES = Capabilities(context_window=1_000_000, max_output_tokens=8_192)


class GoogleProvider(OpenAICompatProvider):
    name = "google"
