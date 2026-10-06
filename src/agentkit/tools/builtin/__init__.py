"""Ready-made tools, enabled by name in `tools.enabled`."""

from agentkit.tools.base import Tool
from agentkit.tools.builtin.calculator import calculator
from agentkit.tools.builtin.current_time import current_time
from agentkit.tools.builtin.files import list_files, read_file
from agentkit.tools.builtin.files_write import search_files, write_file
from agentkit.tools.builtin.http_fetch import http_fetch
from agentkit.tools.builtin.session import ask_human, finish, recall, remember
from agentkit.tools.builtin.sql_query import sql_query
from agentkit.tools.builtin.web_search import web_search

BUILTIN_TOOLS: dict[str, Tool] = {
    t.name: t
    for t in (
        calculator,
        current_time,
        read_file,
        list_files,
        search_files,
        write_file,
        http_fetch,
        web_search,
        sql_query,
        ask_human,
        remember,
        recall,
        finish,
    )
}
