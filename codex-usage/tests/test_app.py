from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import QApplication, QLabel, QSystemTrayIcon

from app import (
    UsageCat,
    enforce_native_topmost,
    keep_last_successful_snapshot,
    load_config,
    saved_window_position,
)
from usage_provider import UsageSnapshot


class UsageDisplayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt_app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.now = datetime.now().astimezone()

    def test_failed_refresh_reuses_last_successful_snapshot(self):
        successful = UsageSnapshot(64, None, "本地 Codex 账户", self.now, "同步成功")
        failed = UsageSnapshot(None, None, "本地 Codex 账户", self.now, "无法连接额度服务")

        displayed, cached = keep_last_successful_snapshot(failed, successful)

        self.assertEqual(displayed.display_percent, 64)
        self.assertEqual(cached, successful)
        self.assertIn("最近一次成功数据", displayed.message)
        self.assertIn("无法连接额度服务", displayed.message)

    def test_first_failed_refresh_has_no_cached_data(self):
        failed = UsageSnapshot(None, None, "本地 Codex 账户", self.now, "无法连接额度服务")

        displayed, cached = keep_last_successful_snapshot(failed, None)

        self.assertEqual(displayed, failed)
        self.assertIsNone(cached)

    def test_tray_click_restores_window(self):
        widget = Mock()

        UsageCat.handle_tray_activated(
            widget,
            QSystemTrayIcon.ActivationReason.Trigger,
        )

        widget.restore_window.assert_called_once_with()

    def test_restore_window_forces_visible_and_raised_state(self):
        widget = Mock()
        widget.windowState.return_value = (
            Qt.WindowState.WindowMinimized | Qt.WindowState.WindowActive
        )

        with patch("app.QTimer.singleShot") as single_shot:
            UsageCat.restore_window(widget)

        widget.hide.assert_called_once_with()
        widget.setWindowState.assert_called_once_with(Qt.WindowState.WindowActive)
        widget.show.assert_called_once_with()
        widget.raise_.assert_called_once_with()
        single_shot.assert_called_once_with(0, widget.raise_)

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
        snapshot = UsageSnapshot(68, None, "本地 Codex 账户", self.now)
        provider = Mock()
        provider.get_usage.return_value = snapshot

        with patch("app.create_provider", return_value=provider):
            widget = UsageCat()
        try:
            widget.show()
            self.qt_app.processEvents()
            self.assertGreaterEqual(ctypes.windll.user32.SetWindowPos.call_count, 1)
        finally:
            widget.tray.hide()
            widget.hide()

    def test_load_config_ignores_legacy_provider_setting(self):
        with TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "settings.json"
            config_path.write_text(
                '{"provider": "manual", "refresh_seconds": 60}',
                encoding="utf-8",
            )
            with patch("app.CONFIG_PATH", config_path):
                self.assertEqual(
                    load_config(),
                    {
                        "provider": "codex_local",
                        "refresh_seconds": 60,
                        "window_position": None,
                    },
                )

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
        widget = Mock()
        widget.config = {}
        widget.pos.return_value = QPoint(120, -80)

        with patch("app.save_config") as save_config:
            UsageCat.save_window_position(widget)

        self.assertEqual(widget.config["window_position"], {"x": 120, "y": -80})
        save_config.assert_called_once_with(widget.config)

    def test_usage_window_does_not_create_pet_widget(self):
        snapshot = UsageSnapshot(68, None, "本地 Codex 账户", self.now)
        provider = Mock()
        provider.get_usage.return_value = snapshot

        with patch("app.create_provider", return_value=provider):
            widget = UsageCat()
        try:
            self.assertFalse(hasattr(widget, "pet"))
        finally:
            widget.tray.hide()
            widget.hide()

    def test_usage_window_balances_spacing_and_bottom_padding(self):
        snapshot = UsageSnapshot(68, None, "本地 Codex 账户", self.now)
        provider = Mock()
        provider.get_usage.return_value = snapshot

        with patch("app.create_provider", return_value=provider):
            widget = UsageCat()
        try:
            widget.show()
            QApplication.processEvents()
            title = next(label for label in widget.findChildren(QLabel) if label.text() == "Codex 额度")
            bottom_padding = widget.card.height() - (widget.reset.y() + widget.reset.height())

            self.assertEqual(widget.size().toTuple(), (240, 152))
            self.assertGreaterEqual(bottom_padding, 0)
            self.assertLess(widget.five_hour_name.x(), widget.five_hour_percent.x())
            self.assertLess(widget.weekly_name.x(), widget.percent.x())
            self.assertEqual(widget.five_hour_name.y(), widget.five_hour_percent.y())
            self.assertEqual(widget.weekly_name.y(), widget.percent.y())
            for label in (
                title,
                widget.five_hour_name,
                widget.five_hour_percent,
                widget.five_hour_reset,
                widget.weekly_name,
                widget.percent,
                widget.reset,
            ):
                self.assertGreaterEqual(label.height(), label.sizeHint().height())
            self.assertIn("font-size: 15px", title.styleSheet())
            self.assertIn("font-size: 15px", widget.five_hour_name.styleSheet())
            self.assertIn("font-size: 12px", widget.five_hour_percent.styleSheet())
            self.assertIn("font-size: 15px", widget.weekly_name.styleSheet())
            self.assertIn("font-size: 12px", widget.percent.styleSheet())
            self.assertEqual(widget.five_hour_bar.height(), 8)
            self.assertEqual(widget.bar.height(), 8)
            self.assertLessEqual(
                widget.five_hour_name.y() + widget.five_hour_name.height(),
                widget.five_hour_bar.y(),
            )
            self.assertLessEqual(
                widget.five_hour_bar.y() + widget.five_hour_bar.height(),
                widget.five_hour_reset.y(),
            )
            self.assertLessEqual(
                widget.five_hour_reset.y() + widget.five_hour_reset.height(),
                widget.weekly_name.y(),
            )
            self.assertLessEqual(
                widget.weekly_name.y() + widget.weekly_name.height(),
                widget.bar.y(),
            )
            self.assertLessEqual(
                widget.bar.y() + widget.bar.height(),
                widget.reset.y(),
            )
        finally:
            widget.tray.hide()
            widget.hide()

    def test_usage_window_uses_requested_quota_accents(self):
        snapshot = UsageSnapshot(68, None, "本地 Codex 账户", self.now)
        provider = Mock()
        provider.get_usage.return_value = snapshot

        with patch("app.create_provider", return_value=provider):
            widget = UsageCat()
        try:
            self.assertEqual(widget.five_hour_name.styleSheet(), "font-weight: 700; color: #493c58; font-size: 15px;")
            self.assertEqual(widget.five_hour_percent.styleSheet(), "font-weight: 800; color: #493c58; font-size: 12px;")
            self.assertEqual(widget.weekly_name.styleSheet(), "font-weight: 700; color: #493c58; font-size: 15px;")
            self.assertEqual(widget.percent.styleSheet(), "font-weight: 800; color: #493c58; font-size: 12px;")
            self.assertEqual(widget.five_hour_fill.styleSheet(), "background: #f2a640; border-radius: 4px;")
            self.assertEqual(widget.five_hour_bar.styleSheet(), "background: #eeeaf2; border-radius: 4px;")
            self.assertEqual(widget.fill.styleSheet(), "background: #e8788d; border-radius: 4px;")
            self.assertEqual(widget.bar.styleSheet(), "background: #eeeaf2; border-radius: 4px;")
        finally:
            widget.tray.hide()
            widget.hide()

    def test_usage_window_displays_separate_five_hour_and_weekly_quotas(self):
        snapshot = UsageSnapshot(
            68,
            self.now,
            "本地 Codex 账户",
            self.now,
            five_hour_remaining_percent=58,
            five_hour_reset_at=self.now,
        )
        provider = Mock()
        provider.get_usage.return_value = snapshot

        with patch("app.create_provider", return_value=provider):
            widget = UsageCat()
        try:
            self.assertEqual(widget.five_hour_name.text(), "5小时")
            self.assertEqual(widget.five_hour_percent.text(), "58% 剩余")
            self.assertEqual(widget.weekly_name.text(), "1周")
            self.assertEqual(widget.percent.text(), "68% 剩余")
            self.assertEqual(widget.five_hour_reset.text(), "重置：已到期，请刷新")
            self.assertEqual(widget.reset.text(), "重置：已到期，请刷新")
            self.assertIsNotNone(widget.five_hour_bar)
            self.assertIsNotNone(widget.bar)
        finally:
            widget.tray.hide()
            widget.hide()

    def test_tray_menu_only_has_refresh_and_quit_actions(self):
        snapshot = UsageSnapshot(68, None, "本地 Codex 账户", self.now)
        provider = Mock()
        provider.get_usage.return_value = snapshot

        with patch("app.create_provider", return_value=provider):
            widget = UsageCat()
        try:
            actions = [action.text() for action in widget.tray.contextMenu().actions()]
            self.assertEqual(actions, ["立即刷新", "退出"])
        finally:
            widget.tray.hide()
            widget.hide()


if __name__ == "__main__":
    unittest.main()
