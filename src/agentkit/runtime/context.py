"""What a run carries with it. Tools receive it as `ctx`; the model never sees it."""

import logging
import uuid
from dataclasses import dataclass, field

from agentkit.config import Config
from agentkit.guardrails.approvals import ApprovalChannel, AutoDeny
from agentkit.guardrails.budget import Budget
from agentkit.guardrails.checks import Checks
from agentkit.memory.state import InMemoryStateStore, StateStore
from agentkit.observability.tracer import Tracer


@dataclass
class RunContext:
    """Only `config` is required. Everything else gets a safe default: a fresh run ID, a
    budget from `limits`, an approval channel that denies, no checks, a tracer writing
    where the config says, and in-memory state."""

    config: Config
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    session_id: str | None = None
    logger: logging.Logger = field(default_factory=lambda: logging.getLogger("agentkit.run"))
    budget: Budget = field(default=None)  # type: ignore[assignment]
    approvals: ApprovalChannel = field(default_factory=AutoDeny)
    checks: Checks = field(default_factory=Checks)
    tracer: Tracer = field(default=None)  # type: ignore[assignment]
    state: StateStore = field(default_factory=InMemoryStateStore)

    def __post_init__(self) -> None:
        if self.budget is None:
            self.budget = Budget(self.config.limits)
        if self.tracer is None:
            self.tracer = Tracer(self.config, self.run_id, session_id=self.session_id)
