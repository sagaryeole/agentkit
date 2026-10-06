"""Ready-made tools, enabled by name in `tools.enabled`."""

from agentkit.tools.base import Tool
from agentkit.tools.builtin.calculator import calculator
from agentkit.tools.builtin.current_time import current_time
from agentkit.tools.builtin.files import list_files, read_file
from agentkit.tools.builtin.http_fetch import http_fetch

BUILTIN_TOOLS: dict[str, Tool] = {
    t.name: t for t in (calculator, current_time, read_file, list_files, http_fetch)
}
