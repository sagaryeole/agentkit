"""Secrets come from a SecretProvider, never from YAML."""

import os
from typing import Protocol


class SecretProvider(Protocol):
    def get(self, name: str) -> str | None: ...


class EnvSecretProvider:
    """Reads secrets from environment variables. The default provider."""

    def get(self, name: str) -> str | None:
        return os.environ.get(name) or None
