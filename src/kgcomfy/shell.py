"""Small, testable wrappers around subprocess, downloads and symlinks."""

from __future__ import annotations

import glob
import logging
import os
import shlex
import shutil
import subprocess
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence, Union

Command = Union[str, Sequence[str]]


class CommandError(RuntimeError):
    """Raised when a command exits with a non-zero status."""

    def __init__(self, command: str, returncode: int, output: str = "") -> None:
        message = f"command failed with exit code {returncode}: {command}"
        if output:
            message = f"{message}\n{output.strip()}"
        super().__init__(message)
        self.command = command
        self.returncode = returncode
        self.output = output


class SafetyError(RuntimeError):
    """Raised when a destructive operation would leave its allowed root."""


class Runner:
    """Execute commands and filesystem operations, optionally as a dry run.

    ``history`` keeps a redacted record of every command. It is handy both for
    tests and for showing users exactly what a notebook would do.
    """

    def __init__(self, dry_run: bool = False, logger: logging.Logger | None = None):
        self.dry_run = dry_run
        self.log = logger or logging.getLogger("kgcomfy")
        self.history: List[str] = []

    # ------------------------------------------------------------------ shell
    def run(
        self,
        command: Command,
        cwd: str | Path | None = None,
        env: Mapping[str, Any] | None = None,
        check: bool = True,
        capture: bool = False,
        desc: str | None = None,
        redact: Sequence[str] = (),
    ) -> subprocess.CompletedProcess:
        display = self._display(command, redact)
        self.history.append(display)

        if desc:
            self.log.info("%s", desc)
        prefix = "[dry-run] " if self.dry_run else ""
        self.log.info("%s$ %s%s", prefix, display, f"  (cwd={cwd})" if cwd else "")

        if self.dry_run:
            return subprocess.CompletedProcess(command, 0, "", "")

        full_env = os.environ.copy()
        if env:
            full_env.update({str(key): str(value) for key, value in env.items()})

        kwargs: Dict[str, Any] = {
            "cwd": str(cwd) if cwd else None,
            "env": full_env,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
        }
        if capture:
            kwargs["stdout"] = subprocess.PIPE
            kwargs["stderr"] = subprocess.STDOUT
        if isinstance(command, str):
            kwargs["shell"] = True

        process = subprocess.run(command, **kwargs)
        if check and process.returncode != 0:
            output = process.stdout if capture else ""
            raise CommandError(display, process.returncode, output or "")
        return process

    def apt_install(self, packages: Sequence[str]) -> None:
        package_list = [package for package in packages if package]
        if not package_list:
            return
        env = {"DEBIAN_FRONTEND": "noninteractive"}
        self.run(["apt-get", "update"], env=env, desc="Updating apt package lists")
        self.run(
            ["apt-get", "install", "-y", *package_list],
            env=env,
            desc=f"Installing apt packages: {', '.join(package_list)}",
        )

    # ---------------------------------------------------------------- download
    def download(
        self,
        url: str,
        dest: str | Path,
        connections: int = 16,
        splits: int = 16,
        overwrite: bool = False,
    ) -> Path:
        dest_path = Path(dest)
        control_file = Path(str(dest_path) + ".aria2")
        complete = (
            dest_path.exists()
            and dest_path.stat().st_size > 0
            and not control_file.exists()
        )
        if complete and not overwrite:
            self.log.info("Archive already present, skipping download: %s", dest_path)
            return dest_path

        if self.dry_run:
            self.history.append(f"download {url} -> {dest_path}")
            self.log.info("[dry-run] download %s -> %s", url, dest_path)
            return dest_path

        dest_path.parent.mkdir(parents=True, exist_ok=True)
        aria2c = shutil.which("aria2c")
        if aria2c:
            self.run(
                [
                    aria2c,
                    "-c",
                    "--allow-overwrite=true",
                    "--auto-file-renaming=false",
                    "-d",
                    str(dest_path.parent),
                    "-x",
                    str(connections),
                    "-s",
                    str(splits),
                    "-o",
                    dest_path.name,
                    url,
                ],
                desc=f"Downloading {url}",
            )
        else:
            self.log.warning("aria2c not found; falling back to urllib")
            urllib.request.urlretrieve(url, dest_path)
        return dest_path

    def extract_tar(self, archive: str | Path, dest: str | Path) -> None:
        dest_path = Path(dest)
        if not self.dry_run:
            dest_path.mkdir(parents=True, exist_ok=True)
        self.run(
            ["tar", "-xf", str(archive), "--directory", str(dest_path)],
            desc=f"Extracting {archive} to {dest_path}",
        )

    # --------------------------------------------------------------- filesystem
    def write_text(self, path: str | Path, content: str) -> Path:
        target = Path(path)
        self.history.append(f"write {target}")
        self.log.info("%swriting %s", "[dry-run] " if self.dry_run else "", target)
        if not self.dry_run:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        return target

    def chmod(self, path: str | Path, mode: int = 0o755) -> None:
        target = Path(path)
        self.history.append(f"chmod {oct(mode)} {target}")
        if self.dry_run:
            self.log.info("[dry-run] chmod %s %s", oct(mode), target)
            return
        os.chmod(target, mode)

    def ensure_symlink(
        self, src: str | Path, dst: str | Path, force: bool = False
    ) -> bool:
        """Point ``dst`` at ``src``, replacing an existing symlink.

        A real file/directory at ``dst`` is only replaced when ``force=True``;
        otherwise the operation is skipped with a warning.
        """
        src_path = Path(src)
        dst_path = Path(dst)

        if dst_path.is_symlink():
            current = os.readlink(dst_path)
            if Path(current) == src_path:
                self.log.info("Symlink already correct: %s -> %s", dst_path, src_path)
                return True
            self.log.info("Replacing symlink %s (was %s)", dst_path, current)
            if not self.dry_run:
                dst_path.unlink()
        elif dst_path.exists():
            if not force:
                self.log.warning(
                    "Destination exists and is not a symlink, skipping: %s", dst_path
                )
                return False
            self.log.info("Removing existing destination: %s", dst_path)
            if not self.dry_run:
                if dst_path.is_dir():
                    shutil.rmtree(dst_path)
                else:
                    dst_path.unlink()

        self.history.append(f"symlink {dst_path} -> {src_path}")
        if not self.dry_run:
            dst_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.symlink(
                    src_path, dst_path, target_is_directory=src_path.is_dir()
                )
            except OSError as exc:
                raise RuntimeError(
                    f"failed to create symlink {dst_path} -> {src_path}: {exc}"
                ) from exc
        self.log.info("Symlink %s -> %s", dst_path, src_path)
        return True

    def copy_path(
        self, src: str | Path, dst: str | Path, force: bool = False
    ) -> bool:
        src_path = Path(src)
        dst_path = Path(dst)

        if dst_path.exists() or dst_path.is_symlink():
            if not force:
                self.log.warning("Destination exists, skipping copy: %s", dst_path)
                return False
            self.remove_path(dst_path)

        self.history.append(f"copy {src_path} -> {dst_path}")
        if not self.dry_run:
            dst_path.parent.mkdir(parents=True, exist_ok=True)
            if src_path.is_dir():
                shutil.copytree(src_path, dst_path)
            else:
                shutil.copy2(src_path, dst_path)
        self.log.info("Copy %s -> %s", src_path, dst_path)
        return True

    def remove_path(
        self, path: str | Path, root: str | Path | None = None
    ) -> bool:
        target = Path(path)
        if root is not None:
            self._assert_within(target, root)

        if not target.exists() and not target.is_symlink():
            return False

        self.log.info("%sRemoving %s", "[dry-run] " if self.dry_run else "", target)
        self.history.append(f"remove {target}")
        if not self.dry_run:
            if target.is_symlink() or target.is_file():
                target.unlink()
            else:
                shutil.rmtree(target)
        return True

    def remove_symlinks(
        self, pattern: str | Path, root: str | Path
    ) -> List[Path]:
        """Delete only symlinks matching ``pattern`` (real files are kept)."""
        pattern_path = Path(os.path.abspath(pattern))
        self._assert_within(pattern_path.parent, root)

        removed: List[Path] = []
        for match in sorted(glob.glob(str(pattern_path))):
            target = Path(match)
            if not target.is_symlink():
                continue
            self.log.info(
                "%sRemoving symlink %s",
                "[dry-run] " if self.dry_run else "",
                target,
            )
            self.history.append(f"remove symlink {target}")
            if not self.dry_run:
                target.unlink()
            removed.append(target)
        return removed

    @staticmethod
    def _assert_within(path: str | Path, root: str | Path) -> None:
        root_abs = Path(os.path.abspath(root))
        path_abs = Path(os.path.abspath(path))
        if path_abs == root_abs:
            return
        try:
            path_abs.relative_to(root_abs)
        except ValueError as exc:
            raise SafetyError(
                f"refusing to modify {path_abs}: outside allowed root {root_abs}"
            ) from exc

    # ----------------------------------------------------------------- helpers
    @staticmethod
    def _display(command: Command, redact: Sequence[str] = ()) -> str:
        if isinstance(command, str):
            display = command
        else:
            display = " ".join(shlex.quote(str(item)) for item in command)
        for secret in redact:
            if secret:
                display = display.replace(str(secret), "***")
        return display
