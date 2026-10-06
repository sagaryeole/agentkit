from agentkit.runtime.spec import AgentSpec

single_agent = AgentSpec(
    name="single_agent",
    description="Answers arithmetic questions with a calculator.",
    instructions="prompts/main.md",
    tools=["calculator"],
)
