from agentkit.runtime.spec import AgentSpec

agent = AgentSpec(
    name="__AGENT_NAME__",
    description="Answers questions, using tools for calculations and text statistics.",
    instructions="prompts/main.md",
    tools=["calculator", "word_count"],
)
