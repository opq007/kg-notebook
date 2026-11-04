import json
import socket
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from kgcomfy.config import WatchdogConfig
from kgcomfy.supervisor import (
    HttpHealthProbe,
    SubprocessSpawner,
    Supervisor,
    rotate_log_if_needed,
)


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []
        self.on_sleep = None

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds
        if self.on_sleep is not None:
            self.on_sleep(seconds)


class FakeProcess:
    def __init__(
        self,
        clock,
        exit_after=None,
        exit_code=1,
        pid=100,
        terminate_code=-15,
    ):
        self.clock = clock
        self.pid = pid
        self.exit_code = exit_code
        self.exit_at = None if exit_after is None else clock.now + exit_after
        self.returncode = None
        self.terminate_code = terminate_code
        self.terminated = False

    def poll(self):
        if (
            self.returncode is None
            and self.exit_at is not None
            and self.clock.now >= self.exit_at
        ):
            self.returncode = self.exit_code
        return self.returncode

    def terminate(self, timeout):
        if self.returncode is None:
            self.terminated = True
            self.returncode = self.terminate_code


class RepeatingSpawner:
    def __init__(
        self, clock, exit_after=None, exit_code=1, terminate_code=-15
    ):
        self.clock = clock
        self.exit_after = exit_after
        self.exit_code = exit_code
        self.terminate_code = terminate_code
        self.calls = 0
        self.created = []

    def __call__(self):
        self.calls += 1
        process = FakeProcess(
            self.clock,
            exit_after=self.exit_after,
            exit_code=self.exit_code,
            pid=100 + self.calls,
            terminate_code=self.terminate_code,
        )
        self.created.append(process)
        return process


class AlwaysFailSpawner:
    def __init__(self):
        self.calls = 0

    def __call__(self):
        self.calls += 1
        raise FileNotFoundError("missing python")


def watchdog_settings(**overrides):
    values = {
        "interval": 1.0,
        "timeout": 0.1,
        "failures": 2,
        "startup_grace": 0.0,
        "backoff_initial": 1.0,
        "backoff_max": 10.0,
        "max_restarts": 2,
        "restart_window": 100.0,
        "shutdown_timeout": 0.0,
        "log_path": "",
        "state_path": "",
        "log_max_bytes": 0,
    }
    values.update(overrides)
    return WatchdogConfig(**values)


class SupervisorPolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state_path = Path(self.tmp.name) / "watchdog.json"

    def tearDown(self):
        self.tmp.cleanup()

    def _supervisor(self, spawner, probe, clock, **settings):
        return Supervisor(
            name="FakeComfy",
            spawn=spawner,
            probe=probe,
            settings=watchdog_settings(
                state_path=str(self.state_path), **settings
            ),
            now=clock.time,
            sleep=clock.sleep,
            wall_clock=clock.time,
        )

    def test_gives_up_after_max_restarts(self):
        clock = FakeClock()
        spawner = RepeatingSpawner(clock, exit_after=1.0)
        supervisor = self._supervisor(
            spawner, lambda: True, clock, max_restarts=2
        )

        result = supervisor.run()

        self.assertEqual(result, 1)
        self.assertEqual(spawner.calls, 3)
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["state"], "failed")
        self.assertEqual(state["restarts"], 2)
        self.assertIn("reached 2 restarts", state["last_error"])

    def test_health_failures_terminate_the_process(self):
        clock = FakeClock()
        spawner = RepeatingSpawner(clock, exit_after=None)
        supervisor = self._supervisor(
            spawner,
            lambda: False,
            clock,
            failures=2,
            max_restarts=0,
        )

        result = supervisor.run()

        self.assertEqual(result, 1)
        self.assertEqual(spawner.calls, 1)
        self.assertTrue(spawner.created[0].terminated)
        state = supervisor.status()
        self.assertEqual(state["last_exit_code"], -15)
        self.assertIn("health check failed", state["last_error"])

    def test_backoff_doubles_between_restarts(self):
        clock = FakeClock()
        spawner = RepeatingSpawner(clock, exit_after=0.0)
        supervisor = self._supervisor(
            spawner,
            lambda: True,
            clock,
            max_restarts=2,
            backoff_initial=2.0,
            backoff_max=100.0,
        )

        result = supervisor.run()

        self.assertEqual(result, 1)
        self.assertEqual(spawner.calls, 3)
        self.assertEqual(clock.sleeps, [2.0, 4.0])

    def test_spawn_failure_is_retried_then_reported(self):
        clock = FakeClock()
        spawner = AlwaysFailSpawner()
        supervisor = self._supervisor(
            spawner, lambda: True, clock, max_restarts=1
        )

        result = supervisor.run()

        self.assertEqual(result, 1)
        self.assertEqual(spawner.calls, 2)
        self.assertIn(
            "FileNotFoundError: missing python",
            supervisor.status()["last_error"],
        )

    def test_stop_terminates_running_child(self):
        clock = FakeClock()
        spawner = RepeatingSpawner(clock, exit_after=None)
        holder = {}

        def on_sleep(_seconds):
            if len(clock.sleeps) >= 2:
                holder["supervisor"].stop()

        clock.on_sleep = on_sleep
        supervisor = self._supervisor(
            spawner, lambda: True, clock, max_restarts=5
        )
        holder["supervisor"] = supervisor

        result = supervisor.run()

        self.assertEqual(result, 0)
        self.assertTrue(spawner.created[0].terminated)
        self.assertEqual(supervisor.status()["state"], "stopped")

    def test_healthy_process_is_not_killed_by_watchdog(self):
        clock = FakeClock()
        spawner = RepeatingSpawner(clock, exit_after=5.0)
        supervisor = self._supervisor(
            spawner, lambda: True, clock, max_restarts=0
        )

        result = supervisor.run()

        # The process exited on its own; it was never terminated by a probe.
        self.assertEqual(result, 1)
        self.assertFalse(spawner.created[0].terminated)
        self.assertEqual(supervisor.status()["last_exit_code"], 1)

    def test_clean_exit_stops_without_restart(self):
        clock = FakeClock()
        spawner = RepeatingSpawner(clock, exit_after=1.0, exit_code=0)
        supervisor = self._supervisor(
            spawner, lambda: True, clock, max_restarts=5
        )

        result = supervisor.run()

        self.assertEqual(result, 0)
        self.assertEqual(spawner.calls, 1)
        self.assertEqual(supervisor.status()["state"], "stopped")

    def test_keyboard_interrupt_stops_and_returns_130(self):
        clock = FakeClock()
        spawner = RepeatingSpawner(clock, exit_after=None)

        def interrupt(_seconds):
            raise KeyboardInterrupt

        clock.on_sleep = interrupt
        supervisor = self._supervisor(
            spawner, lambda: True, clock, max_restarts=5
        )

        result = supervisor.run()

        self.assertEqual(result, 130)
        self.assertTrue(spawner.created[0].terminated)
        self.assertEqual(supervisor.status()["state"], "stopped")

    def test_hang_after_healthy_is_detected_despite_startup_grace(self):
        clock = FakeClock()
        spawner = RepeatingSpawner(clock, exit_after=None)
        calls = {"count": 0}

        def probe():
            calls["count"] += 1
            return calls["count"] == 1

        supervisor = self._supervisor(
            spawner,
            probe,
            clock,
            startup_grace=100.0,
            failures=2,
            max_restarts=0,
        )

        result = supervisor.run()

        self.assertEqual(result, 1)
        self.assertTrue(spawner.created[0].terminated)
        self.assertIn(
            "health check failed", supervisor.status()["last_error"]
        )

    def test_hang_kill_with_clean_exit_still_restarts(self):
        clock = FakeClock()
        spawner = RepeatingSpawner(
            clock, exit_after=None, terminate_code=0
        )
        supervisor = self._supervisor(
            spawner,
            lambda: False,
            clock,
            failures=1,
            max_restarts=0,
        )

        result = supervisor.run()

        self.assertEqual(result, 1)
        self.assertTrue(spawner.created[0].terminated)
        self.assertEqual(supervisor.status()["state"], "failed")
        self.assertIn(
            "health check failed", supervisor.status()["last_error"]
        )

    def test_keyboard_interrupt_during_backoff_does_not_count_restart(self):
        clock = FakeClock()
        spawner = RepeatingSpawner(clock, exit_after=1.0)

        def on_sleep(seconds):
            if seconds == 10.0:
                raise KeyboardInterrupt

        clock.on_sleep = on_sleep
        supervisor = self._supervisor(
            spawner,
            lambda: True,
            clock,
            max_restarts=5,
            backoff_initial=10.0,
            backoff_max=10.0,
        )

        result = supervisor.run()

        self.assertEqual(result, 130)
        self.assertEqual(spawner.calls, 1)
        self.assertEqual(supervisor.status()["restarts"], 0)
        self.assertEqual(supervisor.status()["state"], "stopped")

    def test_running_supervisor_rotates_log(self):
        log_path = Path(self.tmp.name) / "comfy.log"
        log_path.write_bytes(b"x" * 100)
        clock = FakeClock()
        spawner = RepeatingSpawner(clock, exit_after=1.0)
        supervisor = self._supervisor(
            spawner,
            lambda: True,
            clock,
            max_restarts=0,
            log_path=str(log_path),
            log_max_bytes=10,
        )

        supervisor.run()

        self.assertEqual(log_path.read_bytes(), b"")
        self.assertEqual(
            Path(str(log_path) + ".1").read_bytes(), b"x" * 100
        )


class SpawnerTests(unittest.TestCase):
    def test_child_output_is_written_to_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "comfy.log"
            spawner = SubprocessSpawner(
                [
                    sys.executable,
                    "-c",
                    "print('hello from fake comfy'); raise SystemExit(3)",
                ],
                cwd=tmp,
                env={},
                log_path=str(log_path),
                log_max_bytes=0,
            )

            handle = spawner()
            handle.process.wait(timeout=10)

            self.assertEqual(handle.poll(), 3)
            self.assertIn(
                "hello from fake comfy",
                log_path.read_text(encoding="utf-8", errors="replace"),
            )

    def test_log_rotation_keeps_one_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "comfy.log"
            log_path.write_bytes(b"x" * 100)

            rotate_log_if_needed(str(log_path), 10)

            self.assertEqual(
                Path(str(log_path) + ".1").read_bytes(), b"x" * 100
            )
            self.assertEqual(log_path.read_bytes(), b"")

    def test_handle_terminates_a_running_child(self):
        with tempfile.TemporaryDirectory() as tmp:
            spawner = SubprocessSpawner(
                [sys.executable, "-c", "import time; time.sleep(60)"],
                cwd=tmp,
                env={},
                log_path="",
                log_max_bytes=0,
            )

            handle = spawner()
            self.assertIsNone(handle.poll())

            handle.terminate(timeout=5)

            self.assertIsNotNone(handle.poll())


class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/ok":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b"{}")
        else:
            self.send_response(503)
            self.end_headers()

    def log_message(self, *args):
        pass


class HealthProbeTests(unittest.TestCase):
    def test_probe_accepts_2xx_and_rejects_other_status(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), _HealthHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            self.assertTrue(
                HttpHealthProbe(
                    f"http://127.0.0.1:{port}/ok", timeout=2
                )()
            )
            self.assertFalse(
                HttpHealthProbe(
                    f"http://127.0.0.1:{port}/down", timeout=2
                )()
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_probe_returns_false_when_nothing_listens(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]

        self.assertFalse(
            HttpHealthProbe(f"http://127.0.0.1:{port}/ok", timeout=0.2)()
        )


_FAKE_COMFY = r"""
import http.server
import sys
import time

port = int(sys.argv[1])
marker = sys.argv[2]

with open(marker, "a", encoding="utf-8") as handle:
    handle.write("start\n")

class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/system_stats":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok": true}')
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args):
        pass

server = http.server.HTTPServer(("127.0.0.1", port), Handler)
server.timeout = 0.1
print("fake comfy listening on", port, flush=True)
deadline = time.time() + 1.2
while time.time() < deadline:
    server.handle_request()
server.server_close()
raise SystemExit(3)
"""


class SupervisorIntegrationTests(unittest.TestCase):
    """Start a real HTTP process that crashes, and verify restarts end-to-end."""

    def test_real_process_is_restarted_until_budget_is_exhausted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "fake_comfy.py"
            script.write_text(_FAKE_COMFY, encoding="utf-8")
            marker = root / "runs.txt"
            log_path = root / "comfy.log"
            state_path = root / "state.json"

            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]

            settings = WatchdogConfig(
                interval=0.1,
                timeout=0.5,
                failures=3,
                startup_grace=0.8,
                backoff_initial=0.05,
                backoff_max=0.1,
                max_restarts=2,
                restart_window=60.0,
                shutdown_timeout=1.0,
                log_path=str(log_path),
                state_path=str(state_path),
                log_max_bytes=0,
            )
            spawner = SubprocessSpawner(
                [sys.executable, str(script), str(port), str(marker)],
                cwd=tmp,
                env={},
                log_path=str(log_path),
                log_max_bytes=0,
            )
            supervisor = Supervisor(
                name="FakeComfy",
                spawn=spawner,
                probe=HttpHealthProbe(
                    f"http://127.0.0.1:{port}/system_stats", timeout=0.5
                ),
                settings=settings,
                sleep=time.sleep,
            )

            result = supervisor.run()

            self.assertEqual(result, 1)
            self.assertEqual(
                marker.read_text(encoding="utf-8").splitlines(),
                ["start", "start", "start"],
            )
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(state["state"], "failed")
            self.assertEqual(state["restarts"], 2)
            self.assertIn(
                "fake comfy listening",
                log_path.read_text(encoding="utf-8", errors="replace"),
            )
            self.assertFalse(supervisor.status()["healthy"])


if __name__ == "__main__":
    unittest.main()
