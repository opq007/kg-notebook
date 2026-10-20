"""Secret lookup with Kaggle Secrets, environment and explicit overrides."""

from __future__ import annotations

import logging
import os
from typing import Dict, Mapping

from .config import SecretConfig

log = logging.getLogger("kgcomfy")


class SecretError(RuntimeError):
    """Raised when a required secret cannot be resolved."""


class SecretResolver:
    """Resolve logical secret names in this order:

    1. explicit overrides passed by the caller;
    2. environment variables (``secret.env`` first, then ``NAME``);
    3. Kaggle notebook secrets (``UserSecretsClient``).
    """

    def __init__(
        self,
        mapping: Mapping[str, SecretConfig] | None = None,
        overrides: Mapping[str, str] | None = None,
    ):
        self._mapping: Dict[str, SecretConfig] = dict(mapping or {})
        self._overrides: Dict[str, str] = {
            key: value for key, value in (overrides or {}).items() if value
        }
        self._cache: Dict[str, str] = {}
        self._kaggle_client = None
        self._kaggle_import_failed = False

    def get(
        self,
        name: str,
        default: str | None = None,
        required: bool = True,
    ) -> str | None:
        if name in self._cache:
            return self._cache[name]

        value = self._overrides.get(name)
        if not value:
            value = self._from_env(name)
        if not value:
            value = self._from_kaggle(name)

        if value:
            self._cache[name] = value
            return value

        if required:
            raise SecretError(self._missing_message(name))
        return default

    def _from_env(self, name: str) -> str | None:
        config = self._mapping.get(name)
        candidates = []
        if config and config.env:
            candidates.append(config.env)
        candidates.extend([name.upper(), name])
        for candidate in candidates:
            value = os.environ.get(candidate)
            if value:
                log.debug("Using environment variable %s for secret %s", candidate, name)
                return value
        return None

    def _from_kaggle(self, name: str) -> str | None:
        if self._kaggle_import_failed:
            return None
        try:
            from kaggle_secrets import UserSecretsClient  # type: ignore
        except ImportError:
            self._kaggle_import_failed = True
            return None

        kaggle_name = name
        config = self._mapping.get(name)
        if config and config.kaggle:
            kaggle_name = config.kaggle

        try:
            if self._kaggle_client is None:
                self._kaggle_client = UserSecretsClient()
            value = self._kaggle_client.get_secret(kaggle_name)
            if value:
                log.debug("Using Kaggle secret %s for %s", kaggle_name, name)
            return value
        except Exception as exc:  # kaggle_secrets raises a plain Exception
            log.debug("Kaggle secret %s unavailable: %s", kaggle_name, exc)
            return None

    def _missing_message(self, name: str) -> str:
        config = self._mapping.get(name)
        kaggle_name = config.kaggle if config and config.kaggle else name
        env_name = config.env if config and config.env else name.upper()
        return (
            f"missing secret '{name}'. Add notebook secret '{kaggle_name}' in "
            f"Kaggle (Add-ons -> Secrets), or set environment variable {env_name}."
        )
