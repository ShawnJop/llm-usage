import os
import sys
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import QApplication, QSystemTrayIcon

from app import (
    PET_ASSET_PATH,
    WIDGET_WIDTH,
    UsageWindow,
    create_usage_icon,
    enforce_native_topmost,
    keep_last_successful_snapshot,
    load_config,
    saved_window_position,
)
from usage_provider import UsageSnapshot


class UsageIconTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_usage_icon_contains_standard_tray_size(self):
        self.assertTrue(PET_ASSET_PATH.is_file())
        icon = create_usage_icon()
        self.assertFalse(icon.isNull())
        self.assertIn(16, [size.width() for size in icon.availableSizes()])

    def test_failed_refresh_reuses_last_successful_snapshot(self):
        now = datetime.now().astimezone()
        successful = UsageSnapshot(12.4, 35.6, None, "Pro", "Cursor Dashboard", now)
        failed = UsageSnapshot(
            None,
            None,
            None,
            None,
            "Cursor Dashboard",
            now,
            "无法连接 Cursor 用量服务",
        )

        _, cached = keep_last_successful_snapshot(successful, None)
        displayed, retained_cache = keep_last_successful_snapshot(failed, cached)

        self.assertEqual(displayed.auto_used_percent, 12.4)
        self.assertEqual(displayed.api_used_percent, 35.6)
        self.assertEqual(retained_cache, successful)
        self.assertIn("最近一次成功数据", displayed.message)
        self.assertIn("无法连接 Cursor 用量服务", displayed.message)

    def test_first_failed_refresh_has_no_cached_data(self):
        now = datetime.now().astimezone()
        failed = UsageSnapshot(
            None,
            None,
            None,
            None,
            "Cursor Dashboard",
            now,
            "无法连接 Cursor 用量服务",
        )

        displayed, cached = keep_last_successful_snapshot(failed, None)

        self.assertEqual(displayed, failed)
        self.assertIsNone(cached)

    def test_finish_refresh_keeps_last_successful_values_after_failure(self):
        now = datetime.now().astimezone()
        successful = UsageSnapshot(None, 35.6, None, "Pro", "Cursor Dashboard", now)
        failed = UsageSnapshot(
            None,
            None,
            None,
            None,
            "Cursor Dashboard",
            now,
            "无法连接 Cursor 用量服务",
        )

        with patch.object(UsageWindow, "refresh_usage"):
            window = UsageWindow()
        try:
            window.finish_refresh(successful)
            window.finish_refresh(failed)

            self.assertIsNone(window.current_auto)
            self.assertEqual(window.current_api, 36)
            self.assertEqual(window.last_successful_snapshot, successful)
            self.assertEqual(window.source.text(), "状态：更新失败，沿用上次数据")
        finally:
            window.tray.hide()
            window.deleteLater()

    def test_first_failed_refresh_uses_compact_failure_status(self):
        now = datetime.now().astimezone()
        failed = UsageSnapshot(
            None,
            None,
            None,
            None,
            "Cursor Dashboard",
            now,
            "无法连接 Cursor 用量服务",
        )

        with patch.object(UsageWindow, "refresh_usage"):
            window = UsageWindow()
        try:
            window.finish_refresh(failed)

            self.assertEqual(window.source.text(), "状态：获取失败")
        finally:
            window.tray.hide()
            window.deleteLater()

    def test_usage_window_uses_translucent_background(self):
        with patch.object(UsageWindow, "refresh_usage"):
            window = UsageWindow()
        self.assertTrue(window.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground))
        self.assertTrue(window.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating))
        self.assertTrue(window.windowFlags() & Qt.WindowType.FramelessWindowHint)
        self.assertTrue(window.windowFlags() & Qt.WindowType.WindowDoesNotAcceptFocus)
        self.assertEqual(
            window.windowFlags() & Qt.WindowType.WindowType_Mask,
            Qt.WindowType.Tool,
        )
        self.assertEqual(window.width(), WIDGET_WIDTH)
        window.tray.hide()
        window.deleteLater()

    def test_tray_activation_restores_window(self):
        with patch.object(UsageWindow, "refresh_usage"):
            window = UsageWindow()
        with patch.object(window, "restore_window") as restore_window:
            window.handle_tray_activation(QSystemTrayIcon.ActivationReason.Trigger)
            window.handle_tray_activation(QSystemTrayIcon.ActivationReason.DoubleClick)
        self.assertEqual(restore_window.call_count, 2)
        window.tray.hide()
        window.deleteLater()

    def test_restore_window_shows_hidden_window_and_activates_it(self):
        with patch.object(UsageWindow, "refresh_usage"):
            window = UsageWindow()
        window.hide()
        with patch.object(window, "raise_") as raise_window, patch.object(
            window, "activateWindow"
        ) as activate_window:
            window.restore_window()
        self.assertTrue(window.isVisible())
        raise_window.assert_called_once_with()
        activate_window.assert_called_once_with()
        window.tray.hide()
        window.deleteLater()

    @patch("app.sys.platform", "win32")
    @patch("app.ctypes")
    def test_enforce_native_topmost_uses_windows_topmost_layer(self, ctypes):
        enforce_native_topmost(321)

        ctypes.windll.user32.SetWindowPos.assert_called_once_with(
            321,
            -1,
            0,
            0,
            0,
            0,
            0x0001 | 0x0002 | 0x0010,
        )

    @patch("app.sys.platform", "win32")
    @patch("app.ctypes")
    def test_show_enforces_native_topmost(self, ctypes):
        with patch.object(UsageWindow, "refresh_usage"):
            window = UsageWindow()
        try:
            window.show()
            self.app.processEvents()
            self.assertGreaterEqual(ctypes.windll.user32.SetWindowPos.call_count, 1)
        finally:
            window.tray.hide()
            window.hide()

    def test_usage_window_does_not_create_pet_widget(self):
        with patch.object(UsageWindow, "refresh_usage"):
            window = UsageWindow()
        self.assertFalse(hasattr(window, "pet"))
        window.tray.hide()
        window.deleteLater()

    def test_tray_has_icon_and_only_refresh_and_quit_actions(self):
        with patch.object(UsageWindow, "refresh_usage"):
            window = UsageWindow()
        self.assertFalse(window.tray.icon().isNull())
        self.assertEqual(
            [action.text() for action in window.tray.contextMenu().actions()],
            ["立即刷新", "退出"],
        )
        window.tray.hide()
        window.deleteLater()

    def test_load_config_keeps_saved_window_position(self):
        with TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "settings.json"
            config_path.write_text(
                '{"window_position": {"x": 120, "y": -80}}',
                encoding="utf-8",
            )
            with patch("app.CONFIG_PATH", config_path):
                self.assertEqual(saved_window_position(load_config()).toTuple(), (120, -80))

    def test_saved_window_position_rejects_invalid_values(self):
        self.assertIsNone(saved_window_position({"window_position": {"x": "120", "y": 80}}))

    def test_save_window_position_updates_config(self):
        window = Mock()
        window.config = {}
        window.pos.return_value = QPoint(120, -80)

        with patch("app.save_config") as save_config:
            UsageWindow.save_window_position(window)

        self.assertEqual(window.config["window_position"], {"x": 120, "y": -80})
        save_config.assert_called_once_with(window.config)


if __name__ == "__main__":
    unittest.main()
