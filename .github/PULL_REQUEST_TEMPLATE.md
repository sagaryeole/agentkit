## What this changes and why

<!-- One change per pull request. Link the issue it addresses. -->

## How it was checked

<!-- What you ran. Say plainly what is not tested, for example anything that needs a live provider. -->

## Checklist

- [ ] `uv run ruff check . && uv run ruff format --check .` passes
- [ ] `uv run mypy` passes
- [ ] `uv run pytest` passes, with no network and no API key needed
- [ ] New behaviour has tests, and a bug fix has a test that fails without it
- [ ] Documentation is updated, including the reference pages for any new command, option, tool, configuration key or environment variable
- [ ] `CHANGELOG.md` has an entry, with a migration note if this changes `AgentSpec`, the tool contract, the config schema or the eval format (a changed default counts)
- [ ] If an example project changed, its eval suite passes: `uv run agentkit eval run --tags smoke --project-dir examples/<name>`
