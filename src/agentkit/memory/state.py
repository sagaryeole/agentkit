"""Key-value state, scoped to a run, a session or a user."""

import json
import sqlite3
from pathlib import Path
from typing import Any, Literal, Protocol

Scope = Literal["run", "session", "user"]


class StateStore(Protocol):
    def get(self, scope: Scope, owner: str, key: str) -> Any: ...
    def set(self, scope: Scope, owner: str, key: str, value: Any) -> None: ...
    def delete(self, scope: Scope, owner: str, key: str) -> None: ...
    def list(self, scope: Scope, owner: str) -> list[str]: ...


class InMemoryStateStore:
    def __init__(self) -> None:
        self._data: dict[tuple[str, str], dict[str, Any]] = {}

    def get(self, scope: Scope, owner: str, key: str) -> Any:
        return self._data.get((scope, owner), {}).get(key)

    def set(self, scope: Scope, owner: str, key: str, value: Any) -> None:
        self._data.setdefault((scope, owner), {})[key] = value

    def delete(self, scope: Scope, owner: str, key: str) -> None:
        self._data.get((scope, owner), {}).pop(key, None)

    def list(self, scope: Scope, owner: str) -> list[str]:
        return sorted(self._data.get((scope, owner), {}))


class SqliteStateStore:
    """State that survives the process. Values are stored as JSON."""

    def __init__(self, path: Path | str) -> None:
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS state (scope TEXT, owner TEXT, key TEXT, value TEXT, "
            "PRIMARY KEY (scope, owner, key))"
        )
        self._db.commit()

    def get(self, scope: Scope, owner: str, key: str) -> Any:
        row = self._db.execute(
            "SELECT value FROM state WHERE scope = ? AND owner = ? AND key = ?",
            (scope, owner, key),
        ).fetchone()
        return None if row is None else json.loads(row[0])

    def set(self, scope: Scope, owner: str, key: str, value: Any) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO state VALUES (?, ?, ?, ?)",
            (scope, owner, key, json.dumps(value)),
        )
        self._db.commit()

    def delete(self, scope: Scope, owner: str, key: str) -> None:
        self._db.execute(
            "DELETE FROM state WHERE scope = ? AND owner = ? AND key = ?", (scope, owner, key)
        )
        self._db.commit()

    def list(self, scope: Scope, owner: str) -> list[str]:
        rows = self._db.execute(
            "SELECT key FROM state WHERE scope = ? AND owner = ? ORDER BY key", (scope, owner)
        ).fetchall()
        return [row[0] for row in rows]
