import json
import tempfile
import unittest
from pathlib import Path

from kgcomfy.config import ConfigError, load_config, resolve_config_path


class ConfigTests(unittest.TestCase):
    def test_packaged_tts_config_loads(self):
        cfg = load_config("tts-img.yaml")
        self.assertEqual(cfg.runtime.comfy_dir, "/kaggle/ComfyUI")
        self.assertEqual(cfg.runtime.python_bin, "/py312/bin/python3")
        self.assertEqual(len(cfg.models), 6)
        self.assertEqual(cfg.models[0].dest, "models/tts/VoxCPM/VoxCPM2")
        self.assertTrue(cfg.frp.enabled)
        self.assertEqual(cfg.frp.server_addr, "oracle2.120168.xyz")
        self.assertEqual(cfg.filebrowser.port, 8012)
        self.assertEqual(cfg.comfyui.port, 8188)

    def test_resolve_by_bare_name(self):
        path = resolve_config_path("tts-img")
        self.assertTrue(path.exists())
        self.assertEqual(path.name, "tts-img.yaml")

    def test_deep_merge_override(self):
        cfg = load_config(
            "tts-img.yaml",
            {"frp": {"subdomain": "abc"}, "updates": {"comfyui": True}},
        )
        self.assertEqual(cfg.frp.subdomain, "abc")
        self.assertEqual(cfg.frp.server_addr, "oracle2.120168.xyz")
        self.assertTrue(cfg.updates.comfyui)

    def test_python_bin_derived_from_python_dir(self):
        cfg = load_config({"runtime": {"python_dir": "/opt/py"}})
        self.assertEqual(cfg.runtime.python_bin, "/opt/py/bin/python3")

    def test_models_mapping_shorthand(self):
        cfg = load_config(
            {
                "models": {
                    "/input/a.safetensors": "models/clip/a.safetensors",
                    "/input/b.safetensors": {
                        "dest": "models/vae/b.safetensors",
                        "required": False,
                    },
                }
            }
        )
        self.assertEqual(len(cfg.models), 2)
        self.assertEqual(cfg.models[0].src, "/input/a.safetensors")
        self.assertEqual(cfg.models[1].dest, "models/vae/b.safetensors")
        self.assertFalse(cfg.models[1].required)

    def test_secrets_shorthand(self):
        cfg = load_config({"secrets": {"my_token": "kaggle_secret_name"}})
        self.assertEqual(cfg.secrets["my_token"].kaggle, "kaggle_secret_name")

    def test_custom_nodes_shorthand(self):
        cfg = load_config({"custom_nodes": ["ComfyUI-Foo"]})
        self.assertEqual(cfg.custom_nodes[0].name, "ComfyUI-Foo")

    def test_invalid_model_mode(self):
        cfg = load_config({"models": [{"src": "/a", "dest": "b", "mode": "move"}]})
        with self.assertRaises(ConfigError):
            cfg.validate()

    def test_frp_requires_server_addr(self):
        cfg = load_config({"frp": {"enabled": True, "server_addr": ""}})
        with self.assertRaises(ConfigError) as ctx:
            cfg.validate()
        self.assertIn("frp.server_addr", str(ctx.exception))

    def test_json_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cfg.json"
            path.write_text(json.dumps({"updates": {"comfyui": True}}), encoding="utf-8")
            cfg = load_config(path)
        self.assertTrue(cfg.updates.comfyui)

    def test_missing_config_raises(self):
        with self.assertRaises(FileNotFoundError):
            load_config("definitely-not-here.yaml")


if __name__ == "__main__":
    unittest.main()
