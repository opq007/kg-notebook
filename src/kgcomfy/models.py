"""Model path mapping and optional cleanup."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from .config import CleanupConfig, Config, ConfigError, ModelLinkConfig
from .shell import Runner

log = logging.getLogger("kgcomfy")


class ModelLinker:
    """Map read-only Kaggle inputs into ComfyUI's models tree."""

    def __init__(self, config: Config, runner: Runner):
        self.config = config
        self.runner = runner

    def apply(self) -> None:
        for item in self.config.models:
            self._apply_one(item)

    def _resolve_dest(self, dest: str) -> Path:
        path = Path(dest)
        if path.is_absolute():
            return path
        return Path(self.config.runtime.comfy_dir) / path

    def _apply_one(self, item: ModelLinkConfig) -> bool:
        src = Path(item.src)
        dest = self._resolve_dest(item.dest)

        if not src.exists() and not self.runner.dry_run:
            message = f"model source not found: {src} (destination: {dest})"
            if item.required:
                raise FileNotFoundError(message)
            log.warning("%s; skipping optional model", message)
            return False

        mode = item.mode.lower()
        if mode == "symlink":
            return self.runner.ensure_symlink(src, dest, force=item.force)
        if mode == "copy":
            return self.runner.copy_path(src, dest, force=item.force)
        if mode == "hardlink":
            return self._hardlink(src, dest, item.force)

        raise ConfigError(
            f"unsupported model link mode {item.mode!r}; "
            "expected symlink, copy or hardlink"
        )

    def _hardlink(self, src: Path, dest: Path, force: bool) -> bool:
        if dest.exists() or dest.is_symlink():
            if not force:
                log.warning("Destination exists, skipping hardlink: %s", dest)
                return False
            self.runner.remove_path(dest)

        self.runner.history.append(f"hardlink {dest} -> {src}")
        if not self.runner.dry_run:
            dest.parent.mkdir(parents=True, exist_ok=True)
            if src.is_dir():
                raise ConfigError("hardlink mode only supports single files")
            os.link(src, dest)
        log.info("Hardlink %s -> %s", dest, src)
        return True


class Cleaner:
    """Remove stale model files before linking new ones.

    Disabled by default. Paths are restricted to the ComfyUI directory, and
    glob cleanup only deletes symlinks.
    """

    def __init__(self, config: Config, runner: Runner):
        self.config = config
        self.runner = runner

    def run(self) -> None:
        cleanup: CleanupConfig = self.config.cleanup
        if not cleanup.enabled:
            log.info("cleanup.enabled=false; skipping cleanup")
            return

        root = Path(self.config.runtime.comfy_dir)
        for path in cleanup.paths:
            resolved = Path(path)
            if not resolved.is_absolute():
                resolved = root / resolved
            self.runner.remove_path(resolved, root=root)
        for pattern in cleanup.symlink_globs:
            resolved_pattern = Path(pattern)
            if not resolved_pattern.is_absolute():
                resolved_pattern = root / resolved_pattern
            self.runner.remove_symlinks(resolved_pattern, root=root)


def describe_models(config: Config, limit: int = 40) -> list[str]:
    """Return a human-readable listing of the ComfyUI models tree."""
    root = Path(config.runtime.comfy_dir) / "models"
    if not root.exists():
        return [f"models directory not found: {root}"]

    lines: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames.sort()
        for name in dirnames + sorted(filenames):
            if len(lines) >= limit:
                lines.append(f"... (truncated at {limit} entries)")
                return lines
            path = Path(dirpath) / name
            relative = path.relative_to(root)
            if path.is_symlink():
                try:
                    target = os.readlink(path)
                except OSError:
                    target = "?"
                lines.append(f"{relative} -> {target}")
            elif path.is_file():
                size_mb = path.stat().st_size / (1024 * 1024)
                lines.append(f"{relative} ({size_mb:.1f} MiB)")
    return lines
