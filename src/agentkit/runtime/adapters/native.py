"""The reference runtime: a minimal tool-calling loop on the kit's own model layer."""

import dataclasses
import json
import time
from collections import Counter
from collections.abc import AsyncIterator, Callable, Mapping
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from agentkit.errors import AgentKitError, UnsupportedCapability
from agentkit.guardrails.approvals import ApprovalDecision, ApprovalPending
from agentkit.guardrails.budget import Budget, BudgetExceeded
from agentkit.guardrails.checks import run_checks
from agentkit.memory.context import ContextManager
from agentkit.models.base import Message, ModelRequest, ToolResultPart
from agentkit.models.model import Model
from agentkit.models.registry import get_model
from agentkit.runtime.context import RunContext
from agentkit.runtime.executor import execute_tool
from agentkit.runtime.prompts import render_instructions
from agentkit.runtime.spec import (
    AgentSpec,
    ErrorInfo,
    PendingApproval,
    RunEvent,
    RunResult,
    StopReason,
)
from agentkit.tools.base import ToolError, ToolResult
from agentkit.tools.builtin.session import handoff
from agentkit.tools.registry import ToolRegistry

Resume = tuple[PendingApproval, ApprovalDecision]


class NativeAdapter:
    def __init__(
        self,
        registry: ToolRegistry,
        *,
        root: Path | None = None,
        model_factory: Callable[..., Model] = get_model,
        agents: Mapping[str, AgentSpec] | None = None,
    ) -> None:
        self.registry = registry
        self.root = root or Path.cwd()
        self.agents = dict(agents or {})  # the agents a `sub_agents` entry can name
        self._model_factory = model_factory

    def to_native(self, spec: AgentSpec) -> AgentSpec:
        """The native runtime has no library object of its own; the spec is what it runs."""
        return spec

    async def run(self, spec: AgentSpec, input: str | list[Message], ctx: RunContext) -> RunResult:
        return await self._last(self._events(spec, input, ctx, None))

    async def resume(
        self, spec: AgentSpec, pending: PendingApproval, decision: ApprovalDecision, ctx: RunContext
    ) -> RunResult:
        """Continue a run that stopped with `awaiting_approval`, now that there is a decision."""
        return await self._last(self._events(spec, [], ctx, (pending, decision)))

    def stream(
        self, spec: AgentSpec, input: str | list[Message], ctx: RunContext
    ) -> AsyncIterator[RunEvent]:
        return self._events(spec, input, ctx, None)

    @staticmethod
    async def _last(events: AsyncIterator[RunEvent]) -> RunResult:
        result = None
        async for event in events:
            result = event.result or result
        assert result is not None
        return result

    async def _events(
        self, spec: AgentSpec, input: str | list[Message], ctx: RunContext, resume: Resume | None
    ) -> AsyncIterator[RunEvent]:
        caller = ctx.tracer.agent_name
        ctx.tracer.agent_name = spec.name
        with ctx.tracer.span("run", spec.name, model=spec.model) as span:
            span.set_content(input=input if isinstance(input, str) else None)
            async for event in self._loop(spec, input, ctx, resume):
                if event.result is not None:
                    result = event.result
                    span.set(
                        stop_reason=result.stop_reason,
                        steps=result.steps,
                        tool_calls=result.tool_calls,
                        input_tokens=result.usage.input_tokens,
                        output_tokens=result.usage.output_tokens,
                        cost_usd=result.usage.cost_usd,
                    )
                    span.set_content(output=result.output)
                    if result.error is not None:
                        span.fail(f"{result.error.kind}: {result.error.message}")
                yield event
        ctx.tracer.agent_name = caller or spec.name

    async def _loop(
        self, spec: AgentSpec, input: str | list[Message], ctx: RunContext, resume: Resume | None
    ) -> AsyncIterator[RunEvent]:
        started = time.monotonic()
        cfg = ctx.config
        tools = {tool.name: tool for tool in self.registry.select(spec.tools)}
        if spec.sub_agents:
            undefined = sorted(set(spec.sub_agents) - set(self.agents))
            if undefined:
                raise AgentKitError(
                    f"Agent '{spec.name}' lists sub_agents that are not defined: "
                    f"{', '.join(undefined)}. Defined agents: {', '.join(sorted(self.agents))}."
                )
            listing = "\n".join(
                f"- {name}: {self.agents[name].description}" for name in spec.sub_agents
            )
            tools["handoff"] = dataclasses.replace(
                handoff, description=f"{handoff.description}\n\nAgents:\n{listing}"
            )
        model = self._model_factory(spec.model, cfg, agent_name=spec.name)
        if tools and not model.capabilities.tools:
            raise UnsupportedCapability(
                f"Agent '{spec.name}' has tools, but its model '{model.key}' does not support "
                "tool calling. Use another model, or declare capabilities.tools for it if it does."
            )
        schema = spec.output_schema.model_json_schema() if spec.output_schema else None
        messages: list[Message] = []
        tool_specs = [tool.spec for tool in tools.values()]

        async def summarize(transcript: str) -> str:
            """Summarise older turns with the fast model, counted against this run's budget."""
            fast = self._model_factory("fast", cfg, agent_name=spec.name)
            prompt = (
                Path(__file__).parents[2] / "memory" / "prompts" / "summary_v1.md"
            ).read_text()
            request = ModelRequest(
                messages=[
                    Message.from_text("system", prompt),
                    Message.from_text("user", transcript),
                ]
            )
            with ctx.tracer.span("model_call", fast.key, purpose="summarize_context") as span:
                response = await fast.generate(request)
                span.set(
                    input_tokens=response.usage.input_tokens,
                    output_tokens=response.usage.output_tokens,
                    cost_usd=response.usage.cost_usd,
                    stop_reason=response.stop_reason,
                )
            ctx.budget.record_usage(response.usage)
            return response.message.text

        context = ContextManager(
            cfg.memory.context, model.capabilities.context_window, summarize=summarize
        )

        def done(
            stop_reason: StopReason,
            output: Any = None,
            *,
            error: ErrorInfo | None = None,
            pending: PendingApproval | None = None,
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
                pending=pending,
            )
            return RunEvent(type="done", result=result)

        def structured(stop_reason: StopReason, payload: Any, raw: str) -> RunEvent:
            """Finish with `payload` as the output, validated when the spec has a schema."""
            if spec.output_schema is None:
                return done(stop_reason, payload)
            try:
                if isinstance(payload, str):
                    return done(stop_reason, spec.output_schema.model_validate_json(payload))
                return done(stop_reason, spec.output_schema.model_validate(payload))
            except ValidationError as exc:
                problems = "; ".join(e["msg"] for e in exc.errors()[:3])
                message = f"The answer does not match {spec.output_schema.__name__}: {problems}."
                return done("error", raw, error=ErrorInfo(kind="invalid_output", message=message))

        results: list[ToolResultPart] = []
        first_call = 0
        decision: ApprovalDecision | None = None
        if resume is not None:
            pending, decision = resume
            messages = list(pending.messages)
            results, first_call = list(pending.results), pending.call_index
            ctx.budget.restore(pending.steps, pending.tool_calls, pending.usage)
        else:
            # An agent that was handed the task shares the caller's budget.
            if spec.limits is not None and ctx.tracer.depth <= 1:
                ctx.budget = Budget(spec.limits)
            messages.append(Message.from_text("system", render_instructions(spec, self.root)))
            if isinstance(input, str):
                checked = await run_checks(ctx.checks.user_input, input, ctx)
                if not checked.ok:
                    reason = f"The input was rejected by a check: {checked.reason}."
                    yield done("error", error=ErrorInfo(kind="check_failed", message=reason))
                    return
                messages.append(Message.from_text("user", checked.text))
            else:
                messages.extend(input)

        seen: Counter[str] = Counter()
        empty_steps = 0
        last_text = ""
        while True:
            if resume is None:
                try:
                    ctx.budget.check()
                except BudgetExceeded as exc:
                    error = ErrorInfo(kind=exc.limit, message=str(exc))
                    yield done("budget_exceeded", last_text or None, error=error)
                    return
                try:
                    # The transcript stays whole; a long one is sent as a trimmed view.
                    sent = await context.view(messages, tool_specs)
                    request = ModelRequest(messages=sent, tools=tool_specs, response_schema=schema)
                    with ctx.tracer.span("model_call", model.key) as span:
                        response = await model.generate(request)
                        span.set(
                            model_id=response.model_id,
                            stop_reason=response.stop_reason,
                            input_tokens=response.usage.input_tokens,
                            output_tokens=response.usage.output_tokens,
                            cached_input_tokens=response.usage.cached_input_tokens,
                            cost_usd=response.usage.cost_usd,
                            context_trimmed=context.trimmed,
                        )
                        span.set_content(
                            messages=[m.model_dump(mode="json") for m in sent],
                            response=response.message.model_dump(mode="json"),
                        )
                except AgentKitError as exc:
                    error = ErrorInfo(kind=type(exc).__name__, message=str(exc))
                    yield done("error", last_text or None, error=error)
                    return
                ctx.budget.record_model(response.usage)
                text = response.message.text
                if response.stop_reason == "refusal":
                    error = ErrorInfo(kind="refusal", message="The model declined this request.")
                    yield done("error", text or None, error=error)
                    return
                if not response.message.tool_calls:
                    if not text.strip():
                        empty_steps += 1
                        if empty_steps >= cfg.guardrails.max_empty_steps:
                            message = (
                                f"{empty_steps} consecutive steps produced neither a tool call "
                                "nor an answer. Check the prompt and the model's output limit."
                            )
                            yield done(
                                "stalled", error=ErrorInfo(kind="no_progress", message=message)
                            )
                            return
                        continue
                    messages.append(response.message)
                    checked = await run_checks(ctx.checks.final_answer, text, ctx)
                    if not checked.ok:
                        reason = f"The answer was rejected by a check: {checked.reason}."
                        yield done("error", error=ErrorInfo(kind="check_failed", message=reason))
                        return
                    yield structured("completed", checked.text, checked.text)
                    return
                empty_steps = 0
                last_text = text or last_text
                messages.append(response.message)
                if text:
                    yield RunEvent(type="text", text=text)
                results, first_call = [], 0

            calls = messages[-1].tool_calls
            stated_reason = messages[-1].text or None
            for index in range(first_call, len(calls)):
                call = calls[index]
                yield RunEvent(type="tool_call", tool_call=call)
                signature = call.name + json.dumps(call.arguments, sort_keys=True)
                seen[signature] += 1
                if seen[signature] > cfg.guardrails.max_identical_tool_calls:
                    message = (
                        f"{call.name} was called with the same arguments {seen[signature]} times. "
                        "The agent is repeating itself; check the tool's output and the prompt."
                    )
                    yield done(
                        "stalled",
                        last_text or None,
                        error=ErrorInfo(kind="repeated_tool_call", message=message),
                    )
                    return
                tool = tools.get(call.name)
                if tool is None:
                    unknown = ToolError(
                        kind="not_found",
                        message=f"There is no tool named '{call.name}'. "
                        f"Available tools: {', '.join(tools) or 'none'}.",
                    )
                    result = ToolResult(
                        tool_name=call.name,
                        error=unknown,
                        content=json.dumps({"error": unknown.model_dump()}),
                        duration_seconds=0.0,
                    )
                else:
                    try:
                        result = await execute_tool(
                            tool, call.arguments, ctx, reason=stated_reason, decision=decision
                        )
                    except ApprovalPending as waiting:
                        suspended = PendingApproval(
                            request=waiting.request,
                            messages=list(messages),
                            results=list(results),
                            call_index=index,
                            steps=ctx.budget.steps,
                            tool_calls=ctx.budget.tool_calls,
                            usage=ctx.budget.usage,
                        )
                        yield done("awaiting_approval", last_text or None, pending=suspended)
                        return
                    except BudgetExceeded as exc:
                        error = ErrorInfo(kind=exc.limit, message=str(exc))
                        yield done("budget_exceeded", last_text or None, error=error)
                        return
                decision = None
                results.append(
                    ToolResultPart(
                        tool_call_id=call.id,
                        content=result.content,
                        is_error=result.error is not None,
                    )
                )
                yield RunEvent(type="tool_result", tool_result=result)
                if call.name == "handoff" and spec.sub_agents and result.error is None:
                    target = str(call.arguments["agent"])
                    if target in spec.sub_agents:
                        # Control moves to the other agent; its result is this run's result.
                        messages.append(Message(role="tool", content=list(results)))
                        handed: RunResult | None = None
                        task = str(call.arguments["message"])
                        async for event in self._events(self.agents[target], task, ctx, None):
                            if event.result is None:
                                yield event
                            else:
                                handed = event.result
                        assert handed is not None
                        messages.extend(handed.messages)
                        yield done(
                            handed.stop_reason,
                            handed.output,
                            error=handed.error,
                            pending=handed.pending,
                        )
                        return
                    results[-1] = ToolResultPart(
                        tool_call_id=call.id,
                        is_error=True,
                        content=f"There is no agent named '{target}'. "
                        f"Agents you can hand off to: {', '.join(spec.sub_agents)}.",
                    )
                if call.name == "finish" and result.error is None:
                    messages.append(Message(role="tool", content=list(results)))
                    payload = call.arguments.get("result")
                    summary = str(call.arguments.get("summary", ""))
                    yield structured("completed", summary if payload is None else payload, summary)
                    return
            messages.append(Message(role="tool", content=list(results)))
            resume = None
