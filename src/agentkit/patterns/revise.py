"""Evaluator and reviser loop: a draft is reviewed and rewritten a bounded number of times."""

from typing import Any, ClassVar

from pydantic import BaseModel, Field

from agentkit.patterns.base import Pattern, text_of
from agentkit.runtime.context import RunContext
from agentkit.runtime.project import Project
from agentkit.runtime.spec import RunResult


class Verdict(BaseModel):
    """What the evaluator must return."""

    approved: bool
    feedback: str = ""


class ReviseLoop(Pattern):
    """The writer drafts an answer. The evaluator approves it or says what to change, and
    the writer revises. This repeats until the evaluator approves or `max_rounds` reviews
    have been made; the latest draft is the answer either way.

    Use this when a critique pass measurably improves the output. Each round costs two
    more runs, so check with evals that it does."""

    kind: ClassVar[str] = "revise_loop"
    writer: str
    evaluator: str
    max_rounds: int = Field(default=3, ge=1)  # reviews, so at most max_rounds - 1 revisions

    async def run(self, input: str, adapter: Any, project: Project, ctx: RunContext) -> RunResult:
        writer, evaluator = self.agents(project, self.writer, self.evaluator)
        reviewer = evaluator.model_copy(update={"output_schema": Verdict})
        draft: RunResult = await adapter.run(writer, input, ctx)
        for round_number in range(1, self.max_rounds + 1):
            if draft.stop_reason != "completed":
                return draft
            text = text_of(draft)
            review: RunResult = await adapter.run(
                reviewer, f"Task:\n{input}\n\nDraft to review:\n{text}", ctx
            )
            if review.stop_reason != "completed":
                return review
            if review.output.approved or round_number == self.max_rounds:
                break
            draft = await adapter.run(
                writer,
                f"Task:\n{input}\n\nYour previous draft:\n{text}\n\n"
                f"Reviewer feedback:\n{review.output.feedback}\n\nWrite an improved version.",
                ctx,
            )
        return draft
