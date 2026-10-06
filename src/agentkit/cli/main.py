"""The `agentkit` command. Sync wrappers over the kit live here and nowhere else."""

import asyncio
import json
import time
from pathlib import Path
from typing import Annotated

import typer
import yaml
from pydantic_core import to_jsonable_python

from agentkit.cli.scaffold import create_project
from agentkit.config import Config, EnvSecretProvider, load_config
from agentkit.config.loader import PROJECT_FILE
from agentkit.errors import AgentKitError, ConfigError
from agentkit.evals.dataset import load_suites
from agentkit.evals.report import all_attempts, compare, pass_rate, render_comparison, render_report
from agentkit.evals.runner import load_run, run_suites, save_run
from agentkit.guardrails.approvals import make_channel
from agentkit.models import Message, ModelRequest
from agentkit.models.providers.local import list_local_models
from agentkit.models.registry import get_model, local_api_base, resolve_chain
from agentkit.observability.tracer import read_trace, render_trace
from agentkit.runtime.adapters import get_adapter
from agentkit.runtime.context import RunContext
from agentkit.runtime.executor import execute_tool
from agentkit.runtime.project import load_project
from agentkit.runtime.spec import RunResult
from agentkit.tools import ToolRegistry

app = typer.Typer(no_args_is_help=True, help="Build, run and evaluate AI agents.")
config_app = typer.Typer(no_args_is_help=True, help="Inspect and validate configuration.")
models_app = typer.Typer(no_args_is_help=True, help="List and test models.")
app.add_typer(config_app, name="config")
tools_app = typer.Typer(no_args_is_help=True, help="List, describe and call tools.")
app.add_typer(models_app, name="models")
eval_app = typer.Typer(no_args_is_help=True, help="Run, report and compare evals.")
app.add_typer(tools_app, name="tools")
app.add_typer(eval_app, name="eval")

EnvOption = Annotated[
    str | None, typer.Option("--env", help="Environment name; selects agentkit.<env>.yaml.")
]
ProjectDirOption = Annotated[
    Path, typer.Option("--project-dir", help="Directory containing agentkit.yaml.")
]

# Environment variables that can hold credentials for each hosted provider.
PROVIDER_KEY_VARS = {
    "anthropic": ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"),
    "google": ("GOOGLE_API_KEY", "GEMINI_API_KEY"),
    "openai": ("OPENAI_API_KEY",),
}


def _load_or_exit(project_dir: Path, env: str | None) -> Config:
    try:
        return load_config(project_dir, env=env)
    except ConfigError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc


def _local_models(cfg: Config) -> list[str]:
    local = cfg.models.local
    return asyncio.run(
        list_local_models(local_api_base(cfg), server=local.server, api_key=local.api_key)
    )


@config_app.command("show")
def config_show(env: EnvOption = None, project_dir: ProjectDirOption = Path(".")) -> None:
    """Print the merged configuration as YAML."""
    cfg = _load_or_exit(project_dir, env)
    typer.echo(yaml.safe_dump(cfg.model_dump(mode="json"), sort_keys=False), nl=False)


@config_app.command("validate")
def config_validate(env: EnvOption = None, project_dir: ProjectDirOption = Path(".")) -> None:
    """Check that the merged configuration is valid."""
    cfg = _load_or_exit(project_dir, env)
    typer.echo(f"Configuration is valid (env: {cfg.env}).")


@models_app.command("list")
def models_list(
    local: Annotated[
        bool, typer.Option("--local", help="List the model IDs the local server offers.")
    ] = False,
    env: EnvOption = None,
    project_dir: ProjectDirOption = Path("."),
) -> None:
    """Show which model each role resolves to."""
    cfg = _load_or_exit(project_dir, env)
    try:
        if local:
            for model_id in _local_models(cfg):
                typer.echo(model_id)
            return
        for role in cfg.models.roles:
            chain = resolve_chain(cfg, role)
            entry = cfg.models.models[chain[0]]
            fallbacks = f"  (fallbacks: {', '.join(chain[1:])})" if chain[1:] else ""
            typer.echo(f"{role:<10} {chain[0]}  [{entry.provider}]{fallbacks}")
    except AgentKitError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc


@models_app.command("test")
def models_test(
    role: Annotated[str, typer.Argument(help="Model role or key to call.")] = "default",
    env: EnvOption = None,
    project_dir: ProjectDirOption = Path("."),
) -> None:
    """Send one short request to a model and report tokens, cost and latency."""
    cfg = _load_or_exit(project_dir, env)
    request = ModelRequest(messages=[Message.from_text("user", "Reply with the single word: ok")])
    started = time.monotonic()
    try:
        model = get_model(role, cfg)
        response = asyncio.run(model.generate(request))
    except AgentKitError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    usage = response.usage
    cost = "unpriced" if usage.cost_usd is None else f"${usage.cost_usd:.6f}"
    typer.echo(f"model:   {model.key} ({response.model_id})")
    typer.echo(f"reply:   {response.message.text.strip()}")
    typer.echo(f"tokens:  {usage.input_tokens} in, {usage.output_tokens} out")
    typer.echo(f"cost:    {cost}")
    typer.echo(f"latency: {time.monotonic() - started:.1f}s")


def _registry_or_exit(cfg: Config) -> ToolRegistry:
    try:
        return ToolRegistry.from_config(cfg)
    except AgentKitError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc


@tools_app.command("list")
def tools_list(env: EnvOption = None, project_dir: ProjectDirOption = Path(".")) -> None:
    """List the enabled tools with their side effects."""
    for tool in _registry_or_exit(_load_or_exit(project_dir, env)):
        summary = tool.description.splitlines()[0]
        typer.echo(f"{tool.name:<14} {tool.side_effects:<14} {summary}")


@tools_app.command("describe")
def tools_describe(
    name: Annotated[str, typer.Argument(help="Tool name.")],
    env: EnvOption = None,
    project_dir: ProjectDirOption = Path("."),
) -> None:
    """Show what the model is told about a tool."""
    registry = _registry_or_exit(_load_or_exit(project_dir, env))
    try:
        tool = registry.get(name)
    except AgentKitError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"{tool.name}  (side effects: {tool.side_effects}, tags: {', '.join(tool.tags)})")
    typer.echo(f"\n{tool.description}\n")
    typer.echo(json.dumps(tool.spec.input_schema, indent=2))


@tools_app.command("call")
def tools_call(
    name: Annotated[str, typer.Argument(help="Tool name.")],
    args: Annotated[str, typer.Option("--args", help="Arguments as a JSON object.")] = "{}",
    env: EnvOption = None,
    project_dir: ProjectDirOption = Path("."),
) -> None:
    """Call a tool the way an agent would and print what the model would see."""
    cfg = _load_or_exit(project_dir, env)
    registry = _registry_or_exit(cfg)
    try:
        tool = registry.get(name)
        arguments = json.loads(args)
        if not isinstance(arguments, dict):
            raise ValueError("not an object")
    except AgentKitError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    except ValueError as exc:
        typer.echo(
            f'--args must be a JSON object, for example \'{{"path": "."}}\' ({exc}).', err=True
        )
        raise typer.Exit(1) from exc
    result = asyncio.run(execute_tool(tool, arguments, RunContext(cfg)))
    typer.echo(result.content)
    if result.error is not None:
        raise typer.Exit(1)


@app.command()
def new(
    name: Annotated[str, typer.Argument(help="Name of the project directory to create.")],
    pattern: Annotated[
        str, typer.Option("--pattern", help="Agent shape to start from.")
    ] = "single",
    directory: Annotated[Path, typer.Option("--dir", help="Where to create the project.")] = Path(
        "."
    ),
) -> None:
    """Create a new agent project that runs and passes its starter evals straight away."""
    try:
        target = create_project(name, directory, pattern)
    except AgentKitError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"Created {target}. Next:")
    typer.echo(f"  cd {target}")
    typer.echo("  agentkit eval run evals/      # starter evals; no API key needed")
    typer.echo(f'  agentkit run {name.replace("-", "_")} "What is 17 times 23?"')


def run_summary(result: RunResult) -> str:
    """One line printed after every CLI run."""
    usage = result.usage
    cost = "unpriced" if usage.cost_usd is None else f"${usage.cost_usd:.4f}"
    trace = f" | trace: {result.run_id}" if result.trace_path else ""
    return (
        f"[{result.stop_reason}] {result.steps} steps | {result.tool_calls} tool calls | "
        f"{usage.input_tokens} in / {usage.output_tokens} out tokens | {cost} | "
        f"{result.duration_seconds:.1f}s{trace}"
    )


@app.command()
def run(
    agent: Annotated[str, typer.Argument(help="Name of an agent defined in agents/.")],
    input: Annotated[str, typer.Argument(help="What to ask the agent.")],
    model: Annotated[
        str | None, typer.Option("--model", help="Model role or key to use instead.")
    ] = None,
    env: EnvOption = None,
    project_dir: ProjectDirOption = Path("."),
) -> None:
    """Run an agent once and print its answer and a summary of the run."""
    cfg = _load_or_exit(project_dir, env)
    try:
        project = load_project(project_dir, cfg)
        spec = project.agent(agent)
        if model:
            spec = spec.model_copy(update={"model": model})
        ctx = RunContext(cfg, approvals=make_channel(cfg))
        adapter = get_adapter(cfg, project.registry, project.root)
        result = asyncio.run(adapter.run(spec, input, ctx))
    except AgentKitError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    if result.output is not None:
        output = result.output
        typer.echo(output if isinstance(output, str) else json.dumps(to_jsonable_python(output)))
    if result.error is not None:
        typer.echo(f"{result.error.kind}: {result.error.message}", err=True)
    typer.echo(run_summary(result), err=True)
    if result.stop_reason != "completed":
        raise typer.Exit(1)


@app.command()
def trace(
    run_id: Annotated[str, typer.Argument(help="Run ID, or the start of one.")],
    env: EnvOption = None,
    project_dir: ProjectDirOption = Path("."),
) -> None:
    """Print the timeline of a run from its trace file."""
    cfg = _load_or_exit(project_dir, env)
    directory = Path(cfg.observability.jsonl_path)
    matches = sorted(directory.glob(f"{run_id}*.jsonl"))
    if len(matches) != 1:
        found = "no trace" if not matches else f"{len(matches)} traces"
        typer.echo(
            f"Found {found} matching '{run_id}' in {directory}. Give a full run ID; traces are "
            "written there when observability.exporter is jsonl.",
            err=True,
        )
        raise typer.Exit(1)
    typer.echo(render_trace(read_trace(matches[0])))


@eval_app.command("run")
def eval_run(
    path: Annotated[
        Path | None, typer.Argument(help="Suite file or directory; default evals.dataset_dir.")
    ] = None,
    tags: Annotated[
        str | None,
        typer.Option("--tags", help="Only cases with one of these comma-separated tags."),
    ] = None,
    repeat: Annotated[int, typer.Option("--repeat", min=1, help="Run each case this often.")] = 1,
    live: Annotated[bool, typer.Option("--live", help="Use real tools instead of fakes.")] = False,
    record: Annotated[
        bool, typer.Option("--record", help="Call the real model and record its responses.")
    ] = False,
    replay: Annotated[
        bool, typer.Option("--replay", help="Answer from recorded responses; no network.")
    ] = False,
    fail_under: Annotated[
        float | None, typer.Option("--fail-under", help="Exit 1 below this pass rate (percent).")
    ] = None,
    env: EnvOption = None,
    project_dir: ProjectDirOption = Path("."),
) -> None:
    """Run eval suites, print the report and save the results as JSON."""
    cfg = _load_or_exit(project_dir, env)
    if record and replay:
        typer.echo("Use --record or --replay, not both.", err=True)
        raise typer.Exit(1)
    target = path or Path(cfg.evals.dataset_dir)
    if not target.exists() and (project_dir / target).exists():
        target = project_dir / target
    try:
        project = load_project(project_dir, cfg)
        outcome = asyncio.run(
            run_suites(
                load_suites(target),
                project,
                repeat=repeat,
                tags=tags.split(",") if tags else None,
                live=live,
                cassette="record" if record else "replay" if replay else None,
            )
        )
    except AgentKitError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    saved = save_run(outcome, Path(cfg.evals.results_dir))
    typer.echo(render_report(outcome))
    typer.echo(f"\nResults: {saved}")
    attempts = all_attempts(outcome)
    threshold = cfg.evals.fail_under if fail_under is None else fail_under
    if not attempts:
        typer.echo("No cases matched.", err=True)
        raise typer.Exit(1)
    if pass_rate(attempts) < threshold:
        typer.echo(f"Pass rate {pass_rate(attempts):.1f}% is below {threshold:g}%.", err=True)
        raise typer.Exit(1)


@eval_app.command("report")
def eval_report(
    run_ref: Annotated[str, typer.Argument(help="Eval run ID, file path, or 'latest'.")] = "latest",
    env: EnvOption = None,
    project_dir: ProjectDirOption = Path("."),
) -> None:
    """Print the report of a saved eval run."""
    cfg = _load_or_exit(project_dir, env)
    try:
        typer.echo(render_report(load_run(run_ref, Path(cfg.evals.results_dir))))
    except AgentKitError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc


@eval_app.command("compare")
def eval_compare(
    baseline: Annotated[str, typer.Argument(help="Baseline eval run ID or file path.")],
    candidate: Annotated[str, typer.Argument(help="Candidate eval run ID, path or 'latest'.")],
    max_drop: Annotated[
        float | None,
        typer.Option("--max-drop", help="Exit 1 if the pass rate falls by more points."),
    ] = None,
    env: EnvOption = None,
    project_dir: ProjectDirOption = Path("."),
) -> None:
    """Show which cases regressed or improved between two eval runs."""
    cfg = _load_or_exit(project_dir, env)
    directory = Path(cfg.evals.results_dir)
    try:
        before, after = load_run(baseline, directory), load_run(candidate, directory)
    except AgentKitError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    result = compare(before, after)
    typer.echo(render_comparison(before, after, result))
    allowed = cfg.evals.max_pass_rate_drop if max_drop is None else max_drop
    if result.drop > allowed:
        typer.echo(
            f"The pass rate fell by {result.drop:.1f} points, more than the {allowed:g} allowed.",
            err=True,
        )
        raise typer.Exit(1)


@app.command()
def doctor(env: EnvOption = None, project_dir: ProjectDirOption = Path(".")) -> None:
    """Check configuration, provider credentials and the local model server."""
    try:
        cfg = load_config(project_dir, env=env)
    except ConfigError as exc:
        typer.echo(f"[fail] config: {exc}")
        raise typer.Exit(1) from exc
    typer.echo(f"[ ok ] config: valid (env: {cfg.env})")

    if (project_dir / PROJECT_FILE).is_file():
        typer.echo(f"[ ok ] project: found {PROJECT_FILE}")
    else:
        typer.echo(f"[warn] project: no {PROJECT_FILE} in {project_dir}; using shipped defaults")

    providers = {
        cfg.models.models[resolve_chain(cfg, role)[0]].provider for role in cfg.models.roles
    }
    failed = False
    secrets = EnvSecretProvider()
    for provider in sorted(providers):
        if provider == "local":
            try:
                offered = _local_models(cfg)
                typer.echo(f"[ ok ] local: server answers and offers {len(offered)} model(s)")
            except AgentKitError as exc:
                typer.echo(f"[fail] local: {exc}")
                failed = True
            continue
        key_vars = PROVIDER_KEY_VARS.get(provider)
        if key_vars is None:
            typer.echo(f"[ ok ] keys: provider '{provider}' needs no API key")
        elif any(secrets.get(var) for var in key_vars):
            typer.echo(f"[ ok ] keys: credentials for '{provider}' are set")
        else:
            typer.echo(
                f"[warn] keys: none of {', '.join(key_vars)} is set; '{provider}' models will "
                "fail unless the SDK finds a login profile"
            )
    typer.echo("[info] to check a model end to end, run `agentkit models test <role>`")
    if failed:
        raise typer.Exit(1)
