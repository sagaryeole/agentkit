"""Load configuration by layering five sources, later ones overriding earlier:

1. config/default.yaml (plus models.yaml and pricing.yaml) shipped with the kit
2. the project's agentkit.yaml
3. agentkit.<env>.yaml
4. AGENTKIT_* environment variables, with `__` as the nesting separator
5. overrides passed in code or on the CLI
"""

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from agentkit.config.schema import Config
from agentkit.errors import ConfigError

ENV_PREFIX = "AGENTKIT_"
PROJECT_FILE = "agentkit.yaml"


def shipped_config_dir() -> Path:
    """Directory holding default.yaml, models.yaml and pricing.yaml."""
    here = Path(__file__).resolve()
    # Installed wheel first, then a source checkout.
    for candidate in (here.parents[1] / "_shipped_config", here.parents[3] / "config"):
        if (candidate / "default.yaml").is_file():
            return candidate
    raise ConfigError("Shipped default configuration not found. Reinstall agentkit.")


def read_yaml(path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML ({exc}). Fix the syntax and retry.") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: top level must be a mapping of keys to values.")
    return data


def deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    """Merge mappings recursively; any other value in `override` replaces the base value."""
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), Mapping):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def env_overrides(environ: Mapping[str, str]) -> dict[str, Any]:
    """Turn AGENTKIT_LIMITS__MAX_STEPS=10 into {"limits": {"max_steps": 10}}.

    Values are parsed as YAML so numbers, booleans and lists keep their type.
    """
    result: dict[str, Any] = {}
    for name, raw in environ.items():
        if not name.startswith(ENV_PREFIX):
            continue
        path = name[len(ENV_PREFIX) :].lower().split("__")
        try:
            value = yaml.safe_load(raw)
        except yaml.YAMLError:
            value = raw
        node = result
        for part in path[:-1]:
            child = node.get(part)
            if not isinstance(child, dict):
                child = node[part] = {}
            node = child
        node[path[-1]] = value
    return result


def _anchor_paths(cfg: Config, root: Path) -> Config:
    """Make relative paths in the config relative to the project directory, so a command
    behaves the same whether it is run inside the project or with --project-dir."""

    def anchored(value: str) -> str:
        return value if Path(value).is_absolute() else str(root / value)

    cfg.observability.jsonl_path = anchored(cfg.observability.jsonl_path)
    cfg.memory.sqlite_path = anchored(cfg.memory.sqlite_path)
    for name in ("dataset_dir", "results_dir", "cassette_dir"):
        setattr(cfg.evals, name, anchored(getattr(cfg.evals, name)))
    for section, key in (("filesystem", "root"), ("sql_query", "database")):
        settings = cfg.tools.settings.get(section)
        if settings and isinstance(settings.get(key), str):
            settings[key] = anchored(settings[key])
    return cfg


def load_config(
    project_dir: Path | None = None,
    *,
    env: str | None = None,
    overrides: Mapping[str, Any] | None = None,
    environ: Mapping[str, str] | None = None,
) -> Config:
    """Build the validated Config for a project directory (default: the current directory)."""
    root = project_dir or Path.cwd()
    environ = os.environ if environ is None else environ

    shipped_dir = shipped_config_dir()
    shipped = read_yaml(shipped_dir / "default.yaml")
    shipped["models"] = read_yaml(shipped_dir / "models.yaml")
    shipped["pricing"] = read_yaml(shipped_dir / "pricing.yaml")

    project_path = root / PROJECT_FILE
    project = read_yaml(project_path) if project_path.is_file() else {}
    from_env = env_overrides(environ)
    from_code = dict(overrides or {})
    if env is not None:
        from_code["env"] = env

    env_name = (
        from_code.get("env") or from_env.get("env") or project.get("env") or shipped.get("env")
    )
    env_path = root / f"agentkit.{env_name}.yaml"
    env_file = read_yaml(env_path) if env_path.is_file() else {}

    merged: dict[str, Any] = {}
    for layer in (shipped, project, env_file, from_env, from_code):
        merged = deep_merge(merged, layer)

    try:
        return _anchor_paths(Config.model_validate(merged), root)
    except ValidationError as exc:
        problems = "\n".join(
            f"  {'.'.join(str(p) for p in err['loc']) or '<root>'}: {err['msg']}"
            for err in exc.errors()
        )
        raise ConfigError(
            f"Invalid configuration:\n{problems}\n"
            f"Fix these keys in {PROJECT_FILE}, agentkit.<env>.yaml or {ENV_PREFIX}* "
            "environment variables, then run `agentkit config validate`."
        ) from exc
