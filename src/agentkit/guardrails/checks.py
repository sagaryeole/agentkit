"""Input and output checks: small hooks run on user input, tool output and the final answer."""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from pydantic import BaseModel, ValidationError

from agentkit.errors import ConfigError
from agentkit.guardrails.patterns import PATTERNS

if TYPE_CHECKING:
    from agentkit.config import Config
    from agentkit.runtime.context import RunContext


class CheckResult(BaseModel):
    """`text` replaces the checked text. `ok=False` rejects it, with `reason` saying why."""

    text: str
    ok: bool = True
    reason: str | None = None


class Check(Protocol):
    async def __call__(self, text: str, ctx: "RunContext") -> CheckResult: ...


@dataclass
class Checks:
    """The checks a run applies at each of the three hook points. Empty by default."""

    user_input: list[Check] = field(default_factory=list)
    tool_output: list[Check] = field(default_factory=list)
    final_answer: list[Check] = field(default_factory=list)

    @classmethod
    def from_config(cls, cfg: "Config") -> "Checks":
        """The checks `guardrails` asks for: the redactor, at each point named in `redact_at`.

        It removes the patterns in `redact_patterns` and the values of secret-looking
        environment variables. Whoever passes their own `Checks` to a run replaces these.
        """
        from agentkit.config.secrets import known_secret_values

        guard = cfg.guardrails
        checks = cls()
        if not guard.redact_at:
            return checks
        redactor = Redactor(guard.redact_patterns, known_secret_values())
        for point in guard.redact_at:
            getattr(checks, point).append(redactor)
        return checks


async def run_checks(checks: Sequence[Check], text: str, ctx: "RunContext") -> CheckResult:
    """Apply checks in order, each seeing the previous one's text. Stops at the first rejection."""
    for check in checks:
        result = await check(text, ctx)
        if not result.ok:
            return result
        text = result.text
    return CheckResult(text=text)


def redact(text: str, patterns: Iterable[str], secrets: Iterable[str] = ()) -> str:
    """Replace known secret values and anything matching the named patterns."""
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[redacted:secret]")
    for name in patterns:
        if name not in PATTERNS:
            raise ConfigError(
                f"Unknown redaction pattern '{name}' in guardrails.redact_patterns. "
                f"Available: {', '.join(PATTERNS)}."
            )
        text = PATTERNS[name].sub(f"[redacted:{name}]", text)
    return text


class Redactor:
    """A check that removes secrets and personal data from text."""

    def __init__(self, patterns: Sequence[str], secrets: Sequence[str] = ()) -> None:
        self._patterns, self._secrets = list(patterns), list(secrets)

    async def __call__(self, text: str, ctx: "RunContext") -> CheckResult:
        return CheckResult(text=redact(text, self._patterns, self._secrets))


class SchemaCheck:
    """A check that rejects text which is not JSON matching a Pydantic model."""

    def __init__(self, schema: type[BaseModel]) -> None:
        self._schema = schema

    async def __call__(self, text: str, ctx: "RunContext") -> CheckResult:
        try:
            self._schema.model_validate_json(text)
        except ValidationError as exc:
            problems = "; ".join(e["msg"] for e in exc.errors()[:3])
            return CheckResult(
                text=text, ok=False, reason=f"not valid {self._schema.__name__}: {problems}"
            )
        return CheckResult(text=text)
