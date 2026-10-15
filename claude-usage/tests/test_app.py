import os
import sys
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QApplication

from app import (
    DEFAULT_REFRESH_SECONDS,
    LOCAL_CACHE_REFRESH_SECONDS,
    MIN_REFRESH_SECONDS,
    PET_ASSET_PATH,
    STATUS_ROW_HEIGHT,
    WIDGET_SIZE,
    UsageWindow,
    keep_last_successful_snapshot,
    load_config,
    refresh_interval_seconds,
    saved_window_position,
)
from usage_provider import DESKTOP_HISTORY_SOURCE, UsageSnapshot


class ClaudeUsageWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_icon_asset_exists(self):
        self.assertTrue(PET_ASSET_PATH.is_file())

    def test_failed_refresh_reuses_last_successful_snapshot(self):
        now = datetime.now().astimezone()
        success = UsageSnapshot(12, None, 34, None, "Claude", now)
        failure = UsageSnapshot(None, None, None, None, "Claude", now, "请求失败")
        displayed, cached = keep_last_successful_snapshot(failure, success)
        self.assertEqual(displayed.five_hour_used_percent, 12)
        self.assertEqual(cached, success)
        self.assertIn("最近一次成功数据", displayed.message)

    def test_failed_refresh_preserves_retry_backoff(self):
        now = datetime.now().astimezone()
        success = UsageSnapshot(12, None, 34, None, "Claude", now)
        failure = UsageSnapshot(None, None, None, None, "Claude", now, "请求过于频繁", retry_after_seconds=300)

        displayed, cached = keep_last_successful_snapshot(failure, success)

        self.assertEqual(displayed.retry_after_seconds, 300)
        self.assertEqual(cached, success)

    def test_render_displays_used_percent(self):
        now = datetime.now().astimezone()
        with patch.object(UsageWindow, "refresh_usage"):
            window = UsageWindow()
        try:
            window.render(UsageSnapshot(14, now, 36, now, "Claude", now, plan="pro"))
            self.assertEqual(window.five_hour_bar[0].text(), "14% 已使用")
            self.assertEqual(window.weekly_bar[0].text(), "36% 已使用")
            self.assertEqual(window.plan.text(), "Pro")
        finally:
            window.tray.hide()
            window.deleteLater()

    def test_success_hides_status_row_and_shrinks_card(self):
        now = datetime.now().astimezone()
        with patch.object(UsageWindow, "refresh_usage"):
            window = UsageWindow()
        try:
            window.render(UsageSnapshot(14, now, 36, now, "Claude", now, plan="pro"))
            self.assertEqual(window.status.text(), "")
            self.assertTrue(window.status.isHidden())
            self.assertEqual(window.height(), WIDGET_SIZE[1])
            window.render(UsageSnapshot(14, now, 36, now, "Claude", now, "请求失败"))
            self.assertFalse(window.status.isHidden())
            self.assertIn("请求失败", window.status.text())
            self.assertEqual(window.height(), WIDGET_SIZE[1] + STATUS_ROW_HEIGHT)
        finally:
            window.tray.hide()
            window.deleteLater()

    def test_load_config_and_saved_position(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text('{"window_position":{"x":120,"y":-80}}', encoding="utf-8")
            with patch("app.CONFIG_PATH", path):
                config = load_config()
        self.assertEqual(saved_window_position(config), QPoint(120, -80))
        self.assertEqual(config["provider"], "claude_desktop")
        self.assertEqual(config["refresh_seconds"], DEFAULT_REFRESH_SECONDS)

    def test_load_config_migrates_legacy_default_refresh_interval(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text('{"refresh_seconds":300}', encoding="utf-8")
            with patch("app.CONFIG_PATH", path):
                config = load_config()

        self.assertEqual(config["refresh_seconds"], DEFAULT_REFRESH_SECONDS)

    def test_refresh_interval_enforces_floor_and_retry_backoff(self):
        now = datetime.now().astimezone()
        config = {"refresh_seconds": 30}
        cached = UsageSnapshot(14, now, 36, None, DESKTOP_HISTORY_SOURCE, now)
        online = UsageSnapshot(14, now, 36, now, "本机 Claude 桌面版", now)
        throttled = UsageSnapshot(
            14,
            now,
            36,
            now,
            "本机 Claude 桌面版",
            now,
            retry_after_seconds=300,
        )
        self.assertEqual(refresh_interval_seconds(config, cached), LOCAL_CACHE_REFRESH_SECONDS)
        self.assertEqual(refresh_interval_seconds(config, online), MIN_REFRESH_SECONDS)
        self.assertEqual(refresh_interval_seconds({"refresh_seconds": 10}, online), MIN_REFRESH_SECONDS)
        self.assertEqual(refresh_interval_seconds({"refresh_seconds": 120}, online), 120)
        self.assertEqual(refresh_interval_seconds(config, throttled), 300)

    def test_tray_menu_only_has_refresh_and_quit(self):
        with patch.object(UsageWindow, "refresh_usage"):
            window = UsageWindow()
        try:
            self.assertEqual([action.text() for action in window.tray.contextMenu().actions()], ["立即刷新", "退出"])
        finally:
            window.tray.hide()
            window.deleteLater()


if __name__ == "__main__":
    unittest.main()
