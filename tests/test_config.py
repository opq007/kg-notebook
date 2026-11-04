import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kgcomfy.config import (
    ConfigError,
    WebhookConfig,
    load_config,
    resolve_config_path,
)


def minimal_config(**extra):
    data = {
        "frp": {"enabled": False},
        "filebrowser": {"enabled": False},
    }
    data.update(extra)
    return data


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
        self.assertTrue(cfg.watchdog.enabled)
        self.assertEqual(cfg.watchdog.health_path, "/system_stats")
        self.assertEqual(
            cfg.watchdog.state_path, "/kaggle/working/comfyui-watchdog.json"
        )

    def test_watchdog_overrides(self):
        cfg = load_config(
            "tts-img.yaml",
            {"watchdog": {"interval": 5, "max_restarts": 2}},
        )
        self.assertEqual(cfg.watchdog.interval, 5)
        self.assertEqual(cfg.watchdog.max_restarts, 2)
        self.assertEqual(cfg.watchdog.health_path, "/system_stats")

    def test_watchdog_health_path_must_be_absolute(self):
        cfg = load_config(
            minimal_config(watchdog={"health_path": "system_stats"})
        )
        with self.assertRaises(ConfigError) as ctx:
            cfg.validate()
        self.assertIn("watchdog.health_path", str(ctx.exception))

    def test_watchdog_backoff_must_be_ordered(self):
        cfg = load_config(
            minimal_config(
                watchdog={"backoff_initial": 10, "backoff_max": 1}
            )
        )
        with self.assertRaises(ConfigError) as ctx:
            cfg.validate()
        self.assertIn("watchdog.backoff_max", str(ctx.exception))

    def test_disabled_watchdog_still_validates_settings(self):
        cfg = load_config(
            minimal_config(
                watchdog={
                    "enabled": False,
                    "health_path": "",
                    "interval": 0,
                }
            )
        )
        with self.assertRaises(ConfigError) as ctx:
            cfg.validate()
        self.assertIn("watchdog.health_path", str(ctx.exception))
        self.assertIn("watchdog.interval", str(ctx.exception))

    def test_comfyui_literal_port_must_match_config(self):
        cfg = load_config(
            minimal_config(
                comfyui={"port": 8188, "args": ["--port=9999"]}
            )
        )
        with self.assertRaises(ConfigError) as ctx:
            cfg.validate()
        self.assertIn("comfyui.args uses --port=9999", str(ctx.exception))

    def test_comfyui_literal_port_matching_config_is_allowed(self):
        cfg = load_config(
            minimal_config(
                comfyui={"port": 9999, "args": ["--port=9999"]}
            )
        )
        cfg.validate()  # must not raise

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

    def test_bundle_source_override_keeps_url(self):
        cfg = load_config(
            "tts-img.yaml",
            {
                "runtime": {
                    "comfyui_bundle": {
                        "source": "/kaggle/input/ds/ComfyUI.tar.bak",
                    }
                }
            },
        )
        self.assertEqual(
            cfg.runtime.comfyui_bundle.source, "/kaggle/input/ds/ComfyUI.tar.bak"
        )
        self.assertTrue(cfg.runtime.comfyui_bundle.url.startswith("https://"))
        self.assertFalse(cfg.runtime.comfyui_bundle.overwrite)

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


class WebhookConfigTests(unittest.TestCase):
    def test_defaults(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            cfg = load_config(minimal_config())

        self.assertFalse(cfg.webhook.enabled)
        self.assertEqual(cfg.webhook.provider, "auto")
        self.assertEqual(cfg.webhook.timeout, 5.0)
        self.assertEqual(
            cfg.webhook.events,
            [
                "started",
                "crashed",
                "start_failed",
                "restarted",
                "failed",
            ],
        )

    def test_env_overrides_take_precedence(self):
        env = {
            "KGCOMFY_WEBHOOK_ENABLED": "true",
            "KGCOMFY_WEBHOOK_URL": "https://example.com/hook",
            "KGCOMFY_WEBHOOK_PROVIDER": "feishu",
            "KGCOMFY_WEBHOOK_SECRET": "s3cret",
            "KGCOMFY_WEBHOOK_TITLE": "Notebook",
            "KGCOMFY_WEBHOOK_TIMEOUT": "2.5",
            "KGCOMFY_WEBHOOK_EVENTS": "started, failed",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            cfg = load_config(minimal_config())

        self.assertTrue(cfg.webhook.enabled)
        self.assertEqual(cfg.webhook.url, "https://example.com/hook")
        self.assertEqual(cfg.webhook.provider, "feishu")
        self.assertEqual(cfg.webhook.secret, "s3cret")
        self.assertEqual(cfg.webhook.title, "Notebook")
        self.assertEqual(cfg.webhook.timeout, 2.5)
        self.assertEqual(cfg.webhook.events, ["started", "failed"])

    def test_platform_url_alias_detects_provider(self):
        with mock.patch.dict(
            os.environ,
            {"DINGTALK_WEBHOOK_URL": "https://oapi.dingtalk.com/robot/send"},
            clear=True,
        ):
            cfg = load_config(minimal_config())

        self.assertEqual(
            cfg.webhook.url, "https://oapi.dingtalk.com/robot/send"
        )
        self.assertEqual(cfg.webhook.provider, "dingtalk")

    def test_events_all_keyword(self):
        with mock.patch.dict(
            os.environ, {"KGCOMFY_WEBHOOK_EVENTS": "all"}, clear=True
        ):
            cfg = load_config(minimal_config())

        self.assertIn("ready", cfg.webhook.events)
        self.assertIn("stopped", cfg.webhook.events)

    def test_invalid_provider_is_rejected_when_enabled(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            cfg = load_config(
                minimal_config(
                    webhook={"enabled": True, "provider": "email"}
                )
            )
        with self.assertRaises(ConfigError) as ctx:
            cfg.validate()
        self.assertIn("webhook.provider", str(ctx.exception))

    def test_unknown_event_is_rejected_when_enabled(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            cfg = load_config(
                minimal_config(
                    webhook={"enabled": True, "events": ["started", "boom"]}
                )
            )
        with self.assertRaises(ConfigError) as ctx:
            cfg.validate()
        self.assertIn("unknown events", str(ctx.exception))

    def test_disabled_webhook_settings_are_not_validated(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            cfg = load_config(
                minimal_config(
                    webhook={
                        "enabled": False,
                        "provider": "email",
                        "events": ["boom"],
                        "timeout": 0,
                    }
                )
            )
        cfg.validate()  # disabled notifications must not block startup

    def test_enabled_without_url_is_allowed(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            cfg = load_config(minimal_config(webhook={"enabled": True}))
        cfg.validate()  # runtime warns and disables notifications instead

    def test_url_secret_satisfies_validation(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            cfg = load_config(
                minimal_config(
                    webhook={"enabled": True, "url_secret": "hook_url"}
                )
            )
        cfg.validate()  # must not raise

    def test_platform_secret_must_match_alias_url(self):
        env = {
            "FEISHU_WEBHOOK_URL": "https://open.feishu.cn/open-apis/bot/v2/hook/x",
            "DINGTALK_WEBHOOK_SECRET": "wrong-platform",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            cfg = load_config(minimal_config())

        self.assertEqual(cfg.webhook.provider, "feishu")
        self.assertEqual(cfg.webhook.secret, "")

    def test_alias_url_sets_platform_provider(self):
        env = {"DINGTALK_WEBHOOK_URL": "https://oapi.dingtalk.com/robot/send"}
        with mock.patch.dict(os.environ, env, clear=True):
            cfg = load_config(minimal_config(webhook={"provider": "feishu"}))

        self.assertEqual(cfg.webhook.provider, "dingtalk")

    def test_additional_env_overrides(self):
        env = {
            "KGCOMFY_WEBHOOK_URL_SECRET": "hook_url",
            "KGCOMFY_WEBHOOK_SIGN_SECRET": "hook_secret",
            "KGCOMFY_WEBHOOK_LOG_TAIL": "true",
            "KGCOMFY_WEBHOOK_LOG_TAIL_LINES": "25",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            cfg = load_config(minimal_config())

        self.assertEqual(cfg.webhook.url_secret, "hook_url")
        self.assertEqual(cfg.webhook.sign_secret, "hook_secret")
        self.assertTrue(cfg.webhook.include_log_tail)
        self.assertEqual(cfg.webhook.log_tail_lines, 25)

    def test_null_events_means_all(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            cfg = load_config(minimal_config(webhook={"events": None}))
        self.assertEqual(cfg.webhook.events, [])
        cfg.validate()  # empty events is valid and means "all"

    def test_string_events_are_split(self):
        cfg = WebhookConfig(events="started, failed")
        self.assertEqual(cfg.events, ["started", "failed"])

    def test_negative_log_tail_lines_is_rejected_when_enabled(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            cfg = load_config(
                minimal_config(
                    webhook={"enabled": True, "log_tail_lines": -1}
                )
            )
        with self.assertRaises(ConfigError) as ctx:
            cfg.validate()
        self.assertIn("webhook.log_tail_lines", str(ctx.exception))

    def test_env_overrides_can_be_reapplied(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            cfg = load_config(minimal_config())

        cfg.apply_env_overrides(
            {"KGCOMFY_WEBHOOK_URL": "https://first.example/hook"}
        )
        cfg.apply_env_overrides(
            {"KGCOMFY_WEBHOOK_URL": "https://second.example/hook"}
        )

        self.assertEqual(
            cfg.webhook.url, "https://second.example/hook"
        )


if __name__ == "__main__":
    unittest.main()
