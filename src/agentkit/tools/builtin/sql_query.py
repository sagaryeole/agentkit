"""Read-only SQL over a SQLite database."""

import re
import sqlite3
import time
from pathlib import Path
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field

from agentkit.runtime.context import RunContext
from agentkit.tools.base import ToolFailure, tool

_SELECT = re.compile(r"\s*(select|with)\b", re.IGNORECASE)


class SqlQuerySettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    database: str | None = None  # path of the SQLite file
    max_rows: int = Field(default=1000, ge=1)
    timeout_seconds: float = Field(default=10, gt=0)


class QueryResult(BaseModel):
    columns: list[str]
    rows: list[list[Any]]
    truncated: bool


@tool(side_effects="none", idempotent=True, tags=["data"], untrusted_output=True)
def sql_query(
    query: str, max_rows: Annotated[int, Field(ge=1)] = 100, *, ctx: RunContext
) -> QueryResult:
    """Run one read-only SELECT statement against the database and return the rows.

    Use this to answer questions from the data. Only a single SELECT (or WITH ... SELECT)
    is accepted; statements that change data are refused. Add a LIMIT for large tables.

    Args:
        query: The SQL SELECT statement.
        max_rows: Return at most this many rows.
    """
    settings = SqlQuerySettings.model_validate(ctx.config.tools.settings.get("sql_query", {}))
    if not settings.database or not Path(settings.database).is_file():
        raise ToolFailure(
            "internal",
            "No database is configured. The operator must set tools.settings.sql_query.database "
            "to an existing SQLite file.",
        )
    statement = query.strip().rstrip(";").strip()
    if ";" in statement or not _SELECT.match(statement):
        raise ToolFailure(
            "invalid_input",
            "Only a single SELECT statement is allowed. Rewrite the query as one SELECT.",
        )
    limit = min(max_rows, settings.max_rows)
    deadline = time.monotonic() + settings.timeout_seconds
    # mode=ro opens the file read-only, so even a statement that slipped through cannot write.
    connection = sqlite3.connect(f"file:{Path(settings.database).resolve()}?mode=ro", uri=True)
    try:
        connection.execute("PRAGMA query_only = ON")
        connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 10_000)
        cursor = connection.execute(statement)
        rows = cursor.fetchmany(limit + 1)
        columns = [column[0] for column in cursor.description or []]
    except sqlite3.OperationalError as exc:
        if "interrupted" in str(exc):
            raise ToolFailure(
                "timeout",
                f"The query ran longer than {settings.timeout_seconds:g}s. "
                "Narrow it or add a LIMIT.",
            ) from exc
        raise ToolFailure("invalid_input", f"The database rejected the query: {exc}.") from exc
    except sqlite3.Error as exc:
        raise ToolFailure("invalid_input", f"The database rejected the query: {exc}.") from exc
    finally:
        connection.close()
    return QueryResult(
        columns=columns, rows=[list(row) for row in rows[:limit]], truncated=len(rows) > limit
    )
