"""Typed configuration for the Kaggle ComfyUI bootstrap.

Everything that is expected to change between notebooks (or between Kaggle
sessions) lives here: bundle URLs, update switches, custom node list, model
path mapping, frp/filebrowser settings and secret names.  The rest of the
package only consumes :class:`Config` objects.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import types
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import (
    Any,
    Dict,
    List,
    Mapping,
    Union,
    get_args,
    get_origin,
    get_type_hints,
)

log = logging.getLogger("kgcomfy")

PACKAGE_DIR = Path(__file__).resolve().parent
CONFIG_DIR = PACKAGE_DIR / "configs"

_UNION_ORIGINS = (Union,)
if hasattr(types, "UnionType"):  # Python 3.10+
    _UNION_ORIGINS = _UNION_ORIGINS + (types.UnionType,)

#: Webhook platforms understood by :mod:`kgcomfy.webhook`.
WEBHOOK_PROVIDERS = ("auto", "feishu", "dingtalk", "wecom", "slack", "generic")

#: Lifecycle events the watchdog can publish.
WEBHOOK_EVENTS = (
    "started",
    "ready",
    "crashed",
    "restarting",
    "restarted",
    "start_failed",
    "failed",
    "stopped",
)

#: Default lifecycle events, including failed restart attempts.
DEFAULT_WEBHOOK_EVENTS = (
    "started",
    "crashed",
    "start_failed",
    "restarted",
    "failed",
)


def normalize_webhook_events(events: Any) -> List[str]:
    """Lower-case, strip and de-duplicate a webhook event list."""
    if isinstance(events, str):
        events = events.split(",")
    normalized: List[str] = []
    for event in events or []:
        value = str(event).strip().lower()
        if value and value not in normalized:
            normalized.append(value)
    return normalized


class ConfigError(ValueError):
    """Raised when a configuration file is malformed or incomplete."""


@dataclass
class Aria2Config:
    connections: int = 16
    splits: int = 16


@dataclass
class BundleConfig:
    #: Download URL (e.g. ModelScope); used when ``source`` is empty.
    url: str = ""
    #: Local archive path (e.g. a Kaggle dataset file). Takes precedence.
    source: str = ""
    archive: str = ""
    extract_to: str = ""
    #: Stage the archive again even when it already exists.
    overwrite: bool = False


@dataclass
class LinkConfig:
    path: str
    target: str
    force: bool = False


@dataclass
class RuntimeConfig:
    comfy_dir: str = "/kaggle/ComfyUI"
    python_dir: str = "/py312"
    # Empty means "<python_dir>/bin/python3"; derived in __post_init__.
    python_bin: str = ""
    install_bundles: bool = True
    apt_packages: List[str] = field(default_factory=lambda: ["aria2"])
    aria2: Aria2Config = field(default_factory=Aria2Config)
    comfyui_bundle: BundleConfig = field(default_factory=BundleConfig)
    python_bundle: BundleConfig = field(default_factory=BundleConfig)
    links: List[LinkConfig] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.python_bin:
            self.python_bin = f"{self.python_dir}/bin/python3"


@dataclass
class UpdatesConfig:
    comfyui: bool = False
    comfyui_requirements: bool = True
    custom_nodes: bool = False
    custom_node_requirements: bool = False


@dataclass
class CustomNodeConfig:
    name: str
    repo: str = ""
    ref: str = ""
    enabled: bool = True
    install_requirements: bool = False
    #: Delete the directory and clone again instead of pulling.
    reclone: bool = False


@dataclass
class ModelLinkConfig:
    src: str
    dest: str
    #: symlink | copy | hardlink
    mode: str = "symlink"
    force: bool = False
    required: bool = True


@dataclass
class ComfyUIConfig:
    port: int = 8188
    args: List[str] = field(
        default_factory=lambda: [
            "--disable-pinned-memory",
            "--force-fp16",
            "--port={port}",
        ]
    )
    env: Dict[str, str] = field(
        default_factory=lambda: {
            "CUDA_VISIBLE_DEVICES": "0,1",
            "MPLBACKEND": "Agg",
        }
    )
    requirements_file: str = "requirements.txt"

    def has_port_argument(self) -> bool:
        """Return True when ``args`` already contains --port."""
        return any(
            arg == "--port" or arg.startswith("--port=") for arg in self.args
        )

    def port_values(self) -> List[str]:
        """Return the values of --port/--port=N in ``args``."""
        values: List[str] = []
        for index, arg in enumerate(self.args):
            if arg.startswith("--port="):
                values.append(arg.split("=", 1)[1])
            elif arg == "--port" and index + 1 < len(self.args):
                values.append(self.args[index + 1])
        return values


@dataclass
class FrpProxyConfig:
    name: str
    local_port: int
    type: str = "http"
    local_ip: str = "127.0.0.1"
    subdomain: str = ""
    locations: List[str] = field(default_factory=lambda: ["/"])


@dataclass
class FrpConfig:
    enabled: bool = True
    #: Local path (e.g. a Kaggle input file) or an HTTP(S) URL.
    binary: str = ""
    binary_dest: str = "/kaggle/frpc/frpc"
    config_path: str = "/kaggle/frpc.toml"
    server_addr: str = ""
    server_port: int = 7780
    token_secret: str = "frp_token"
    subdomain: str = ""
    public_scheme: str = "http"
    #: Optional explicit proxy list. When empty, proxies are derived from the
    #: ComfyUI port and the filebrowser port.
    proxies: List[FrpProxyConfig] = field(default_factory=list)

    def effective_proxies(
        self, comfy_port: int, filebrowser_port: int | None
    ) -> List[FrpProxyConfig]:
        if self.proxies:
            return list(self.proxies)
        proxies = [
            FrpProxyConfig(
                name=f"{self.subdomain}-comfyui-frp",
                local_port=comfy_port,
                subdomain=self.subdomain,
                locations=["/"],
            )
        ]
        if filebrowser_port is not None:
            proxies.append(
                FrpProxyConfig(
                    name=f"{self.subdomain}-fslist-frp",
                    local_port=filebrowser_port,
                    subdomain=self.subdomain,
                    locations=["/fslist"],
                )
            )
        return proxies


@dataclass
class FileBrowserConfig:
    enabled: bool = True
    binary_url: str = ""
    binary_path: str = "/kaggle/working/filebrowser"
    config_path: str = "/kaggle/config.yaml"
    base_url: str = "/fslist"
    port: int = 8012
    sources: List[Dict[str, Any]] = field(
        default_factory=lambda: [
            {"path": "/kaggle", "config": {"defaultEnabled": True}}
        ]
    )
    admin_username: str = "admin"
    admin_password_secret: str = "frp_token"


@dataclass
class SecretConfig:
    #: Secret name in the Kaggle notebook secret store.
    kaggle: str = ""
    #: Environment variable name used as a local fallback.
    env: str = ""


@dataclass
class CleanupConfig:
    enabled: bool = False
    paths: List[str] = field(default_factory=list)
    #: Glob patterns; only symlinks are removed, never real files.
    symlink_globs: List[str] = field(default_factory=list)


@dataclass
class PublishConfig:
    modelscope_repo: str = ""
    modelscope_token_secret: str = "modelscope_token"
    repo_type: str = "dataset"


@dataclass
class WatchdogConfig:
    """ComfyUI process supervision (Phase 1: in-cell watchdog)."""

    enabled: bool = True
    #: HTTP path probed on the ComfyUI port to detect a hung process.
    health_path: str = "/system_stats"
    interval: float = 20.0
    timeout: float = 5.0
    #: Consecutive failed probes with a closed port before a restart.  A failed
    #: HTTP probe while the port is still listening is treated as "busy".
    failures: int = 3
    #: Startup period where failed probes are ignored before the first success.
    startup_grace: float = 300.0
    backoff_initial: float = 5.0
    backoff_max: float = 300.0
    #: At most max_restarts restarts inside restart_window seconds.
    max_restarts: int = 5
    restart_window: float = 600.0
    #: Seconds to wait after SIGTERM before killing the process group.
    shutdown_timeout: float = 15.0
    log_path: str = "/kaggle/working/comfyui.log"
    state_path: str = "/kaggle/working/comfyui-watchdog.json"
    #: Rotate the child log once it grows past this size (0 disables).
    log_max_bytes: int = 50 * 1024 * 1024


@dataclass
class WebhookConfig:
    """ComfyUI lifecycle notifications over HTTP.

    Payloads for Feishu, DingTalk (including signing), WeCom, Slack and a
    generic JSON shape are built by :mod:`kgcomfy.webhook`.  The URL and signing
    secret can also be resolved at runtime through ``url_secret`` /
    ``sign_secret`` (Kaggle Secrets or environment variables).
    """

    enabled: bool = False
    #: auto | feishu | dingtalk | wecom | slack | generic
    provider: str = "auto"
    url: str = ""
    #: Signing secret for Feishu/DingTalk custom bots.
    secret: str = ""
    #: Logical secret names resolved through SecretResolver.
    url_secret: str = ""
    sign_secret: str = ""
    timeout: float = 5.0
    #: Prefix of every message title; can double as a DingTalk keyword.
    title: str = "ComfyUI"
    #: Empty means "all events".
    events: List[str] = field(default_factory=lambda: list(DEFAULT_WEBHOOK_EVENTS))
    #: Attach the tail of the ComfyUI log to unexpected-stop messages.
    include_log_tail: bool = False
    log_tail_lines: int = 10

    def __post_init__(self) -> None:
        self.provider = (self.provider or "auto").strip().lower()
        self.events = normalize_webhook_events(self.events)

    def apply_env(
        self, env: Mapping[str, str] | None = None
    ) -> "WebhookConfig":
        """Apply ``KGCOMFY_WEBHOOK_*`` and platform aliases to this config.

        Environment values win over YAML; repeated calls re-apply, which lets
        a notebook set variables after ``load_config()``.
        """
        values = os.environ if env is None else env

        def raw(name: str) -> str:
            value = values.get(name, "")
            return str(value).strip() if value is not None else ""

        enabled = _env_bool(
            raw("KGCOMFY_WEBHOOK_ENABLED"), "KGCOMFY_WEBHOOK_ENABLED"
        )
        if enabled is not None:
            self.enabled = enabled

        provider_env = raw("KGCOMFY_WEBHOOK_PROVIDER")
        if provider_env:
            self.provider = provider_env.lower()

        aliases = (
            ("feishu", "FEISHU_WEBHOOK"),
            ("dingtalk", "DINGTALK_WEBHOOK"),
            ("wecom", "WECOM_WEBHOOK"),
            ("slack", "SLACK_WEBHOOK"),
        )

        alias_provider = ""
        alias_prefix = ""
        url = raw("KGCOMFY_WEBHOOK_URL")
        if not url:
            for candidate_provider, prefix in aliases:
                alias_url = raw(f"{prefix}_URL")
                if alias_url:
                    url = alias_url
                    alias_provider = candidate_provider
                    alias_prefix = prefix
                    break

        if alias_provider and not provider_env:
            if self.provider not in ("auto", alias_provider):
                log.warning(
                    "webhook.provider=%s overridden by %s_URL",
                    self.provider,
                    alias_prefix,
                )
            self.provider = alias_provider
        elif (
            alias_provider
            and provider_env
            and provider_env.lower() != alias_provider
        ):
            log.warning(
                "webhook.provider=%s does not match %s_URL; keeping provider",
                provider_env,
                alias_prefix,
            )

        # Platform-specific secrets are only paired with their own alias URL;
        # KGCOMFY_WEBHOOK_SECRET remains the generic override.
        secret = raw("KGCOMFY_WEBHOOK_SECRET")
        if not secret and alias_provider:
            for candidate_provider, prefix in aliases:
                if candidate_provider == alias_provider:
                    secret = raw(f"{prefix}_SECRET")
                    break

        if url:
            self.url = url
        if secret:
            self.secret = secret

        url_secret = raw("KGCOMFY_WEBHOOK_URL_SECRET")
        if url_secret:
            self.url_secret = url_secret

        sign_secret = raw("KGCOMFY_WEBHOOK_SIGN_SECRET")
        if sign_secret:
            self.sign_secret = sign_secret

        title = raw("KGCOMFY_WEBHOOK_TITLE")
        if title:
            self.title = title

        timeout = raw("KGCOMFY_WEBHOOK_TIMEOUT")
        if timeout:
            try:
                self.timeout = float(timeout)
            except ValueError:
                log.warning(
                    "Ignoring invalid KGCOMFY_WEBHOOK_TIMEOUT=%r", timeout
                )

        events = raw("KGCOMFY_WEBHOOK_EVENTS")
        if events:
            self.events = (
                list(WEBHOOK_EVENTS)
                if events.lower() in ("all", "*")
                else normalize_webhook_events(events.split(","))
            )

        log_tail = _env_bool(
            raw("KGCOMFY_WEBHOOK_LOG_TAIL"), "KGCOMFY_WEBHOOK_LOG_TAIL"
        )
        if log_tail is not None:
            self.include_log_tail = log_tail

        log_tail_lines = raw("KGCOMFY_WEBHOOK_LOG_TAIL_LINES")
        if log_tail_lines:
            try:
                self.log_tail_lines = int(log_tail_lines)
            except ValueError:
                log.warning(
                    "Ignoring invalid KGCOMFY_WEBHOOK_LOG_TAIL_LINES=%r",
                    log_tail_lines,
                )

        return self


@dataclass
class Config:
    log_level: str = "INFO"
    dry_run: bool = False
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)
    updates: UpdatesConfig = field(default_factory=UpdatesConfig)
    custom_nodes: List[CustomNodeConfig] = field(default_factory=list)
    models: List[ModelLinkConfig] = field(default_factory=list)
    comfyui: ComfyUIConfig = field(default_factory=ComfyUIConfig)
    watchdog: WatchdogConfig = field(default_factory=WatchdogConfig)
    webhook: WebhookConfig = field(default_factory=WebhookConfig)
    frp: FrpConfig = field(default_factory=FrpConfig)
    filebrowser: FileBrowserConfig = field(default_factory=FileBrowserConfig)
    secrets: Dict[str, SecretConfig] = field(default_factory=dict)
    cleanup: CleanupConfig = field(default_factory=CleanupConfig)
    publish: PublishConfig = field(default_factory=PublishConfig)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "Config":
        if data is None:
            data = {}
        if not isinstance(data, Mapping):
            raise ConfigError(
                f"config root must be a mapping, got {type(data).__name__}"
            )
        return _construct(cls, _normalize(data))

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def to_redacted_dict(self) -> Dict[str, Any]:
        """Return ``to_dict()`` with webhook credentials masked."""
        data = self.to_dict()
        webhook = data.get("webhook")
        if isinstance(webhook, dict):
            for key in ("url", "secret"):
                if webhook.get(key):
                    webhook[key] = "***"
        return data

    def apply_env_overrides(
        self, env: Mapping[str, str] | None = None
    ) -> "Config":
        """Apply environment overrides to the webhook config.

        Environment values win over YAML and ``load_config(overrides=...)``.
        """
        self.webhook.apply_env(env)
        return self

    def validate(self) -> None:
        errors: List[str] = []
        if not self.runtime.comfy_dir:
            errors.append("runtime.comfy_dir must not be empty")
        if not self.runtime.python_bin:
            errors.append("runtime.python_bin must not be empty")

        valid_modes = {"symlink", "copy", "hardlink"}
        for index, item in enumerate(self.models):
            if item.mode not in valid_modes:
                errors.append(
                    f"models[{index}].mode={item.mode!r} is invalid; "
                    f"expected one of {sorted(valid_modes)}"
                )

        for literal_port in self.comfyui.port_values():
            if literal_port and literal_port != "{port}":
                if literal_port != str(self.comfyui.port):
                    errors.append(
                        f"comfyui.args uses --port={literal_port} but "
                        f"comfyui.port={self.comfyui.port}; use '{{port}}' "
                        "or make them match"
                    )
        if any(
            arg == "--port" and index + 1 >= len(self.comfyui.args)
            for index, arg in enumerate(self.comfyui.args)
        ):
            errors.append("comfyui.args contains --port without a value")

        watchdog = self.watchdog
        watchdog_rules = [
            ("interval", watchdog.interval > 0, "must be greater than 0"),
            ("timeout", watchdog.timeout > 0, "must be greater than 0"),
            ("failures", watchdog.failures >= 1, "must be at least 1"),
            (
                "startup_grace",
                watchdog.startup_grace >= 0,
                "must not be negative",
            ),
            (
                "backoff_initial",
                watchdog.backoff_initial > 0,
                "must be greater than 0",
            ),
            (
                "max_restarts",
                watchdog.max_restarts >= 0,
                "must not be negative",
            ),
            (
                "restart_window",
                watchdog.restart_window > 0,
                "must be greater than 0",
            ),
            (
                "shutdown_timeout",
                watchdog.shutdown_timeout >= 0,
                "must not be negative",
            ),
            (
                "log_max_bytes",
                watchdog.log_max_bytes >= 0,
                "must not be negative",
            ),
        ]
        for name, valid, message in watchdog_rules:
            if not valid:
                errors.append(f"watchdog.{name} {message}")
        if not watchdog.health_path.startswith("/"):
            errors.append(
                "watchdog.health_path must start with '/' "
                f"(got {watchdog.health_path!r})"
            )
        if watchdog.backoff_max < watchdog.backoff_initial:
            errors.append(
                "watchdog.backoff_max must be greater than or equal to "
                "watchdog.backoff_initial"
            )
        if not watchdog.log_path:
            errors.append("watchdog.log_path must not be empty")
        if not watchdog.state_path:
            errors.append("watchdog.state_path must not be empty")

        webhook = self.webhook
        if webhook.enabled:
            if webhook.provider not in WEBHOOK_PROVIDERS:
                errors.append(
                    f"webhook.provider={webhook.provider!r} is invalid; "
                    f"expected one of {list(WEBHOOK_PROVIDERS)}"
                )
            if webhook.timeout <= 0:
                errors.append("webhook.timeout must be greater than 0")
            if webhook.log_tail_lines < 0:
                errors.append("webhook.log_tail_lines must not be negative")
            unknown_events = sorted(set(webhook.events) - set(WEBHOOK_EVENTS))
            if unknown_events:
                errors.append(
                    f"webhook.events contains unknown events: {unknown_events}; "
                    f"expected values from {list(WEBHOOK_EVENTS)}"
                )

        if self.frp.enabled:
            if not self.frp.server_addr:
                errors.append("frp.server_addr is required when frp.enabled=true")
            if not self.frp.binary and not Path(self.frp.binary_dest).exists():
                errors.append(
                    "frp.binary is required when frp.enabled=true and the binary "
                    f"is not already present at {self.frp.binary_dest}"
                )
            if not self.frp.token_secret:
                errors.append("frp.token_secret is required when frp.enabled=true")

        if self.filebrowser.enabled and not self.filebrowser.binary_url:
            if not Path(self.filebrowser.binary_path).exists():
                errors.append(
                    "filebrowser.binary_url is required when the binary is not "
                    f"already present at {self.filebrowser.binary_path}"
                )

        if errors:
            raise ConfigError("invalid configuration:\n  - " + "\n  - ".join(errors))


def load_config(
    source: str | Path | Mapping[str, Any] | Config | None = "tts-img.yaml",
    overrides: Mapping[str, Any] | None = None,
) -> Config:
    """Load a config from YAML/JSON, a mapping, or an existing ``Config``.

    ``overrides`` is deep-merged on top of the loaded data, which makes small
    per-notebook tweaks (``{"frp": {"subdomain": "tts2"}}``) cheap.  Webhook
    environment variables are applied last, so they win over ``overrides``.
    """
    if isinstance(source, Config):
        data: Dict[str, Any] = source.to_dict()
    elif isinstance(source, Mapping):
        data = copy.deepcopy(dict(source))
    elif source is None:
        data = {}
    else:
        data = _read_file(resolve_config_path(source))

    if overrides:
        data = deep_merge(data, dict(overrides))

    return Config.from_dict(data).apply_env_overrides()


def resolve_config_path(name: str | Path) -> Path:
    """Resolve a config name against cwd and the packaged ``configs/`` dir."""
    raw = Path(name)
    candidates: List[Path] = []
    if raw.is_absolute() or raw.exists():
        candidates.append(raw)
    candidates.append(CONFIG_DIR / raw.name)
    if not raw.suffix:
        candidates.append(CONFIG_DIR / f"{raw.name}.yaml")

    for candidate in candidates:
        if candidate.exists():
            return candidate

    searched = ", ".join(str(item) for item in candidates)
    raise FileNotFoundError(f"config file not found: {name} (searched: {searched})")


def _env_bool(value: str, name: str) -> bool | None:
    """Parse a boolean environment value, returning None when unset/invalid."""
    if not value:
        return None
    lowered = value.lower()
    if lowered in ("1", "true", "yes", "on"):
        return True
    if lowered in ("0", "false", "no", "off"):
        return False
    log.warning("Ignoring invalid %s=%r", name, value)
    return None


def deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> Dict[str, Any]:
    result: Dict[str, Any] = copy.deepcopy(dict(base))
    for key, value in override.items():
        current = result.get(key)
        if isinstance(current, Mapping) and isinstance(value, Mapping):
            result[key] = deep_merge(current, value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _read_file(path: Path) -> Dict[str, Any]:
    suffix = path.suffix.lower()
    text = path.read_text(encoding="utf-8")
    if suffix in (".yaml", ".yml"):
        try:
            import yaml
        except ImportError as exc:  # pragma: no cover - depends on environment
            raise ConfigError(
                "PyYAML is required to read YAML configs; "
                "install it with `pip install pyyaml`"
            ) from exc
        data = yaml.safe_load(text) or {}
    elif suffix == ".json":
        data = json.loads(text)
    else:
        raise ConfigError(f"unsupported config format: {path.suffix or path}")

    if not isinstance(data, Mapping):
        raise ConfigError(f"config root must be a mapping in {path}")
    return dict(data)


def _normalize(data: Mapping[str, Any]) -> Dict[str, Any]:
    """Expand convenient shorthand forms before typed construction."""
    normalized: Dict[str, Any] = copy.deepcopy(dict(data))

    models = normalized.get("models")
    if isinstance(models, Mapping):
        expanded: List[Dict[str, Any]] = []
        for src, value in models.items():
            if isinstance(value, str):
                expanded.append({"src": src, "dest": value})
            elif isinstance(value, Mapping):
                expanded.append({"src": src, **dict(value)})
            else:
                raise ConfigError(
                    "models mapping values must be a destination string or a mapping"
                )
        normalized["models"] = expanded

    nodes = normalized.get("custom_nodes")
    if isinstance(nodes, list):
        normalized["custom_nodes"] = [
            {"name": item} if isinstance(item, str) else item for item in nodes
        ]

    secrets = normalized.get("secrets")
    if isinstance(secrets, Mapping):
        normalized["secrets"] = {
            key: ({"kaggle": value} if isinstance(value, str) else value)
            for key, value in secrets.items()
        }

    return normalized


def _construct(cls: type, data: Mapping[str, Any]) -> Any:
    if not isinstance(data, Mapping):
        raise ConfigError(f"{cls.__name__} must be a mapping")

    hints = get_type_hints(cls)
    known = {item.name for item in fields(cls)}
    unknown = sorted(set(data) - known)
    if unknown:
        log.warning("Ignoring unknown config keys for %s: %s", cls.__name__, unknown)

    kwargs: Dict[str, Any] = {}
    for item in fields(cls):
        if item.name not in data:
            continue
        kwargs[item.name] = _coerce(
            hints[item.name], data[item.name], f"{cls.__name__}.{item.name}"
        )
    try:
        return cls(**kwargs)
    except TypeError as exc:
        raise ConfigError(f"invalid {cls.__name__} config: {exc}") from exc


def _coerce(tp: Any, value: Any, path: str) -> Any:
    if value is None or tp is Any:
        return value

    origin = get_origin(tp)
    args = get_args(tp)

    if is_dataclass(tp):
        if isinstance(value, Mapping):
            return _construct(tp, value)
        if tp is SecretConfig and isinstance(value, str):
            return SecretConfig(kaggle=value)
        raise ConfigError(f"{path} must be a mapping, got {type(value).__name__}")

    if origin in (list, List):
        if not isinstance(value, (list, tuple)):
            raise ConfigError(f"{path} must be a list, got {type(value).__name__}")
        item_type = args[0] if args else Any
        return [_coerce(item_type, item, f"{path}[]") for item in value]

    if origin in (dict, Dict):
        if not isinstance(value, Mapping):
            raise ConfigError(f"{path} must be a mapping, got {type(value).__name__}")
        value_type = args[1] if len(args) == 2 else Any
        return {
            key: _coerce(value_type, item, f"{path}.{key}")
            for key, item in value.items()
        }

    if origin in _UNION_ORIGINS:
        candidates = [arg for arg in args if arg is not type(None)]
        if len(candidates) == 1:
            return _coerce(candidates[0], value, path)

    return value
