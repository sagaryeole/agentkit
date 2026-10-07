# Security

## Reporting a vulnerability

**Please do not open a public issue for a security problem.** Report it privately so a fix can be ready before the details are public.

Use GitHub's private vulnerability reporting: on the repository page, open the **Security** tab and choose **Report a vulnerability**. If that option is not available, contact the maintainers privately by the means given in the repository's profile or `README`, and ask for a private channel before sharing details.

Include:

- what you did, with the smallest example that shows it (a config file, a tool, a prompt);
- what happened, and what you expected;
- the version (`uv run python -c "import agentkit; print(agentkit.__version__)"`) and your Python and operating system versions;
- whether it needs a particular model or provider.

The maintainers will acknowledge the report, investigate, and agree a timeline for a fix and disclosure with you. This is a volunteer-run project and cannot promise a fixed response time, but a report that is complete and reproducible is looked at first. You will be credited in the changelog unless you prefer not to be.

### Supported versions

The project is pre-1.0. Security fixes go into the latest release, and into `main`.

### What counts

A vulnerability here is a way to make the kit do something its guardrails are meant to prevent, for example: reading or writing outside the file workspace, fetching a loopback or private address through `http_fetch`, running a side-effecting tool without the approval its policy requires, leaking a secret into a trace or log, or defeating a budget. Weaknesses that follow from how language models work, such as a model being talked into something by text in a web page, are expected and are what the guardrails below are there to bound; reports that show a guardrail failing to bound them are very welcome.

## Threat model

An agent built on the kit reads content it does not control and can take actions. The kit assumes that the model can be misled and limits what a misled agent can do. Four threats shape the design.

### 1. Prompt injection through tool output

Text returned by a tool (a web page, a file, a database row, another agent's answer) can contain instructions. The model may follow them.

What the kit does:

- Tools that return outside content are declared `untrusted_output`. Their results reach the model inside a delimited block with a note that the content is data. The closing delimiter is escaped so content cannot end the block early.
- Nothing in tool output can change which tools the agent has, the approval policy or the limits. Those come only from configuration and code.

- Tools from MCP servers are always `untrusted_output`, and are treated as `write` until configuration declares otherwise; a server's own claims about its tools are ignored.

What remains: delimiting lowers the success rate of injection; it does not prevent it. The defences below are what bound the damage. The descriptions of MCP tools come from the server and enter the prompt as they are, so an MCP server must be trusted like code.

### 2. Excessive agency

An agent does more than the task needs, by mistake or because it was told to by injected text.

What the kit does:

- Least privilege: an agent receives only the tools named in its `AgentSpec`.
- A tool is treated as side-effecting unless it declares otherwise, and side-effecting calls need approval unless configuration allows them. A destructive tool can only be allowed by an override naming that tool.
- The default approval channel in code denies. Unattended runs and evals deny every side-effecting call unless a case explicitly allows a tool.
- `auto_approve` refuses to load when `env` is `prod`.
- File tools resolve paths, including symbolic links, and refuse anything outside one configured root.
- `sql_query` accepts a single `SELECT`, opens the database read-only and limits rows and time.
- `calculator` evaluates a restricted arithmetic syntax and never calls `eval`.
- There is no tool that executes model-written code.
- A local MCP server is started with a minimal environment plus only the variables it is granted.

What remains: an approver who approves without reading, and any project tool that is declared `none` but has side effects.

### 3. Data exfiltration through tools

A misled agent sends data it can read to somewhere an attacker can see, for example by fetching a URL with the data in it.

What the kit does:

- `http_fetch` fetches nothing until `allowed_domains` is configured, and then only those domains. It refuses non-http schemes and loopback, link-local and private addresses, connects to the address it checked rather than resolving the name again, and applies the same checks to every redirect.
- Falling back from a local model to a hosted one is off unless configured on a role mapped to a local model, so data does not leave the machine implicitly.
- Secrets come from the environment, never from YAML. Before a trace is written or a log line is emitted, the values of secret-looking environment variables and anything matching `guardrails.redact_patterns` (emails and API-key shapes by default) are redacted. `observability.capture_content: false` stores metadata only.
- The same redactor also runs on what the model is given and says: by default on tool output and on the final answer (`guardrails.redact_at`), so a secret a tool returns never reaches the model and cannot be repeated to the user. It does not run on the user's own input unless you add `user_input`. A tool that must return emails to the model needs `tool_output` removed from `redact_at`.

What remains: conversation history and session notes are stored unredacted in the project's `.agentkit/memory.db`, so protect that file like the data it holds; an allowed domain that reflects or stores what it is sent, `web_search` queries (which go to the configured search service), and project tools that send data out. Setting `allowed_domains` to `*` removes the main protection. Redaction is pattern based, so it catches the shapes it knows (emails, common key formats) and the values of secret-looking environment variables, not every kind of personal data or credential; it also does not apply to the user's input by default.

### 4. Runaway cost

A loop, a stuck agent or a large input spends without limit.

What the kit does:

- Every run has a budget: steps, tool calls, input tokens, output tokens, cost and wall time. It is checked before every model call and every tool call, and a breach stops the run and names the limit.
- A run stops when it repeats the same tool call or makes no progress.
- Model calls and tool calls have timeouts; retries are bounded.
- Tool output is truncated before it reaches the model.

What remains: cost is only tracked for models with an entry in the pricing config, and a single model call can exceed a token limit before the next check. The wall-time limit is checked between calls, not during one.

## For people building on the kit

- Declare `side_effects` honestly on every tool, and mark outside content `untrusted_output`.
- Keep `allowed_domains` narrow.
- Do not give one agent both a way to read untrusted content and an unapproved way to send data out.
- Review eval cases that set `approve_tools` as carefully as code.
- Dependencies are pinned in `uv.lock` and scanned in CI with `pip-audit`.
