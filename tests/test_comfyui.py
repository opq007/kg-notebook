import tempfile
import unittest
from pathlib import Path

from kgcomfy.comfyui import ComfyUIInstaller
from kgcomfy.config import load_config
from kgcomfy.shell import Runner


class ComfyUIInstallerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.comfy = self.root / "ComfyUI"
        self.python_dir = self.root / "py312"

    def tearDown(self):
        self.tmp.cleanup()

    def _config(self, **extra):
        data = {
            "dry_run": True,
            "runtime": {
                "comfy_dir": str(self.comfy),
                "python_dir": str(self.python_dir),
                "install_bundles": True,
                "apt_packages": ["aria2"],
                "comfyui_bundle": {
                    "url": "https://example.com/ComfyUI.tar",
                    "archive": str(self.root / "ComfyUI.tar"),
                    "extract_to": str(self.root),
                },
                "python_bundle": {
                    "url": "https://example.com/py312.tar",
                    "archive": str(self.root / "py312.tar"),
                    "extract_to": str(self.root),
                },
                "links": [
                    {
                        "path": str(self.root / "working" / "ComfyUI"),
                        "target": str(self.comfy),
                    }
                ],
            },
            "frp": {"enabled": False},
            "filebrowser": {"enabled": False},
            "models": [],
        }
        data.update(extra)
        return load_config(data)

    def test_install_runtime_dry_run_issues_expected_commands(self):
        cfg = self._config()
        runner = Runner(dry_run=True)
        ComfyUIInstaller(cfg, runner).install_runtime()

        history = "\n".join(runner.history)
        self.assertIn("apt-get update", history)
        self.assertIn("apt-get install -y aria2", history)
        self.assertIn("download https://example.com/ComfyUI.tar", history)
        self.assertIn("download https://example.com/py312.tar", history)
        self.assertIn("tar -xf", history)
        self.assertIn("symlink", history)

    def test_install_runtime_skips_existing_dirs(self):
        self.comfy.mkdir()
        self.python_dir.mkdir()
        cfg = self._config()
        runner = Runner(dry_run=True)
        ComfyUIInstaller(cfg, runner).install_runtime()

        history = "\n".join(runner.history)
        self.assertNotIn("download https://example.com/ComfyUI.tar", history)
        self.assertNotIn("download https://example.com/py312.tar", history)

    def test_install_custom_nodes_clones_missing_node(self):
        (self.comfy / "custom_nodes").mkdir(parents=True)
        cfg = self._config(
            custom_nodes=[
                {
                    "name": "ComfyUI-Foo",
                    "repo": "https://example.com/Foo.git",
                    "enabled": True,
                }
            ]
        )
        runner = Runner(dry_run=True)
        ComfyUIInstaller(cfg, runner).install_custom_nodes()

        history = "\n".join(runner.history)
        self.assertIn("git clone https://example.com/Foo.git", history)
        self.assertIn("ComfyUI-Foo", history)

    def test_disabled_node_is_ignored(self):
        (self.comfy / "custom_nodes").mkdir(parents=True)
        cfg = self._config(
            custom_nodes=[
                {
                    "name": "ComfyUI-Foo",
                    "repo": "https://example.com/Foo.git",
                    "enabled": False,
                }
            ]
        )
        runner = Runner(dry_run=True)
        ComfyUIInstaller(cfg, runner).install_custom_nodes()
        self.assertEqual(runner.history, [])

    def test_update_respects_flags(self):
        cfg = self._config()
        runner = Runner(dry_run=True)
        ComfyUIInstaller(cfg, runner).update()
        self.assertEqual(runner.history, [])

    def test_update_runs_git_and_pip_when_enabled(self):
        self.comfy.mkdir()
        (self.comfy / ".git").mkdir()
        (self.comfy / "requirements.txt").write_text("", encoding="utf-8")
        cfg = self._config(updates={"comfyui": True, "comfyui_requirements": True})
        runner = Runner(dry_run=True)
        ComfyUIInstaller(cfg, runner).update()

        history = "\n".join(runner.history)
        self.assertIn("git pull", history)
        self.assertIn("-m pip install -r", history)

    def test_run_comfyui_builds_expected_args(self):
        self.comfy.mkdir()
        cfg = self._config()
        runner = Runner(dry_run=True)
        ComfyUIInstaller(cfg, runner).run_comfyui()

        command = runner.history[-1]
        self.assertIn("main.py", command)
        self.assertIn("--port=8188", command)
        self.assertIn("--disable-pinned-memory", command)
        self.assertIn("--force-fp16", command)


if __name__ == "__main__":
    unittest.main()
