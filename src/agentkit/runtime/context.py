"""What a run carries with it. Tools receive it as `ctx`; the model never sees it."""

import logging
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from agentkit.config import Config
from agentkit.guardrails.approvals import ApprovalChannel, AutoDeny
from agentkit.guardrails.budget import Budget
from agentkit.guardrails.checks import Checks
from agentkit.memory.state import InMemoryStateStore, StateStore
from agentkit.observability.tracer import Tracer

if TYPE_CHECKING:
    from agentkit.runtime.spec import RunResult

RunLogger = logging.Logger | logging.LoggerAdapter[logging.Logger]


@dataclass
class RunContext:
    """Only `config` is required. Everything else gets a safe default: a fresh run ID, a
    budget from `limits`, an approval channel that denies, the checks `guardrails` asks
    for, a tracer writing where the config says, a logger that stamps each record with
    the run ID, and in-memory state."""

    config: Config
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    session_id: str | None = None
    logger: RunLogger = field(default=None)  # type: ignore[assignment]
    budget: Budget = field(default=None)  # type: ignore[assignment]
    approvals: ApprovalChannel = field(default_factory=AutoDeny)
    checks: Checks = field(default=None)  # type: ignore[assignment]
    tracer: Tracer = field(default=None)  # type: ignore[assignment]
    state: StateStore = field(default_factory=InMemoryStateStore)

    def __post_init__(self) -> None:
        if self.logger is None:
            self.logger = logging.LoggerAdapter(
                logging.getLogger("agentkit.run"), {"run_id": self.run_id}
            )
        if self.budget is None:
            self.budget = Budget(self.config.limits)
        if self.checks is None:
            self.checks = Checks.from_config(self.config)
        if self.tracer is None:
            self.tracer = Tracer(self.config, self.run_id, session_id=self.session_id)

    def log_result(self, result: "RunResult") -> None:
        """Log how a run ended: quietly when it completed, loudly when it did not."""
        agent = self.tracer.agent_name
        detail = f": {result.error.kind}: {result.error.message}" if result.error else ""
        message = f"run {result.stop_reason} (agent {agent}, {result.steps} steps){detail}"
        if result.stop_reason == "completed":
            self.logger.debug(message)
        elif result.stop_reason == "error":
            self.logger.error(message)
        else:
            self.logger.warning(message)
