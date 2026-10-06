"""File tools. Every path is confined to `tools.settings.filesystem.root`."""

from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from agentkit.runtime.context import RunContext
from agentkit.tools.base import ToolFailure, tool

MAX_ENTRIES = 500


class FilesystemSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    root: str = "./workspace"


def resolve_in_root(ctx: RunContext, path: str) -> Path:
    """Resolve `path` under the configured root, refusing anything that ends up outside it.

    Symbolic links are resolved first, so a link inside the root cannot point out of it.
    """
    settings = FilesystemSettings.model_validate(ctx.config.tools.settings.get("filesystem", {}))
    root = Path(settings.root).resolve()
    if not root.is_dir():
        raise ToolFailure(
            "not_found",
            "The file workspace does not exist. The operator must create it or set "
            "tools.settings.filesystem.root.",
        )
    target = (root / path).resolve()
    if not target.is_relative_to(root):
        raise ToolFailure(
            "permission",
            f"'{path}' is outside the file workspace. Use a path relative to the workspace root.",
        )
    return target


class FileContent(BaseModel):
    path: str
    content: str
    total_chars: int
    next_offset: int | None  # pass as `offset` to read the rest; null at the end


class FileEntry(BaseModel):
    name: str
    type: str
    size: int | None


@tool(side_effects="none", idempotent=True, tags=["files"], untrusted_output=True)
def read_file(
    path: str,
    offset: Annotated[int, Field(ge=0)] = 0,
    max_chars: Annotated[int, Field(ge=1, le=15_000)] = 10_000,
    *,
    ctx: RunContext,
) -> FileContent:
    """Read a text file from the workspace.

    Use this when you need the contents of a file whose path you know.
    Use `list_files` first if you do not know the path. Long files come in pieces:
    when `next_offset` is not null, call again with it as `offset`.

    Args:
        path: File path relative to the workspace root.
        offset: Character position to start from.
        max_chars: Maximum number of characters to return.
    """
    target = resolve_in_root(ctx, path)
    if not target.is_file():
        raise ToolFailure(
            "not_found", f"'{path}' is not a file. Use list_files to see what exists."
        )
    text = target.read_text(encoding="utf-8", errors="replace")
    end = offset + max_chars
    return FileContent(
        path=path,
        content=text[offset:end],
        total_chars=len(text),
        next_offset=end if end < len(text) else None,
    )


@tool(side_effects="none", idempotent=True, tags=["files"], untrusted_output=True)
def list_files(path: str = ".", *, ctx: RunContext) -> list[FileEntry]:
    """List the files and directories directly inside a workspace directory.

    Use this to find out what exists before reading a file. It does not recurse;
    call it again on a subdirectory to look inside.

    Args:
        path: Directory path relative to the workspace root. "." is the root itself.
    """
    target = resolve_in_root(ctx, path)
    if not target.is_dir():
        raise ToolFailure("not_found", f"'{path}' is not a directory in the workspace.")
    entries = []
    for entry in sorted(target.iterdir(), key=lambda p: p.name)[:MAX_ENTRIES]:
        if entry.is_symlink():
            entries.append(FileEntry(name=entry.name, type="symlink", size=None))
        elif entry.is_dir():
            entries.append(FileEntry(name=entry.name, type="directory", size=None))
        else:
            entries.append(FileEntry(name=entry.name, type="file", size=entry.stat().st_size))
    return entries
