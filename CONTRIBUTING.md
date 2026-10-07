# Contributing to agentkit

Thank you for thinking about contributing. This project is small on purpose, and the most useful contributions are often a clear bug report, a documentation fix, or a new provider, tool or grader written to the existing pattern. This page tells you how to do each.

By taking part you agree to follow the [Code of Conduct](CODE_OF_CONDUCT.md). To report a **security** problem, do not open an issue; follow [SECURITY.md](SECURITY.md).

## Ways to help

### Report a bug

Open an issue using the bug report template. The most useful reports include:

- what you ran, and what you expected;
- what happened, with the full error message;
- the output of `uv run agentkit doctor` and the kit version;
- the smallest project, config or eval case that reproduces it.

A run's trace is very helpful, but it can contain your prompts and data. Read it before attaching it, and use `observability.capture_content: false` if in doubt.

### Report something that does not work against a real provider

The Anthropic, OpenAI and Gemini providers have been tested with stubs and mocks only, so reports from real use are especially valuable. Include the output of `uv run agentkit models test <role>`, with keys removed.

### Suggest a feature

Open an issue first, before writing code, describing the problem you are trying to solve and why the current kit does not solve it. The kit follows a few firm rules (see the [development guide](docs/development.md#rules-that-shape-every-change)): no speculative abstractions, no features outside its stated scope, and an abstraction only when two real agents need the same thing. Several things are deliberately left out, such as a web UI, a hosted service and a vector store, and a proposal for one of those is unlikely to be accepted. Agreeing the idea first saves you from writing something that cannot be merged.

### Improve the documentation

Fix a typo, clarify a confusing paragraph, add a missing example. Documentation changes need no issue first. If a page is wrong, say so even if you do not know the fix.

### Send a code change

Small fixes can go straight to a pull request. For anything larger, open an issue first.

## Making a change

1. **Fork and branch.** Branch from `main`, with a short descriptive name.
2. **Set up** as the [development guide](docs/development.md#set-up) describes: `uv sync`.
3. **Make the change**, keeping it focused. One pull request should do one thing.
4. **Add or update tests.** A bug fix includes a test that fails without the fix. A new feature includes tests for what it does and for how it fails. Unit tests must not need a network or an API key.
5. **Update the documentation** in the same change if you changed behaviour that a page describes. Some documentation is checked by tests: a new command, option, builtin tool, configuration key or environment variable must be added to its reference page, or the tests fail.
6. **Update the changelog.** Add a line under the unreleased version in [CHANGELOG.md](CHANGELOG.md). A change that breaks `AgentSpec`, the tool contract, the configuration schema or the eval format, including a changed default, needs a migration note.
7. **Run the checks.** All of these must pass:

   ```bash
   uv run ruff check . && uv run ruff format --check .
   uv run mypy
   uv run pytest
   ```

   If you added or changed an example project, also run its evals: `uv run agentkit eval run --tags smoke --project-dir examples/<name>`.
8. **Open a pull request** with the template filled in. Say what changed and why, and what you did to check it. If something is not verified, say so.

## Style

- Python 3.11 or newer, fully typed. `mypy --strict` must pass.
- `ruff` formats and lints; `uv run ruff format .` fixes formatting. Lines are 100 characters.
- Match the surrounding code's naming, comment density and idiom.
- Error messages are written for the person reading them: name the cause, and say what to do next. Every kit error derives from `AgentKitError`.
- Docstrings of tools are written for the model, not for developers.

## Commit messages

Write a short summary line in the imperative mood ("Add a SQL query tool"), then a paragraph saying what changed and why when the reason is not obvious. Say plainly what is untested.

## Adding to the kit

The [development guide](docs/development.md#extending-the-kit) has a recipe for each common extension: a provider, a builtin tool, a grader, a pattern, a configuration key, a command and a runtime adapter.

## Review

A maintainer will review your pull request. Expect questions, and requests to simplify: the project values small modules and few concepts over completeness. It helps to keep a pull request small, to respond to comments, and to say if you can no longer work on it.

## Licence

agentkit is licensed under the [Apache License 2.0](LICENSE). Unless you state otherwise, a contribution you submit for inclusion is licensed under the same terms, as the licence provides (section 5). Do not submit code you do not have the right to contribute, including code copied from a project with an incompatible licence.
