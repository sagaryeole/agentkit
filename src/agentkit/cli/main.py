"""The `agentkit` command. Sync wrappers over the kit live here and nowhere else."""

import asyncio
import json
import time
from pathlib import Path
from typing import Annotated

import typer
import yaml

from agentkit.config import Config, EnvSecretProvider, load_config
from agentkit.config.loader import PROJECT_FILE
from agentkit.errors import AgentKitError, ConfigError
from agentkit.models import Message, ModelRequest
from agentkit.models.providers.local import list_local_models
from agentkit.models.registry import get_model, local_api_base, resolve_chain
from agentkit.runtime.context import RunContext
from agentkit.runtime.executor import execute_tool
from agentkit.tools import ToolRegistry

app = typer.Typer(no_args_is_help=True, help="Build, run and evaluate AI agents.")
config_app = typer.Typer(no_args_is_help=True, help="Inspect and validate configuration.")
models_app = typer.Typer(no_args_is_help=True, help="List and test models.")
app.add_typer(config_app, name="config")
tools_app = typer.Typer(no_args_is_help=True, help="List, describe and call tools.")
app.add_typer(models_app, name="models")
app.add_typer(tools_app, name="tools")

EnvOption = Annotated[
    str | None, typer.Option("--env", help="Environment name; selects agentkit.<env>.yaml.")
]
ProjectDirOption = Annotated[
    Path, typer.Option("--project-dir", help="Directory containing agentkit.yaml.")
]

# Environment variables that can hold credentials for each hosted provider.
PROVIDER_KEY_VARS = {
    "anthropic": ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"),
    "google": ("GOOGLE_API_KEY",),
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
