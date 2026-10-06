"""Record model responses to a cassette file and replay them, so evals need no network."""

import hashlib
import json
from collections.abc import AsyncIterator
from pathlib import Path

from agentkit.errors import ModelError
from agentkit.models.base import (
    Capabilities,
    ModelProvider,
    ModelRequest,
    ModelResponse,
    StreamEvent,
    estimate_tokens,
)

REPLAY_CAPABILITIES = Capabilities(context_window=10_000_000, max_output_tokens=1_000_000)


def request_key(model_key: str, req: ModelRequest) -> str:
    """Identifies a request by its model and everything sent to it."""
    body = req.model_dump(mode="json")
    canonical = json.dumps({"model": model_key, "request": body}, sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


class _CassetteProvider:
    def __init__(self, model_key: str, path: Path, capabilities: Capabilities) -> None:
        self.model_key = model_key
        self.path = path
        self.capabilities = capabilities

    async def generate(self, req: ModelRequest) -> ModelResponse:
        raise NotImplementedError

    async def stream(self, req: ModelRequest) -> AsyncIterator[StreamEvent]:
        response = await self.generate(req)
        if response.message.text:
            yield StreamEvent(type="text_delta", text=response.message.text)
        yield StreamEvent(type="done", response=response)

    async def count_tokens(self, req: ModelRequest) -> int:
        return estimate_tokens(req)


class RecordingProvider(_CassetteProvider):
    """Calls the real provider and appends each response to the cassette."""

    name = "recording"

    def __init__(self, model_key: str, path: Path, inner: ModelProvider) -> None:
        super().__init__(model_key, path, inner.capabilities)
        self._inner = inner

    async def generate(self, req: ModelRequest) -> ModelResponse:
        response = await self._inner.generate(req)
        entry = {
            "key": request_key(self.model_key, req),
            "model": self.model_key,
            # Replay must behave like the recorded model, so its capabilities are kept too.
            "capabilities": self.capabilities.model_dump(),
            "response": response.model_dump(mode="json"),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\n")
        return response


class ReplayProvider(_CassetteProvider):
    """Answers from the cassette and never touches the network."""

    name = "replay"

    def __init__(
        self, model_key: str, path: Path, capabilities: Capabilities = REPLAY_CAPABILITIES
    ) -> None:
        super().__init__(model_key, path, capabilities)
        self._responses: dict[str, dict[str, object]] = {}
        if path.is_file():
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    entry = json.loads(line)
                    self._responses[entry["key"]] = entry["response"]
                    if entry["model"] == model_key:
                        self.capabilities = Capabilities.model_validate(entry["capabilities"])

    async def generate(self, req: ModelRequest) -> ModelResponse:
        recorded = self._responses.get(request_key(self.model_key, req))
        if recorded is None:
            raise ModelError(
                f"The cassette {self.path} has no recorded response for this request to "
                f"'{self.model_key}'. The prompt, tools or inputs changed since it was recorded; "
                "record again with `agentkit eval run --record`."
            )
        return ModelResponse.model_validate(recorded)
