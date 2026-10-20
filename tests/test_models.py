import os
import tempfile
import unittest
from pathlib import Path

from kgcomfy.config import load_config
from kgcomfy.models import Cleaner, ModelLinker
from kgcomfy.shell import Runner, SafetyError
from tests.test_shell import symlinks_supported


class ModelLinkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.comfy = self.root / "ComfyUI"
        self.comfy.mkdir()

    def _require_symlinks(self):
        if not symlinks_supported():
            self.skipTest("symlinks require elevated privileges on this platform")

    def tearDown(self):
        self.tmp.cleanup()

    def _config(self, models):
        return load_config(
            {"runtime": {"comfy_dir": str(self.comfy), "install_bundles": False}, "models": models}
        )

    def test_relative_dest_resolves_under_comfy_dir(self):
        self._require_symlinks()
        src = self.root / "src.safetensors"
        src.write_text("data", encoding="utf-8")
        cfg = self._config([{"src": str(src), "dest": "models/clip/src.safetensors"}])

        ModelLinker(cfg, Runner()).apply()

        dest = self.comfy / "models/clip/src.safetensors"
        self.assertTrue(dest.is_symlink())
        self.assertEqual(dest.read_text(encoding="utf-8"), "data")

    def test_missing_required_source_raises(self):
        cfg = self._config([{"src": str(self.root / "missing"), "dest": "models/x"}])
        with self.assertRaises(FileNotFoundError):
            ModelLinker(cfg, Runner()).apply()

    def test_missing_optional_source_is_skipped(self):
        cfg = self._config(
            [{"src": str(self.root / "missing"), "dest": "models/x", "required": False}]
        )
        ModelLinker(cfg, Runner()).apply()  # must not raise

    def test_copy_mode(self):
        src = self.root / "src.txt"
        src.write_text("copy-me", encoding="utf-8")
        cfg = self._config([{"src": str(src), "dest": "models/src.txt", "mode": "copy"}])

        ModelLinker(cfg, Runner()).apply()

        dest = self.comfy / "models/src.txt"
        self.assertFalse(dest.is_symlink())
        self.assertEqual(dest.read_text(encoding="utf-8"), "copy-me")

    def test_dry_run_does_not_create_links(self):
        src = self.root / "src.txt"
        src.write_text("data", encoding="utf-8")
        cfg = self._config([{"src": str(src), "dest": "models/src.txt"}])

        runner = Runner(dry_run=True)
        ModelLinker(cfg, runner).apply()

        self.assertFalse((self.comfy / "models/src.txt").exists())
        self.assertTrue(any("symlink" in item for item in runner.history))


class CleanerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.comfy = self.root / "ComfyUI"
        self.comfy.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_disabled_is_noop(self):
        target = self.comfy / "models/old"
        target.mkdir(parents=True)
        cfg = load_config(
            {
                "runtime": {"comfy_dir": str(self.comfy)},
                "cleanup": {"enabled": False, "paths": [str(target)]},
            }
        )
        Cleaner(cfg, Runner()).run()
        self.assertTrue(target.exists())

    def test_removes_path_within_root(self):
        target = self.comfy / "models/old"
        target.mkdir(parents=True)
        cfg = load_config(
            {
                "runtime": {"comfy_dir": str(self.comfy)},
                "cleanup": {"enabled": True, "paths": [str(target)]},
            }
        )
        Cleaner(cfg, Runner()).run()
        self.assertFalse(target.exists())

    def test_refuses_path_outside_root(self):
        outside = self.root / "outside"
        outside.mkdir()
        cfg = load_config(
            {
                "runtime": {"comfy_dir": str(self.comfy)},
                "cleanup": {"enabled": True, "paths": [str(outside)]},
            }
        )
        with self.assertRaises(SafetyError):
            Cleaner(cfg, Runner()).run()


if __name__ == "__main__":
    unittest.main()
