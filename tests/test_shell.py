import os
import tempfile
import unittest
from pathlib import Path

from kgcomfy.shell import Runner, SafetyError


def symlinks_supported() -> bool:
    try:
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src"
            src.mkdir()
            os.symlink(src, Path(tmp) / "dst", target_is_directory=True)
        return True
    except (OSError, NotImplementedError):
        return False


class RunnerDryRunTests(unittest.TestCase):
    def test_run_records_history_without_executing(self):
        runner = Runner(dry_run=True)
        result = runner.run(["echo", "hello"])
        self.assertEqual(result.returncode, 0)
        self.assertEqual(runner.history, ["echo hello"])

    def test_redaction(self):
        runner = Runner(dry_run=True)
        runner.run(["tool", "--token", "supersecret"], redact=["supersecret"])
        self.assertNotIn("supersecret", runner.history[-1])
        self.assertIn("***", runner.history[-1])

    def test_download_skips_existing_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "a.tar"
            dest.write_bytes(b"x" * 10)
            runner = Runner(dry_run=True)
            runner.download("https://example.com/a.tar", dest)
            self.assertEqual(runner.history, [])

    def test_remove_path_outside_root_is_rejected(self):
        runner = Runner(dry_run=True)
        with self.assertRaises(SafetyError):
            runner.remove_path("/etc/passwd", root="/kaggle/ComfyUI")


class RunnerFileTests(unittest.TestCase):
    def setUp(self):
        if not symlinks_supported():
            self.skipTest("symlinks require elevated privileges on this platform")
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.runner = Runner()

    def tearDown(self):
        self.tmp.cleanup()

    def test_ensure_symlink_creates_and_replaces(self):
        src1 = self.root / "src1"
        src1.write_text("1", encoding="utf-8")
        src2 = self.root / "src2"
        src2.write_text("2", encoding="utf-8")
        dest = self.root / "link"

        self.assertTrue(self.runner.ensure_symlink(src1, dest))
        self.assertTrue(dest.is_symlink())
        self.assertEqual(dest.read_text(encoding="utf-8"), "1")

        self.assertTrue(self.runner.ensure_symlink(src2, dest))
        self.assertEqual(dest.read_text(encoding="utf-8"), "2")

    def test_ensure_symlink_keeps_real_destination_without_force(self):
        src = self.root / "src"
        src.write_text("src", encoding="utf-8")
        dest = self.root / "dest"
        dest.write_text("keep", encoding="utf-8")

        self.assertFalse(self.runner.ensure_symlink(src, dest))
        self.assertEqual(dest.read_text(encoding="utf-8"), "keep")

    def test_ensure_symlink_force_replaces_real_destination(self):
        src = self.root / "src"
        src.write_text("src", encoding="utf-8")
        dest = self.root / "dest"
        dest.write_text("old", encoding="utf-8")

        self.assertTrue(self.runner.ensure_symlink(src, dest, force=True))
        self.assertTrue(dest.is_symlink())
        self.assertEqual(dest.read_text(encoding="utf-8"), "src")

    def test_remove_symlinks_keeps_real_files(self):
        real = self.root / "real.safetensors"
        real.write_text("data", encoding="utf-8")
        target = self.root / "target.safetensors"
        target.write_text("data", encoding="utf-8")
        link = self.root / "link.safetensors"
        os.symlink(target, link)

        removed = self.runner.remove_symlinks(self.root / "*.safetensors", self.root)
        self.assertEqual(removed, [link])
        self.assertTrue(real.exists())
        self.assertFalse(link.exists())


if __name__ == "__main__":
    unittest.main()
