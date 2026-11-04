import json
import unittest
from unittest import mock

from kgcomfy import cli
from kgcomfy.config import load_config


class CliTests(unittest.TestCase):
    def test_run_propagates_watchdog_failure_code(self):
        config = mock.Mock()
        config.dry_run = False

        with mock.patch(
            "kgcomfy.cli.load_config", return_value=config
        ), mock.patch("kgcomfy.cli.KaggleComfy") as app_cls:
            app_cls.return_value.run.return_value = 1
            self.assertEqual(cli.main(["run", "--config", "test.yaml"]), 1)

    def test_test_webhook_command_exit_codes(self):
        config = mock.Mock()
        config.dry_run = False

        for sent, expected in ((True, 0), (False, 1)):
            with self.subTest(sent=sent), mock.patch(
                "kgcomfy.cli.load_config", return_value=config
            ), mock.patch("kgcomfy.cli.KaggleComfy") as app_cls:
                app_cls.return_value.test_webhook.return_value = sent
                self.assertEqual(
                    cli.main(["test-webhook", "--config", "test.yaml"]),
                    expected,
                )

    def test_show_config_redacts_webhook_credentials(self):
        config = load_config(
            {
                "frp": {"enabled": False},
                "filebrowser": {"enabled": False},
                "webhook": {
                    "enabled": True,
                    "url": "https://example.com/hook?token=secret",
                    "secret": "s3cret",
                },
            }
        )

        with mock.patch(
            "kgcomfy.cli.load_config", return_value=config
        ), mock.patch("builtins.print") as fake_print:
            self.assertEqual(cli.main(["show-config"]), 0)

        data = json.loads(fake_print.call_args[0][0])
        self.assertEqual(data["webhook"]["url"], "***")
        self.assertEqual(data["webhook"]["secret"], "***")


if __name__ == "__main__":
    unittest.main()
