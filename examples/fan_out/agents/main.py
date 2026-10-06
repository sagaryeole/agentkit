from agentkit.patterns import FanOut
from agentkit.runtime.spec import AgentSpec

benefits = AgentSpec(
    name="benefits",
    description="Argues for the proposal.",
    instructions="prompts/benefits.md",
    model="fast",
)

risks = AgentSpec(
    name="risks",
    description="Argues against the proposal.",
    instructions="prompts/risks.md",
    model="fast",
)

advisor = AgentSpec(
    name="advisor",
    description="Weighs both sides and recommends.",
    instructions="prompts/advisor.md",
)

decide = FanOut(
    name="decide",
    description="Collects the case for and against a proposal, then recommends.",
    workers=["benefits", "risks"],
    merger="advisor",
)
