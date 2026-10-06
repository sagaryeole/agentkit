from agentkit.patterns import ReviseLoop
from agentkit.runtime.spec import AgentSpec

writer = AgentSpec(
    name="writer",
    description="Writes short announcements.",
    instructions="prompts/writer.md",
)

reviewer = AgentSpec(
    name="reviewer",
    description="Checks an announcement against the house rules.",
    instructions="prompts/reviewer.md",
    model="fast",
)

polish = ReviseLoop(
    name="polish",
    description="Writes an announcement and revises it until it meets the house rules.",
    writer="writer",
    evaluator="reviewer",
    max_rounds=3,
)
