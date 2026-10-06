from agentkit.patterns import router
from agentkit.runtime.spec import AgentSpec

numbers = AgentSpec(
    name="numbers",
    description="Answers anything that needs a calculation.",
    instructions="prompts/numbers.md",
    tools=["calculator"],
)

words = AgentSpec(
    name="words",
    description="Answers questions about a piece of text, such as its length.",
    instructions="prompts/words.md",
    tools=["word_count"],
)

agent = router(
    name="__AGENT_NAME__",
    description="Sends each request to the numbers or the words agent.",
    routes=["numbers", "words"],
)
