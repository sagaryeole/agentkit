# Patterns

## Which one to start with

1. **Are the steps known in advance?** Use a [fixed workflow](#fixed-workflow). It is cheaper, faster and easier to test than letting a model decide the order.
2. **Otherwise, use a [single agent with tools](#single-agent-with-tools).** One agent with a clear prompt and a few well-described tools handles most tasks.
3. **Add more agents only on evidence from evals.** If a suite shows the single agent failing in a way that a split would fix (it confuses two kinds of request, or its context fills up), move to a [router](#router) or a [hand-off](#hand-off-between-agents). Every extra agent adds cost, latency and a new way to fail.

[Human in the loop](#human-in-the-loop) is not an alternative to these. Add it to any of them wherever a step has side effects or the agent is unsure.

| Pattern | Use when | In the kit |
|---|---|---|
| Single agent with tools | The default starting point | `AgentSpec` |
| Fixed workflow | The steps are known in advance | `Workflow` |
| Router | Inputs fall into categories handled differently | `router(...)` |
| Hand-off between agents | One agent's context is overloaded, or the skills are clearly separate | `AgentSpec.sub_agents` |
| Human in the loop | A step has side effects or low confidence | Approval gate, `ask_human` |
| Planner and executor | The task needs a plan that is then carried out | `PlanAndExecute` |
| Parallel fan-out and merge | Independent subtasks can run at the same time | `FanOut` |
| Evaluator and reviser loop | A critique pass improves quality | `ReviseLoop` |

Each pattern has a runnable project under `examples/` with an eval suite.

The multi-agent patterns (workflow, planner and executor, fan-out, reviser loop) are defined next to the agents in `agents/` and run by name, like an agent: `agentkit run <name> "..."`. Within one of them every agent is a nested run in the same trace and draws on one shared budget, so the limits in the config bound the whole pattern.

## Single agent with tools

One model, one prompt, a list of tools. The agent calls tools until it can answer.

```python
# agents/main.py
from agentkit.runtime.spec import AgentSpec

assistant = AgentSpec(
    name="assistant",
    description="Answers arithmetic questions.",
    instructions="prompts/main.md",      # a prompt file, or the prompt text itself
    tools=["calculator"],
)
```

Example: `examples/single_agent`.

Use it for almost everything at first. It stops being enough when the tool list grows past what the model chooses well from (the registry warns beyond 20), or when the prompt has to describe several unrelated jobs.

## Fixed workflow

Agents run one after another. The first receives the input; each later one receives the previous agent's output.

```python
from agentkit.patterns import Workflow

brief = Workflow(
    name="brief",
    description="Meeting notes in, a short brief out.",
    steps=["extractor", "writer"],       # agent names, in order
)
```

Run it by name like an agent: `agentkit run brief "..."`. The steps share one budget and appear as nested runs in one trace. If a step does not complete, the workflow stops and returns that step's result.

Example: `examples/workflow`. Start a project from it with `agentkit new <name> --pattern workflow`.

Use it when you could write the steps on a whiteboard before seeing the input. Each step can use a smaller model and a shorter prompt than one agent doing everything. Do not use it when the next step depends on what the previous one found in ways you cannot enumerate; that is what an agent is for.

## Router

A small agent reads the request and hands it to the specialist for its category.

```python
from agentkit.patterns import router

support = router(
    name="support",
    description="Sends each request to billing or shipping.",
    routes=["billing", "shipping"],      # names of agents defined in the project
)
```

The router runs on the `fast` model role, since routing is a classification task. It sees each route's `description`, so write those to say what the agent handles. The chosen specialist starts with a fresh context containing only the message the router passes, and its answer is the answer of the run.

Example: `examples/router`. Start a project from it with `agentkit new <name> --pattern router`.

Use it when requests fall into categories that need different prompts or tools, and misrouting is recoverable. Do not use it when one request often spans categories, or when there are only two routes and one prompt could cover both.

## Hand-off between agents

The router is one use of a general mechanism. Any agent that lists `sub_agents` gets a `handoff` tool and can transfer the task while it works:

```python
intake = AgentSpec(
    name="intake",
    description="Collects the details of a claim.",
    instructions="prompts/intake.md",
    tools=["lookup_policy"],
    sub_agents=["fraud_review"],
)
```

Control moves to the other agent and does not come back: its result ends the run. The receiving agent does not see the earlier conversation, so the calling agent must put everything needed into the hand-off message. Both share one budget.

Use it when a second stage needs a different prompt, tools or model, and the first stage can summarise what it learned. If the first agent needs the answer back to continue, hand-off is the wrong tool; make the second capability a tool instead.

Hand-offs run on the native adapter only. The other multi-agent patterns work on both adapters.

## Human in the loop

Two mechanisms, both already in every run.

**Approval.** A tool call whose policy is `require` waits for the run's approval channel. By default that covers every tool declared `write` or `destructive`. The operator sees the tool, its arguments and the sentence the agent wrote just before calling it, and can approve, deny, or edit the arguments. A denial goes back to the agent as an error, and the run continues so the agent can do something else or explain.

| `guardrails.approval_channel` | Behaviour |
|---|---|
| `cli` | Asks at the terminal. The default. |
| `callback` | Calls your function, for a web or chat front end. |
| `auto_deny` | Denies everything. For unattended runs and evals. |
| `auto_approve` | Approves everything. For tests; refused when `env` is `prod`. |

A callback that cannot answer yet returns `None`. The run then stops with `awaiting_approval` and a `pending` state, which is plain data you can store. Later, `adapter.resume(spec, pending, decision, ctx)` continues from that tool call.

**Asking.** Give the agent the `ask_human` tool and tell it in the prompt when to use it, for example when it does not know which of two things the operator means.

Example: `examples/human_in_loop`.

Tell the agent to state what it is about to do before a side-effecting call; that sentence is what the approver reads. Do not put approval on steps nobody will actually review, since people learn to approve without reading.

## Planner and executor

One agent turns the task into an ordered list of steps. Another carries out each step in its own run, seeing the task and the results so far.

```python
from agentkit.patterns import PlanAndExecute

solve = PlanAndExecute(
    name="solve",
    description="Plans a multi-step calculation, then works through it.",
    planner="planner",
    executor="doer",
    max_plan_steps=5,                    # a longer plan stops the run
)
```

The planner is made to return a list of steps as structured output; you do not set an output schema on it. The output of the last step is the answer of the run, so tell the planner to end with the step that writes the answer. A plan longer than `max_plan_steps` ends the run with an error before anything is executed.

Example: `examples/planner`.

Use it when a task needs several steps and you cannot list them before seeing the task. The planner can use the `reasoning` role and the executor a cheaper one. Do not use it when the steps are always the same (use a workflow), or when later steps depend on surprises in earlier ones: the plan is fixed once written, and a single agent adapts better.

## Parallel fan-out and merge

Several agents get the same input and run at the same time. One more agent receives the task and every result, and writes the answer.

```python
from agentkit.patterns import FanOut

decide = FanOut(
    name="decide",
    description="Collects the case for and against a proposal, then recommends.",
    workers=["benefits", "risks"],
    merger="advisor",
)
```

If any worker does not complete, the run stops with that worker's result and the merger is not called.

Example: `examples/fan_out`.

Use it when the parts do not depend on each other: several angles on one question, or several sources to check. It is faster than doing them in turn and costs the same. Do not use it when one worker needs another's result. Workers that need approval would prompt the operator at the same time, so keep side-effecting tools out of them.

## Evaluator and reviser loop

A writer drafts. An evaluator approves the draft or says what to change, and the writer revises, a bounded number of times.

```python
from agentkit.patterns import ReviseLoop

polish = ReviseLoop(
    name="polish",
    description="Writes an announcement and revises it until it meets the house rules.",
    writer="writer",
    evaluator="reviewer",
    max_rounds=3,                        # reviews, so at most two revisions
)
```

The evaluator is made to return a verdict (`approved`, `feedback`) as structured output. The loop ends when the evaluator approves or after `max_rounds` reviews; the latest draft is the answer either way.

Example: `examples/revise_loop`.

Use it when the evaluator checks something concrete that the writer often misses, such as a house style or a set of required facts. Give the evaluator explicit rules; "make it better" produces endless feedback. Each round is two more runs, so measure with evals that the extra rounds improve the result before paying for them.
