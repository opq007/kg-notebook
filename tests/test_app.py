import tempfile
import unittest
from pathlib import Path

from kgcomfy.app import KaggleComfy
from kgcomfy.config import ConfigError, load_config


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

    def test_watchdog_status_reads_persisted_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "watchdog.json"
            state.write_text('{"state": "failed", "restarts": 3}', encoding="utf-8")
            app = KaggleComfy(
                offline_config(watchdog={"state_path": str(state)})
            )
            self.assertEqual(app.watchdog_status()["state"], "failed")
            self.assertEqual(app.watchdog_status()["restarts"], 3)


if __name__ == "__main__":
    unittest.main()
