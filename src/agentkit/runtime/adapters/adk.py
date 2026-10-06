"""Run an AgentSpec on Google ADK. Needs the `agentkit[adk]` extra.

The adapter translates: an AgentSpec becomes an ADK `LlmAgent`, kit tools become ADK
tools that call the kit's tool executor, and model roles become ADK model objects.
`to_native` returns the `LlmAgent` for anyone who needs ADK directly.

Gaps against the native adapter, by design of ADK or not built yet:
- Input must be a string; a message history cannot be passed in, so sessions and
  `agentkit chat` need the native adapter. Long histories are not trimmed.
- A run that needs an approval the channel has not decided cannot be resumed; it ends
  with `awaiting_approval` and no `pending` state.
- `sub_agents` are not mapped. Use the native adapter for hand-offs.
- For hosted and local models ADK calls the provider itself (Gemini directly, the rest
  through LiteLLM), so the kit's retry, timeout and fallback chain do not apply to model
  calls. Budgets, cost, tracing and every tool guardrail still do.
- A call to a tool the agent does not have fails the run instead of being reported to
  the model.
"""

import json
import time
from collections import Counter
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from google.adk.agents import LlmAgent
from google.adk.models.base_llm import BaseLlm
from google.adk.models.lite_llm import LiteLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext
from google.genai import types
from pydantic import ValidationError

from agentkit.config import Config
from agentkit.errors import AgentKitError
from agentkit.guardrails.approvals import ApprovalPending
from agentkit.guardrails.budget import Budget, BudgetExceeded
from agentkit.guardrails.checks import run_checks
from agentkit.models.base import (
    ContentPart,
    Message,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolResultPart,
    ToolSpec,
    Usage,
)
from agentkit.models.model import Model, cost_usd
from agentkit.models.providers.local import LocalProvider
from agentkit.models.providers.openai_compat import OpenAICompatProvider
from agentkit.models.registry import build_provider, price_for, resolve_chain
from agentkit.observability.tracer import Span
from agentkit.runtime.context import RunContext
from agentkit.runtime.executor import execute_tool
from agentkit.runtime.prompts import render_instructions
from agentkit.runtime.spec import AgentSpec, ErrorInfo, RunEvent, RunResult, StopReason
from agentkit.tools.base import Tool, ToolResult
from agentkit.tools.registry import ToolRegistry

APP_NAME = "agentkit"


class _Stalled(AgentKitError):
    pass


@dataclass
class _RunState:
    """What the ADK callbacks and tools of one run share."""

    ctx: RunContext
    model_key: str
    last_text: str = ""
    seen: Counter[str] = field(default_factory=Counter)
    results: list[ToolResult] = field(default_factory=list)
    finished: dict[str, Any] | None = None
    open_span: AbstractContextManager[Span] | None = None
    span: Span | None = None


def content_to_message(content: types.Content) -> Message:
    """An ADK `Content` as a kit message. Function responses become a `tool` message."""
    parts: list[ContentPart] = []
    for index, part in enumerate(content.parts or []):
        if part.function_call is not None:
            call = part.function_call
            parts.append(
                ToolCallPart(
                    id=call.id or f"{call.name}_{index}",
                    name=call.name or "",
                    arguments=dict(call.args or {}),
                )
            )
        elif part.function_response is not None:
            answer = part.function_response
            payload = answer.response or {}
            text = payload.get("result") if isinstance(payload.get("result"), str) else None
            parts.append(
                ToolResultPart(
                    tool_call_id=answer.id or f"{answer.name}_{index}",
                    content=text if text is not None else json.dumps(payload),
                    is_error="error" in payload,
                )
            )
        elif part.text:
            parts.append(TextPart(text=part.text))
    if any(isinstance(p, ToolResultPart) for p in parts):
        return Message(role="tool", content=parts)
    return Message(role="assistant" if content.role == "model" else "user", content=parts)


def to_model_request(request: LlmRequest) -> ModelRequest:
    messages: list[Message] = []
    system = request.config.system_instruction if request.config else None
    if isinstance(system, str) and system:
        messages.append(Message.from_text("system", system))
    messages += [content_to_message(content) for content in request.contents]
    tools = [
        ToolSpec(
            name=declaration.name or "",
            description=declaration.description or "",
            input_schema=declaration.parameters_json_schema or {"type": "object"},
        )
        for tool in (request.config.tools or [] if request.config else [])
        if isinstance(tool, types.Tool)
        for declaration in tool.function_declarations or []
    ]
    return ModelRequest(messages=messages, tools=tools)


def to_llm_response(response: ModelResponse) -> LlmResponse:
    parts = []
    for part in response.message.content:
        if isinstance(part, TextPart):
            parts.append(types.Part(text=part.text))
        elif isinstance(part, ToolCallPart):
            call = types.FunctionCall(id=part.id, name=part.name, args=part.arguments)
            parts.append(types.Part(function_call=call))
    usage = response.usage
    return LlmResponse(
        content=types.Content(role="model", parts=parts),
        usage_metadata=types.GenerateContentResponseUsageMetadata(
            prompt_token_count=usage.input_tokens,
            candidates_token_count=usage.output_tokens,
            total_token_count=usage.input_tokens + usage.output_tokens,
        ),
        custom_metadata={"agentkit_cost_usd": usage.cost_usd, "agentkit_model": response.model_id},
    )


class KitLlm(BaseLlm):
    """An ADK model backed by the kit's model layer. Used for the fake provider and for
    recorded or replayed responses, so the same evals run on both adapters."""

    kit_model: Any = None
    response_schema: dict[str, Any] | None = None

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        request = to_model_request(llm_request)
        request.response_schema = self.response_schema
        yield to_llm_response(await self.kit_model.generate(request))


class KitTool(BaseTool):
    """A kit tool as ADK sees it. Every call goes through the kit's tool executor."""

    def __init__(self, tool: Tool, state: _RunState) -> None:
        super().__init__(name=tool.name, description=tool.description)
        self._tool = tool
        self._state = state

    def _get_declaration(self) -> types.FunctionDeclaration:
        return types.FunctionDeclaration(
            name=self._tool.name,
            description=self._tool.description,
            parameters_json_schema=self._tool.spec.input_schema,
        )

    async def run_async(self, *, args: dict[str, Any], tool_context: ToolContext) -> Any:
        state = self._state
        cfg = state.ctx.config
        signature = self._tool.name + json.dumps(args, sort_keys=True, default=str)
        state.seen[signature] += 1
        if state.seen[signature] > cfg.guardrails.max_identical_tool_calls:
            raise _Stalled(
                f"{self._tool.name} was called with the same arguments {state.seen[signature]} "
                "times. The agent is repeating itself; check the tool's output and the prompt."
            )
        result = await execute_tool(self._tool, args, state.ctx, reason=state.last_text or None)
        state.results.append(result)
        if result.error is not None:
            return {"error": result.error.model_dump()}
        if self._tool.name == "finish":
            state.finished = dict(args)
            tool_context.actions.skip_summarization = True  # end the run here
        return {"result": result.content}


class AdkAdapter:
    def __init__(
        self,
        registry: ToolRegistry,
        config: Config,
        *,
        root: Path | None = None,
        model_factory: Callable[..., Model] | None = None,
    ) -> None:
        self.registry = registry
        self.config = config
        self.root = root or Path.cwd()
        self._model_factory = model_factory

    def _model(self, spec: AgentSpec, key: str) -> Any:
        """The ADK model object for a catalogue entry."""
        cfg = self.config
        entry = cfg.models.models[key]
        schema = spec.output_schema.model_json_schema() if spec.output_schema else None
        if self._model_factory is not None or entry.provider == "fake":
            factory = self._model_factory or (
                lambda name, cfg, **kw: Model([_link(cfg, key)], cfg.retry)
            )
            kit_model = factory(spec.model, cfg, agent_name=spec.name)
            return KitLlm(model=key, kit_model=kit_model, response_schema=schema)
        if entry.provider == "google":
            return entry.model_id  # ADK's own Gemini support
        if entry.provider == "anthropic":
            return LiteLlm(model=f"anthropic/{entry.model_id}")
        provider = build_provider(cfg, key)
        if isinstance(provider, OpenAICompatProvider):
            # Local servers and other OpenAI-style endpoints, in LiteLLM's openai/<id> form.
            return LiteLlm(
                model=f"openai/{provider.model_id}",
                api_base=provider.api_base,
                api_key=provider.api_key,
            )
        raise AgentKitError(
            f"The adk adapter cannot run provider '{entry.provider}' (model '{key}'). "
            "Use the native adapter for it."
        )

    def _agent(self, spec: AgentSpec, state: _RunState) -> LlmAgent:
        if spec.sub_agents:
            raise AgentKitError(
                f"Agent '{spec.name}' has sub_agents, which the adk adapter does not map. "
                "Run it with runtime.adapter: native."
            )
        ctx = state.ctx
        instruction = render_instructions(spec, self.root)
        model = self._model(spec, state.model_key)
        if spec.output_schema is not None and not isinstance(model, KitLlm):
            instruction += (
                "\n\nGive your final answer as a single JSON value conforming to this JSON "
                f"Schema, with no other text:\n{json.dumps(spec.output_schema.model_json_schema())}"
            )
        price = price_for(self.config, state.model_key)

        def before_model(callback_context: Any, llm_request: LlmRequest) -> None:
            ctx.budget.check()
            state.open_span = ctx.tracer.span("model_call", state.model_key)
            state.span = state.open_span.__enter__()

        def after_model(callback_context: Any, llm_response: LlmResponse) -> None:
            if llm_response.partial:
                return
            meta = llm_response.usage_metadata
            usage = Usage(
                input_tokens=(meta.prompt_token_count or 0) if meta else 0,
                output_tokens=(meta.candidates_token_count or 0) if meta else 0,
                cached_input_tokens=(meta.cached_content_token_count or 0) if meta else 0,
            )
            custom = llm_response.custom_metadata or {}
            if "agentkit_cost_usd" in custom:
                usage.cost_usd = custom["agentkit_cost_usd"]
            elif price is not None:
                usage.cost_usd = cost_usd(usage, price)
            ctx.budget.record_model(usage)
            parts = llm_response.content.parts or [] if llm_response.content else []
            text = "".join(p.text for p in parts if p.text)
            state.last_text = text or state.last_text
            if state.span is not None and state.open_span is not None:
                state.span.set(
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    cost_usd=usage.cost_usd,
                    stop_reason="tool_use" if any(p.function_call for p in parts) else "end",
                )
                state.span.set_content(response=text)
                state.open_span.__exit__(None, None, None)
                state.open_span = state.span = None

        return LlmAgent(
            name=spec.name,
            description=spec.description,
            model=model,
            instruction=instruction,
            tools=[KitTool(tool, state) for tool in self.registry.select(spec.tools)],
            before_model_callback=before_model,
            after_model_callback=after_model,
        )

    def to_native(self, spec: AgentSpec, ctx: RunContext | None = None) -> LlmAgent:
        """The ADK `LlmAgent` this adapter would run, for use with ADK directly."""
        ctx = ctx or RunContext(self.config)
        key = resolve_chain(self.config, spec.model, agent_name=spec.name)[0]
        return self._agent(spec, _RunState(ctx=ctx, model_key=key))

    async def run(self, spec: AgentSpec, input: str | list[Message], ctx: RunContext) -> RunResult:
        result = None
        async for event in self.stream(spec, input, ctx):
            result = event.result or result
        assert result is not None
        return result

    async def stream(
        self, spec: AgentSpec, input: str | list[Message], ctx: RunContext
    ) -> AsyncIterator[RunEvent]:
        if not isinstance(input, str):
            raise AgentKitError(
                "The adk adapter takes the input as a string. Pass the latest user message, "
                "or use the native adapter to pass a message history."
            )
        started = time.monotonic()
        ctx.tracer.agent_name = spec.name
        ctx.logger.debug("run started (agent %s, model %s)", spec.name, spec.model)
        key = resolve_chain(self.config, spec.model, agent_name=spec.name)[0]
        state = _RunState(ctx=ctx, model_key=key)
        if spec.limits is not None:
            ctx.budget = Budget(spec.limits)
        messages: list[Message] = []

        def done(
            stop_reason: StopReason, output: Any = None, error: ErrorInfo | None = None
        ) -> RunEvent:
            result = RunResult(
                run_id=ctx.run_id,
                output=output,
                stop_reason=stop_reason,
                usage=ctx.budget.usage,
                steps=ctx.budget.steps,
                tool_calls=ctx.budget.tool_calls,
                duration_seconds=time.monotonic() - started,
                trace_path=str(ctx.tracer.path) if ctx.tracer.path else None,
                error=error,
                messages=list(messages),
            )
            return RunEvent(type="done", result=result)

        def failed(stop_reason: StopReason, kind: str, exc: BaseException) -> RunEvent:
            if state.open_span is not None:  # a model call that never returned
                state.open_span.__exit__(type(exc), exc, None)
                state.open_span = None
            return done(
                stop_reason, state.last_text or None, ErrorInfo(kind=kind, message=str(exc))
            )

        with ctx.tracer.span("run", spec.name, model=spec.model, adapter="adk") as span:
            final: RunEvent | None = None
            checked = await run_checks(ctx.checks.user_input, input, ctx)
            if not checked.ok:
                reason = f"The input was rejected by a check: {checked.reason}."
                final = done("error", error=ErrorInfo(kind="check_failed", message=reason))
            else:
                agent = self._agent(spec, state)
                provider = (
                    None
                    if isinstance(agent.model, KitLlm | str)
                    else build_provider(self.config, key)
                )
                if isinstance(provider, LocalProvider):
                    await provider._preflight()  # the kit's clear errors for a missing server
                messages.append(Message.from_text("system", str(agent.instruction)))
                messages.append(Message.from_text("user", checked.text))
                sessions = InMemorySessionService()
                await sessions.create_session(
                    app_name=APP_NAME, user_id="user", session_id=ctx.run_id
                )
                runner = Runner(app_name=APP_NAME, agent=agent, session_service=sessions)
                new_message = types.Content(role="user", parts=[types.Part(text=checked.text)])
                answer = ""
                try:
                    async for event in runner.run_async(
                        user_id="user", session_id=ctx.run_id, new_message=new_message
                    ):
                        if event.content is None or event.partial:
                            continue
                        message = content_to_message(event.content)
                        messages.append(message)
                        if message.role == "assistant":
                            if message.text:
                                answer = message.text
                                yield RunEvent(type="text", text=message.text)
                            for call in message.tool_calls:
                                yield RunEvent(type="tool_call", tool_call=call)
                        elif message.role == "tool":
                            for tool_result in state.results:
                                yield RunEvent(type="tool_result", tool_result=tool_result)
                            state.results.clear()
                            answer = ""
                except BudgetExceeded as exc:
                    final = failed("budget_exceeded", exc.limit, exc)
                except _Stalled as exc:
                    final = failed("stalled", "repeated_tool_call", exc)
                except ApprovalPending as exc:
                    final = failed("awaiting_approval", "approval_pending", exc)
                except Exception as exc:  # ADK and LiteLLM raise their own error types
                    final = failed("error", type(exc).__name__, exc)
                if final is None:
                    final = await self._finish(spec, state, answer, done, ctx)
            assert final is not None and final.result is not None
            outcome = final.result
            span.set(
                stop_reason=outcome.stop_reason,
                steps=outcome.steps,
                tool_calls=outcome.tool_calls,
                input_tokens=outcome.usage.input_tokens,
                output_tokens=outcome.usage.output_tokens,
                cost_usd=outcome.usage.cost_usd,
            )
            span.set_content(input=input, output=outcome.output)
            if outcome.error is not None:
                span.fail(f"{outcome.error.kind}: {outcome.error.message}")
            ctx.log_result(outcome)
            yield final

    @staticmethod
    async def _finish(
        spec: AgentSpec,
        state: _RunState,
        answer: str,
        done: Callable[..., RunEvent],
        ctx: RunContext,
    ) -> RunEvent:
        payload: Any = answer
        if state.finished is not None:
            result = state.finished.get("result")
            payload = result if result is not None else str(state.finished.get("summary", ""))
        elif not answer.strip():
            message = "The model returned neither a tool call nor an answer."
            return done("stalled", None, ErrorInfo(kind="no_progress", message=message))
        else:
            checked = await run_checks(ctx.checks.final_answer, answer, ctx)
            if not checked.ok:
                reason = f"The answer was rejected by a check: {checked.reason}."
                return done("error", None, ErrorInfo(kind="check_failed", message=reason))
            payload = checked.text
        if spec.output_schema is None:
            return done("completed", payload)
        try:
            if isinstance(payload, str):
                return done("completed", spec.output_schema.model_validate_json(payload))
            return done("completed", spec.output_schema.model_validate(payload))
        except ValidationError as exc:
            problems = "; ".join(e["msg"] for e in exc.errors()[:3])
            message = f"The answer does not match {spec.output_schema.__name__}: {problems}."
            return done("error", payload, ErrorInfo(kind="invalid_output", message=message))


def _link(cfg: Config, key: str) -> Any:
    from agentkit.models.model import Link

    return Link(
        key=key,
        provider=build_provider(cfg, key),
        timeout_seconds=cfg.timeouts.model_call_seconds,
        price=price_for(cfg, key),
        defaults=cfg.models.models[key].defaults,
    )
