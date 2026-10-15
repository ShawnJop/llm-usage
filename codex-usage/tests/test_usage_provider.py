import json
import tempfile
import unittest
from pathlib import Path

from usage_provider import CodexLocalUsageProvider, JsonFileUsageProvider, ManualUsageProvider, extract_remaining_percent


class UsageProviderTests(unittest.TestCase):
    def test_remaining_field(self):
        self.assertEqual(extract_remaining_percent({"weekly_remaining_percent": 42}), 42)

    def test_used_field_is_inverted(self):
        self.assertEqual(extract_remaining_percent({"weekly_used_percent": 42}), 58)

    def test_json_provider(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "usage.json"
            path.write_text(json.dumps({"weekly_remaining_percent": 91}), encoding="utf-8")
            self.assertEqual(JsonFileUsageProvider(path).get_usage().display_percent, 91)

    def test_manual_provider(self):
        self.assertEqual(ManualUsageProvider(17).get_usage().display_percent, 17)

    def test_local_codex_provider_uses_weekly_window(self):
        payload = {
            "rate_limit": {
                "primary_window": {"used_percent": 42, "limit_window_seconds": 18_000, "reset_at": 1_800_000_000},
                "secondary_window": {"used_percent": 14, "limit_window_seconds": 604_800, "reset_at": 1_800_604_800},
            }
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "auth.json"
            path.write_text(json.dumps({"tokens": {"access_token": "test", "account_id": "account"}}), encoding="utf-8")
            snapshot = CodexLocalUsageProvider(path, fetch_usage=lambda _token, _account: payload).get_usage()
        self.assertEqual(snapshot.display_percent, 86)
        self.assertEqual(snapshot.source, "本地 Codex 账户")

    def test_local_codex_provider_includes_five_hour_window(self):
        payload = {
            "rate_limit": {
                "primary_window": {"used_percent": 42, "limit_window_seconds": 18_000, "reset_at": 1_800_000_000},
                "secondary_window": {"used_percent": 14, "limit_window_seconds": 604_800, "reset_at": 1_800_604_800},
            }
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "auth.json"
            path.write_text(json.dumps({"tokens": {"access_token": "test", "account_id": "account"}}), encoding="utf-8")
            snapshot = CodexLocalUsageProvider(path, fetch_usage=lambda _token, _account: payload).get_usage()

        self.assertEqual(snapshot.five_hour_remaining_percent, 58)
        self.assertIsNotNone(snapshot.five_hour_reset_at)


if __name__ == "__main__":
    unittest.main()
