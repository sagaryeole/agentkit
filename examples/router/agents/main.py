from agentkit.patterns import router
from agentkit.runtime.spec import AgentSpec

billing = AgentSpec(
    name="billing",
    description="Handles charges, refunds and invoices.",
    instructions="prompts/billing.md",
    tools=["calculator"],
)

shipping = AgentSpec(
    name="shipping",
    description="Handles delivery status, delays and address changes.",
    instructions="prompts/shipping.md",
)

support = router(
    name="support",
    description="Sends each customer request to billing or shipping.",
    routes=["billing", "shipping"],
)
