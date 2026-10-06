from agentkit.patterns import PlanAndExecute
from agentkit.runtime.spec import AgentSpec

planner = AgentSpec(
    name="planner",
    description="Breaks a problem into steps.",
    instructions="prompts/planner.md",
    model="reasoning",
)

doer = AgentSpec(
    name="doer",
    description="Carries out one step.",
    instructions="prompts/doer.md",
    tools=["calculator"],
)

solve = PlanAndExecute(
    name="solve",
    description="Plans a multi-step calculation, then works through it.",
    planner="planner",
    executor="doer",
    max_plan_steps=5,
)
