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
import socket
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
from .shell import describe_exception, merge_env
from .webhook import (
    Notifier,
    flush_notifications,
    send_notification,
)

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


def _format_duration(seconds: float) -> str:
    total = max(0, int(seconds))
    if total < 60:
        return f"{total}s"
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m{secs}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes}m{secs}s"


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


class TcpPortProbe:
    """Return True while something still accepts TCP connections on the port.

    A listening socket is owned by the kernel, so it stays connectable even
    when the application cannot serve HTTP (long task, GIL starvation).  The
    watchdog uses this as a safety net: a failed HTTP probe with a live port is
    treated as "busy", not "hung".
    """

    def __init__(self, host: str, port: int, timeout: float = 1.0):
        self.host = host
        self.port = int(port)
        self.timeout = timeout

    def __call__(self) -> bool | None:
        try:
            with socket.create_connection(
                (self.host, self.port), timeout=self.timeout
            ):
                return True
        except ConnectionRefusedError:
            return False
        except OSError:
            # Timeouts/resets/route errors are unknown, not "closed".
            return None


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
        notifier: Notifier | None = None,
        port_open: Callable[[], bool | None] | None = None,
    ):
        self.name = name
        self.settings = settings
        self._spawn = spawn
        self._probe = probe
        self._log = logger or log
        self._now = now
        self._sleep = sleep
        self._wall_clock = wall_clock
        self._notifier = notifier
        self._port_open = port_open

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
            self._notify(
                "stopped",
                f"{self.name} 已停止",
                {"reason": "用户中断"},
            )
            result = EXIT_INTERRUPTED
        except Exception as exc:  # keep the notebook cell alive and leave a trace
            self._log.exception("Watchdog for %s crashed", self.name)
            error = describe_exception(exc)
            self._update_inactive(
                SupervisorState.FAILED,
                last_error=error,
            )
            self._notify(
                "failed",
                f"{self.name} 看门狗异常退出",
                {"error": error},
            )
            result = EXIT_GAVE_UP
        finally:
            self._terminate_current()
            if result in (EXIT_CLEAN, EXIT_INTERRUPTED):
                self._update_inactive(SupervisorState.STOPPED)
            self._flush_notifier()
        return result

    def stop(self) -> None:
        """Ask the loop to stop and terminate the current child immediately."""
        self._stop.set()
        self._terminate_current()

    def status(self) -> SupervisorStatus:
        return cast(SupervisorStatus, dict(self._state))

    @property
    def notifier(self) -> Notifier | None:
        return self._notifier

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
        has_started_process = False

        while not self._stop.is_set():
            started = self._now()
            handle: ProcessHandle | None = None
            exit_code: int | None = None
            last_error = ""
            spawn_failed = False

            try:
                handle = self._spawn()
            except Exception as exc:
                spawn_failed = True
                last_error = describe_exception(exc)
                self._log.error("Failed to start %s: %s", self.name, last_error)
                exit_code = -1
                if not self._stop.is_set():
                    self._notify(
                        "start_failed",
                        f"{self.name} 启动失败",
                        {
                            "error": last_error,
                            "restarts": completed_restarts,
                        },
                    )
            else:
                self._current = handle
                first_process = not has_started_process
                if not first_process:
                    completed_restarts += 1
                has_started_process = True
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
                if first_process:
                    self._notify(
                        "started",
                        f"{self.name} 已启动",
                        {
                            "pid": handle.pid,
                            "restarts": completed_restarts,
                        },
                    )

            reason = ""
            if handle is not None:
                outcome = self._watch(handle, started, completed_restarts)
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
                self._notify(
                    "stopped",
                    f"{self.name} 已正常退出",
                    {
                        "exit_code": exit_code,
                        "restarts": completed_restarts,
                    },
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
            tail = self._tail_lines(None)
            self._log_tail(tail[-_TAIL_LINES:])
            # Clear orphaned children (custom nodes) before the next start.
            self._terminate_current()

            if not spawn_failed:
                self._notify(
                    "crashed",
                    f"{self.name} 意外停止",
                    {
                        "exit_code": exit_code,
                        "uptime": _format_duration(uptime),
                        "reason": reason or "进程异常退出",
                        "restarts": completed_restarts,
                        "log_path": settings.log_path,
                        "log_tail": tail,
                    },
                )

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
                self._notify(
                    "failed",
                    f"{self.name} 重启失败，看门狗已停止",
                    {
                        "exit_code": exit_code,
                        "restarts": completed_restarts,
                        "restart_attempts": len(restarts),
                        "max_restarts": settings.max_restarts,
                        "restart_window": settings.restart_window,
                        "last_error": last_error,
                        "log_path": settings.log_path,
                        "log_tail": tail,
                    },
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
            self._notify(
                "restarting",
                f"{self.name} 将在 {delay:g}s 后重启",
                {
                    "delay": f"{delay:g}s",
                    "attempt": len(restarts),
                    "max_restarts": settings.max_restarts,
                    "exit_code": exit_code,
                    "reason": last_error,
                },
            )
            self._pause(delay)

        self._notify(
            "stopped",
            f"{self.name} 已停止",
            {"reason": "收到停止请求", "restarts": completed_restarts},
        )
        return EXIT_CLEAN

    def _watch(
        self, handle: ProcessHandle, started: float, completed_restarts: int = 0
    ) -> WatchOutcome:
        """Poll the process and health endpoint until exit or restart."""
        settings = self.settings
        healthy = False
        ever_healthy = False
        health_failures = 0
        announced_ready = False
        announced_restarted = False
        busy_logged = False
        unknown_logged = False

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
                    if not announced_ready:
                        self._notify(
                            "ready",
                            f"{self.name} 服务已就绪",
                            {
                                "pid": handle.pid,
                                "startup_seconds": round(elapsed, 1),
                                "restarts": completed_restarts,
                            },
                        )
                        announced_ready = True
                    if completed_restarts > 0 and not announced_restarted:
                        self._notify_restarted(
                            handle, elapsed, completed_restarts
                        )
                        announced_restarted = True
                healthy = True
                ever_healthy = True
                health_failures = 0
                busy_logged = False
                unknown_logged = False
            else:
                if healthy:
                    self._update_state(
                        SupervisorState.RUNNING, pid=handle.pid, healthy=False
                    )
                    healthy = False

                port_open: bool | None = False
                if self._port_open is not None:
                    try:
                        result = self._port_open()
                        port_open = (
                            None if result is None else bool(result)
                        )
                    except Exception:
                        self._log.debug(
                            "TCP port check for %s failed",
                            self.name,
                            exc_info=True,
                        )
                        port_open = None

                if port_open is True:
                    # The process still owns its listening socket: it is
                    # probably busy with a long task, not hung.  This applies
                    # during startup too, so a restarted but busy process still
                    # gets one "restarted" notification.
                    if not busy_logged:
                        self._log.warning(
                            "%s health check timed out but its port is still "
                            "listening; treating it as busy",
                            self.name,
                        )
                        busy_logged = True
                    unknown_logged = False
                    health_failures = 0
                    if completed_restarts > 0 and not announced_restarted:
                        self._notify_restarted(
                            handle,
                            elapsed,
                            completed_restarts,
                            "health check timed out; port is listening",
                        )
                        announced_restarted = True
                else:
                    if port_open is None:
                        if not unknown_logged:
                            self._log.warning(
                                "%s health check failed and TCP port state is "
                                "unknown; not counting toward restart",
                                self.name,
                            )
                            unknown_logged = True
                        busy_logged = False
                    else:
                        busy_logged = False
                        unknown_logged = False
                        if ever_healthy or elapsed >= settings.startup_grace:
                            health_failures += 1
                            self._log.warning(
                                "%s health check failed (%d/%d)",
                                self.name,
                                health_failures,
                                settings.failures,
                            )
                            if health_failures >= settings.failures:
                                self._log.error(
                                    "%s is unresponsive; restarting", self.name
                                )
                                self._terminate_current()
                                code = handle.poll()
                                if code is None:
                                    code = -9
                                return WatchOutcome(
                                    code, "health check failed"
                                )

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

    def _notify_restarted(
        self,
        handle: ProcessHandle,
        elapsed: float,
        completed_restarts: int,
        reason: str = "",
    ) -> None:
        details: Dict[str, Any] = {
            "pid": handle.pid,
            "startup_seconds": round(elapsed, 1),
            "restarts": completed_restarts,
        }
        if reason:
            details["reason"] = reason
        self._notify("restarted", f"{self.name} 已重启", details)

    def _notify(
        self,
        event: str,
        summary: str,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        send_notification(
            self._notifier, self._log, event, summary, details
        )

    def _flush_notifier(self) -> None:
        flush_notifications(self._notifier, self._log)

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

    def _tail_lines(self, limit: int | None = _TAIL_LINES) -> list[str]:
        if not self.settings.log_path:
            return []
        path = Path(self.settings.log_path)
        if not path.is_file():
            return []
        try:
            with path.open("rb") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                handle.seek(max(0, size - _TAIL_BYTES))
                text = handle.read().decode("utf-8", "replace")
        except OSError as exc:
            self._log.warning("Cannot read log tail %s: %s", path, exc)
            return []
        lines = text.splitlines()
        return lines if limit is None else lines[-limit:]

    def _log_tail(self, lines: list[str] | None = None) -> None:
        if lines is None:
            lines = self._tail_lines(_TAIL_LINES)
        if lines:
            self._log.error(
                "Last %d lines of %s:\n%s",
                len(lines),
                self.settings.log_path,
                "\n".join(lines),
            )
