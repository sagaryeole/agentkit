from agentkit.runtime.spec import AgentSpec

toolbox = AgentSpec(
    name="toolbox",
    description="Answers questions using the builtin tools.",
    instructions=(
        "Use the tools to find facts instead of guessing: the calculator for arithmetic, "
        "current_time for dates, the file tools for the workspace and http_fetch for web pages."
    ),
    tools=["calculator", "current_time", "read_file", "list_files", "http_fetch"],
)
