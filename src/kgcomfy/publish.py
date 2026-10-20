"""Optional helpers for snapshotting and uploading the runtime bundle."""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

from .config import Config, ConfigError
from .secrets import SecretResolver
from .shell import Runner

log = logging.getLogger("kgcomfy")


class Publisher:
    """Create tarballs and upload them to a ModelScope dataset repository."""

    def __init__(self, config: Config, runner: Runner, secrets: SecretResolver):
        self.config = config
        self.runner = runner
        self.secrets = secrets

    def create_tar(self, archive: str | Path, source: str | Path) -> Path:
        archive_path = Path(archive)
        source_path = Path(source)
        if not source_path.exists() and not self.runner.dry_run:
            raise FileNotFoundError(f"snapshot source not found: {source_path}")

        if not self.runner.dry_run:
            archive_path.parent.mkdir(parents=True, exist_ok=True)
        self.runner.run(
            [
                "tar",
                "-cf",
                str(archive_path),
                "-C",
                str(source_path.parent),
                source_path.name,
            ],
            desc=f"Creating {archive_path}",
        )
        return archive_path

    def modelscope_login(self) -> None:
        binary, token, env = self._modelscope()
        self.runner.run(
            [binary, "login", "--token", token],
            env=env,
            redact=[token],
            desc="Logging in to ModelScope",
        )

    def modelscope_upload(
        self,
        local_file: str | Path,
        remote_name: str | None = None,
        repo: str | None = None,
        repo_type: str | None = None,
    ) -> None:
        local_path = Path(local_file)
        if not local_path.exists() and not self.runner.dry_run:
            raise FileNotFoundError(f"upload file not found: {local_path}")

        target_repo = repo or self.config.publish.modelscope_repo
        if not target_repo:
            raise ConfigError("publish.modelscope_repo is not configured")

        self.modelscope_login()
        binary, _token, env = self._modelscope()
        self.runner.run(
            [
                binary,
                "upload",
                target_repo,
                str(local_path),
                remote_name or local_path.name,
                "--repo-type",
                repo_type or self.config.publish.repo_type,
            ],
            env=env,
            desc=f"Uploading {local_path.name} to {target_repo}",
        )

    def _modelscope(self) -> tuple[str, str, dict[str, str]]:
        token = self.secrets.get(self.config.publish.modelscope_token_secret)
        bin_dir = str(Path(self.config.runtime.python_bin).parent)
        env = {"PATH": bin_dir + os.pathsep + os.environ.get("PATH", "")}
        binary = shutil.which("modelscope", path=env["PATH"])
        if not binary:
            raise RuntimeError(
                "modelscope CLI not found. Install it into the portable runtime: "
                f"{self.config.runtime.python_bin} -m pip install modelscope"
            )
        return binary, token or "", env
