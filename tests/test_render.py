import unittest

import yaml

from kgcomfy.config import FileBrowserConfig, FrpConfig
from kgcomfy.filebrowser import render_filebrowser_config
from kgcomfy.frp import render_frp_config


class FrpRenderTests(unittest.TestCase):
    def test_default_proxies_and_escaping(self):
        cfg = FrpConfig(
            server_addr="example.com",
            server_port=7000,
            subdomain="tts",
        )
        proxies = cfg.effective_proxies(8188, 8012)
        text = render_frp_config(cfg, 'tok"en\\x', proxies)

        self.assertIn('serverAddr = "example.com"', text)
        self.assertIn("serverPort = 7000", text)
        self.assertIn('auth.token = "tok\\"en\\\\x"', text)
        self.assertEqual(text.count("[[proxies]]"), 2)
        self.assertIn('name = "tts-comfyui-frp"', text)
        self.assertIn('name = "tts-fslist-frp"', text)
        self.assertIn("localPort = 8188", text)
        self.assertIn("localPort = 8012", text)
        self.assertIn('locations = ["/fslist"]', text)

    def test_explicit_proxies_override_defaults(self):
        cfg = FrpConfig(server_addr="example.com", proxies=[])
        self.assertEqual(len(cfg.effective_proxies(1, 2)), 2)

    def test_filebrowser_proxy_is_omitted_when_disabled(self):
        cfg = FrpConfig(server_addr="example.com", subdomain="tts")
        proxies = cfg.effective_proxies(8188, None)
        self.assertEqual(len(proxies), 1)
        self.assertEqual(proxies[0].name, "tts-comfyui-frp")


class FileBrowserRenderTests(unittest.TestCase):
    def test_round_trip(self):
        cfg = FileBrowserConfig(base_url="/fs", port=9000)
        text = render_filebrowser_config(cfg, "secret")
        data = yaml.safe_load(text)

        self.assertEqual(data["server"]["baseURL"], "/fs")
        self.assertEqual(data["server"]["port"], 9000)
        self.assertEqual(data["auth"]["adminUsername"], "admin")
        self.assertEqual(data["auth"]["adminPassword"], "secret")
        self.assertEqual(data["server"]["sources"][0]["path"], "/kaggle")


if __name__ == "__main__":
    unittest.main()
