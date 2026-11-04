import unittest
from unittest import mock

from kgcomfy import cli


class CliTests(unittest.TestCase):
    def test_run_propagates_watchdog_failure_code(self):
        config = mock.Mock()
        config.dry_run = False

        with mock.patch(
            "kgcomfy.cli.load_config", return_value=config
        ), mock.patch("kgcomfy.cli.KaggleComfy") as app_cls:
            app_cls.return_value.run.return_value = 1
            self.assertEqual(cli.main(["run", "--config", "test.yaml"]), 1)


if __name__ == "__main__":
    unittest.main()
