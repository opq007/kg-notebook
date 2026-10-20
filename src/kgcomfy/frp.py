"""frp client installation, TOML generation and background startup."""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import List

from .config import ConfigError, FrpConfig, FrpProxyConfig
from .secrets import SecretResolver
from .shell import Runner

log = logging.getLogger("kgcomfy")


def _toml_string(value: str) -> str:
    # JSON string escaping is valid TOML basic-string escaping.
    return json.dumps(str(value), ensure_ascii=False)


def _toml_array(values: List[str]) -> str:
    return "[" + ", ".join(_toml_string(value) for value in values) + "]"


def render_frp_config(
    config: FrpConfig, token: str, proxies: List[FrpProxyConfig]
) -> str:
    lines: List[str] = [
        f"serverAddr = {_toml_string(config.server_addr)}",
        f"serverPort = {config.server_port}",
        "",
        'auth.method = "token"',
        f"auth.token = {_toml_string(token)}",
        "",
    ]

    for proxy in proxies:
        lines.extend(
            [
                "[[proxies]]",
                f"name = {_toml_string(proxy.name)}",
                f"type = {_toml_string(proxy.type)}",
                f"localIP = {_toml_string(proxy.local_ip)}",
                f"localPort = {proxy.local_port}",
            ]
        )
        if proxy.subdomain:
            lines.append(f"subdomain = {_toml_string(proxy.subdomain)}")
        lines.append(f"locations = {_toml_array(proxy.locations)}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


class FrpService:
    def __init__(
        self,
        config: FrpConfig,
        runner: Runner,
        secrets: SecretResolver,
        comfy_port: int,
        filebrowser_port: int | None,
    ):
        self.config = config
        self.runner = runner
        self.secrets = secrets
        self.comfy_port = comfy_port
        self.filebrowser_port = filebrowser_port
        self._thread: threading.Thread | None = None

    @property
    def proxies(self) -> List[FrpProxyConfig]:
        return self.config.effective_proxies(self.comfy_port, self.filebrowser_port)

    def install(self) -> None:
        if not self.config.binary:
            raise ConfigError("frp.binary is required when frp is enabled")

        dest = Path(self.config.binary_dest)
        source = self.config.binary
        if source.startswith(("http://", "https://")):
            self.runner.download(source, dest)
        else:
            source_path = Path(source)
            if not source_path.exists() and not self.runner.dry_run:
                raise FileNotFoundError(
                    f"frpc binary not found: {source_path}. Upload it as a Kaggle "
                    "dataset file or set frp.binary to a download URL."
                )
            if source_path.resolve() != dest.resolve():
                self.runner.copy_path(source_path, dest, force=True)

        self.runner.chmod(dest, 0o755)

    def write_config(self) -> str:
        token = self.secrets.get(self.config.token_secret)
        content = render_frp_config(self.config, token or "", self.proxies)
        self.runner.write_text(self.config.config_path, content)
        return content

    def public_url(self) -> str:
        if not self.config.server_addr:
            return ""
        host = self.config.server_addr
        if self.config.subdomain:
            host = f"{self.config.subdomain}.{host}"
        return f"{self.config.public_scheme}://{host}"

    def start(self) -> threading.Thread:
        if self._thread and self._thread.is_alive():
            return self._thread
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="kgcomfy-frp"
        )
        self._thread.start()
        return self._thread

    def _run(self) -> None:
        try:
            self.runner.run(
                [self.config.binary_dest, "-c", self.config.config_path],
                desc="Starting frpc",
            )
        except Exception:  # keep the failure visible in notebook output
            log.exception("frpc stopped unexpectedly")
