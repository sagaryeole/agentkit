"""Tracing: one span per run, model call and tool call.

Spans go to the OpenTelemetry API, so an application that configures an
OpenTelemetry SDK gets them in any OTLP backend, and to the kit's own exporter,
which by default writes one JSON-lines file per run and needs no service.
"""

import copy
import itertools
import json
import sys
import time
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from opentelemetry import trace as otel

from agentkit.config import Config
from agentkit.config.secrets import known_secret_values
from agentkit.errors import ConfigError
from agentkit.guardrails.checks import redact

SpanKind = Literal["run", "model_call", "tool_call"]
_OTEL = otel.get_tracer("agentkit")


class Span:
    """A span in progress. `set` records metadata; `set_content` records inputs and outputs,
    which are dropped when `observability.capture_content` is false."""

    def __init__(self, span_id: str) -> None:
        self.span_id = span_id
        self.attributes: dict[str, Any] = {}
        self.content: dict[str, Any] = {}
        self.status = "ok"

    def set(self, **attributes: Any) -> None:
        self.attributes.update(attributes)

    def set_content(self, **content: Any) -> None:
        self.content.update(content)

    def fail(self, message: str) -> None:
        self.status = "error"
        self.attributes["error"] = message


class Tracer:
    def __init__(
        self,
        cfg: Config,
        run_id: str,
        *,
        session_id: str | None = None,
        agent_name: str | None = None,
        secrets: Sequence[str] | None = None,
    ) -> None:
        obs = cfg.observability
        if obs.exporter == "otlp":
            raise ConfigError(
                "observability.exporter 'otlp' is not shipped yet. Use jsonl, console or none; "
                "spans also reach any OpenTelemetry SDK your application configures."
            )
        self.run_id = run_id
        self.session_id = session_id
        self.agent_name = agent_name
        self.path = Path(obs.jsonl_path) / f"{run_id}.jsonl" if obs.exporter == "jsonl" else None
        self._env = cfg.env
        self._exporter = obs.exporter
        self._capture_content = obs.capture_content
        self._patterns = cfg.guardrails.redact_patterns
        self._secrets = known_secret_values() if secrets is None else list(secrets)
        self._stack: list[tuple[str, otel.Span]] = []
        self._sequence = itertools.count(1)

    def child(self) -> "Tracer":
        """A tracer for work that runs at the same time as other work in this run. It
        writes to the same trace under the currently open span, but tracks its own
        nesting, so parallel branches do not confuse each other's parents."""
        branch = copy.copy(self)
        branch._stack = list(self._stack)
        return branch

    @property
    def depth(self) -> int:
        """How many spans are open: 1 inside a top-level run, more inside a nested one."""
        return len(self._stack)

    def _scrub(self, value: Any) -> Any:
        """Redact every string in a JSON-like value. Runs before anything is exported."""
        if isinstance(value, str):
            return redact(value, self._patterns, self._secrets)
        if isinstance(value, dict):
            return {str(k): self._scrub(v) for k, v in value.items()}
        if isinstance(value, list | tuple):
            return [self._scrub(v) for v in value]
        if value is None or isinstance(value, bool | int | float):
            return value
        return self._scrub(str(value))

    @contextmanager
    def span(self, kind: SpanKind, name: str, **attributes: Any) -> Iterator[Span]:
        span = Span(uuid.uuid4().hex[:16])
        span.set(**attributes)
        sequence = next(self._sequence)  # spans are written when they end; this keeps order
        parent_id, parent_otel = self._stack[-1] if self._stack else (None, None)
        context = otel.set_span_in_context(parent_otel) if parent_otel else None
        otel_span = _OTEL.start_span(f"{kind} {name}", context=context)
        self._stack.append((span.span_id, otel_span))
        started_at = datetime.now(UTC)
        started = time.monotonic()
        try:
            yield span
        except BaseException as exc:
            span.fail(f"{type(exc).__name__}: {exc}")
            raise
        finally:
            self._stack.pop()
            record: dict[str, Any] = {
                "span_id": span.span_id,
                "seq": sequence,
                "parent_id": parent_id,
                "kind": kind,
                "name": name,
                "run_id": self.run_id,
                "session_id": self.session_id,
                "agent_name": self.agent_name,
                "env": self._env,
                "start": started_at.isoformat(timespec="milliseconds"),
                "duration_ms": round((time.monotonic() - started) * 1000, 1),
                "status": span.status,
                "attributes": span.attributes,
            }
            if self._capture_content:
                record["content"] = span.content
            record = self._scrub(record)
            for key, value in record["attributes"].items():
                if isinstance(value, str | bool | int | float):
                    otel_span.set_attribute(f"agentkit.{key}", value)
            for key in ("run_id", "session_id", "agent_name", "env"):
                if record[key] is not None:
                    otel_span.set_attribute(f"agentkit.{key}", record[key])
            if span.status == "error":
                otel_span.set_status(otel.StatusCode.ERROR)
            otel_span.end()
            self._export(record)

    def _export(self, record: dict[str, Any]) -> None:
        if self._exporter == "jsonl" and self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record) + "\n")
        elif self._exporter == "console":
            print(json.dumps(record), file=sys.stderr)


def read_trace(path: Path) -> list[dict[str, Any]]:
    """The spans of one run, in the order they started."""
    lines = path.read_text(encoding="utf-8").splitlines()
    return sorted((json.loads(line) for line in lines if line.strip()), key=lambda s: s["seq"])


def render_trace(spans: list[dict[str, Any]]) -> str:
    """A readable timeline of one run for the terminal."""
    if not spans:
        return "The trace is empty."
    origin = datetime.fromisoformat(spans[0]["start"])
    depth: dict[str, int] = {}
    lines = []
    for span in spans:
        level = depth[span["span_id"]] = depth.get(span["parent_id"], -1) + 1
        offset = (datetime.fromisoformat(span["start"]) - origin).total_seconds()
        attrs = span["attributes"]
        if span["kind"] == "model_call":
            cost = attrs.get("cost_usd")
            detail = (
                f"{attrs.get('input_tokens', '?')} in / {attrs.get('output_tokens', '?')} out"
                f"{'' if cost is None else f', ${cost:.4f}'}, stop: {attrs.get('stop_reason')}"
            )
        elif span["kind"] == "tool_call":
            arguments = json.dumps(span.get("content", {}).get("arguments", {}))
            outcome = attrs.get("error_kind") or "ok"
            approval = f", approval: {attrs['approval']}" if attrs.get("approval") else ""
            detail = f"{arguments[:80]} -> {outcome}{approval}"
        else:
            detail = (
                f"{attrs.get('steps', '?')} steps, {attrs.get('tool_calls', '?')} tool calls, "
                f"stop: {attrs.get('stop_reason')}"
            )
        mark = "!" if span["status"] == "error" else " "
        lines.append(
            f"{offset:8.2f}s {mark}{'  ' * level}{span['kind']:<10} {span['name']:<28} "
            f"{span['duration_ms']:>9.1f}ms  {detail}"
        )
    return "\n".join(lines)
