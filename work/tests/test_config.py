"""秘密の値の読み方のテスト（環境変数と .env）。値はすべて架空。"""

import tempfile
import unittest
from pathlib import Path

from nepsync.config import ConfigError, load_secrets

FILE = """# 架空の値
NEPLECTOR_BASE_URL=https://file.example/
NEPLECTOR_API_KEY="file-key"
DISCORD_BOT_TOKEN=file-token
OPS_WEBHOOK_URL=https://file.example/hook
"""
ENV = {"NEPLECTOR_BASE_URL": "https://env.example/", "NEPLECTOR_API_KEY": "env-key",
       "DISCORD_BOT_TOKEN": "env-token", "OPS_WEBHOOK_URL": "https://env.example/hook"}


class LoadSecrets(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / ".env"

    def test_file_only_is_read_as_before(self):
        self.path.write_text(FILE, encoding="utf-8")
        secrets = load_secrets(self.path, {})
        self.assertEqual(secrets.neplector_base_url, "https://file.example")
        self.assertEqual(secrets.neplector_api_key, "file-key")
        self.assertEqual(secrets.discord_bot_token, "file-token")
        self.assertEqual(secrets.ops_webhook_url, "https://file.example/hook")

    def test_environment_only_works_without_a_file(self):
        secrets = load_secrets(self.path, ENV)
        self.assertEqual(secrets.neplector_base_url, "https://env.example")
        self.assertEqual(secrets.neplector_api_key, "env-key")
        self.assertEqual(secrets.discord_bot_token, "env-token")
        self.assertEqual(secrets.ops_webhook_url, "https://env.example/hook")

    def test_environment_wins_over_the_file_per_key(self):
        self.path.write_text(FILE, encoding="utf-8")
        secrets = load_secrets(self.path, {"NEPLECTOR_BASE_URL": "https://env.example", "UNRELATED": "x"})
        self.assertEqual(secrets.neplector_base_url, "https://env.example")
        self.assertEqual(secrets.neplector_api_key, "file-key")

    def test_empty_environment_value_does_not_hide_the_file(self):
        self.path.write_text(FILE, encoding="utf-8")
        secrets = load_secrets(self.path, {"NEPLECTOR_API_KEY": "  "})
        self.assertEqual(secrets.neplector_api_key, "file-key")

    def test_missing_everywhere_names_the_key_and_no_value(self):
        with self.assertRaises(ConfigError) as caught:
            load_secrets(self.path, {key: value for key, value in ENV.items() if key != "DISCORD_BOT_TOKEN"})
        self.assertIn("DISCORD_BOT_TOKEN", str(caught.exception))
        self.assertNotIn("env-key", str(caught.exception))
