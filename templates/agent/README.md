# __PROJECT_NAME__

An agent built on agentkit.

```
agentkit doctor                              # check config and credentials
agentkit eval run evals/                     # starter evals; pass with no API key
agentkit run __AGENT_NAME__ "What is 17 times 23?"
agentkit chat __AGENT_NAME__                    # a conversation that is kept
agentkit trace <run id>                      # timeline of a run
```

- `agents/main.py` defines the agent; `prompts/main.md` is its prompt.
- `tools/` holds this project's own tools; builtin tools are enabled in `agentkit.yaml`.
- `evals/` holds eval suites. Re-run them after changing a prompt, a tool or the model.
