"""Error types raised by the kit. Every message says what to do next."""


class AgentKitError(Exception):
    """Base class for all errors raised by agentkit."""


class ConfigError(AgentKitError):
    """Configuration could not be read or failed validation."""


class ModelError(AgentKitError):
    """A model call failed. `retryable` says whether trying again can help."""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class UnsupportedCapability(AgentKitError):
    """A request needs something the chosen model does not support."""
