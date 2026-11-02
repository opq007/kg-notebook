"""ComfyUI runtime installation, optional updates and custom node management."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import List

from .config import BundleConfig, Config, ConfigError, CustomNodeConfig
from .shell import Runner

log = logging.getLogger("kgcomfy")


class ComfyUIInstaller:
    def __init__(self, config: Config, runner: Runner):
        self.config = config
        self.runner = runner
        self.runtime = config.runtime

    @property
    def comfy_dir(self) -> Path:
        return Path(self.runtime.comfy_dir)

    @property
    def custom_nodes_dir(self) -> Path:
        return self.comfy_dir / "custom_nodes"

    # ------------------------------------------------------------- installation
    def install_runtime(self) -> None:
        """Install apt packages, ComfyUI and the portable Python runtime."""
        if self.runtime.install_bundles:
            self.runner.apt_install(self.runtime.apt_packages)
            self._install_bundle(
                "ComfyUI", self.runtime.comfyui_bundle, self.comfy_dir
            )
            self._install_bundle(
                "Python runtime",
                self.runtime.python_bundle,
                Path(self.runtime.python_dir),
            )
        else:
            log.info("runtime.install_bundles=false; skipping bundle installation")

        # Links are useful even when the runtime was prepared elsewhere.
        for link in self.runtime.links:
            self.runner.ensure_symlink(link.target, link.path, force=link.force)

    def _install_bundle(
        self, label: str, bundle: BundleConfig, expected_dir: Path
    ) -> None:
        if expected_dir.exists():
            log.info("%s already present at %s; skipping", label, expected_dir)
            return
        if not bundle.archive:
            raise ConfigError(f"{label} bundle archive path is not configured")
        if not bundle.source and not bundle.url:
            raise ConfigError(
                f"{label} is missing but neither its bundle source nor url is "
                "configured"
            )

        self._stage_archive(label, bundle)
        self.runner.extract_tar(bundle.archive, bundle.extract_to)

        if not expected_dir.exists() and not self.runner.dry_run:
            raise RuntimeError(
                f"{label} was extracted but {expected_dir} does not exist; "
                "check the bundle archive layout"
            )

    def _stage_archive(self, label: str, bundle: BundleConfig) -> None:
        """Place the bundle archive at ``bundle.archive`` before extraction.

        A bundle can come from a download URL or from a local file (typically
        a Kaggle dataset input); ``source`` wins when both are set.
        """
        archive = Path(bundle.archive)
        if bundle.source:
            source = Path(bundle.source)
            if not source.exists() and not self.runner.dry_run:
                raise FileNotFoundError(
                    f"{label} bundle archive not found: {source}. Add it to a "
                    "Kaggle dataset and set bundle.source, or set bundle.url."
                )
            if source.exists() and source.is_dir():
                raise ConfigError(
                    f"{label} bundle source must be an archive file, got "
                    f"directory: {source}"
                )

            ready = (
                archive.exists()
                and archive.stat().st_size > 0
                and not bundle.overwrite
            )
            if ready:
                log.info("Archive already present, skipping copy: %s", archive)
                return
            if source.resolve() != archive.resolve():
                self.runner.copy_path(source, archive, force=True)
            return

        self.runner.download(
            bundle.url,
            archive,
            connections=self.runtime.aria2.connections,
            splits=self.runtime.aria2.splits,
            overwrite=bundle.overwrite,
        )

    def install_custom_nodes(self) -> None:
        """Clone configured nodes that are missing; install their requirements."""
        if not self.custom_nodes_dir.exists():
            log.warning(
                "custom_nodes directory not found at %s; skipping node installation",
                self.custom_nodes_dir,
            )
            return

        for node in self.config.custom_nodes:
            if not node.enabled:
                continue

            path = self.custom_nodes_dir / node.name
            if node.reclone and (path.exists() or path.is_symlink()):
                self.runner.remove_path(path, root=self.custom_nodes_dir)

            cloned = False
            if not path.exists():
                if not node.repo:
                    log.warning(
                        "Custom node %s is missing and has no repo configured; "
                        "leaving it alone",
                        node.name,
                    )
                    continue
                self._clone(node, path)
                cloned = True

            if cloned or self._should_install_requirements(node):
                self._install_requirements(node, path)

    def _clone(self, node: CustomNodeConfig, path: Path) -> None:
        command = ["git", "clone"]
        if node.ref:
            command.extend(["--branch", node.ref, "--depth", "1"])
        command.extend([node.repo, str(path)])
        self.runner.run(command, desc=f"Cloning custom node {node.name}")

    def _install_requirements(self, node: CustomNodeConfig, path: Path) -> None:
        requirements = path / "requirements.txt"
        if not requirements.exists():
            log.info("No requirements.txt for custom node %s", node.name)
            return
        self.runner.run(
            [
                self.runtime.python_bin,
                "-m",
                "pip",
                "install",
                "-r",
                str(requirements),
            ],
            cwd=path,
            desc=f"Installing requirements for {node.name}",
        )

    def _should_install_requirements(self, node: CustomNodeConfig) -> bool:
        return node.install_requirements or self.config.updates.custom_node_requirements

    # ------------------------------------------------------------------ updates
    def update(self) -> None:
        if self.config.updates.comfyui:
            self._update_comfyui()
        if self.config.updates.custom_nodes:
            self._update_custom_nodes()

    def _update_comfyui(self) -> None:
        if not (self.comfy_dir / ".git").exists():
            log.warning(
                "ComfyUI at %s is not a git checkout; skipping git pull", self.comfy_dir
            )
            return

        self.runner.run(["git", "pull"], cwd=self.comfy_dir, desc="Updating ComfyUI")
        if not self.config.updates.comfyui_requirements:
            return

        requirements = self.comfy_dir / self.config.comfyui.requirements_file
        if requirements.exists():
            self.runner.run(
                [
                    self.runtime.python_bin,
                    "-m",
                    "pip",
                    "install",
                    "-r",
                    str(requirements),
                ],
                cwd=self.comfy_dir,
                desc="Installing ComfyUI requirements",
            )
        else:
            log.info("ComfyUI requirements file not found: %s", requirements)

    def _update_custom_nodes(self) -> None:
        updated: List[str] = []
        for node in self.config.custom_nodes:
            if not node.enabled:
                continue
            path = self.custom_nodes_dir / node.name
            if not path.exists():
                log.warning("Custom node %s is not installed; skipping update", node.name)
                continue
            if not (path / ".git").exists():
                log.info(
                    "Custom node %s is not a git checkout; skipping git pull", node.name
                )
                continue

            self.runner.run(["git", "pull"], cwd=path, desc=f"Updating {node.name}")
            updated.append(node.name)
            if self._should_install_requirements(node):
                self._install_requirements(node, path)

        if not updated:
            log.info("No custom nodes were updated")

    # --------------------------------------------------------------- execution
    def run_comfyui(self) -> None:
        if not self.comfy_dir.exists() and not self.runner.dry_run:
            raise FileNotFoundError(
                f"ComfyUI directory not found: {self.comfy_dir}; run install first"
            )

        args = [self.runtime.python_bin, "main.py"]
        for configured in self.config.comfyui.args:
            args.append(configured.replace("{port}", str(self.config.comfyui.port)))
        if not any(arg.startswith("--port") for arg in args):
            args.append(f"--port={self.config.comfyui.port}")

        self.runner.run(
            args,
            cwd=self.comfy_dir,
            env=self.config.comfyui.env,
            desc="Starting ComfyUI",
        )
