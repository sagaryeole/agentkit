from agentkit.tools import tool


@tool(side_effects="none", idempotent=True, tags=["text"])
def word_count(text: str) -> int:
    """Count the words in a piece of text.

    Use this when asked how long a text is in words.
    Do not use it to count characters or sentences.

    Args:
        text: The text to count.
    """
    return len(text.split())
