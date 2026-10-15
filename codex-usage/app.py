from __future__ import annotations

import ctypes
import json
import sys
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QPoint, QRect, Qt, QTimer
from PySide6.QtGui import QAction, QGuiApplication, QIcon, QImage, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QSystemTrayIcon,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from usage_provider import UsageSnapshot, create_provider


APP_NAME = "Codex Usage Cat"
CONFIG_PATH = Path.home() / "AppData" / "Roaming" / "CodexUsageCat" / "settings.json"
PET_ASSET_PATH = Path(__file__).with_name("assets") / "dragonfruit_pet.png"
WIDGET_SIZE = (240, 152)
DEFAULT_REFRESH_SECONDS = 30
MIN_REFRESH_SECONDS = 30
ICON_SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)
_PET_ICON_CACHE: QIcon | None = None
_TOPMOST_FLAGS = 0x0001 | 0x0002 | 0x0010


def enforce_native_topmost(window_id: int) -> None:
    """在 Windows 上将窗口放入系统置顶层，同时不移动、不抢焦点。"""
    if sys.platform != "win32" or not window_id:
        return
    ctypes.windll.user32.SetWindowPos(window_id, -1, 0, 0, 0, 0, _TOPMOST_FLAGS)


def keep_last_successful_snapshot(
    snapshot: UsageSnapshot,
    last_successful_snapshot: UsageSnapshot | None,
) -> tuple[UsageSnapshot, UsageSnapshot | None]:
    """在本次取数失败时保留本次运行中最近一次可用的额度。"""
    if snapshot.display_percent is not None:
        return snapshot, snapshot
    if last_successful_snapshot is None:
        return snapshot, None

    message = "本次获取失败，展示最近一次成功数据"
    if snapshot.message:
        message = f"{message}：{snapshot.message}"
    return replace(last_successful_snapshot, message=message), last_successful_snapshot


def pixmap_alpha_bounds(pixmap: QPixmap) -> QRect:
    image = pixmap.toImage().convertToFormat(QImage.Format.Format_ARGB32)
    width, height = image.width(), image.height()
    min_x, min_y = width, height
    max_x, max_y = -1, -1

    for y in range(height):
        for x in range(width):
            if image.pixelColor(x, y).alpha() > 0:
                min_x = min(min_x, x)
                min_y = min(min_y, y)
                max_x = max(max_x, x)
                max_y = max(max_y, y)

    if max_x < min_x or max_y < min_y:
        return QRect(0, 0, width, height)
    return QRect(min_x, min_y, max_x - min_x + 1, max_y - min_y + 1)


def create_pet_icon() -> QIcon:
    global _PET_ICON_CACHE
    if _PET_ICON_CACHE is not None:
        return QIcon(_PET_ICON_CACHE)

    source = QPixmap(str(PET_ASSET_PATH))
    if source.isNull():
        return QIcon()

    cropped = source.copy(pixmap_alpha_bounds(source))
    icon = QIcon()
    for size in ICON_SIZES:
        canvas = QPixmap(size, size)
        canvas.fill(Qt.GlobalColor.transparent)
        target = cropped.scaled(
            size,
            size,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        painter = QPainter(canvas)
        painter.drawPixmap((size - target.width()) // 2, (size - target.height()) // 2, target)
        painter.end()
        icon.addPixmap(canvas)
    _PET_ICON_CACHE = icon
    return QIcon(_PET_ICON_CACHE)


def load_config() -> dict:
    try:
        config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        return {
            "provider": "codex_local",
            "refresh_seconds": config.get("refresh_seconds", DEFAULT_REFRESH_SECONDS),
            "window_position": config.get("window_position"),
        }
    except (OSError, json.JSONDecodeError):
        return {
            "provider": "codex_local",
            "refresh_seconds": DEFAULT_REFRESH_SECONDS,
            "window_position": None,
        }


def save_config(config: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")


def saved_window_position(config: dict) -> QPoint | None:
    position = config.get("window_position")
    if not isinstance(position, dict):
        return None
    x, y = position.get("x"), position.get("y")
    if type(x) is not int or type(y) is not int:
        return None
    return QPoint(x, y)


def format_reset_label(reset_at: datetime | None) -> str:
    if not reset_at:
        return "重置时间：待数据源提供"
    remaining = reset_at - datetime.now().astimezone()
    if remaining.total_seconds() <= 0:
        return "重置：已到期，请刷新"
    days, seconds = remaining.days, remaining.seconds
    hours, minutes = divmod(seconds // 60, 60)
    return f"重置：{reset_at:%m-%d %H:%M} · 约{days}天{hours}小时{minutes}分"


def rounded_percent(value: float | None) -> int | None:
    if value is None:
        return None
    return round(max(0.0, min(100.0, value)))


class UsageCat(QWidget):
    def __init__(self):
        flags = Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.WindowDoesNotAcceptFocus
        super().__init__(None, flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setWindowTitle(APP_NAME)
        self.setFixedSize(*WIDGET_SIZE)
        self.pet_icon = create_pet_icon()
        if not self.pet_icon.isNull():
            self.setWindowIcon(self.pet_icon)
        self.config = load_config()
        self.last_successful_snapshot: UsageSnapshot | None = None
        self.drag_offset: QPoint | None = None
        self.build_ui()
        self.create_tray()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh_usage)
        self.refresh_usage()
        self.apply_timer()
        self.restore_window_position()

    def showEvent(self, event):
        super().showEvent(event)
        enforce_native_topmost(int(self.winId()))

    def build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(5, 5, 5, 5)
        self.card = QFrame()
        self.card.setObjectName("card")
        self.card.setStyleSheet("#card { background: #fff8ee; border: 2px solid #493c58; border-radius: 18px; }")
        card_layout = QHBoxLayout(self.card)
        card_layout.setContentsMargins(12, 4, 12, 4)
        content = QVBoxLayout()
        content.setContentsMargins(0, 0, 0, 0)
        content.setSpacing(0)
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 1)
        title = QLabel("Codex 额度")
        title.setStyleSheet("color: #493c58; font-weight: 800; font-size: 15px;")
        header.addWidget(title)
        header.addStretch()
        five_hour_row = QHBoxLayout()
        five_hour_row.setContentsMargins(0, 7, 0, 7)
        self.five_hour_name = QLabel("5小时")
        self.five_hour_name.setStyleSheet("font-weight: 700; color: #493c58; font-size: 15px;")
        self.five_hour_percent = QLabel("--")
        self.five_hour_percent.setStyleSheet("font-weight: 800; color: #493c58; font-size: 12px;")
        five_hour_row.addWidget(self.five_hour_name)
        five_hour_row.addStretch()
        five_hour_row.addWidget(self.five_hour_percent)
        self.five_hour_bar = QFrame()
        self.five_hour_bar.setFixedHeight(8)
        self.five_hour_bar.setStyleSheet("background: #eeeaf2; border-radius: 4px;")
        self.five_hour_fill = QFrame(self.five_hour_bar)
        self.five_hour_fill.setGeometry(0, 0, 0, 8)
        self.five_hour_fill.setStyleSheet("background: #f2a640; border-radius: 4px;")
        self.five_hour_reset = QLabel("重置时间：待数据源提供")
        self.five_hour_reset.setMinimumWidth(190)
        self.five_hour_reset.setWordWrap(False)
        self.five_hour_reset.setStyleSheet("color: #665a6d; font-size: 11px;")
        weekly_row = QHBoxLayout()
        weekly_row.setContentsMargins(0, 7, 0, 7)
        self.weekly_name = QLabel("1周")
        self.weekly_name.setStyleSheet("font-weight: 700; color: #493c58; font-size: 15px;")
        self.percent = QLabel("--")
        self.percent.setStyleSheet("font-weight: 800; color: #493c58; font-size: 12px;")
        weekly_row.addWidget(self.weekly_name)
        weekly_row.addStretch()
        weekly_row.addWidget(self.percent)
        self.bar = QFrame()
        self.bar.setFixedHeight(8)
        self.bar.setStyleSheet("background: #eeeaf2; border-radius: 4px;")
        self.fill = QFrame(self.bar)
        self.fill.setGeometry(0, 0, 0, 8)
        self.fill.setStyleSheet("background: #e8788d; border-radius: 4px;")
        self.reset = QLabel("重置时间：待数据源提供")
        self.reset.setMinimumWidth(190)
        self.reset.setWordWrap(False)
        self.reset.setStyleSheet("color: #665a6d; font-size: 11px;")
        content.addLayout(header)
        content.addLayout(five_hour_row)
        content.addWidget(self.five_hour_bar)
        content.addWidget(self.five_hour_reset)
        content.addLayout(weekly_row)
        content.addWidget(self.bar)
        content.addWidget(self.reset)
        card_layout.addLayout(content, 1)
        outer.addWidget(self.card)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.update_bar()

    def update_bar(self):
        self.update_progress_bar(
            self.five_hour_bar,
            self.five_hour_fill,
            getattr(self, "current_five_hour_percent", None),
        )
        self.update_progress_bar(self.bar, self.fill, getattr(self, "current_percent", None))

    @staticmethod
    def update_progress_bar(bar: QFrame, fill: QFrame, value: int | None):
        width = bar.width() if value is not None else 0
        fill.setGeometry(0, 0, int(width * (value or 0) / 100), bar.height())

    def create_tray(self):
        self.tray = QSystemTrayIcon(self)
        self.tray.setToolTip(APP_NAME)
        tray_icon = self.pet_icon if not self.pet_icon.isNull() else self.style().standardIcon(self.style().StandardPixmap.SP_ComputerIcon)
        self.tray.setIcon(tray_icon)
        menu = QMenu()
        refresh_action = QAction("立即刷新", self)
        refresh_action.triggered.connect(self.refresh_usage)
        quit_action = QAction("退出", self)
        quit_action.triggered.connect(self.quit_application)
        menu.addAction(refresh_action)
        menu.addAction(quit_action)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self.handle_tray_activated)
        self.tray.show()

    def handle_tray_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.restore_window()

    def restore_window(self):
        """强制恢复被 Win+D 隐藏、但 Qt 仍视为可见的窗口。"""
        state = self.windowState() & ~Qt.WindowState.WindowMinimized
        self.hide()
        self.setWindowState(state)
        self.show()
        self.raise_()
        QTimer.singleShot(0, self.raise_)

    def refresh_usage(self):
        snapshot = create_provider(self.config).get_usage()
        snapshot, self.last_successful_snapshot = keep_last_successful_snapshot(
            snapshot,
            self.last_successful_snapshot,
        )
        self.render(snapshot)

    def render(self, snapshot: UsageSnapshot):
        self.current_percent = snapshot.display_percent
        self.current_five_hour_percent = rounded_percent(snapshot.five_hour_remaining_percent)
        self.five_hour_percent.setText(
            f"{self.current_five_hour_percent}% 剩余"
            if self.current_five_hour_percent is not None
            else "暂无数据"
        )
        self.five_hour_reset.setText(format_reset_label(snapshot.five_hour_reset_at))
        self.percent.setText(
            f"{snapshot.display_percent}% 剩余"
            if snapshot.display_percent is not None
            else "暂无数据"
        )
        self.reset.setText(format_reset_label(snapshot.reset_at))
        self.tray.setToolTip(
            f"{APP_NAME}：{self.five_hour_name.text()} {self.five_hour_percent.text()}；"
            f"{self.weekly_name.text()} {self.percent.text()}"
        )
        self.update_bar()

    def apply_timer(self):
        self.timer.start(max(MIN_REFRESH_SECONDS, int(self.config.get("refresh_seconds", DEFAULT_REFRESH_SECONDS))) * 1000)

    def show_autostart_help(self):
        self.tray.showMessage(APP_NAME, "在项目目录执行：powershell -ExecutionPolicy Bypass -File .\\install_autostart.ps1\n卸载：加上 -Uninstall", QSystemTrayIcon.MessageIcon.Information, 8000)

    def move_to_bottom_right(self):
        screen = QGuiApplication.primaryScreen().availableGeometry()
        self.move(screen.right() - self.width() - 16, screen.bottom() - self.height() - 16)

    def restore_window_position(self):
        position = saved_window_position(self.config)
        if position is None:
            self.move_to_bottom_right()
        else:
            self.move(position)

    def save_window_position(self):
        position = self.pos()
        self.config["window_position"] = {"x": position.x(), "y": position.y()}
        save_config(self.config)

    def quit_application(self):
        self.save_window_position()
        QApplication.quit()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event):
        if self.drag_offset is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self.drag_offset)
            event.accept()

    def mouseReleaseEvent(self, event):
        self.drag_offset = None
        self.save_window_position()
        event.accept()

    def closeEvent(self, event):
        self.save_window_position()
        event.ignore()
        self.hide()


def main() -> int:
    app = QApplication(sys.argv)
    app_icon = create_pet_icon()
    if not app_icon.isNull():
        app.setWindowIcon(app_icon)
    app.setQuitOnLastWindowClosed(False)
    widget = UsageCat()
    app.aboutToQuit.connect(widget.save_window_position)
    widget.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
