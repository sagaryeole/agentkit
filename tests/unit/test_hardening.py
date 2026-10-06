"""Logging with the run ID, redaction wired to config, and `doctor`'s connectivity checks."""

import asyncio
import io
import json
import logging
from pathlib import Path
from typing import Any

import anthropic
import httpx
import pytest
from typer.testing import CliRunner

from agentkit.cli import main as cli_main
from agentkit.cli.main import app
from agentkit.errors import ConfigError, ModelError
from agentkit.guardrails.checks import CheckResult, Checks
from agentkit.models import Message, ModelRequest
from agentkit.models.base import Capabilities
from agentkit.models.providers.anthropic import AnthropicProvider
from agentkit.models.providers.fake import FakeProvider
from agentkit.models.providers.google import GoogleProvider
from agentkit.models.providers.local import LocalProvider
from agentkit.models.providers.openai_compat import OpenAICompatProvider
from agentkit.observability.logs import configure_logging
from agentkit.observability.tracer import read_trace
from agentkit.runtime.context import RunContext
from agentkit.runtime.spec import AgentSpec
from agentkit.tools import ToolFailure, tool
from tests.unit.helpers import config, scripted_adapter, tool_calls

EMAIL = "ada@example.com"
KEY = "sk-live-9f8e7d6c5b4a39281706"
SECRET = "hunter2-correct-horse"


@pytest.fixture(autouse=True)
def isolated_logging(monkeypatch: pytest.MonkeyPatch) -> Any:
    for name in ("MODEL_PROVIDER", "AGENTKIT_ENV", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    yield
    logger = logging.getLogger("agentkit")
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
    logger.setLevel(logging.NOTSET)
    logger.propagate = True


@tool(side_effects="none")
def flaky(n: int) -> str:
    """Always fails with a message the model can act on."""
    raise ToolFailure("not_found", f"No item {n}.")


@tool(side_effects="none")
def crashing() -> str:
    """Raises something unexpected, naming a customer and a secret."""
    raise RuntimeError(f"lookup failed for {EMAIL} using {KEY} and {SECRET}")


@tool(side_effects="none")
def whois() -> str:
    """Look up the account owner."""
    return f"owner: {EMAIL}, key {KEY}, vault password {SECRET}"


def spec_for(*tools: Any) -> AgentSpec:
    return AgentSpec(
        name="helper", description="d", instructions="Help.", tools=[t.name for t in tools]
    )


def log_lines(stream: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in stream.getvalue().splitlines()]


# logging


async def test_logs_are_json_lines_that_carry_the_run_id(tmp_path: Path) -> None:
    stream = io.StringIO()
    cfg = config(tmp_path, limits={"max_steps": 2})
    configure_logging(cfg, stream)
    script = [tool_calls(("flaky", {"n": 7}), text="Trying."), tool_calls(("flaky", {"n": 8}))]
    adapter, _ = scripted_adapter(script, flaky)
    ctx = RunContext(cfg)
    result = await adapter.run(spec_for(flaky), "go", ctx)

    lines = log_lines(stream)
    assert [(entry["level"], entry["logger"]) for entry in lines] == [
        ("WARNING", "agentkit.run"),
        ("WARNING", "agentkit.run"),
        ("WARNING", "agentkit.run"),
    ]
    assert all(entry["run_id"] == result.run_id == ctx.run_id for entry in lines)
    assert lines[0]["message"] == "tool flaky failed (not_found): No item 7."
    assert "budget_exceeded" in lines[2]["message"] and "limits.max_steps" in lines[2]["message"]
    assert lines[0]["time"].endswith("+00:00")
    # the same ID finds the trace
    assert read_trace(Path(result.trace_path or ""))[0]["run_id"] == lines[0]["run_id"]


async def test_a_completed_run_is_quiet_until_debug_level(tmp_path: Path) -> None:
    stream = io.StringIO()
    configure_logging(config(tmp_path), stream)
    adapter, _ = scripted_adapter(["fine", "fine"])
    spec = spec_for()
    await adapter.run(spec, "go", RunContext(config(tmp_path)))
    assert stream.getvalue() == ""

    configure_logging(config(tmp_path, observability={"log_level": "DEBUG"}), stream)
    await adapter.run(spec, "go", RunContext(config(tmp_path)))
    messages = [entry["message"] for entry in log_lines(stream)]
    assert messages[0] == "run started (agent helper, model default)"
    assert messages[1].startswith("run completed (agent helper, 1 steps)")

    stream.truncate(0)
    stream.seek(0)
    configure_logging(config(tmp_path, observability={"log_level": "ERROR"}), stream)
    adapter, _ = scripted_adapter([tool_calls(("flaky", {"n": 1})), "ok"], flaky)
    await adapter.run(spec_for(flaky), "go", RunContext(config(tmp_path)))
    assert stream.getvalue() == ""  # the failed tool call is only a warning


async def test_logs_never_contain_secrets_or_personal_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VAULT_PASSWORD", SECRET)
    stream = io.StringIO()
    cfg = config(tmp_path)
    configure_logging(cfg, stream)
    adapter, _ = scripted_adapter([tool_calls(("crashing", {})), "sorry"], crashing)
    await adapter.run(spec_for(crashing), "go", RunContext(cfg))

    written = stream.getvalue()
    assert "RuntimeError" in written  # the failure is logged, with its traceback
    for planted in (EMAIL, KEY, SECRET):
        assert planted not in written
    assert "[redacted:email]" in written and "[redacted:secret]" in written


def test_configure_logging_replaces_its_own_handler_and_sets_the_level(tmp_path: Path) -> None:
    logger = logging.getLogger("agentkit")
    for level in ("WARNING", "DEBUG"):
        configure_logging(config(tmp_path, observability={"log_level": level}), io.StringIO())
        assert logger.level == getattr(logging, level)
    assert len([h for h in logger.handlers if getattr(h, "_agentkit", False)]) == 1
    assert logger.propagate is False


def test_the_cli_configures_logging_from_the_config(tmp_path: Path) -> None:
    (tmp_path / "agentkit.yaml").write_text("observability: {log_level: DEBUG}\n")
    result = CliRunner().invoke(app, ["config", "validate", "--project-dir", str(tmp_path)])
    assert result.exit_code == 0
    assert logging.getLogger("agentkit").level == logging.DEBUG


# redaction wired to config


class Recorder:
    """What the model would have been sent, and what it answered."""

    def __init__(self, tmp_path: Path, **guardrails: Any) -> None:
        self.cfg = config(tmp_path, guardrails=guardrails) if guardrails else config(tmp_path)

    async def run(
        self, answer: str = f"Mail {EMAIL} and use {KEY}.", **ctx: Any
    ) -> tuple[Any, Any]:
        script = [tool_calls(("whois", {})), answer]
        adapter, model = scripted_adapter(script, whois)
        result = await adapter.run(
            spec_for(whois), f"Who owns it? I am {EMAIL}.", RunContext(self.cfg, **ctx)
        )
        return result, model


async def test_by_default_the_model_never_sees_secrets_from_tools_or_repeats_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VAULT_PASSWORD", SECRET)
    result, model = await Recorder(tmp_path).run()

    tool_text = model.requests[1].messages[-1].content[0].content
    assert (
        "owner: [redacted:email], key [redacted:api_key], vault password [redacted:secret]"
        in tool_text
    )
    assert result.output == "Mail [redacted:email] and use [redacted:api_key]."
    # the user's own message is not touched by default
    assert f"I am {EMAIL}." in model.requests[0].messages[-1].text


async def test_redact_at_chooses_where_the_redactor_runs(tmp_path: Path) -> None:
    everywhere = Recorder(tmp_path, redact_at=["user_input", "tool_output", "final_answer"])
    _, model = await everywhere.run()
    assert "I am [redacted:email]." in model.requests[0].messages[-1].text

    nowhere = Recorder(tmp_path, redact_at=[])
    result, model = await nowhere.run()
    assert EMAIL in model.requests[1].messages[-1].content[0].content
    assert result.output == f"Mail {EMAIL} and use {KEY}."
    # the trace is redacted regardless
    trace = Path(result.trace_path or "").read_text()
    assert EMAIL not in trace and KEY not in trace

    only_email = Recorder(tmp_path, redact_patterns=["email"])
    result, _ = await only_email.run()
    assert result.output == f"Mail [redacted:email] and use {KEY}."


async def test_checks_passed_in_code_replace_the_configured_ones(tmp_path: Path) -> None:
    class Shout:
        async def __call__(self, text: str, ctx: RunContext) -> CheckResult:
            return CheckResult(text=text.upper())

    _, model = await Recorder(tmp_path).run(checks=Checks(final_answer=[Shout()]))
    assert EMAIL in model.requests[1].messages[-1].content[0].content  # no redactor any more


def test_unknown_redaction_patterns_are_a_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"unknown redaction pattern \['phone'\]"):
        config(tmp_path, guardrails={"redact_patterns": ["email", "phone"]})
    with pytest.raises(ConfigError, match="redact_at"):
        config(tmp_path, guardrails={"redact_at": ["everywhere"]})


# connectivity checks


def caps() -> Capabilities:
    return Capabilities(context_window=1000, max_output_tokens=100)


def models_endpoint(status: int = 200, ids: list[str] | None = None) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/models")
        return httpx.Response(status, json={"data": [{"id": i} for i in ids or []]})

    return httpx.MockTransport(handle)


async def test_openai_style_ping_checks_the_key_and_the_model_id() -> None:
    def provider(cls: type[OpenAICompatProvider], model: str, transport: Any) -> Any:
        return cls(
            model, api_base="https://x/v1", api_key="k", capabilities=caps(), transport=transport
        )

    ok = provider(OpenAICompatProvider, "m1", models_endpoint(ids=["m1", "m2"]))
    assert await ok.ping() == "https://x/v1 answers and offers 'm1'"

    with pytest.raises(ModelError, match="rejected the API key"):
        await provider(OpenAICompatProvider, "m1", models_endpoint(401)).ping()
    with pytest.raises(ModelError, match=r"does not offer a model 'm9'. Offered: m1, m2"):
        await provider(OpenAICompatProvider, "m9", models_endpoint(ids=["m1", "m2"])).ping()
    assert (
        "does not list models"
        in await provider(OpenAICompatProvider, "m1", models_endpoint(404)).ping()
    )
    with pytest.raises(ModelError, match="HTTP 503"):
        await provider(OpenAICompatProvider, "m1", models_endpoint(503)).ping()

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(ModelError, match="Could not reach"):
        await provider(OpenAICompatProvider, "m1", httpx.MockTransport(refuse)).ping()
    # Gemini lists models as "models/<id>"
    google = provider(GoogleProvider, "gemini-x", models_endpoint(ids=["models/gemini-x"]))
    assert "offers 'gemini-x'" in await google.ping()


async def test_local_ping_tolerates_a_model_not_chosen_yet() -> None:
    transport = models_endpoint(ids=["qwen"])

    def local(model: str) -> LocalProvider:
        return LocalProvider(
            model, api_base="http://127.0.0.1:1234/v1", server="lmstudio", transport=transport
        )

    assert await local("qwen").ping() == "lmstudio server answers and has 'qwen'"
    assert "no model is selected yet (set LOCAL_MODEL_ID)" in await local("").ping()
    with pytest.raises(ModelError, match="Model 'gone' is not available"):
        await local("gone").ping()


async def test_anthropic_ping_counts_tokens_which_checks_credentials_and_model() -> None:
    calls: list[dict[str, Any]] = []
    failure: list[Exception] = []

    class Messages:
        async def count_tokens(self, **kwargs: Any) -> Any:
            calls.append(kwargs)
            if failure:
                raise failure[0]
            return type("Counted", (), {"input_tokens": 8})()

    class Client:
        messages = Messages()

    provider = AnthropicProvider("claude-x", client=Client())  # type: ignore[arg-type]
    assert await provider.ping() == "Anthropic API answers and accepts 'claude-x'"
    assert calls[0]["model"] == "claude-x" and "max_tokens" not in calls[0]  # nothing is generated

    response = httpx.Response(404, request=httpx.Request("POST", "https://api.anthropic.com"))
    failure.append(anthropic.NotFoundError("model: claude-x", response=response, body=None))
    with pytest.raises(ModelError, match="HTTP 404"):
        await provider.ping()
    assert await FakeProvider().ping() == "scripted model; nothing to connect to"
    assert isinstance(ModelRequest(messages=[Message.from_text("user", "x")]), ModelRequest)


# doctor


class Stub:
    def __init__(self, outcome: str | Exception | float) -> None:
        self.outcome = outcome

    async def ping(self) -> str:
        if isinstance(self.outcome, float):
            await asyncio.sleep(self.outcome)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return str(self.outcome)


def run_doctor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcomes: dict[str, Any], *args: str
) -> tuple[Any, list[str]]:
    contacted: list[str] = []

    def build(cfg: Any, key: str, environ: Any = None) -> Stub:
        contacted.append(key)
        return Stub(outcomes[key])

    monkeypatch.setattr(cli_main, "build_provider", build)
    result = CliRunner().invoke(app, ["doctor", "--project-dir", str(tmp_path), *args])
    return result, contacted


DEFAULT_MODELS = (
    "anthropic/claude-sonnet-5-5",
    "anthropic/claude-haiku-4-5",
    "anthropic/claude-opus-5-5",
)


def test_doctor_contacts_each_model_in_use_once_when_credentials_are_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    outcomes = {key: "answers" for key in DEFAULT_MODELS}
    result, contacted = run_doctor(tmp_path, monkeypatch, outcomes)
    assert result.exit_code == 0, result.output
    assert sorted(contacted) == sorted(DEFAULT_MODELS)  # judge shares a model with default
    assert "[ ok ] anthropic/claude-haiku-4-5: answers" in result.output


def test_a_model_that_cannot_be_reached_fails_the_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    outcomes: dict[str, Any] = {key: "answers" for key in DEFAULT_MODELS}
    outcomes["anthropic/claude-opus-5-5"] = ModelError("Anthropic answered HTTP 404: no such model")
    result, _ = run_doctor(tmp_path, monkeypatch, outcomes)
    assert result.exit_code == 1
    assert "[fail] anthropic/claude-opus-5-5: Anthropic answered HTTP 404" in result.output
    assert "[ ok ] anthropic/claude-sonnet-5-5: answers" in result.output  # the rest still run


def test_a_silent_provider_times_out_instead_of_hanging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setattr(cli_main, "PING_TIMEOUT_SECONDS", 0.05)
    result, _ = run_doctor(tmp_path, monkeypatch, {key: 5.0 for key in DEFAULT_MODELS})
    assert result.exit_code == 1 and "no answer within 0.05s" in result.output


def test_without_credentials_doctor_warns_and_does_not_try(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, contacted = run_doctor(tmp_path, monkeypatch, {})
    assert result.exit_code == 0 and contacted == []
    assert result.output.count("[warn] keys: none of ANTHROPIC_API_KEY") == 1
    assert (
        "[skip] anthropic/claude-sonnet-5-5: not contacted, no credentials to try" in result.output
    )


def test_offline_contacts_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    result, contacted = run_doctor(tmp_path, monkeypatch, {}, "--offline")
    assert result.exit_code == 0 and contacted == []
    assert "[ ok ] keys: credentials for 'anthropic/claude-sonnet-5-5' are set" in result.output
    assert "not contacted (--offline)" in result.output


def test_doctor_uses_the_key_variable_declared_for_a_compatible_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "agentkit.yaml").write_text(
        "models:\n  roles: {default: gw/m, fast: gw/m, reasoning: gw/m, judge: gw/m}\n"
        "  models:\n    gw/m: {provider: openai_compat, model_id: m,"
        " api_base: 'https://gw.example.com/v1', api_key_env: GW_KEY}\n"
    )
    missing, contacted = run_doctor(tmp_path, monkeypatch, {})
    assert "[warn] keys: none of GW_KEY is set" in missing.output and contacted == []
    monkeypatch.setenv("GW_KEY", "k")
    present, contacted = run_doctor(tmp_path, monkeypatch, {"gw/m": "gateway answers"})
    assert contacted == ["gw/m"] and "[ ok ] gw/m: gateway answers" in present.output


def test_doctor_on_the_local_provider_and_fake_needs_no_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "agentkit.yaml").write_text(
        "models: {roles: {default: fake/default, fast: fake/default,"
        " reasoning: fake/default, judge: fake/default}}\n"
    )
    result, contacted = run_doctor(tmp_path, monkeypatch, {"fake/default": "scripted"})
    assert result.exit_code == 0 and contacted == ["fake/default"]
    assert "[ ok ] fake/default: scripted" in result.output and "keys:" not in result.output
