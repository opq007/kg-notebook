import tempfile
import unittest
from pathlib import Path

from kgcomfy.config import ConfigError, FrpConfig
from kgcomfy.frp import FrpService, resolve_binary_source
from kgcomfy.secrets import SecretResolver
from kgcomfy.shell import Runner


def make_service(tmp: Path, binary: str, dry_run: bool = False) -> FrpService:
    config = FrpConfig(
        enabled=True,
        binary=binary,
        binary_dest=str(tmp / "bin" / "frpc"),
        config_path=str(tmp / "frpc.toml"),
        server_addr="example.com",
    )
    return FrpService(
        config, Runner(dry_run=dry_run), SecretResolver(), comfy_port=8188,
        filebrowser_port=8012,
    )


class ResolveBinarySourceTests(unittest.TestCase):
    def test_file_is_returned_as_is(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "frpc"
            source.write_text("ELF", encoding="utf-8")
            self.assertEqual(
                resolve_binary_source(source, Path(tmp) / "dest"), source
            )

    def test_directory_resolves_to_flat_binary(self):
        with tempfile.TemporaryDirectory() as tmp:
            dataset = Path(tmp) / "opq-utils"
            dataset.mkdir()
            binary = dataset / "frpc"
            binary.write_text("ELF", encoding="utf-8")
            (dataset / "README.md").write_text("docs", encoding="utf-8")
            self.assertEqual(
                resolve_binary_source(dataset, Path(tmp) / "dest" / "frpc"),
                binary,
            )

    def test_directory_resolves_to_nested_binary(self):
        with tempfile.TemporaryDirectory() as tmp:
            dataset = Path(tmp) / "opq-utils"
            nested = dataset / "frp_0.62.1_linux_amd64"
            nested.mkdir(parents=True)
            binary = nested / "frpc"
            binary.write_text("ELF", encoding="utf-8")
            self.assertEqual(
                resolve_binary_source(dataset, Path(tmp) / "dest" / "frpc"),
                binary,
            )

    def test_prefers_shallowest_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            dataset = Path(tmp) / "dataset"
            nested = dataset / "old"
            nested.mkdir(parents=True)
            (nested / "frpc").write_text("old", encoding="utf-8")
            shallow = dataset / "frpc"
            shallow.write_text("new", encoding="utf-8")
            self.assertEqual(
                resolve_binary_source(dataset, Path(tmp) / "dest" / "frpc"),
                shallow,
            )

    def test_directory_without_binary_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            dataset = Path(tmp) / "dataset"
            dataset.mkdir()
            (dataset / "notes.txt").write_text("no binary", encoding="utf-8")
            with self.assertRaises(ConfigError):
                resolve_binary_source(dataset, Path(tmp) / "dest" / "frpc")


class FrpInstallTests(unittest.TestCase):
    def test_install_from_directory_copies_file_not_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dataset = root / "opq-utils"
            dataset.mkdir()
            (dataset / "frpc").write_text("#!/bin/sh\n", encoding="utf-8")
            (dataset / "frpc.toml.example").write_text("x", encoding="utf-8")

            service = make_service(root, str(dataset))
            service.install()

            dest = Path(service.config.binary_dest)
            self.assertTrue(dest.is_file())
            self.assertEqual(dest.read_text(encoding="utf-8"), "#!/bin/sh\n")

    def test_install_from_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "frpc"
            source.write_text("ELF", encoding="utf-8")

            service = make_service(root, str(source))
            service.install()

            dest = Path(service.config.binary_dest)
            self.assertTrue(dest.is_file())
            self.assertEqual(dest.read_text(encoding="utf-8"), "ELF")

    def test_install_replaces_stale_directory_at_dest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "frpc"
            source.write_text("ELF", encoding="utf-8")
            dest = root / "bin" / "frpc"
            dest.mkdir(parents=True)
            (dest / "stale").write_text("leftover", encoding="utf-8")

            service = make_service(root, str(source))
            service.install()

            self.assertTrue(dest.is_file())

    def test_install_dry_run_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "frpc"
            source.write_text("ELF", encoding="utf-8")

            service = make_service(root, str(source), dry_run=True)
            service.install()

            self.assertFalse(Path(service.config.binary_dest).exists())

    def test_empty_binary_with_existing_dest_is_reused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dest = root / "bin" / "frpc"
            dest.parent.mkdir(parents=True)
            dest.write_text("ELF", encoding="utf-8")

            service = make_service(root, "")
            service.install()

            self.assertTrue(dest.is_file())

    def test_empty_binary_without_dest_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = make_service(Path(tmp), "")
            with self.assertRaises(ConfigError):
                service.install()


if __name__ == "__main__":
    unittest.main()
