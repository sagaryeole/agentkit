"""Secrets come from a SecretProvider, never from YAML."""

import os
import re
from collections.abc import Mapping
from typing import Protocol


class SecretProvider(Protocol):
    def get(self, name: str) -> str | None: ...


class EnvSecretProvider:
    """Reads secrets from environment variables. The default provider."""

    def get(self, name: str) -> str | None:
        return os.environ.get(name) or None


_SECRET_NAME = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|CREDENTIALS)$")


def known_secret_values(environ: Mapping[str, str] | None = None) -> list[str]:
    """Values of environment variables that look like secrets, for redaction before export."""
    environ = os.environ if environ is None else environ
    return [v for k, v in environ.items() if _SECRET_NAME.search(k.upper()) and len(v) >= 8]
