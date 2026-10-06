from agentkit.patterns import Workflow
from agentkit.runtime.spec import AgentSpec

extractor = AgentSpec(
    name="extractor",
    description="Pulls decisions and action items out of meeting notes.",
    instructions="prompts/extractor.md",
    model="fast",
)

writer = AgentSpec(
    name="writer",
    description="Turns extracted points into a short brief.",
    instructions="prompts/writer.md",
)

brief = Workflow(
    name="brief",
    description="Meeting notes in, a short brief out.",
    steps=["extractor", "writer"],
)
