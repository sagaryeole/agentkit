"""What a run carries with it. Tools receive it as `ctx`; the model never sees it."""

import logging
import uuid
from dataclasses import dataclass, field

from agentkit.config import Config


@dataclass
class RunContext:
    config: Config
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    session_id: str | None = None
    logger: logging.Logger = field(default_factory=lambda: logging.getLogger("agentkit.run"))
