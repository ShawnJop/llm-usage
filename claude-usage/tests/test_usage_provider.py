import base64
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import requests

from usage_provider import (
    ClaudeDesktopHistoryUsageProvider,
    ClaudeDesktopUsageProvider,
    FallbackUsageProvider,
    ClaudeLocalUsageProvider,
    ClaudeLoginError,
    JsonFileUsageProvider,
    ManualUsageProvider,
    create_provider,
    decrypt_cookie_value,
    fetch_claude_usage,
    fetch_desktop_usage,
    parse_claude_usage,
    read_desktop_session_key,
    read_desktop_oauth_token,
    resolve_claude_credentials_path,
    resolve_claude_desktop_dir,
    find_cookie_databases,
    parse_plan_usage_history,
    select_desktop_oauth_token,
)


def no_oauth_token(_desktop_dir, _now):
    raise ClaudeLoginError("Claude 桌面版尚未登录或登录态无法读取，请在桌面应用重新登录后刷新")


def http_error(status_code):
    response = requests.Response()
    response.status_code = status_code
    return requests.exceptions.HTTPError(response=response)


class ClaudeUsageProviderTests(unittest.TestCase):
    def payload(self):
        return {
            "five_hour": {"utilization": 14.4, "resets_at": "2026-09-08T10:00:00Z"},
            "seven_day": {"utilization": 36.2, "resets_at": "2026-09-13T01:00:00Z"},
        }

    def test_parses_session_and_weekly_windows(self):
        snapshot = parse_claude_usage(self.payload())
        self.assertEqual(snapshot.five_hour_used_percent, 14.4)
        self.assertEqual(snapshot.weekly_used_percent, 36.2)
        self.assertIsNotNone(snapshot.five_hour_reset_at)
        self.assertIsNotNone(snapshot.weekly_reset_at)

    def test_local_provider_reads_claude_code_oauth_without_exposing_token(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".credentials.json"
            path.write_text(json.dumps({"claudeAiOauth": {"accessToken": "secret", "subscriptionType": "pro"}}), encoding="utf-8")
            snapshot = ClaudeLocalUsageProvider(path, fetch_usage=lambda token: self.payload()).get_usage()
        self.assertEqual(snapshot.source, "本机 Claude Code 账户")
        self.assertEqual(snapshot.plan, "pro")
        self.assertNotIn("secret", snapshot.message)

    def test_credentials_path_uses_claude_config_dir_before_default_home(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": directory}):
                path = resolve_claude_credentials_path()
        self.assertEqual(path, Path(directory) / ".credentials.json")

    def test_explicit_credentials_path_overrides_claude_config_dir(self):
        explicit = Path("custom") / "credentials.json"
        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": "ignored"}):
            path = resolve_claude_credentials_path(explicit)
        self.assertEqual(path, explicit)

    def test_fetch_usage_sends_oauth_headers_with_app_user_agent(self):
        captured = {}

        class Response:
            def raise_for_status(self):
                return None

            def json(self):
                return self_payload

        self_payload = self.payload()

        def fake_get(url, *, headers, timeout):
            captured.update(url=url, headers=headers, timeout=timeout)
            return Response()

        payload = fetch_claude_usage("secret", http_get=fake_get)

        self.assertEqual(payload, self_payload)
        self.assertEqual(captured["url"], "https://api.anthropic.com/api/oauth/usage")
        self.assertEqual(captured["timeout"], 10)
        self.assertEqual(captured["headers"]["Authorization"], "Bearer secret")
        self.assertEqual(captured["headers"]["User-Agent"], "claude-usage/1.0")
        self.assertEqual(captured["headers"]["anthropic-beta"], "oauth-2025-04-20")

    def test_parses_limits_when_legacy_quota_fields_are_absent(self):
        payload = {
            "limits": [
                {
                    "kind": "session",
                    "percent": 12.5,
                    "resets_at": "2026-09-08T10:00:00Z",
                    "scope": None,
                },
                {
                    "kind": "weekly_all",
                    "percent": 34.5,
                    "resets_at": "2026-09-13T01:00:00Z",
                    "scope": None,
                },
            ]
        }
        snapshot = parse_claude_usage(payload)
        self.assertEqual(snapshot.five_hour_used_percent, 12.5)
        self.assertEqual(snapshot.weekly_used_percent, 34.5)
        self.assertIsNotNone(snapshot.five_hour_reset_at)
        self.assertIsNotNone(snapshot.weekly_reset_at)

    def test_forbidden_response_has_actionable_message_without_token(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".credentials.json"
            path.write_text(json.dumps({"claudeAiOauth": {"accessToken": "secret"}}), encoding="utf-8")
            response = requests.Response()
            response.status_code = 403
            error = requests.exceptions.HTTPError(response=response)

            def reject(_token):
                raise error

            snapshot = ClaudeLocalUsageProvider(path, fetch_usage=reject).get_usage()

        self.assertFalse(snapshot.has_usage)
        self.assertIn("请求被拒绝", snapshot.message)
        self.assertNotIn("secret", snapshot.message)

    def test_json_provider_accepts_raw_usage_response(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "usage.json"
            path.write_text(json.dumps(self.payload()), encoding="utf-8")
            snapshot = JsonFileUsageProvider(path).get_usage()
        self.assertEqual(snapshot.five_hour_used_percent, 14.4)
        self.assertEqual(snapshot.weekly_used_percent, 36.2)

    def test_json_provider_accepts_limits_usage_response(self):
        payload = {
            "limits": [
                {"kind": "session", "percent": 12.5, "resets_at": "2026-09-08T10:00:00Z"},
                {"kind": "weekly_all", "percent": 34.5, "resets_at": "2026-09-13T01:00:00Z"},
            ]
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "usage.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            snapshot = JsonFileUsageProvider(path).get_usage()
        self.assertEqual(snapshot.five_hour_used_percent, 12.5)
        self.assertEqual(snapshot.weekly_used_percent, 34.5)


class ClaudeDesktopProviderTests(unittest.TestCase):
    def payload(self):
        return {
            "five_hour": {"utilization": 32, "resets_at": "2026-09-10T08:00:00Z"},
            "seven_day": {"utilization": 23, "resets_at": "2026-09-15T01:00:00Z"},
        }

    def test_desktop_provider_builds_snapshot_without_exposing_session_key(self):
        def fetch(session_key, org_id):
            self.assertEqual(session_key, "SK-TEST")
            return self.payload(), "Pro"

        snapshot = ClaudeDesktopUsageProvider(session_key="SK-TEST", fetch_usage=fetch).get_usage()
        self.assertEqual(snapshot.source, "本机 Claude 桌面版")
        self.assertEqual(snapshot.five_hour_used_percent, 32)
        self.assertEqual(snapshot.weekly_used_percent, 23)
        self.assertEqual(snapshot.plan, "Pro")
        self.assertNotIn("SK-TEST", snapshot.message)

    def test_desktop_provider_reuses_session_key_in_memory(self):
        reads = 0

        def read_session_key(_desktop_dir):
            nonlocal reads
            reads += 1
            return "SK-CACHED"

        provider = ClaudeDesktopUsageProvider(
            read_session_key=read_session_key,
            fetch_usage=lambda session_key, _org_id: (self.payload(), "Pro"),
        )

        first = provider.get_usage()
        second = provider.get_usage()

        self.assertEqual(first.five_hour_used_percent, 32)
        self.assertEqual(second.five_hour_used_percent, 32)
        self.assertEqual(reads, 1)

    def test_desktop_provider_discards_rejected_cached_session_key(self):
        reads = 0
        fetches = 0

        def read_session_key(_desktop_dir):
            nonlocal reads
            reads += 1
            return f"SK-{reads}"

        def fetch_usage(_session_key, _org_id):
            nonlocal fetches
            fetches += 1
            if fetches == 2:
                response = requests.Response()
                response.status_code = 401
                raise requests.exceptions.HTTPError(response=response)
            return self.payload(), "Pro"

        provider = ClaudeDesktopUsageProvider(
            read_session_key=read_session_key, fetch_usage=fetch_usage, read_access_token=no_oauth_token
        )

        self.assertTrue(provider.get_usage().has_usage)
        self.assertFalse(provider.get_usage().has_usage)
        self.assertTrue(provider.get_usage().has_usage)
        self.assertEqual(reads, 2)

    def test_missing_login_degrades_to_message_not_exception(self):
        def raise_login(_desktop_dir):
            raise ClaudeLoginError("Claude 桌面版尚未登录，请先在桌面应用登录后刷新")

        def no_token(_desktop_dir, _now):
            raise ClaudeLoginError("Claude 桌面版尚未登录或登录态无法读取，请在桌面应用重新登录后刷新")

        snapshot = ClaudeDesktopUsageProvider(
            read_session_key=raise_login, read_access_token=no_token
        ).get_usage()
        self.assertFalse(snapshot.has_usage)
        self.assertIn("登录", snapshot.message)

    def test_cookie_lock_falls_back_to_oauth_token(self):
        captured = {}

        def locked(_desktop_dir):
            raise ClaudeLoginError("Claude 桌面版正在占用登录数据，请先退出桌面版并刷新成功一次后再重新打开")

        def read_token(_desktop_dir, _now):
            return "AT-TEST", "max"

        def fetch_oauth(token):
            captured["token"] = token
            return self.payload()

        snapshot = ClaudeDesktopUsageProvider(
            read_session_key=locked,
            read_access_token=read_token,
            fetch_oauth_usage=fetch_oauth,
        ).get_usage()

        self.assertEqual(snapshot.source, "本机 Claude 桌面版")
        self.assertEqual(snapshot.five_hour_used_percent, 32)
        self.assertEqual(snapshot.weekly_used_percent, 23)
        self.assertEqual(snapshot.plan, "max")
        self.assertEqual(snapshot.message, "")
        self.assertEqual(captured["token"], "AT-TEST")
        self.assertNotIn("AT-TEST", snapshot.message)

    def test_cookie_lock_reports_lock_reason_when_oauth_also_missing(self):
        def locked(_desktop_dir):
            raise ClaudeLoginError("Claude 桌面版正在占用登录数据，请先退出桌面版并刷新成功一次后再重新打开")

        def no_token(_desktop_dir, _now):
            raise ClaudeLoginError("Claude 桌面版尚未登录或登录态无法读取，请在桌面应用重新登录后刷新")

        snapshot = ClaudeDesktopUsageProvider(
            read_session_key=locked,
            read_access_token=no_token,
            fetch_oauth_usage=lambda token: self.payload(),
        ).get_usage()

        self.assertFalse(snapshot.has_usage)
        self.assertIn("占用", snapshot.message)

    def test_oauth_token_reused_in_memory_after_cookie_lock(self):
        reads = 0

        def locked(_desktop_dir):
            raise ClaudeLoginError("Claude 桌面版正在占用登录数据，请先退出桌面版并刷新成功一次后再重新打开")

        def read_token(_desktop_dir, _now):
            nonlocal reads
            reads += 1
            return "AT-CACHED", "pro"

        provider = ClaudeDesktopUsageProvider(
            read_session_key=locked,
            read_access_token=read_token,
            fetch_oauth_usage=lambda token: self.payload(),
        )
        provider.get_usage()
        provider.get_usage()
        self.assertEqual(reads, 1)

    def test_cookie_failure_switches_to_oauth_within_same_refresh(self):
        cookie_fetches = 0

        def rate_limited(_session_key, _org_id):
            nonlocal cookie_fetches
            cookie_fetches += 1
            raise http_error(429)

        provider = ClaudeDesktopUsageProvider(
            session_key="SK-CACHED",
            fetch_usage=rate_limited,
            read_access_token=lambda _desktop_dir, _now: ("AT-TEST", "pro"),
            fetch_oauth_usage=lambda token: self.payload(),
        )

        first = provider.get_usage()
        second = provider.get_usage()

        self.assertEqual(first.five_hour_used_percent, 32)
        self.assertEqual(first.plan, "pro")
        self.assertEqual(first.message, "")
        self.assertIsNone(first.retry_after_seconds)
        self.assertTrue(second.has_usage)
        # 上次成功的 OAuth 链路优先，不再反复请求正被限流的 claude.ai。
        self.assertEqual(cookie_fetches, 1)

    def test_rejected_oauth_token_is_dropped_and_cookie_takes_over(self):
        reads = 0
        oauth_fetches = 0

        def read_token(_desktop_dir, _now):
            nonlocal reads
            reads += 1
            return f"AT-{reads}", "pro"

        def fetch_oauth(_token):
            nonlocal oauth_fetches
            oauth_fetches += 1
            if oauth_fetches == 2:
                raise http_error(401)
            return self.payload()

        provider = ClaudeDesktopUsageProvider(
            read_session_key=lambda _desktop_dir: "SK",
            fetch_usage=lambda _session_key, _org_id: (self.payload(), "Pro"),
            read_access_token=read_token,
            fetch_oauth_usage=fetch_oauth,
        )
        provider.preferred_channel = "oauth"

        self.assertEqual(provider.get_usage().plan, "pro")
        rescued = provider.get_usage()
        self.assertEqual(rescued.plan, "Pro")
        self.assertEqual(rescued.message, "")
        self.assertIsNone(provider.access_token)
        self.assertEqual(reads, 1)

    def test_oauth_rate_limit_is_reported_when_cookie_locked(self):
        def locked(_desktop_dir):
            raise ClaudeLoginError("Claude 桌面版正在占用登录数据，请先退出桌面版并刷新成功一次后再重新打开")

        def rate_limited(_token):
            raise http_error(429)

        snapshot = ClaudeDesktopUsageProvider(
            read_session_key=locked,
            read_access_token=lambda _desktop_dir, _now: ("AT-TEST", "pro"),
            fetch_oauth_usage=rate_limited,
        ).get_usage()

        self.assertFalse(snapshot.has_usage)
        self.assertIn("请求过于频繁", snapshot.message)
        self.assertEqual(snapshot.retry_after_seconds, 300)
        self.assertNotIn("AT-TEST", snapshot.message)

    def test_select_desktop_oauth_token_picks_valid_profile_scoped_token(self):
        now = datetime(2026, 1, 1, tzinfo=timezone(timedelta(hours=8)))
        future = int((now + timedelta(days=30)).timestamp() * 1000)
        later = int((now + timedelta(days=60)).timestamp() * 1000)
        past = int((now - timedelta(days=1)).timestamp() * 1000)
        base = "acct:dev|sess:org:https://api.anthropic.com:user:inference"
        entries = {
            f"{base} user:office": {"token": "NO-PROFILE", "expiresAt": later},
            f"{base} user:profile:a": {"token": "EXPIRED", "expiresAt": past, "subscriptionType": "pro"},
            f"{base} user:profile:b": {
                "token": "VALID",
                "expiresAt": later,
                "subscriptionType": "max",
                "refreshToken": "RT",
            },
            f"{base} user:profile:c": {"token": "OLDER", "expiresAt": future, "subscriptionType": "pro"},
        }
        config = {"oauth:tokenCacheV2": base64.b64encode(b"ciphertext").decode()}
        with patch("usage_provider.decrypt_cookie_value", return_value=json.dumps(entries)):
            token, plan = select_desktop_oauth_token(config, b"key", now)
        self.assertEqual(token, "VALID")
        self.assertEqual(plan, "max")

    def test_select_desktop_oauth_token_returns_none_without_valid_entry(self):
        now = datetime(2026, 1, 1, tzinfo=timezone(timedelta(hours=8)))
        past = int((now - timedelta(days=1)).timestamp() * 1000)
        entries = {
            "acct:dev|sess:org:https://api.anthropic.com:user:inference user:profile": {
                "token": "EXPIRED",
                "expiresAt": past,
            }
        }
        config = {"oauth:tokenCacheV2": base64.b64encode(b"ciphertext").decode()}
        with patch("usage_provider.decrypt_cookie_value", return_value=json.dumps(entries)):
            token, plan = select_desktop_oauth_token(config, b"key", now)
        self.assertIsNone(token)
        self.assertIsNone(plan)

    def test_read_desktop_oauth_token_reports_missing_config(self):
        with tempfile.TemporaryDirectory() as workspace:
            with self.assertRaisesRegex(ClaudeLoginError, "未找到 Claude 桌面版配置"):
                read_desktop_oauth_token(Path(workspace))

    def test_locked_cookie_database_is_not_reported_as_decryption_failure(self):
        database = Path("Cookies")
        with (
            patch("usage_provider.find_cookie_databases", return_value=[database]),
            patch("usage_provider.load_os_crypt_key", return_value=b"key"),
            patch("usage_provider.query_session_cookies", side_effect=PermissionError("locked")),
        ):
            with self.assertRaisesRegex(ClaudeLoginError, "占用"):
                read_desktop_session_key(Path("Claude"))

    def test_desktop_forbidden_response_prompts_relogin(self):
        response = requests.Response()
        response.status_code = 403
        error = requests.exceptions.HTTPError(response=response)

        def fetch(_session_key, _org_id):
            raise error

        snapshot = ClaudeDesktopUsageProvider(
            session_key="SK", fetch_usage=fetch, read_access_token=no_oauth_token
        ).get_usage()
        self.assertFalse(snapshot.has_usage)
        self.assertIn("重新登录", snapshot.message)

    def test_decrypt_cookie_value_roundtrips_v10_aes_gcm(self):
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        key = AESGCM.generate_key(bit_length=256)
        nonce = os.urandom(12)
        blob = b"v10" + nonce + AESGCM(key).encrypt(nonce, "session-value-123".encode("utf-8"), None)
        self.assertEqual(decrypt_cookie_value(blob, key), "session-value-123")

    def test_fetch_desktop_usage_resolves_org_and_plan(self):
        captured = {}

        class Response:
            def __init__(self, body):
                self._body = body

            def raise_for_status(self):
                return None

            def json(self):
                return self._body

        payload = self.payload()

        def fake_get(url, *, headers, timeout):
            captured.setdefault("urls", []).append(url)
            captured["cookie"] = headers.get("Cookie")
            captured["user_agent"] = headers.get("User-Agent")
            if url.endswith("/api/organizations"):
                return Response([
                    {"uuid": "org-api", "capabilities": ["api"]},
                    {"uuid": "org-chat", "capabilities": ["chat", "claude_max"]},
                ])
            return Response(payload)

        result, plan = fetch_desktop_usage("SK", None, http_get=fake_get)
        self.assertEqual(result, payload)
        self.assertEqual(plan, "Max")
        self.assertEqual(captured["cookie"], "sessionKey=SK")
        self.assertEqual(captured["user_agent"], "claude-usage/1.0")
        self.assertTrue(captured["urls"][-1].endswith("/api/organizations/org-chat/usage"))

    def test_desktop_dir_prefers_explicit_then_env(self):
        explicit = Path("custom") / "Claude"
        self.assertEqual(resolve_claude_desktop_dir(explicit), explicit)
        with patch.dict(os.environ, {"CLAUDE_DESKTOP_DIR": str(Path("env") / "Claude")}):
            self.assertEqual(resolve_claude_desktop_dir(), Path("env") / "Claude")

    def test_desktop_dir_falls_back_to_msix_package_dir(self):
        with tempfile.TemporaryDirectory() as workspace:
            root = Path(workspace)
            roaming, local = root / "Roaming", root / "Local"
            (roaming / "Claude").mkdir(parents=True)
            package = local / "Packages" / "Claude_pzs8sxrjxfjjc" / "LocalCache" / "Roaming" / "Claude"
            (package / "Network").mkdir(parents=True)
            (package / "Network" / "Cookies").write_bytes(b"db")
            (package / "Cache").mkdir()
            (package / "Cache" / "Cookies").write_bytes(b"noise")
            env = {"APPDATA": str(roaming), "LOCALAPPDATA": str(local), "CLAUDE_DESKTOP_DIR": ""}
            with patch.dict(os.environ, env):
                self.assertEqual(resolve_claude_desktop_dir(), package)
                self.assertEqual(
                    find_cookie_databases(package), [package / "Network" / "Cookies"]
                )

    def test_history_snapshot_uses_latest_sample_and_infers_reset(self):
        now = datetime(2026, 9, 10, 16, 30, tzinfo=timezone(timedelta(hours=8)))
        start = int((now - timedelta(minutes=10)).timestamp() * 1000)
        payload = {
            "version": 2,
            "samples": [
                {"t": start - 600_000, "u": {"fh": 60, "sd": 27}},
                {"t": start - 300_000, "u": {"fh": 0, "sd": 28}},
                {"t": start, "u": {"fh": 8, "sd": 29}},
            ],
        }
        snapshot = parse_plan_usage_history(payload, "缓存", now)
        self.assertEqual(snapshot.five_hour_used_percent, 8)
        self.assertEqual(snapshot.weekly_used_percent, 29)
        # sd 一路上升、没有回落边界，7 天重置时间不做推算。
        self.assertIsNone(snapshot.weekly_reset_at)
        self.assertEqual(snapshot.message, "")
        # 窗口边界是 fh 由 0 回升的那条样本，重置时间为其后 5 小时。
        self.assertEqual(snapshot.five_hour_reset_at, snapshot.updated_at + timedelta(hours=5))

    def test_history_snapshot_infers_weekly_reset_from_observed_drop(self):
        now = datetime(2026, 9, 10, 16, 30, tzinfo=timezone(timedelta(hours=8)))
        boundary = datetime(2026, 9, 9, 9, 4, tzinfo=timezone(timedelta(hours=8)))
        payload = {
            "version": 2,
            "samples": [
                {"t": int((boundary - timedelta(minutes=5)).timestamp() * 1000), "u": {"fh": 4, "sd": 88}},
                {"t": int(boundary.timestamp() * 1000), "u": {"fh": 5, "sd": 2}},
                {"t": int(now.timestamp() * 1000), "u": {"fh": 6, "sd": 9}},
            ],
        }
        snapshot = parse_plan_usage_history(payload, "缓存", now)
        # 回落边界 + 7 天，并按整点取整。
        self.assertEqual(
            snapshot.weekly_reset_at,
            datetime(2026, 9, 16, 9, 0, tzinfo=timezone(timedelta(hours=8))),
        )

    def test_history_snapshot_skips_weekly_reset_without_drop(self):
        now = datetime(2026, 9, 10, 16, 30, tzinfo=timezone(timedelta(hours=8)))
        payload = {
            "version": 2,
            "samples": [
                {"t": int((now - timedelta(minutes=5)).timestamp() * 1000), "u": {"fh": 5, "sd": 20}},
                {"t": int(now.timestamp() * 1000), "u": {"fh": 6, "sd": 21}},
            ],
        }
        self.assertIsNone(parse_plan_usage_history(payload, "缓存", now).weekly_reset_at)

    def test_history_snapshot_marks_stale_cache_and_skips_reset(self):
        now = datetime(2026, 9, 10, 16, 30, tzinfo=timezone(timedelta(hours=8)))
        sampled = int((now - timedelta(hours=6)).timestamp() * 1000)
        payload = {"version": 2, "samples": [{"t": sampled, "u": {"fh": 0, "sd": 14}}]}
        snapshot = parse_plan_usage_history(payload, "缓存", now)
        self.assertEqual(snapshot.weekly_used_percent, 14)
        self.assertIsNone(snapshot.five_hour_reset_at)
        self.assertIn("桌面版未在运行", snapshot.message)

    def test_history_provider_reports_missing_cache(self):
        with tempfile.TemporaryDirectory() as workspace:
            provider = ClaudeDesktopHistoryUsageProvider(workspace)
            snapshot = provider.get_usage()
            self.assertFalse(snapshot.has_usage)
            self.assertIn("未找到桌面版用量缓存", snapshot.message)

    def test_fallback_returns_first_provider_with_usage(self):
        now = datetime.now().astimezone()
        empty = ManualUsageProvider()
        filled = ManualUsageProvider(five_hour=8, weekly=29)
        self.assertEqual(FallbackUsageProvider([empty, filled]).get_usage().five_hour_used_percent, 8)
        self.assertEqual(FallbackUsageProvider([empty, empty]).get_usage().source, "手动输入")
        self.assertIsNotNone(now)

    def test_fallback_preserves_primary_failure_and_retry_backoff(self):
        response = requests.Response()
        response.status_code = 429

        def reject(_session_key, _org_id):
            raise requests.exceptions.HTTPError(response=response)

        online = ClaudeDesktopUsageProvider(session_key="SK", fetch_usage=reject, read_access_token=no_oauth_token)
        cached = ManualUsageProvider(five_hour=0, weekly=33)

        snapshot = FallbackUsageProvider([online, cached]).get_usage()

        self.assertEqual(snapshot.five_hour_used_percent, 0)
        self.assertEqual(snapshot.retry_after_seconds, 300)
        self.assertIn("请求过于频繁", snapshot.message)

    def test_fallback_completes_missing_reset_from_next_provider(self):
        partial = ManualUsageProvider(five_hour=8, weekly=29, five_hour_reset_at="2026-09-10T21:00:00+08:00")
        complete = ManualUsageProvider(
            five_hour=9,
            weekly=31,
            five_hour_reset_at="2026-09-10T22:00:00+08:00",
            weekly_reset_at="2026-09-15T09:00:00+08:00",
        )
        snapshot = FallbackUsageProvider([partial, complete]).get_usage()
        # 使用量与已有重置时间以第一个数据源为准，只补缺失的每周重置时间。
        self.assertEqual(snapshot.five_hour_used_percent, 8)
        self.assertEqual(snapshot.weekly_used_percent, 29)
        self.assertEqual(snapshot.five_hour_reset_at.hour, 21)
        self.assertIsNotNone(snapshot.weekly_reset_at)
        self.assertEqual(snapshot.weekly_reset_at.hour, 9)

    def test_create_provider_defaults_to_cookie_then_desktop_cache(self):
        default = create_provider({})
        self.assertIsInstance(default, FallbackUsageProvider)
        self.assertIsInstance(default.providers[0], ClaudeDesktopUsageProvider)
        self.assertIsInstance(default.providers[1], ClaudeDesktopHistoryUsageProvider)
        self.assertIsInstance(
            create_provider({"provider": "claude_desktop_history"}), ClaudeDesktopHistoryUsageProvider
        )
        self.assertIsInstance(
            create_provider({"provider": "claude_desktop_cookie"}), ClaudeDesktopUsageProvider
        )
        self.assertIsInstance(create_provider({"provider": "claude_local"}), ClaudeLocalUsageProvider)


if __name__ == "__main__":
    unittest.main()
