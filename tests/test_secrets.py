import os
import unittest
from unittest import mock

from kgcomfy.config import SecretConfig
from kgcomfy.secrets import SecretError, SecretResolver


class SecretResolverTests(unittest.TestCase):
    def tearDown(self):
        for name in ("FRP_TOKEN", "MY_TOKEN"):
            os.environ.pop(name, None)

    def test_explicit_override_wins(self):
        resolver = SecretResolver(overrides={"token": "override"})
        self.assertEqual(resolver.get("token"), "override")

    def test_env_fallback_uses_configured_name(self):
        os.environ["FRP_TOKEN"] = "from-env"
        resolver = SecretResolver(
            {"frp_token": SecretConfig(kaggle="frp_token", env="FRP_TOKEN")}
        )
        self.assertEqual(resolver.get("frp_token"), "from-env")

    def test_env_fallback_uses_uppercase_logical_name(self):
        os.environ["MY_TOKEN"] = "upper-env"
        resolver = SecretResolver()
        self.assertEqual(resolver.get("my_token"), "upper-env")

    def test_missing_required_raises_with_hint(self):
        resolver = SecretResolver(
            {"frp_token": SecretConfig(kaggle="frp_token", env="FRP_TOKEN")}
        )
        with self.assertRaises(SecretError) as ctx:
            resolver.get("frp_token")
        message = str(ctx.exception)
        self.assertIn("frp_token", message)
        self.assertIn("FRP_TOKEN", message)

    def test_missing_optional_returns_default(self):
        resolver = SecretResolver()
        self.assertEqual(resolver.get("nope", default="fallback", required=False), "fallback")

    def test_kaggle_client_is_used_when_available(self):
        fake_client = mock.Mock()
        fake_client.get_secret.return_value = "from-kaggle"
        fake_module = mock.Mock()
        fake_module.UserSecretsClient.return_value = fake_client

        with mock.patch.dict("sys.modules", {"kaggle_secrets": fake_module}):
            resolver = SecretResolver({"token": SecretConfig(kaggle="kg_name")})
            self.assertEqual(resolver.get("token"), "from-kaggle")
        fake_client.get_secret.assert_called_once_with("kg_name")


if __name__ == "__main__":
    unittest.main()
