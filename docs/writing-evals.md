# Writing evals

Evals answer one question: after a change to a prompt, a tool or a model, is the agent better, worse or the same? An agent without an eval suite is not finished, and changing any of those three means running the suite again.

## A suite

One YAML file per suite, in your project's `evals/` folder:

```yaml
suite: support_triage
agent: triage_agent              # an agent or workflow defined in agents/
defaults:
  graders: [no_error, within_budget]   # applied to every case

cases:
  - id: refund_simple
    input: "I was charged twice for order 1182."
    setup:
      fixtures: [orders_small]         # scripted tool results, see below
    expect:
      tool_calls:
        must_include: [lookup_order]
        must_not_include: [issue_refund]
      output:
        contains: ["1182"]
    graders:
      - type: llm_judge
        rubric: "The reply acknowledges the duplicate charge and states the next step."
    tags: [billing, smoke]
```

A case passes when every grader passes. Case ids must be unique within a suite. Unknown keys are errors, so a typo does not silently disable a check.

A suite can also be a `.jsonl` file: one line with `suite`, `agent` and `defaults`, and one line per case.

## Running

```
agentkit eval run evals/                      # every suite in the folder
agentkit eval run evals/support.yaml --tags smoke
agentkit eval run evals/ --repeat 3           # each case three times
agentkit eval report                          # the last run again
agentkit eval compare <baseline> latest
```

Each run prints a report and saves its results as JSON under `evals.results_dir` (`.agentkit/evals/`). A run is referred to by its ID, the start of its ID, a file path, or `latest`.

`eval run` exits with status 1 when the pass rate is below `evals.fail_under` (100 by default; override with `--fail-under`).

Agent output varies between runs. `--repeat N` runs each case N times and reports how many attempts passed, which tells a flaky case from a broken one.

## What a case runs against

By default an eval is cheap and repeatable, and cannot change anything:

**Tools.** Unless you pass `--live`:

- A tool with a fixture answers from the fixture.
- A tool with no side effects (`side_effects="none"`) and no fixture runs for real.
- Any other tool with no fixture returns an error saying it has no fixture.

**Approvals.** Every call that needs approval is denied, unless the case lists the tool in `setup.approve_tools`. This holds with `--live` too.

**The model.** In this order:

1. With `--replay`, recorded responses. With `--record`, the configured model, recording as it goes.
2. Otherwise, if the case has `setup.fake_model`, those scripted replies.
3. Otherwise the configured model.

### Scripted model replies

`fake_model` lists what the model "says", turn by turn. No model is called, so the case needs no key and no network.

```yaml
    setup:
      fake_model:
        - text: "Let me look that up."
          tool_call: { name: lookup_order, arguments: { order_id: 1182 } }
        - text: "Order 1182 was charged twice; I have asked for a refund review."
```

A turn has `text`, a `tool_call`, several `tool_calls`, or text with calls. When the agent hands off to another agent or is a workflow, all agents consume the same script in order.

A scripted case does not test the model. It tests everything around it: that the prompt file loads, the tools are wired and validated, approvals and budgets behave, and the output is parsed. That is what a fast check on every commit should cover. To find out whether the model itself behaves, remove `fake_model` or use `--record`.

### Fixtures

A fixture scripts tool results. It lives in `evals/fixtures/<name>.yaml`:

```yaml
tools:
  lookup_order:
    - when: { order_id: 1182 }                  # matches when these arguments are equal
      returns: { id: 1182, status: charged_twice }
    - error: { kind: not_found, message: "No such order." }   # no `when`: matches anything
```

Rules are tried in order and the first match wins. A call that matches no rule returns a `not_found` error. A case can list several fixtures; later ones replace earlier ones tool by tool.

### Record and replay

To evaluate a real model without paying for it on every run, record once and replay:

```
agentkit eval run evals/ --record     # calls the model, writes evals/cassettes/<suite>.jsonl
agentkit eval run evals/ --replay     # answers from the cassette; no network, no key
```

A response is found by the exact request that produced it. If the prompt, a tool description or a tool result changes, replay reports that nothing is recorded for the request, and you record again. Keep tool results stable with fixtures; a tool that returns the current time will never replay. Commit the cassettes you want CI to use.

## Graders

`expect:` covers the common checks. Each key below is shorthand for the grader of the same name.

```yaml
    expect:
      tool_calls:
        must_include:                    # a name, or a name with arguments that must match
          - lookup_order
          - { name: lookup_order, arguments: { order_id: 1182 } }
        must_not_include: [issue_refund]
        in_order: [lookup_order, draft_reply]   # these calls, in this order, others may come between
      output:
        exact: "Done."
        contains: ["1182", "refund"]     # every string must appear
        regex: "refund(ed)?"
        schema: TriageResult             # a Pydantic model defined in agents/ or tools/
        fields: { category: billing }    # named fields of a structured output
```

Graders with parameters go under `graders:`.

| Grader | Parameters | Passes when |
|---|---|---|
| `no_error` | none | The run ended with `completed`. |
| `within_budget` | `max_steps`, `max_tokens`, `max_cost_usd`, `max_seconds` (all optional) | The run stayed under each stated limit and did not hit its own budget. |
| `exact` | `value` | The output equals the value, ignoring surrounding whitespace. |
| `contains` | `values` | Every value appears in the output. |
| `regex` | `pattern` | The pattern is found in the output. |
| `schema` | `name` | The output validates against that model. |
| `fields` | `fields` | Each named field of the structured output equals the expected value. |
| `tool_calls` | `must_include`, `must_not_include`, `in_order` | The calls made satisfy all three. |
| `trajectory` | `tool_sequence`, `max_steps` | The tools were called in exactly that sequence, within that many steps. |
| `llm_judge` | `rubric`, `min_score` (optional) | The judge model scores the output at or above the minimum. |
| `custom` | `function` | Your function says so. |

### LLM judge

```yaml
      - type: llm_judge
        rubric: "The reply states the refund amount in euros."
```

The judge is the model behind the `judge` role, set separately from the agent's model. It returns a score from 0 to 1 and a reason; the case passes at `evals.judge_min_score` (0.7) or above, or at the grader's own `min_score`.

Write one rubric per thing you care about, and make it something two people would score the same way. "The reply is good" cannot be graded. "The reply names the order number and gives a date" can.

When a case scripts the model with `fake_model`, the judge is skipped rather than called, and a skipped grade counts neither for nor against the case. The report lists skipped graders.

### Custom graders

A function of the case and the run result, in a module importable from the project folder:

```python
# graders.py in the project root
from agentkit.evals.graders import GradeResult


def under_fifty_words(case, result):
    words = len(str(result.output).split())
    return GradeResult(passed=words <= 50, reason=f"{words} words")
```

```yaml
      - type: custom
        function: "graders:under_fifty_words"
```

`result` is the `RunResult`: `output`, `stop_reason`, `steps`, `tool_calls`, `usage`, `error`, and `messages`, the full transcript. The function may be async.

## Reading the report

```
Suite support_triage (agent: triage_agent)
  pass rate: 66.7% (2/3 attempts, 3 cases)
    no_error       100.0%
    contains       50.0%
  steps:   mean 2.0, p95 3.0
  tokens:  mean 1204, p95 1890
  cost:    mean 0.0031, p95 0.0052
  latency: mean 2.10s, p95 3.40s
  failing cases:
    refund_simple (0/1 passed)
      contains: output lacks ['1182']: 'Your refund is on its way.'
      trace: 4f0c...
```

Under the pass rate is the pass rate of each grader, which shows what kind of thing is failing. Each failing case gives the grader's reason and the run ID of a failed attempt; `agentkit trace <run id>` shows what the agent did.

## Comparing two runs

```
agentkit eval run evals/            # before the change; note the run ID
# change the prompt, tool or model
agentkit eval run evals/
agentkit eval compare <baseline id> latest
```

`compare` lists the cases that regressed and improved, and exits with status 1 if the pass rate fell by more than `evals.max_pass_rate_drop` points (2 by default; `--max-drop` overrides). To judge whether a local model is good enough for an agent, run the suite on the hosted model, then with `MODEL_PROVIDER=local`, and compare the two.

## In CI

Tag a fast subset `smoke` and run it on every change, with scripted or replayed models so it needs no key:

```
agentkit eval run evals/ --tags smoke
```

Run the full suite against the real model on demand or on a schedule, and gate on `eval compare` against a stored baseline.

## Writing good cases

- Start from real inputs, including the awkward ones: missing information, two requests in one message, a request the agent must refuse.
- Check behaviour as well as the answer. `must_not_include` on a side-effecting tool catches an agent that acts before it should.
- Prefer exact checks (`fields`, `tool_calls`, `contains`) to a judge where they can express what you mean. They are free and never disagree with themselves.
- Give every tool at least one case, and every bug you fix a case that would have caught it.
- Keep a case to one behaviour, so a failure says what broke.
