"""filebrowser installation, YAML generation and background startup."""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any, Dict

from .config import ConfigError, FileBrowserConfig
from .secrets import SecretResolver
from .shell import Runner

log = logging.getLogger("kgcomfy")


def render_filebrowser_config(config: FileBrowserConfig, password: str) -> str:
    data: Dict[str, Any] = {
        "server": {
            "baseURL": config.base_url,
            "port": config.port,
            "sources": config.sources,
        },
        "auth": {
            "adminUsername": config.admin_username,
            "adminPassword": password,
        },
    }

    try:
        import yaml
    except ImportError:  # JSON is valid YAML and filebrowser parses both
        return json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True)


class FileBrowserService:
    def __init__(
        self,
        config: FileBrowserConfig,
        runner: Runner,
        secrets: SecretResolver,
    ):
        self.config = config
        self.runner = runner
        self.secrets = secrets
        self._thread: threading.Thread | None = None

    def install(self) -> None:
        dest = Path(self.config.binary_path)
        if dest.exists() and dest.stat().st_size > 0:
            log.info("filebrowser already present at %s", dest)
        else:
            if not self.config.binary_url:
                raise ConfigError(
                    "filebrowser.binary_url is required when the binary is missing"
                )
            self.runner.download(self.config.binary_url, dest)
        self.runner.chmod(dest, 0o755)

    def write_config(self) -> str:
        password = self.secrets.get(self.config.admin_password_secret)
        content = render_filebrowser_config(self.config, password or "")
        self.runner.write_text(self.config.config_path, content)
        return content

    def start(self) -> threading.Thread:
        if self._thread and self._thread.is_alive():
            return self._thread
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="kgcomfy-filebrowser"
        )
        self._thread.start()
        return self._thread

    def _run(self) -> None:
        binary = Path(self.config.binary_path)
        try:
            self.runner.run(
                [str(binary), "-c", self.config.config_path],
                cwd=binary.parent,
                desc="Starting filebrowser",
            )
        except Exception:  # keep the failure visible in notebook output
            log.exception("filebrowser stopped unexpectedly")
