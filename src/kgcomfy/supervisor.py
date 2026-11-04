"""ComfyUI watchdog: restart the process after crashes or hung health checks.

Phase 1 runs the supervisor inside the notebook cell that starts ComfyUI, so
the cell keeps blocking exactly like ``subprocess.run`` did before.  The
supervisor is independent of frp and the file browser: ComfyUI is restarted on
the same port, so existing frp tunnels keep working without being rebuilt.
"""

from __future__ import annotations

import http.client
import json
import logging
import os
import shutil
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    Mapping,
    NamedTuple,
    Protocol,
    Sequence,
    TypedDict,
    cast,
)

from .config import WatchdogConfig
from .shell import merge_env

log = logging.getLogger("kgcomfy")

_POSIX = os.name == "posix"
_SIGKILL = getattr(signal, "SIGKILL", signal.SIGTERM)
_TAIL_BYTES = 16 * 1024
_TAIL_LINES = 40
_KILL_WAIT_SECONDS = 5.0


class SupervisorState(str, Enum):
    STARTING = "starting"
    RUNNING = "running"
    RESTARTING = "restarting"
    FAILED = "failed"
    STOPPED = "stopped"


EXIT_CLEAN = 0
EXIT_GAVE_UP = 1
EXIT_INTERRUPTED = 130


class SupervisorStatus(TypedDict, total=False):
    name: str
    state: str
    pid: int | None
    healthy: bool
    restarts: int
    last_exit_code: int | None
    last_error: str
    started_at: str
    updated_at: str
    health_path: str
    log_path: str
    next_restart_in: float | None


class WatchOutcome(NamedTuple):
    exit_code: int | None
    reason: str = ""


def rotate_log_if_needed(
    path_text: str, max_bytes: int, logger: logging.Logger | None = None
) -> None:
    """Copy-truncate the live log so rotation also works while ComfyUI runs."""
    if not path_text or max_bytes <= 0:
        return
    path = Path(path_text)
    sink = logger or log
    try:
        if not path.exists() or path.stat().st_size <= max_bytes:
            return
        backup = Path(str(path) + ".1")
        shutil.copyfile(path, backup)
        with path.open("wb"):
            pass
        sink.info("Rotated ComfyUI log: %s -> %s", path, backup)
    except OSError as exc:
        sink.warning("Could not rotate ComfyUI log %s: %s", path, exc)


class ProcessHandle(Protocol):
    """Minimal process interface, small enough to fake in tests."""

    pid: int

    def poll(self) -> int | None:
        ...

    def terminate(self, timeout: float) -> None:
        ...


class SubprocessHandle:
    """Wrap ``subprocess.Popen`` and terminate the whole process group."""

    def __init__(self, process: subprocess.Popen):
        self.process = process
        self.pid = process.pid

    def poll(self) -> int | None:
        return self.process.poll()

    def terminate(self, timeout: float) -> None:
        # On POSIX the child was started with start_new_session=True, so its
        # pid is also the process-group id.  Signal the group even when the
        # direct child already exited: custom nodes may have left children
        # holding GPU memory, which would make the next start fail with OOM.
        self._send(signal.SIGTERM, self.process.terminate)
        try:
            self.process.wait(timeout=max(0.0, timeout))
        except subprocess.TimeoutExpired:
            self.kill()

    def kill(self) -> None:
        self._send(_SIGKILL, self.process.kill)
        try:
            self.process.wait(timeout=_KILL_WAIT_SECONDS)
        except subprocess.TimeoutExpired:
            pass

    def _send(self, sig: int, windows_action: Callable[[], None]) -> None:
        if _POSIX:
            self._signal_group(sig)
        elif self.process.poll() is None:
            windows_action()

    def _signal_group(self, sig: int) -> None:
        try:
            os.killpg(self.pid, sig)
        except ProcessLookupError:
            pass
        except PermissionError:
            try:
                self.process.send_signal(sig)
            except ProcessLookupError:
                pass


class SubprocessSpawner:
    """Spawn ComfyUI with its output appended to ``log_path``."""

    def __init__(
        self,
        command: Sequence[str],
        cwd: str | Path,
        env: Mapping[str, str],
        log_path: str,
        log_max_bytes: int = 0,
        logger: logging.Logger | None = None,
    ):
        self.command = [str(item) for item in command]
        self.cwd = str(cwd)
        self.env = merge_env(env)
        self.log_path = log_path
        self.log_max_bytes = log_max_bytes
        self.log = logger or log

    def __call__(self) -> ProcessHandle:
        rotate_log_if_needed(self.log_path, self.log_max_bytes, self.log)
        log_file = None
        if self.log_path:
            try:
                Path(self.log_path).parent.mkdir(parents=True, exist_ok=True)
                log_file = open(self.log_path, "ab", buffering=0)
            except OSError as exc:
                self.log.error(
                    "Cannot open ComfyUI log %s (%s); child output is discarded",
                    self.log_path,
                    exc,
                )

        try:
            kwargs: Dict[str, Any] = {
                "cwd": self.cwd,
                "env": self.env,
                "stdout": log_file if log_file is not None else subprocess.DEVNULL,
                "stderr": subprocess.STDOUT,
            }
            if _POSIX:
                kwargs["start_new_session"] = True
            process = subprocess.Popen(self.command, **kwargs)
        finally:
            if log_file is not None:
                log_file.close()

        self.log.info(
            "Started ComfyUI (pid %s): %s", process.pid, " ".join(self.command)
        )
        return SubprocessHandle(process)


class HttpHealthProbe:
    """Return True when ComfyUI answers the configured path with 2xx."""

    def __init__(self, url: str, timeout: float = 5.0):
        self.url = url
        self.timeout = timeout
        # Never route localhost health checks through an HTTP proxy.
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({})
        )

    def __call__(self) -> bool:
        try:
            with self._opener.open(self.url, timeout=self.timeout) as response:
                status = getattr(response, "status", None)
                if status is None:  # pragma: no cover - very old Python
                    status = response.getcode()
                response.read(1024)
                return 200 <= int(status) < 300
        except (
            urllib.error.URLError,
            http.client.HTTPException,
            OSError,
            ValueError,
        ):
            return False


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat(
        timespec="seconds"
    )


class Supervisor:
    """Watch one process, restarting it after a crash or failed health check.

    ``spawn`` and ``probe`` are injectable so the restart policy can be tested
    without launching a GPU process; ``now``/``sleep``/``wall_clock`` let tests
    advance time instantly.
    """

    def __init__(
        self,
        name: str,
        spawn: Callable[[], ProcessHandle],
        probe: Callable[[], bool],
        settings: WatchdogConfig,
        logger: logging.Logger | None = None,
        now: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] | None = None,
        wall_clock: Callable[[], float] = time.time,
    ):
        self.name = name
        self.settings = settings
        self._spawn = spawn
        self._probe = probe
        self._log = logger or log
        self._now = now
        self._sleep = sleep
        self._wall_clock = wall_clock

        self._stop = threading.Event()
        self._current: ProcessHandle | None = None
        self._state_warned = False
        self._state: SupervisorStatus = {
            "name": name,
            "state": SupervisorState.STARTING.value,
            "pid": None,
            "healthy": False,
            "restarts": 0,
            "last_exit_code": None,
            "last_error": "",
            "started_at": "",
            "updated_at": "",
            "health_path": settings.health_path,
            "log_path": settings.log_path,
        }

    # ------------------------------------------------------------- public API
    def run(self) -> int:
        """Block until stopped.

        Returns ``EXIT_CLEAN`` for a clean stop (including ComfyUI exiting
        with code 0), ``EXIT_GAVE_UP`` when the restart budget is exhausted or
        the supervisor crashes, and ``EXIT_INTERRUPTED`` when interrupted.
        """
        self._log.info(
            "Watchdog for %s enabled: health=%s, interval=%.1fs, "
            "startup_grace=%.1fs, max %d restarts/%gs",
            self.name,
            self.settings.health_path,
            self.settings.interval,
            self.settings.startup_grace,
            self.settings.max_restarts,
            self.settings.restart_window,
        )
        self._update_state(SupervisorState.STARTING)
        result = EXIT_CLEAN
        try:
            result = self._loop()
        except KeyboardInterrupt:
            self._log.warning("Interrupted; stopping %s", self.name)
            self.stop()
            result = EXIT_INTERRUPTED
        except Exception as exc:  # keep the notebook cell alive and leave a trace
            self._log.exception("Watchdog for %s crashed", self.name)
            self._update_inactive(
                SupervisorState.FAILED,
                last_error=f"{type(exc).__name__}: {exc}",
            )
            result = EXIT_GAVE_UP
        finally:
            self._terminate_current()
            if result in (EXIT_CLEAN, EXIT_INTERRUPTED):
                self._update_inactive(SupervisorState.STOPPED)
        return result

    def stop(self) -> None:
        """Ask the loop to stop and terminate the current child immediately."""
        self._stop.set()
        self._terminate_current()

    def status(self) -> SupervisorStatus:
        return cast(SupervisorStatus, dict(self._state))

    @classmethod
    def load_state(cls, path_text: str) -> SupervisorStatus:
        """Load a persisted watchdog state, returning {} when unavailable."""
        if not path_text:
            return {}
        try:
            return cast(
                SupervisorStatus,
                json.loads(Path(path_text).read_text(encoding="utf-8")),
            )
        except (OSError, ValueError):
            return {}

    # ---------------------------------------------------------------- loop
    def _loop(self) -> int:
        settings = self.settings
        restarts: deque[float] = deque()
        completed_restarts = 0
        spawned = False

        while not self._stop.is_set():
            started = self._now()
            handle: ProcessHandle | None = None
            exit_code: int | None = None
            last_error = ""

            try:
                handle = self._spawn()
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                self._log.error("Failed to start %s: %s", self.name, last_error)
                exit_code = -1
            else:
                self._current = handle
                if spawned:
                    completed_restarts += 1
                spawned = True
                self._update_state(
                    SupervisorState.RUNNING,
                    pid=handle.pid,
                    healthy=False,
                    restarts=completed_restarts,
                    started_at=_iso(self._wall_clock()),
                    last_error="",
                    next_restart_in=None,
                )
                self._log.info("%s started (pid %s)", self.name, handle.pid)

            reason = ""
            if handle is not None:
                outcome = self._watch(handle, started)
                exit_code = outcome.exit_code
                reason = outcome.reason

            if self._stop.is_set():
                break

            if exit_code is None:
                exit_code = -1

            # Exit 0 is a clean stop, unless we killed a hung process and it
            # handled SIGTERM gracefully (reason == "health check failed").
            if exit_code == 0 and not reason:
                self._log.info(
                    "%s exited cleanly (exit=0); watchdog stopping", self.name
                )
                self._update_inactive(
                    SupervisorState.STOPPED,
                    restarts=completed_restarts,
                    last_exit_code=exit_code,
                    last_error="",
                )
                return EXIT_CLEAN

            if reason and not last_error:
                last_error = reason
            uptime = self._now() - started
            self._log.error(
                "%s stopped unexpectedly (exit=%s, uptime=%.1fs)",
                self.name,
                exit_code,
                uptime,
            )
            self._log_tail()
            # Clear orphaned children (custom nodes) before the next start.
            self._terminate_current()

            # A run that survived a full window starts a fresh restart budget.
            if uptime >= settings.restart_window:
                restarts.clear()
            now = self._now()
            while restarts and restarts[0] < now - settings.restart_window:
                restarts.popleft()

            if len(restarts) >= settings.max_restarts:
                message = (
                    f"{self.name} reached {settings.max_restarts} restarts "
                    f"within {settings.restart_window:g}s; giving up"
                )
                if last_error:
                    message = f"{message}; last failure: {last_error}"
                self._log.error(message)
                self._update_inactive(
                    SupervisorState.FAILED,
                    restarts=completed_restarts,
                    last_exit_code=exit_code,
                    last_error=message,
                )
                return EXIT_GAVE_UP

            restarts.append(now)
            delay = min(
                settings.backoff_initial * (2 ** (len(restarts) - 1)),
                settings.backoff_max,
            )
            self._log.warning("Restarting %s in %.1fs", self.name, delay)
            self._update_inactive(
                SupervisorState.RESTARTING,
                restarts=completed_restarts,
                last_exit_code=exit_code,
                last_error=last_error,
                next_restart_in=delay,
            )
            self._pause(delay)

        return EXIT_CLEAN

    def _watch(self, handle: ProcessHandle, started: float) -> WatchOutcome:
        """Poll the process and health endpoint until exit or restart."""
        settings = self.settings
        healthy = False
        ever_healthy = False
        health_failures = 0

        while not self._stop.is_set():
            code = handle.poll()
            if code is not None:
                return WatchOutcome(code)

            rotate_log_if_needed(
                settings.log_path, settings.log_max_bytes, self._log
            )

            elapsed = self._now() - started
            if self._probe():
                if not healthy:
                    self._log.info("%s health check passed", self.name)
                    self._update_state(
                        SupervisorState.RUNNING, pid=handle.pid, healthy=True
                    )
                healthy = True
                ever_healthy = True
                health_failures = 0
            elif ever_healthy or elapsed >= settings.startup_grace:
                if healthy:
                    self._update_state(
                        SupervisorState.RUNNING, pid=handle.pid, healthy=False
                    )
                    healthy = False
                health_failures += 1
                self._log.warning(
                    "%s health check failed (%d/%d)",
                    self.name,
                    health_failures,
                    settings.failures,
                )
                if health_failures >= settings.failures:
                    self._log.error("%s is unresponsive; restarting", self.name)
                    self._terminate_current()
                    code = handle.poll()
                    if code is None:
                        code = -9
                    return WatchOutcome(code, "health check failed")

            self._pause(settings.interval)

        return WatchOutcome(None)

    # ------------------------------------------------------------- helpers
    def _pause(self, seconds: float) -> None:
        if seconds <= 0:
            return
        if self._sleep is not None:
            self._sleep(seconds)
        else:
            self._stop.wait(seconds)

    def _terminate_current(self) -> None:
        handle = self._current
        if handle is None:
            return
        self._current = None
        try:
            handle.terminate(timeout=self.settings.shutdown_timeout)
        except Exception:
            self._log.exception("Failed to terminate %s", self.name)

    def _update_inactive(
        self, status: SupervisorState, **fields: Any
    ) -> None:
        fields.setdefault("next_restart_in", None)
        self._update_state(status, pid=None, healthy=False, **fields)

    def _update_state(
        self, status: str | SupervisorState, **fields: Any
    ) -> None:
        self._state.update(fields)
        self._state["state"] = (
            status.value if isinstance(status, SupervisorState) else status
        )
        self._state["updated_at"] = _iso(self._wall_clock())
        self._persist_state()

    def _persist_state(self) -> None:
        path_text = self.settings.state_path
        if not path_text:
            return
        path = Path(path_text)
        tmp = path.with_name(path.name + ".tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(
                json.dumps(self._state, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(tmp, path)
        except OSError as exc:
            if not self._state_warned:
                self._log.warning("Cannot write watchdog state %s: %s", path, exc)
                self._state_warned = True

    def _log_tail(self) -> None:
        if not self.settings.log_path:
            return
        path = Path(self.settings.log_path)
        if not path.is_file():
            return
        try:
            with path.open("rb") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                handle.seek(max(0, size - _TAIL_BYTES))
                text = handle.read().decode("utf-8", "replace")
        except OSError as exc:
            self._log.warning("Cannot read log tail %s: %s", path, exc)
            return

        lines = text.splitlines()[-_TAIL_LINES:]
        if lines:
            self._log.error(
                "Last %d lines of %s:\n%s",
                len(lines),
                path,
                "\n".join(lines),
            )
