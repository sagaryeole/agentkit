"""Sessions and state (spec section 12)."""

from agentkit.config import Config
from agentkit.memory.sessions import InMemorySessionStore, SessionStore, SqliteSessionStore
from agentkit.memory.state import InMemoryStateStore, SqliteStateStore, StateStore

__all__ = ["SessionStore", "StateStore", "make_stores"]


def make_stores(cfg: Config) -> tuple[SessionStore, StateStore]:
    """The session and state stores named by `memory.store`."""
    if cfg.memory.store == "memory":
        return InMemorySessionStore(), InMemoryStateStore()
    return SqliteSessionStore(cfg.memory.sqlite_path), SqliteStateStore(cfg.memory.sqlite_path)
