import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kgcomfy.app import KaggleComfy
from kgcomfy.config import ConfigError, load_config
from kgcomfy.shell import CommandError


class RecordingNotifier:
    def __init__(self):
        self.events = []

    def notify(self, event, summary, details=None):
        self.events.append((event, summary, dict(details or {})))
        return True

    def notify_async(self, event, summary, details=None):
        return self.notify(event, summary, details)

    def flush(self, timeout=None):
        return True


def offline_config(**extra):
    data = {
        "dry_run": True,
        "runtime": {"install_bundles": False},
        "frp": {"enabled": False},
        "filebrowser": {"enabled": False},
        "models": [],
    }
    data.update(extra)
    return load_config(data)


class KaggleComfyTests(unittest.TestCase):
    def test_prepare_without_services(self):
        app = KaggleComfy(offline_config())
        app.prepare()  # must not raise
        self.assertEqual(app.public_url(), "")
        self.assertEqual(app.filebrowser_url(), "http://127.0.0.1:8012/fslist/")

    def test_public_url_uses_subdomain(self):
        cfg = offline_config(
            frp={
                "enabled": True,
                "server_addr": "example.com",
                "subdomain": "tts",
                "binary": "/tmp/frpc",
            }
        )
        app = KaggleComfy(cfg)
        self.assertEqual(app.public_url(), "http://tts.example.com")
        self.assertEqual(
            app.filebrowser_url(), "http://tts.example.com/fslist/"
        )

    def test_frp_proxies_follow_filebrowser_toggle(self):
        cfg = offline_config(
            frp={
                "enabled": True,
                "server_addr": "example.com",
                "subdomain": "tts",
                "binary": "/tmp/frpc",
            },
            filebrowser={"enabled": False},
        )
        app = KaggleComfy(cfg)
        self.assertEqual(len(app.frp.proxies), 1)
        self.assertEqual(app.frp.proxies[0].local_port, 8188)

    def test_invalid_config_is_rejected_early(self):
        cfg = load_config({"frp": {"enabled": True}})
        with self.assertRaises(ConfigError):
            KaggleComfy(cfg)

    def test_commands_history_is_exposed(self):
        app = KaggleComfy(offline_config())
        app.diagnostics()
        self.assertTrue(any("nvidia-smi" in item for item in app.commands))

    def test_publisher_create_tar_dry_run(self):
        app = KaggleComfy(offline_config())
        archive = app.publisher().create_tar("/tmp/out.tar", "/tmp/ComfyUI")
        self.assertEqual(Path(archive), Path("/tmp/out.tar"))
        self.assertTrue(any(item.startswith("tar -cf") for item in app.commands))

    def test_run_comfyui_dry_run_still_builds_command(self):
        app = KaggleComfy(offline_config())
        result = app.run_comfyui()
        self.assertEqual(result, 0)
        command = app.commands[-1]
        self.assertIn("main.py", command)
        self.assertIn("--port=8188", command)

    def test_run_comfyui_can_disable_watchdog(self):
        app = KaggleComfy(offline_config(watchdog={"enabled": True}))
        result = app.run_comfyui(watchdog_enabled=False)
        self.assertEqual(result, 0)
        self.assertIn("main.py", app.commands[-1])

    def test_build_supervisor_uses_watchdog_config(self):
        app = KaggleComfy(
            offline_config(
                comfyui={"port": 9999},
                watchdog={"health_path": "/health", "interval": 7},
            )
        )
        supervisor = app.build_supervisor()
        self.assertEqual(supervisor.settings.health_path, "/health")
        self.assertEqual(supervisor.settings.interval, 7)
        self.assertEqual(supervisor.status()["health_path"], "/health")

    def test_build_supervisor_wires_port_probe(self):
        app = KaggleComfy(offline_config(comfyui={"port": 9999}))

        with mock.patch("kgcomfy.app.TcpPortProbe") as probe_cls:
            app.build_supervisor()

        probe_cls.assert_called_once_with("127.0.0.1", 9999)

    def test_watchdog_status_reads_persisted_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "watchdog.json"
            state.write_text('{"state": "failed", "restarts": 3}', encoding="utf-8")
            app = KaggleComfy(
                offline_config(watchdog={"state_path": str(state)})
            )
            self.assertEqual(app.watchdog_status()["state"], "failed")
            self.assertEqual(app.watchdog_status()["restarts"], 3)

    def test_webhook_disabled_returns_none(self):
        app = KaggleComfy(offline_config(dry_run=False))
        self.assertIsNone(app.webhook_notifier())

    def test_webhook_url_secret_is_resolved(self):
        app = KaggleComfy(
            offline_config(
                dry_run=False,
                webhook={"enabled": True, "url_secret": "hook_url"},
            ),
            secret_overrides={"hook_url": "https://example.com/hook"},
        )

        notifier = app.webhook_notifier()

        self.assertIsNotNone(notifier)
        self.assertEqual(notifier.url, "https://example.com/hook")
        self.assertEqual(notifier.provider, "generic")

    def test_webhook_is_suppressed_in_dry_run(self):
        app = KaggleComfy(
            offline_config(
                webhook={"enabled": True, "url": "https://example.com/hook"}
            )
        )
        self.assertIsNone(app.webhook_notifier())

    def test_webhook_is_injected_into_supervisor(self):
        app = KaggleComfy(
            offline_config(
                dry_run=False,
                webhook={"enabled": True, "url": "https://example.com/hook"},
            )
        )
        supervisor = app.build_supervisor()
        self.assertIs(supervisor.notifier, app.webhook_notifier())

    def test_webhook_context_uses_local_url_when_frp_disabled(self):
        app = KaggleComfy(
            offline_config(
                dry_run=False,
                webhook={
                    "enabled": True,
                    "url": "https://example.com/hook",
                },
            )
        )

        notifier = app.webhook_notifier()

        self.assertIsNotNone(notifier)
        self.assertEqual(notifier.context["url"], "http://127.0.0.1:8188")

    def test_watchdog_setup_failure_notifies_start_failed(self):
        app = KaggleComfy(offline_config(dry_run=False))
        notifier = RecordingNotifier()
        app.comfy.require_installed = mock.Mock(
            side_effect=FileNotFoundError("missing ComfyUI")
        )

        with mock.patch.object(
            app, "webhook_notifier", return_value=notifier
        ), self.assertRaises(FileNotFoundError):
            app.run_comfyui()

        self.assertEqual(
            [event for event, _summary, _details in notifier.events],
            ["start_failed"],
        )

    def test_unresolved_sign_secret_warns(self):
        for direct_secret in ("", "direct-secret"):
            with self.subTest(direct_secret=direct_secret):
                app = KaggleComfy(
                    offline_config(
                        dry_run=False,
                        webhook={
                            "enabled": True,
                            "url": "https://example.com/hook",
                            "secret": direct_secret,
                            "sign_secret": "test_missing_webhook_secret",
                        },
                    )
                )

                with self.assertLogs("kgcomfy", level="WARNING") as logs:
                    notifier = app.webhook_notifier()

                self.assertIsNotNone(notifier)
                self.assertEqual(notifier.secret, direct_secret)
                self.assertTrue(
                    any("did not resolve" in line for line in logs.output)
                )

    def test_webhook_enabled_without_url_is_disabled_at_runtime(self):
        app = KaggleComfy(
            offline_config(dry_run=False, webhook={"enabled": True})
        )
        with self.assertLogs("kgcomfy", level="WARNING") as logs:
            self.assertIsNone(app.webhook_notifier())
        self.assertTrue(any("no URL resolved" in line for line in logs.output))

    def test_non_watchdog_path_notifies_started_and_stopped(self):
        app = KaggleComfy(offline_config(dry_run=False))
        notifier = RecordingNotifier()
        app.comfy.require_installed = mock.Mock()
        app.comfy.run_comfyui = mock.Mock()

        with mock.patch.object(
            app, "webhook_notifier", return_value=notifier
        ):
            result = app.run_comfyui(watchdog_enabled=False)

        self.assertEqual(result, 0)
        self.assertEqual(
            [event for event, _summary, _details in notifier.events],
            ["started", "stopped"],
        )

    def test_non_watchdog_command_failure_notifies_crashed(self):
        app = KaggleComfy(offline_config(dry_run=False))
        notifier = RecordingNotifier()
        app.comfy.require_installed = mock.Mock()
        app.comfy.run_comfyui = mock.Mock(
            side_effect=CommandError("main.py", 1)
        )

        with mock.patch.object(
            app, "webhook_notifier", return_value=notifier
        ), self.assertRaises(CommandError):
            app.run_comfyui(watchdog_enabled=False)

        self.assertEqual(
            [event for event, _summary, _details in notifier.events],
            ["started", "crashed"],
        )

    def test_run_reports_prepare_failure_as_start_failed(self):
        app = KaggleComfy(offline_config(dry_run=False))
        notifier = RecordingNotifier()
        app.prepare = mock.Mock(side_effect=RuntimeError("install failed"))

        with mock.patch.object(
            app, "webhook_notifier", return_value=notifier
        ), self.assertRaises(RuntimeError):
            app.run()

        self.assertEqual(
            [event for event, _summary, _details in notifier.events],
            ["start_failed"],
        )

    def test_non_watchdog_exec_failure_notifies_start_failed(self):
        app = KaggleComfy(offline_config(dry_run=False))
        notifier = RecordingNotifier()
        app.comfy.require_installed = mock.Mock()
        app.comfy.run_comfyui = mock.Mock(side_effect=OSError("cannot exec"))

        with mock.patch.object(
            app, "webhook_notifier", return_value=notifier
        ), self.assertRaises(OSError):
            app.run_comfyui(watchdog_enabled=False)

        self.assertEqual(
            [event for event, _summary, _details in notifier.events],
            ["started", "start_failed"],
        )

    def test_non_watchdog_install_failure_notifies_start_failed(self):
        app = KaggleComfy(offline_config(dry_run=False))
        notifier = RecordingNotifier()
        app.comfy.require_installed = mock.Mock(
            side_effect=FileNotFoundError("missing ComfyUI")
        )

        with mock.patch.object(
            app, "webhook_notifier", return_value=notifier
        ), self.assertRaises(FileNotFoundError):
            app.run_comfyui(watchdog_enabled=False)

        self.assertEqual(
            [event for event, _summary, _details in notifier.events],
            ["start_failed"],
        )


if __name__ == "__main__":
    unittest.main()
