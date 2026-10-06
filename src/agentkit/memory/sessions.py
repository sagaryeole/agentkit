"""Conversation history, kept between the runs of one session."""

import json
import sqlite3
from pathlib import Path
from typing import Protocol

from agentkit.models.base import Message, OpaquePart


class SessionStore(Protocol):
    def append(self, session_id: str, messages: list[Message]) -> None: ...
    def load(self, session_id: str) -> list[Message]: ...
    def truncate(self, session_id: str, keep_last: int = 0) -> None:
        """Forget all but the newest `keep_last` messages of a session."""
        ...


def storable(messages: list[Message]) -> list[Message]:
    """What may be replayed in a later run: no system prompts, which each run adds itself,
    and no provider-specific blocks such as thinking, which are only valid inside the
    request sequence that produced them."""
    kept = []
    for message in messages:
        if message.role == "system":
            continue
        content = [part for part in message.content if not isinstance(part, OpaquePart)]
        if content:
            kept.append(message.model_copy(update={"content": content}))
    return kept


class InMemorySessionStore:
    def __init__(self) -> None:
        self._sessions: dict[str, list[Message]] = {}

    def append(self, session_id: str, messages: list[Message]) -> None:
        self._sessions.setdefault(session_id, []).extend(messages)

    def load(self, session_id: str) -> list[Message]:
        return list(self._sessions.get(session_id, []))

    def truncate(self, session_id: str, keep_last: int = 0) -> None:
        history = self._sessions.get(session_id, [])
        self._sessions[session_id] = history[len(history) - keep_last :] if keep_last else []


class SqliteSessionStore:
    """History that survives the process, one row per message."""

    def __init__(self, path: Path | str) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS messages "
            "(seq INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, message TEXT)"
        )
        self._db.commit()

    def append(self, session_id: str, messages: list[Message]) -> None:
        self._db.executemany(
            "INSERT INTO messages (session_id, message) VALUES (?, ?)",
            [(session_id, message.model_dump_json()) for message in messages],
        )
        self._db.commit()

    def load(self, session_id: str) -> list[Message]:
        rows = self._db.execute(
            "SELECT message FROM messages WHERE session_id = ? ORDER BY seq", (session_id,)
        ).fetchall()
        return [Message.model_validate(json.loads(row[0])) for row in rows]

    def truncate(self, session_id: str, keep_last: int = 0) -> None:
        self._db.execute(
            "DELETE FROM messages WHERE session_id = ? AND seq NOT IN "
            "(SELECT seq FROM messages WHERE session_id = ? ORDER BY seq DESC LIMIT ?)",
            (session_id, session_id, keep_last),
        )
        self._db.commit()
