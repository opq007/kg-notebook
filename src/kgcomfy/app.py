"""High-level orchestrator used by notebooks."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import List, Mapping

from .comfyui import ComfyUIInstaller
from .config import Config, load_config
from .filebrowser import FileBrowserService
from .frp import FrpService
from .logging_utils import setup_logging
from .models import Cleaner, ModelLinker, describe_models
from .publish import Publisher
from .secrets import SecretResolver
from .shell import Runner
from .supervisor import (
    EXIT_CLEAN,
    HttpHealthProbe,
    SubprocessSpawner,
    Supervisor,
    SupervisorStatus,
)

log = logging.getLogger("kgcomfy")


def _local_url(port: int) -> str:
    return f"http://127.0.0.1:{port}"


class KaggleComfy:
    """Facade that wires config, runner, secrets and services together.

    Typical notebook usage::

        app = KaggleComfy(load_config("tts-img.yaml"))
        app.install()
        app.update()
        app.link_models()
        app.start_services()
        app.run_comfyui()
    """

    def __init__(
        self,
        config: Config | str | Path | Mapping | None = None,
        secret_overrides: Mapping[str, str] | None = None,
    ):
        if not isinstance(config, Config):
            config = load_config(config)
        config.validate()

        self.config = config
        self.log = setup_logging(config.log_level)
        self.runner = Runner(dry_run=config.dry_run, logger=self.log)
        self.secrets = SecretResolver(config.secrets, secret_overrides)

        self.comfy = ComfyUIInstaller(config, self.runner)
        self.models = ModelLinker(config, self.runner)
        self.cleaner = Cleaner(config, self.runner)
        self.filebrowser = FileBrowserService(
            config.filebrowser, self.runner, self.secrets
        )
        self.frp = FrpService(
            config.frp,
            self.runner,
            self.secrets,
            comfy_port=config.comfyui.port,
            filebrowser_port=(
                config.filebrowser.port if config.filebrowser.enabled else None
            ),
        )
        self._threads: List = []
        self._supervisor: Supervisor | None = None

    # ------------------------------------------------------------- lifecycle
    def install(self) -> None:
        """Install runtime bundles and clone missing custom nodes."""
        self.comfy.install_runtime()
        self.comfy.install_custom_nodes()

    def update(self) -> None:
        """Run the optional git/pip updates selected in ``config.updates``."""
        self.comfy.update()

    def cleanup(self) -> None:
        """Remove stale model paths/symlinks when ``config.cleanup.enabled``."""
        self.cleaner.run()

    def link_models(self) -> None:
        """Apply the configured source -> ComfyUI models mapping."""
        self.models.apply()

    def prepare(self) -> None:
        """Install, update, clean up and link models in the usual order."""
        self.install()
        self.update()
        self.cleanup()
        self.link_models()

    def start_services(self) -> None:
        """Start filebrowser and frpc in daemon threads."""
        if self.config.filebrowser.enabled:
            self.filebrowser.install()
            self.filebrowser.write_config()
            self._threads.append(self.filebrowser.start())
        else:
            log.info("filebrowser.enabled=false; not starting file browser")

        if self.config.frp.enabled:
            self.frp.install()
            self.frp.write_config()
            self._threads.append(self.frp.start())
            log.info("Public ComfyUI URL: %s", self.public_url())
            log.info("Public file browser: %s", self.filebrowser_url())
        else:
            log.info("frp.enabled=false; ComfyUI stays on 127.0.0.1")

    def run_comfyui(self, watchdog_enabled: bool | None = None) -> int:
        """Start ComfyUI in the foreground (blocks the notebook cell).

        With ``watchdog_enabled`` enabled (the default comes from
        ``config.watchdog.enabled``; ``None`` means "use the config") the
        process is restarted after an abnormal exit or when ``/system_stats``
        stays unresponsive.  Returns 0 when stopped normally, 1 when the
        watchdog gives up, and 130 when the cell is interrupted.
        """
        if self.config.frp.enabled:
            log.info("ComfyUI will be available at %s", self.public_url())
        else:
            log.info(
                "ComfyUI will be available at %s",
                _local_url(self.config.comfyui.port),
            )

        if watchdog_enabled is None:
            watchdog_enabled = self.config.watchdog.enabled

        if not watchdog_enabled or self.runner.dry_run:
            if watchdog_enabled:
                log.info(
                    "[dry-run] watchdog would supervise ComfyUI on port %s",
                    self.config.comfyui.port,
                )
            else:
                log.info(
                    "Watchdog disabled; starting ComfyUI without supervision"
                )
            self.comfy.run_comfyui()
            return EXIT_CLEAN

        self.comfy.require_installed()
        self._supervisor = self.build_supervisor()
        return self._supervisor.run()

    def build_supervisor(self) -> Supervisor:
        """Build a supervisor for ComfyUI using the current config."""
        settings = self.config.watchdog
        health_url = (
            f"{_local_url(self.config.comfyui.port)}{settings.health_path}"
        )
        spawner = SubprocessSpawner(
            self.comfy.build_command(),
            cwd=self.comfy.comfy_dir,
            env=self.comfy.build_env(),
            log_path=settings.log_path,
            log_max_bytes=settings.log_max_bytes,
            logger=self.log,
        )
        return Supervisor(
            name="ComfyUI",
            spawn=spawner,
            probe=HttpHealthProbe(health_url, timeout=settings.timeout),
            settings=settings,
            logger=self.log,
        )

    def run(self) -> int:
        """Full pipeline: prepare, start services, run ComfyUI."""
        self.prepare()
        self.start_services()
        return self.run_comfyui()

    # --------------------------------------------------------------- helpers
    def public_url(self) -> str:
        return self.frp.public_url()

    def watchdog_status(self) -> SupervisorStatus:
        """Return live supervisor state, or the last persisted state."""
        if self._supervisor is not None:
            return self._supervisor.status()
        return Supervisor.load_state(self.config.watchdog.state_path)

    def filebrowser_url(self) -> str:
        base = self.public_url() or _local_url(self.config.filebrowser.port)
        return f"{base}{self.config.filebrowser.base_url.rstrip('/')}/"

    def list_models(self, limit: int = 40) -> List[str]:
        return describe_models(self.config, limit=limit)

    def diagnostics(self) -> None:
        self._safe_run(["nvidia-smi"], "GPU information")
        self._safe_run(["nvcc", "--version"], "CUDA compiler version")
        self._safe_run(
            [
                self.config.runtime.python_bin,
                "-c",
                "import cv2; print(cv2.__version__)",
            ],
            "OpenCV version",
        )
        self._safe_run(
            [self.config.runtime.python_bin, "-m", "pip", "list"],
            "Installed Python packages",
        )

    def _safe_run(self, command, desc: str) -> None:
        try:
            self.runner.run(command, check=False, desc=desc)
        except OSError as exc:
            log.warning("%s unavailable: %s", desc, exc)

    def publisher(self) -> Publisher:
        return Publisher(self.config, self.runner, self.secrets)

    @property
    def commands(self) -> List[str]:
        """Redacted history of commands and file operations executed so far."""
        return list(self.runner.history)
