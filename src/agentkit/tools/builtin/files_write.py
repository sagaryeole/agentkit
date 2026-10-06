"""File tools that search and write. Paths are confined exactly as in files.py."""

from typing import Annotated

from pydantic import BaseModel, Field

from agentkit.runtime.context import RunContext
from agentkit.tools.base import ToolFailure, tool
from agentkit.tools.builtin.files import resolve_in_root

MAX_FILE_BYTES = 1_000_000


class Match(BaseModel):
    path: str
    line: int
    text: str


class WriteResult(BaseModel):
    path: str
    chars_written: int


@tool(side_effects="none", idempotent=True, tags=["files"], untrusted_output=True)
def search_files(
    query: str,
    path: str = ".",
    max_results: Annotated[int, Field(ge=1, le=200)] = 50,
    *,
    ctx: RunContext,
) -> list[Match]:
    """Find lines containing a piece of text in the workspace files.

    Use this to locate where something is mentioned when you do not know the file.
    The match is a plain, case-insensitive substring; it is not a regular expression.

    Args:
        query: Text to look for.
        path: Directory (or single file) to search, relative to the workspace root.
        max_results: Stop after this many matching lines.
    """
    start = resolve_in_root(ctx, path)
    if not start.exists():
        raise ToolFailure("not_found", f"'{path}' does not exist in the workspace.")
    root = resolve_in_root(ctx, ".")
    needle = query.lower()
    matches: list[Match] = []
    files = [start] if start.is_file() else sorted(p for p in start.rglob("*") if p.is_file())
    for file in files:
        # Skip links that lead out of the workspace, and files too large or not text.
        if not file.resolve().is_relative_to(root) or file.stat().st_size > MAX_FILE_BYTES:
            continue
        try:
            lines = file.read_text(encoding="utf-8").splitlines()
        except (UnicodeDecodeError, OSError):
            continue
        for number, line in enumerate(lines, 1):
            if needle in line.lower():
                relative = str(file.relative_to(root))
                matches.append(Match(path=relative, line=number, text=line.strip()[:300]))
                if len(matches) >= max_results:
                    return matches
    return matches


@tool(side_effects="write", tags=["files"])
def write_file(path: str, content: str, append: bool = False, *, ctx: RunContext) -> WriteResult:
    """Write a text file in the workspace, creating folders as needed.

    Use this to save output the user asked for. It replaces the file unless `append`
    is true, so read the file first if its current content matters.

    Args:
        path: File path relative to the workspace root.
        content: Text to write.
        append: Add to the end of the file instead of replacing it.
    """
    target = resolve_in_root(ctx, path)
    if target.is_dir():
        raise ToolFailure("invalid_input", f"'{path}' is a directory. Give a file path.")
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a" if append else "w", encoding="utf-8") as handle:
        handle.write(content)
    return WriteResult(path=path, chars_written=len(content))
