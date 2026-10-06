from agentkit.runtime.spec import AgentSpec

notetaker = AgentSpec(
    name="notetaker",
    description="Saves notes to files, checking with the operator when unsure.",
    instructions="prompts/main.md",
    tools=["write_file", "read_file", "ask_human"],
)
