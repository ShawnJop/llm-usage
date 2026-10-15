import base64
from contextlib import closing
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from usage_provider import (
    CursorSessionError,
    JsonFileUsageProvider,
    display_percent,
    parse_cursor_usage,
    read_local_cursor_access_token,
    resolve_cursor_session_token,
    web_session_token,
)


class CursorUsageProviderTests(unittest.TestCase):
    def payload(self):
        return {
            "billingCycleEnd": "2026-08-01T01:00:00.000Z",
            "membershipType": "pro",
            "individualUsage": {"plan": {"autoPercentUsed": 89, "apiPercentUsed": 19}},
        }

    def test_dashboard_percent_used_fields_are_displayed_as_used(self):
        snapshot = parse_cursor_usage(self.payload())
        self.assertEqual(snapshot.auto_used_percent, 89)
        self.assertEqual(snapshot.api_used_percent, 19)
        self.assertEqual(snapshot.plan, "pro")

    def test_dashboard_usage_is_rounded_up_like_cursor(self):
        self.assertEqual(display_percent(0.4933333333333333), 1)
        self.assertEqual(display_percent(7.955555555555556), 8)

    @staticmethod
    def jwt(subject: str = "auth0|user_123", expires_at: int | None = None) -> str:
        def segment(value):
            body = json.dumps(value, separators=(",", ":")).encode()
            return base64.urlsafe_b64encode(body).rstrip(b"=").decode()

        return f"{segment({'alg': 'none'})}.{segment({'sub': subject, 'exp': expires_at or int(time.time()) + 3600})}.signature"

    def test_builds_dashboard_cookie_from_ide_access_token(self):
        token = self.jwt()
        self.assertEqual(web_session_token(token), f"user_123%3A%3A{token}")

    def test_reads_access_token_from_cursor_state_database(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.vscdb"
            token = self.jwt()
            with closing(sqlite3.connect(path)) as connection:
                connection.execute("CREATE TABLE ItemTable (key TEXT UNIQUE, value BLOB)")
                connection.execute("INSERT INTO ItemTable VALUES (?, ?)", ("cursorAuth/accessToken", token))
                connection.commit()
            self.assertEqual(read_local_cursor_access_token([path]), (token, path))

    def test_explicit_cookie_overrides_local_discovery(self):
        cookie, source = resolve_cursor_session_token("WorkosCursorSessionToken=user_1%3A%3Atoken; other=x")
        self.assertEqual(cookie, "user_1%3A%3Atoken")
        self.assertIn("手动会话", source)

    def test_rejects_expired_local_access_token(self):
        with self.assertRaises(CursorSessionError):
            web_session_token(self.jwt(expires_at=1))

    def test_json_provider_reads_simplified_export(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "usage.json"
            path.write_text(json.dumps({"auto_used_percent": 73, "api_used_percent": 15}), encoding="utf-8")
            snapshot = JsonFileUsageProvider(path).get_usage()
        self.assertEqual(snapshot.auto_used_percent, 73)
        self.assertEqual(snapshot.api_used_percent, 15)


if __name__ == "__main__":
    unittest.main()
