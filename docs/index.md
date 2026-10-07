# agentkit documentation

agentkit is a foundation for building AI agents you can test, observe and bound. This is the map of the documentation. If you are new, read the first three pages in order.

## Start here

1. **[Getting started](getting-started.md)** — install, create a project, run its evals, add a tool, connect a real model. About ten minutes.
2. **[Concepts](concepts.md)** — what a project, an agent, a run, a role and a trace are, and how a request travels through the kit.
3. **[Patterns](patterns.md)** — which agent shape to use, and why to start with the simplest.

## Guides

| Page | Answers |
|---|---|
| [Configuration](configuration.md) | How configuration is layered, every key and its default, and every environment variable. |
| [Models and providers](models.md) | Roles, the model catalogue, fallbacks, pricing, local models, and adding a provider. |
| [Writing tools](writing-tools.md) | The tool contract, approvals, failures, untrusted output, and MCP servers. |
| [Writing evals](writing-evals.md) | The dataset format, graders, fixtures, record and replay, and comparing runs. |
| [Guardrails](guardrails.md) | Budgets, stall detection, approvals, checks and redaction. |
| [Observability](observability.md) | Traces, logs, the trace viewer and OpenTelemetry. |
| [Sessions and chat](sessions.md) | Conversation history and what happens when it grows too long. |
| [Using agentkit as a library](library.md) | Running agents, streaming, approvals and sessions from your own Python code. |
| [ADK adapter](adk.md) | Running the same agents on Google ADK. |

## Reference

| Page | Contains |
|---|---|
| [CLI reference](reference/cli.md) | Every command and option, with exit codes. |
| [Builtin tools](reference/builtin-tools.md) | Every builtin tool: arguments, side effects, settings and safety notes. |
| [Changelog](../CHANGELOG.md) | Releases, what counts as a breaking change, and migration notes. |

## Help and contributing

| Page | For |
|---|---|
| [Troubleshooting and FAQ](troubleshooting.md) | Error messages and what to do about them. |
| [Security](../SECURITY.md) | The threat model and how to report a vulnerability. |
| [Contributing](../CONTRIBUTING.md) | How to propose a change. |
| [Development guide](development.md) | Repository layout, tests, conventions and how to extend the kit. |
| [Code of Conduct](../CODE_OF_CONDUCT.md) | How we treat each other. |

## Conventions in these docs

- Commands are written as `uv run agentkit ...`, run from a checkout of the repository. If you installed agentkit into your own project, drop the `uv run` and run `agentkit` from that project's environment.
- `--project-dir <dir>` points a command at a project. Without it, the current directory is the project. Every command that reads configuration also takes `--env <name>`.
- Placeholders in angle brackets, such as `<run id>` or `<dir>`, are for you to replace.
