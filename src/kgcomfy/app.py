"""High-level orchestrator used by notebooks."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Mapping

from .comfyui import ComfyUIInstaller
from .config import Config, load_config
from .filebrowser import FileBrowserService
from .frp import FrpService
from .logging_utils import setup_logging
from .models import Cleaner, ModelLinker, describe_models
from .publish import Publisher
from .secrets import SecretResolver
from .shell import CommandError, Runner, describe_exception
from .supervisor import (
    EXIT_CLEAN,
    HttpHealthProbe,
    SubprocessSpawner,
    Supervisor,
    SupervisorStatus,
    TcpPortProbe,
)
from .webhook import (
    Notifier,
    WebhookNotifier,
    flush_notifications,
    send_notification,
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
        # ``load_config`` already applies these; doing it again keeps direct
        # ``Config`` callers and the CLI consistent.
        config.apply_env_overrides()
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
        self._webhook: WebhookNotifier | None = None
        self._webhook_url_warned = False
        self._webhook_sign_secret_warned = False

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
            notifier = self.webhook_notifier()
            try:
                try:
                    self.comfy.require_installed()
                except Exception as exc:
                    self._notify_failure(
                        notifier, "start_failed", "ComfyUI 启动失败", exc
                    )
                    raise

                self._notify(
                    notifier,
                    "started",
                    "ComfyUI 启动中（未启用看门狗）",
                )
                try:
                    self.comfy.run_comfyui()
                except KeyboardInterrupt:
                    self._notify(
                        notifier,
                        "stopped",
                        "ComfyUI 已停止",
                        {"reason": "用户中断"},
                    )
                    raise
                except Exception as exc:
                    crashed = isinstance(exc, CommandError)
                    self._notify_failure(
                        notifier,
                        "crashed" if crashed else "start_failed",
                        "ComfyUI 意外停止" if crashed else "ComfyUI 启动失败",
                        exc,
                    )
                    raise
                self._notify(
                    notifier,
                    "stopped",
                    "ComfyUI 已停止",
                    {"reason": "进程退出"},
                )
                return EXIT_CLEAN
            finally:
                self._flush_webhook(notifier)

        notifier = self.webhook_notifier()
        try:
            self.comfy.require_installed()
            self._supervisor = self.build_supervisor()
        except Exception as exc:
            self._notify_failure(
                notifier, "start_failed", "ComfyUI 启动失败", exc
            )
            raise
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
            port_open=TcpPortProbe("127.0.0.1", self.config.comfyui.port),
            settings=settings,
            logger=self.log,
            notifier=self.webhook_notifier(),
        )

    def webhook_notifier(self) -> WebhookNotifier | None:
        """Build the configured webhook notifier once, or return None.

        A disabled, dry-run, unresolvable or malformed configuration only logs
        a warning: notifications must never prevent ComfyUI from starting.
        """
        if self._webhook is not None:
            return self._webhook

        settings = self.config.webhook
        if not settings.enabled:
            return None
        if self.config.dry_run:
            self.log.info("dry-run: webhook notifications are suppressed")
            return None

        url = settings.url or self._secret_value(settings.url_secret)
        resolved_sign_secret = (
            self._secret_value(settings.sign_secret)
            if settings.sign_secret
            else ""
        )
        secret = settings.secret or resolved_sign_secret
        if not url:
            if not self._webhook_url_warned:
                self.log.warning(
                    "webhook.enabled=true but no URL resolved; set "
                    "webhook.url, webhook.url_secret, or "
                    "KGCOMFY_WEBHOOK_URL"
                )
                self._webhook_url_warned = True
            return None
        if (
            settings.sign_secret
            and not resolved_sign_secret
            and not self._webhook_sign_secret_warned
        ):
            self.log.warning(
                "webhook.sign_secret=%s did not resolve; %s",
                settings.sign_secret,
                (
                    "using webhook.secret instead"
                    if settings.secret
                    else "notifications will be sent unsigned"
                ),
            )
            self._webhook_sign_secret_warned = True

        context: Dict[str, str] = {}
        if self.config.frp.enabled:
            access_url = self.public_url()
        else:
            access_url = _local_url(self.config.comfyui.port)
        if access_url:
            context["url"] = access_url
        try:
            self._webhook = WebhookNotifier(
                settings,
                url=url,
                secret=secret or "",
                context=context,
                logger=self.log,
            )
        except Exception as exc:
            self.log.warning("Cannot build webhook notifier: %s", exc)
            return None
        self.log.info(
            "Webhook notifications enabled (%s)", self._webhook.provider
        )
        return self._webhook

    def _secret_value(self, name: str) -> str:
        if not name:
            return ""
        return self.secrets.get(name, required=False) or ""

    def test_webhook(self) -> bool:
        """Send one test message; returns False when disabled or failed."""
        notifier = self.webhook_notifier()
        if notifier is None:
            return False
        return notifier.send_test()

    def _notify(
        self,
        notifier: Notifier | None,
        event: str,
        summary: str,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        send_notification(notifier, self.log, event, summary, details)

    def _notify_failure(
        self,
        notifier: Notifier | None,
        event: str,
        summary: str,
        exc: BaseException,
    ) -> None:
        self._notify(
            notifier, event, summary, {"error": describe_exception(exc)}
        )
        self._flush_webhook(notifier)

    def _flush_webhook(self, notifier: Notifier | None) -> None:
        flush_notifications(notifier, self.log)

    def run(self) -> int:
        """Full pipeline: prepare, start services, run ComfyUI."""
        notifier = self.webhook_notifier()
        try:
            self.prepare()
            self.start_services()
        except Exception as exc:
            self._notify_failure(
                notifier, "start_failed", "ComfyUI 启动准备失败", exc
            )
            raise
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
