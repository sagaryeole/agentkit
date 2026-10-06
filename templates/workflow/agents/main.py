from agentkit.patterns import Workflow
from agentkit.runtime.spec import AgentSpec

drafter = AgentSpec(
    name="drafter",
    description="Writes a first answer.",
    instructions="prompts/drafter.md",
    tools=["calculator"],
)

editor = AgentSpec(
    name="editor",
    description="Shortens the draft to one sentence.",
    instructions="prompts/editor.md",
    tools=["word_count"],
)

workflow = Workflow(
    name="__AGENT_NAME__",
    description="Draft an answer, then edit it down.",
    steps=["drafter", "editor"],
)
