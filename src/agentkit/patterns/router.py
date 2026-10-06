"""Router: one agent that sends each request to the specialist agent for its category."""

from agentkit.runtime.spec import AgentSpec

INSTRUCTIONS = (
    "You route requests. Decide which one of the agents available through the handoff tool "
    "should handle the user's request, then call handoff once with that agent's name and the "
    "full request. Do not answer the request yourself. If no agent fits, say so briefly."
)


def router(name: str, description: str, routes: list[str], model: str = "fast") -> AgentSpec:
    """An agent whose only job is to hand each request to one of `routes`.

    Use it when inputs fall into distinct categories that are handled differently. It runs
    on the `fast` role by default, since routing is a small classification task.
    """
    return AgentSpec(
        name=name,
        description=description,
        instructions=INSTRUCTIONS,
        model=model,
        sub_agents=routes,
    )
