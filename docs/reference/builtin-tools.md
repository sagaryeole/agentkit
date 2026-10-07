# Builtin tools

The kit ships 14 ready-made tools. Enable the ones a project needs in `tools.enabled`, then list them by name in the `tools` of each agent that should have them:

```yaml
# agentkit.yaml
tools:
  enabled: [calculator, read_file, list_files, search_files]
```

By default `http_fetch`, `read_file` and `calculator` are enabled. An agent can only use tools its `AgentSpec` names, whatever is enabled.

See what the model is told about any of them with `agentkit tools describe <name>`, and try a call with `agentkit tools call <name> --args '{...}'`. The [writing-tools guide](../writing-tools.md) explains the contract they follow, including approvals and untrusted output.

## Summary

| Tool | Side effects | Default policy | Untrusted output | Needs configuration |
|---|---|---|---|---|
| [`calculator`](#calculator) | none | allow | no | no |
| [`current_time`](#current_time) | none | allow | no | no |
| [`read_file`](#read_file) | none | allow | yes | the workspace folder |
| [`list_files`](#list_files) | none | allow | yes | the workspace folder |
| [`search_files`](#search_files) | none | allow | yes | the workspace folder |
| [`write_file`](#write_file) | write | require approval | no | the workspace folder |
| [`http_fetch`](#http_fetch) | read_external | allow | yes | `allowed_domains` |
| [`web_search`](#web_search) | read_external | allow | yes | a SearXNG address |
| [`sql_query`](#sql_query) | none | allow | yes | a SQLite file |
| [`ask_human`](#ask_human) | none | allow | no | an approval channel that can answer |
| [`remember`](#remember) | write | allow (override) | no | no |
| [`recall`](#recall) | none | allow | no | no |
| [`finish`](#finish) | none | allow | no | no |
| [`handoff`](#handoff) | none | allow | no | added by `sub_agents` |

"Untrusted output" means the result is delimited for the model as outside content. "Default policy" is what the shipped `tools.approval` configuration does with the tool; change it in config.

## The file tools and the workspace

`read_file`, `list_files`, `search_files` and `write_file` work inside one folder, set by `tools.settings.filesystem.root` (default `./workspace`, relative to the project). Every path the model gives is resolved against that folder, **including through symbolic links**, and anything that ends up outside it is refused with a `permission` error. If the folder does not exist, the tools say so; create it first.

### `read_file`

Read a text file from the workspace, in pieces.

| Argument | Type | Default | Meaning |
|---|---|---|---|
| `path` | string | required | File path relative to the workspace root. |
| `offset` | integer ≥ 0 | `0` | Character position to start from. |
| `max_chars` | integer 1–15000 | `10000` | Maximum characters to return. |

Returns `path`, `content`, `total_chars` and `next_offset`. When `next_offset` is not null, call again with it as `offset` to read on. Errors: `not_found` if the path is not a file, `permission` outside the workspace.

### `list_files`

List the files and directories directly inside a workspace directory. It does not recurse.

| Argument | Type | Default | Meaning |
|---|---|---|---|
| `path` | string | `.` | Directory relative to the workspace root. |

Returns a list of `name`, `type` (`file`, `directory` or `symlink`) and `size` (files only), sorted by name, up to 500 entries.

### `search_files`

Find lines containing some text in the workspace files. The match is a plain, case-insensitive substring, not a regular expression.

| Argument | Type | Default | Meaning |
|---|---|---|---|
| `query` | string | required | Text to look for. |
| `path` | string | `.` | Directory or single file to search. |
| `max_results` | integer 1–200 | `50` | Stop after this many matching lines. |

Returns a list of `path`, `line` (1-based) and `text` (trimmed to 300 characters). It skips files larger than 1 MB, files that are not UTF-8 text, and links that lead outside the workspace.

### `write_file`

Write a text file in the workspace, creating folders as needed. **This tool has side effects and needs approval** unless configuration allows it.

| Argument | Type | Default | Meaning |
|---|---|---|---|
| `path` | string | required | File path relative to the workspace root. |
| `content` | string | required | Text to write. |
| `append` | boolean | `false` | Add to the end instead of replacing the file. |

Returns `path` and `chars_written`. Errors: `invalid_input` if the path is a directory, `permission` outside the workspace.

## Computation and time

### `calculator`

Evaluate an arithmetic expression exactly. It parses the expression and evaluates only arithmetic; it never uses `eval`, so nothing else can run.

| Argument | Type | Meaning |
|---|---|---|
| `expression` | string, up to 500 characters | For example `(1717 * 2323) / 7` or `sqrt(2) * pi`. |

Supported: numbers, `+ - * / // % **`, parentheses, the constants `pi` and `e`, and the functions `sqrt`, `exp`, `log`, `log10`, `log2`, `sin`, `cos`, `tan`, `floor`, `ceil`, `abs`, `round`, `min`, `max`. Anything else, such as names, attribute access, strings or lists, is refused with `invalid_input`, as are division by zero and powers too large to compute.

### `current_time`

Return the current date and time in a time zone.

| Argument | Type | Default | Meaning |
|---|---|---|---|
| `timezone` | string | `UTC` | An IANA name such as `Europe/Berlin` or `Asia/Kolkata`. |

Returns `iso` (with the UTC offset), `timezone` and `weekday`. An unknown zone is `invalid_input`.

## Reaching outside

### `http_fetch`

Fetch a web page and return its text. **Nothing can be fetched until `tools.settings.http_fetch.allowed_domains` is set.**

| Argument | Type | Default | Meaning |
|---|---|---|---|
| `url` | http or https URL | required | The page to fetch. |
| `max_bytes` | integer 1–2,000,000 | `200000` | Truncate the body after this many bytes. |

Returns `url` (after redirects), `status`, `content_type`, `text` and `truncated`. HTML is reduced to its visible text, with scripts and styles removed. Only text content types are returned.

Safety rules, enforced in code:

- Only `http` and `https`.
- The host must match `allowed_domains`. `*.example.com` also matches `example.com`; `*` allows everything and removes the main protection.
- The host is resolved by the tool itself, and the request is refused unless **every** address is public. Loopback, link-local, private and carrier-grade-NAT ranges are blocked, including the IPv4 forms of IPv6 addresses.
- The connection goes to the address that was checked, with the original `Host` header and TLS server name, so a second DNS answer cannot redirect it.
- Up to five redirects are followed, and each hop is checked by the same rules.

```yaml
tools:
  settings:
    http_fetch:
      allowed_domains: ["docs.example.com", "*.wikipedia.org"]
```

### `web_search`

Search the web and return titles, URLs and snippets, through a [SearXNG](https://docs.searxng.org) instance with its JSON output format enabled.

| Argument | Type | Default | Meaning |
|---|---|---|---|
| `query` | string | required | What to search for. |
| `max_results` | integer 1–20 | `5` | How many results to return. |

Returns a list of `title`, `url` and `snippet` (up to 500 characters). Configure the instance:

```yaml
tools:
  settings:
    web_search:
      backend: searxng
      base_url: http://127.0.0.1:8080
```

If SearXNG answers 403, its `settings.yml` needs `json` added to `search.formats`. Queries go to the configured service, so treat what goes in them as sent there. SearXNG is the only backend that ships; the backend sits behind one interface so another can be added.

### `sql_query`

Run one read-only `SELECT` against a SQLite database.

| Argument | Type | Default | Meaning |
|---|---|---|---|
| `query` | string | required | One `SELECT` or `WITH ... SELECT` statement. |
| `max_rows` | integer ≥ 1 | `100` | Return at most this many rows (also capped by `max_rows` in settings). |

Returns `columns`, `rows` and `truncated`. Anything other than a single `SELECT` is refused. The file is opened read-only with `query_only` set, so even a statement that slipped through could not write, and queries stop after a time limit.

```yaml
tools:
  settings:
    sql_query:
      database: data/shop.db      # relative to the project
      max_rows: 1000
      timeout_seconds: 10
```

## Talking to the operator and keeping notes

### `ask_human`

Ask the operator a question and wait for the answer. Use it when the agent cannot continue without something only a person knows.

| Argument | Type | Meaning |
|---|---|---|
| `question` | string | One clear question, with the context needed to answer it. |

The question goes to the run's approval channel. The terminal channel (`cli`) asks at the prompt; a `callback` channel answers through the function you gave it; `auto_deny`, used for unattended runs and evals, answers with a `permission` error saying nobody is available, so the agent continues without asking.

### `remember`

Save a short note under a key for the rest of the session. Saving under an existing key replaces the note. It is allowed without approval by the shipped configuration, because it only writes to the session's own notes.

| Argument | Type | Meaning |
|---|---|---|
| `key` | string | A short name, such as `customer_name`. |
| `value` | string | The note. |

### `recall`

Read back notes saved with `remember`.

| Argument | Type | Default | Meaning |
|---|---|---|---|
| `key` | string | all notes | The note to read. Leave out for every note. |

Notes are scoped to the session (or to the run, when there is no session). Reading an unknown key is a `not_found` error that lists the saved keys. In `agentkit chat`, notes persist with the session; see [Sessions and chat](../sessions.md).

## Control

### `finish`

Declare the task complete and end the run, handing back a result. A successful call ends the run immediately with `completed`.

| Argument | Type | Meaning |
|---|---|---|
| `summary` | string | One or two sentences on what was done. |
| `result` | object, optional | The structured result, when the task asks for one. |

The run's output is `result` if given, otherwise `summary`, and it is parsed into the agent's `output_schema` if it has one. Give an agent `finish` when you want a structured, explicit end to a task rather than a free-text reply.

### `handoff`

Transfer the task to another agent, which takes over and gives the final answer.

| Argument | Type | Meaning |
|---|---|---|
| `agent` | string | Name of the agent to transfer to. |
| `message` | string | The task for that agent, with every detail it needs. It does not see the earlier conversation. |

You do not enable or list this tool. **An agent that sets `sub_agents` gets it automatically**, with a description that lists those agents. See [Patterns](../patterns.md#hand-off-between-agents). Hand-offs run on the native runtime only.

## Tools that are not builtin

- **Your own tools:** write them in your project's `tools/` folder; see [Writing tools](../writing-tools.md).
- **Tools from MCP servers:** declare the server under `tools.mcp_servers`; see [Writing tools](../writing-tools.md#tools-from-mcp-servers).
- **A tool that executes code** is not provided. It would need a sandbox, and the kit does not ship one.
