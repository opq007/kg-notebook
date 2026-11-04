import io
import tarfile
import tempfile
import unittest
from pathlib import Path

from kgcomfy.comfyui import ComfyUIInstaller
from kgcomfy.config import ConfigError, load_config
from kgcomfy.shell import Runner


class ComfyUIInstallerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.comfy = self.root / "ComfyUI"
        self.python_dir = self.root / "py312"

    def tearDown(self):
        self.tmp.cleanup()

    def _config(self, comfyui_bundle=None, python_bundle=None, **extra):
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
        if comfyui_bundle is not None:
            data["runtime"]["comfyui_bundle"] = comfyui_bundle
        if python_bundle is not None:
            data["runtime"]["python_bundle"] = python_bundle
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

    def test_local_source_is_copied_then_extracted(self):
        source = self.root / "input" / "ComfyUI.tar.bak"
        archive = self.root / "ComfyUI.tar"
        cfg = self._config(
            comfyui_bundle={
                "source": str(source),
                "archive": str(archive),
                "extract_to": str(self.root),
            }
        )
        runner = Runner(dry_run=True)
        ComfyUIInstaller(cfg, runner)._install_bundle(
            "ComfyUI", cfg.runtime.comfyui_bundle, self.comfy
        )

        history = "\n".join(runner.history)
        self.assertIn(f"copy {source}", history)
        self.assertIn("tar -xf", history)
        self.assertIn(str(archive), history)
        self.assertNotIn("download", history)

    def test_local_source_wins_over_url(self):
        source = self.root / "input" / "ComfyUI.tar.bak"
        cfg = self._config(
            comfyui_bundle={
                "url": "https://example.com/ComfyUI.tar",
                "source": str(source),
                "archive": str(self.root / "ComfyUI.tar"),
                "extract_to": str(self.root),
            }
        )
        runner = Runner(dry_run=True)
        ComfyUIInstaller(cfg, runner)._install_bundle(
            "ComfyUI", cfg.runtime.comfyui_bundle, self.comfy
        )

        self.assertNotIn("download", "\n".join(runner.history))

    def test_bundle_without_any_source_raises(self):
        cfg = self._config(
            comfyui_bundle={
                "archive": str(self.root / "ComfyUI.tar"),
                "extract_to": str(self.root),
            }
        )
        with self.assertRaises(ConfigError):
            ComfyUIInstaller(cfg, Runner(dry_run=True))._install_bundle(
                "ComfyUI", cfg.runtime.comfyui_bundle, self.comfy
            )

    def test_missing_local_source_raises(self):
        cfg = self._config(
            comfyui_bundle={
                "source": str(self.root / "missing.tar"),
                "archive": str(self.root / "ComfyUI.tar"),
                "extract_to": str(self.root),
            }
        )
        with self.assertRaises(FileNotFoundError):
            ComfyUIInstaller(cfg, Runner())._install_bundle(
                "ComfyUI", cfg.runtime.comfyui_bundle, self.comfy
            )

    def test_existing_archive_is_not_copied_again(self):
        source = self.root / "input.tar"
        source.write_text("new", encoding="utf-8")
        archive = self.root / "ComfyUI.tar"
        archive.write_text("already staged", encoding="utf-8")
        cfg = self._config(
            comfyui_bundle={
                "source": str(source),
                "archive": str(archive),
                "extract_to": str(self.root),
            }
        )
        runner = Runner()
        ComfyUIInstaller(cfg, runner)._stage_archive(
            "ComfyUI", cfg.runtime.comfyui_bundle
        )

        self.assertEqual(runner.history, [])
        self.assertEqual(archive.read_text(encoding="utf-8"), "already staged")

    def test_overwrite_stages_archive_again(self):
        source = self.root / "input.tar"
        source.write_text("new", encoding="utf-8")
        archive = self.root / "ComfyUI.tar"
        archive.write_text("old", encoding="utf-8")
        cfg = self._config(
            comfyui_bundle={
                "source": str(source),
                "archive": str(archive),
                "extract_to": str(self.root),
                "overwrite": True,
            }
        )
        ComfyUIInstaller(cfg, Runner())._stage_archive(
            "ComfyUI", cfg.runtime.comfyui_bundle
        )

        self.assertEqual(archive.read_text(encoding="utf-8"), "new")

    def test_local_archive_extracts_into_expected_dir(self):
        source = self.root / "input" / "ComfyUI.tar.bak"
        source.parent.mkdir()
        payload = b"print('hi')\n"
        with tarfile.open(source, "w") as tar:
            info = tarfile.TarInfo("ComfyUI/main.py")
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))

        archive = self.root / "ComfyUI.tar"
        cfg = self._config(
            comfyui_bundle={
                "source": str(source),
                "archive": str(archive),
                "extract_to": str(self.root),
            }
        )
        ComfyUIInstaller(cfg, Runner())._install_bundle(
            "ComfyUI", cfg.runtime.comfyui_bundle, self.comfy
        )

        self.assertTrue(archive.is_file())
        self.assertTrue((self.comfy / "main.py").is_file())

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

    def test_build_command_is_reusable(self):
        cfg = self._config(
            comfyui={
                "port": 9999,
                "args": ["--port={port}", "--cpu"],
            }
        )
        installer = ComfyUIInstaller(cfg, Runner(dry_run=True))

        command = installer.build_command()
        self.assertEqual(command[0], cfg.runtime.python_bin)
        self.assertEqual(command[1], "main.py")
        self.assertEqual(command[2], "--port=9999")
        self.assertEqual(command[-1], "--cpu")

    def test_build_env_returns_overrides(self):
        cfg = self._config(
            comfyui={"env": {"CUDA_VISIBLE_DEVICES": "1", "NUM": 2}}
        )
        env = ComfyUIInstaller(cfg, Runner(dry_run=True)).build_env()

        self.assertEqual(env, {"CUDA_VISIBLE_DEVICES": "1", "NUM": "2"})


if __name__ == "__main__":
    unittest.main()
