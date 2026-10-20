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


class ConfigError(ValueError):
    """Raised when a configuration file is malformed or incomplete."""


@dataclass
class Aria2Config:
    connections: int = 16
    splits: int = 16


@dataclass
class BundleConfig:
    url: str = ""
    archive: str = ""
    extract_to: str = ""


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
class Config:
    log_level: str = "INFO"
    dry_run: bool = False
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)
    updates: UpdatesConfig = field(default_factory=UpdatesConfig)
    custom_nodes: List[CustomNodeConfig] = field(default_factory=list)
    models: List[ModelLinkConfig] = field(default_factory=list)
    comfyui: ComfyUIConfig = field(default_factory=ComfyUIConfig)
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
    per-notebook tweaks (``{"frp": {"subdomain": "tts2"}}``) cheap.
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

    return Config.from_dict(data)


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
