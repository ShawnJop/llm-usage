from __future__ import annotations

import ctypes
import json
import sys
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, QPoint, QRect, QRunnable, Qt, QThreadPool, QTimer, Signal
from PySide6.QtGui import QAction, QGuiApplication, QIcon, QImage, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication, QDialog, QDoubleSpinBox, QFileDialog, QFormLayout, QFrame,
    QHBoxLayout, QLabel, QLineEdit, QMenu, QPushButton, QSystemTrayIcon,
    QVBoxLayout, QWidget,
)

from usage_provider import UsageSnapshot, create_provider, display_percent


APP_NAME = "Cursor Usage"
CONFIG_PATH = Path.home() / "AppData" / "Roaming" / "CursorUsage" / "settings.json"
DEFAULT_REFRESH_SECONDS = 30
MIN_REFRESH_SECONDS = 30
ICON_SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)
PET_ASSET_PATH = Path(__file__).with_name("assets") / "mangosteen-transparent.png"
WIDGET_WIDTH = 240
# 将卡片上下两段留白各压缩约一半，避免标题区显得过高。
WIDGET_HEIGHT = 156
_USAGE_ICON_CACHE: QIcon | None = None
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
    """本次取数失败时，继续展示本次运行中最近一次有效用量。"""
    if snapshot.auto_used_percent is not None or snapshot.api_used_percent is not None:
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


def create_usage_icon() -> QIcon:
    """Create one scalable pet icon for the window and system tray."""
    global _USAGE_ICON_CACHE
    if _USAGE_ICON_CACHE is not None:
        return QIcon(_USAGE_ICON_CACHE)

    source = QPixmap(str(PET_ASSET_PATH))
    if source.isNull():
        return QIcon()
    cropped = source.copy(pixmap_alpha_bounds(source))
    icon = QIcon()
    for size in ICON_SIZES:
        canvas = QPixmap(size, size)
        canvas.fill(Qt.GlobalColor.transparent)
        painter = QPainter(canvas)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        target = cropped.scaled(
            size, size, Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        painter.drawPixmap((size - target.width()) // 2, (size - target.height()) // 2, target)
        painter.end()
        icon.addPixmap(canvas)
    _USAGE_ICON_CACHE = icon
    return QIcon(_USAGE_ICON_CACHE)


class RefreshSignals(QObject):
    completed = Signal(object)


class UsageRefreshTask(QRunnable):
    def __init__(self, config: dict):
        super().__init__()
        self.config = config
        self.signals = RefreshSignals()

    def run(self) -> None:
        self.signals.completed.emit(create_provider(self.config).get_usage())


def load_config() -> dict:
    try:
        config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        config.setdefault("refresh_seconds", DEFAULT_REFRESH_SECONDS)
        return config
    except (OSError, json.JSONDecodeError):
        return {"provider": "cursor_dashboard", "refresh_seconds": DEFAULT_REFRESH_SECONDS}


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


class SettingsDialog(QDialog):
    def __init__(self, config: dict, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Cursor Usage 设置")
        self.setMinimumWidth(420)
        form = QFormLayout(self)
        self.provider = QLineEdit(config.get("provider", "cursor_dashboard"))
        self.provider.setPlaceholderText("cursor_dashboard、manual、json_file 或 mock")
        self.auto = self.percent_input(manual_used_value(config, "auto", 11))
        self.api = self.percent_input(manual_used_value(config, "api", 81))
        self.reset_at = QLineEdit(config.get("manual_reset_at", ""))
        self.reset_at.setPlaceholderText("例如 2026-08-01T09:00:00+08:00")
        self.json_path = QLineEdit(config.get("json_path", ""))
        browse = QPushButton("选择 JSON…")
        browse.clicked.connect(self.choose_json)
        path_row = QHBoxLayout()
        path_row.addWidget(self.json_path)
        path_row.addWidget(browse)
        self.refresh = QDoubleSpinBox()
        self.refresh.setRange(MIN_REFRESH_SECONDS, 3600)
        self.refresh.setSuffix(" 秒")
        self.refresh.setValue(float(config.get("refresh_seconds", DEFAULT_REFRESH_SECONDS)))
        form.addRow("数据源", self.provider)
        form.addRow("手动 Cursor Models 已使用", self.auto)
        form.addRow("手动 API 已使用", self.api)
        form.addRow("预计重置", self.reset_at)
        form.addRow("JSON 文件", path_row)
        form.addRow("自动刷新", self.refresh)
        hint = QLabel("Dashboard 默认自动读取 Cursor 本地登录状态；CURSOR_SESSION_TOKEN 仅作为手动覆盖。凭据不会保存到本应用设置。")
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#667085; font-size:11px")
        form.addRow(hint)
        actions = QHBoxLayout()
        cancel, ok = QPushButton("取消"), QPushButton("保存")
        cancel.clicked.connect(self.reject)
        ok.clicked.connect(self.accept)
        actions.addStretch(); actions.addWidget(cancel); actions.addWidget(ok)
        form.addRow(actions)

    @staticmethod
    def percent_input(value: object) -> QDoubleSpinBox:
        input_box = QDoubleSpinBox()
        input_box.setRange(0, 100)
        input_box.setSuffix(" %")
        input_box.setValue(float(value or 0))
        return input_box

    def choose_json(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "选择用量 JSON", str(Path.home()), "JSON 文件 (*.json)")
        if path:
            self.json_path.setText(path)

    def value(self) -> dict:
        return {
            "provider": self.provider.text().strip().lower() or "cursor_dashboard",
            "manual_auto_used": self.auto.value(), "manual_api_used": self.api.value(),
            "manual_reset_at": self.reset_at.text().strip(), "json_path": self.json_path.text().strip(),
            "refresh_seconds": int(self.refresh.value()),
        }


class UsageWindow(QWidget):
    def __init__(self):
        flags = (
            # Tool 窗口仍可置顶并响应鼠标拖动，但 Windows 不会为它创建任务栏按钮。
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        super().__init__(None, flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setWindowTitle(APP_NAME)
        self.usage_icon = create_usage_icon()
        self.setWindowIcon(self.usage_icon)
        self.setFixedSize(WIDGET_WIDTH, WIDGET_HEIGHT)
        self.config = load_config()
        self.current_auto = self.current_api = None
        self.last_successful_snapshot: UsageSnapshot | None = None
        self.drag_offset: QPoint | None = None
        self.refreshing = False
        self.refresh_pending = False
        self.refresh_task: UsageRefreshTask | None = None
        self.thread_pool = QThreadPool.globalInstance()
        self.build_ui()
        self.create_tray()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh_usage)
        self.refresh_usage()
        self.apply_timer()
        self.restore_window_position()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        enforce_native_topmost(int(self.winId()))

    def move_to_bottom_right(self) -> None:
        screen = QGuiApplication.primaryScreen().availableGeometry()
        self.move(screen.right() - self.width() - 24, screen.bottom() - self.height() - 24)

    def restore_window_position(self) -> None:
        position = saved_window_position(self.config)
        if position is None:
            self.move_to_bottom_right()
        else:
            self.move(position)

    def save_window_position(self) -> None:
        position = self.pos()
        self.config["window_position"] = {"x": position.x(), "y": position.y()}
        save_config(self.config)

    def quit_application(self) -> None:
        self.save_window_position()
        QApplication.quit()

    def build_ui(self) -> None:
        self.setStyleSheet("UsageWindow { background:transparent; }")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(5, 5, 5, 5)
        self.card = QFrame()
        self.card.setObjectName("usageCard")
        self.card.setStyleSheet(
            "#usageCard { background:#fffdfb; border:2px solid #493c58; border-radius:18px; }"
        )
        card_layout = QHBoxLayout(self.card)
        card_layout.setContentsMargins(12, 4, 12, 4)
        content = QVBoxLayout()
        content.setContentsMargins(0, 0, 0, 0)
        # 紧凑各用量分组之间的垂直留白，避免卡片信息显得松散。
        content.setSpacing(0)
        header = QHBoxLayout()
        # 嵌套布局的默认上下边距会让标题区显得过高，固定为约一半的留白。
        header.setContentsMargins(0, 4, 0, 4)
        title = QLabel("Cursor 月度用量")
        title.setStyleSheet("font-size:15px; font-weight:800; color:#493c58")
        header.addWidget(title); header.addStretch()
        content.addLayout(header)
        self.auto_bar = self.make_bar(content, "Cursor Models", "#7b61d1")
        self.api_bar = self.make_bar(content, "Other Models", "#25a66a")
        self.reset = QLabel("重置：待数据源提供")
        self.reset.setStyleSheet("color:#665a6d; font-size:11px")
        self.source = QLabel("数据源：--")
        self.source.setWordWrap(True)
        self.source.setStyleSheet("color:#8b7e90; font-size:10px")
        footer = QVBoxLayout()
        footer.setContentsMargins(0, 0, 0, 0)
        footer.setSpacing(0)
        footer.addWidget(self.reset)
        footer.addWidget(self.source)
        content.addLayout(footer)
        card_layout.addLayout(content, 1)
        layout.addWidget(self.card)

    @staticmethod
    def make_bar(layout: QVBoxLayout, label: str, color: str) -> tuple[QLabel, QFrame, QFrame]:
        row = QHBoxLayout()
        # 与标题区一致，将用量行的默认上下边距从约 9px 收紧至 4px。
        row.setContentsMargins(0, 4, 0, 4)
        name, value = QLabel(label), QLabel("--")
        name.setStyleSheet("font-weight:700; color:#493c58; font-size:12px")
        value.setStyleSheet("font-weight:800; color:#493c58; font-size:12px")
        row.addWidget(name); row.addStretch(); row.addWidget(value)
        bar = QFrame(); bar.setFixedHeight(8); bar.setStyleSheet("background:#eeeaf2;border-radius:4px")
        fill = QFrame(bar); fill.setStyleSheet(f"background:{color};border-radius:4px")
        layout.addLayout(row); layout.addWidget(bar)
        return value, bar, fill

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.update_bars()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event) -> None:
        if self.drag_offset is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self.drag_offset)
            event.accept()

    def mouseReleaseEvent(self, event) -> None:
        self.drag_offset = None
        self.save_window_position()
        event.accept()

    def update_bars(self) -> None:
        for value, bar_info in ((self.current_auto, self.auto_bar), (self.current_api, self.api_bar)):
            _, bar, fill = bar_info
            width = int(bar.width() * (value or 0) / 100) if value is not None else 0
            fill.setGeometry(0, 0, width, bar.height())

    def create_tray(self) -> None:
        self.tray = QSystemTrayIcon(self)
        self.tray.setToolTip(APP_NAME)
        self.tray.setIcon(self.usage_icon)
        menu = QMenu()
        refresh_action, quit_action = QAction("立即刷新", self), QAction("退出", self)
        refresh_action.triggered.connect(self.refresh_usage); quit_action.triggered.connect(self.quit_application)
        menu.addAction(refresh_action); menu.addAction(quit_action)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self.handle_tray_activation)
        self.tray.show()

    def handle_tray_activation(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self.restore_window()

    def restore_window(self) -> None:
        """Restore the dashboard from the system tray."""
        if self.isMinimized():
            self.showNormal()
        else:
            self.show()
        self.raise_()
        self.activateWindow()

    def refresh_usage(self) -> None:
        if self.refreshing:
            self.refresh_pending = True
            return
        self.refreshing = True
        self.refresh_pending = False
        self.source.setText("数据源正在刷新…")
        self.refresh_task = UsageRefreshTask(dict(self.config))
        self.refresh_task.signals.completed.connect(self.finish_refresh)
        self.thread_pool.start(self.refresh_task)

    def finish_refresh(self, snapshot: UsageSnapshot) -> None:
        snapshot, self.last_successful_snapshot = keep_last_successful_snapshot(
            snapshot,
            self.last_successful_snapshot,
        )
        self.render(snapshot)
        self.refreshing = False
        self.refresh_task = None
        if self.refresh_pending:
            QTimer.singleShot(0, self.refresh_usage)

    def render(self, snapshot: UsageSnapshot) -> None:
        self.current_auto, self.current_api = display_percent(snapshot.auto_used_percent), display_percent(snapshot.api_used_percent)
        for percentage, bar_info in ((self.current_auto, self.auto_bar), (self.current_api, self.api_bar)):
            label, _, _ = bar_info
            label.setText(f"{percentage}% 已使用" if percentage is not None else "--")
        if snapshot.reset_at:
            remaining = snapshot.reset_at - datetime.now().astimezone()
            self.reset.setText(
                f"重置：{snapshot.reset_at:%m-%d %H:%M}"
                + (f"（{format_countdown(remaining)}）" if remaining.total_seconds() > 0 else "（请刷新）")
            )
        else:
            self.reset.setText("重置：待数据源提供")
        source_name = snapshot.source.split("（", 1)[0]
        if snapshot.message:
            status = "更新失败，沿用上次数据" if "最近一次成功数据" in snapshot.message else "获取失败"
            self.source.setText(f"状态：{status}")
        else:
            self.source.setText(f"数据源：{source_name}")
        auto_text = f"Cursor Models {self.current_auto}%" if self.current_auto is not None else "Cursor Models --"
        api_text = f"Other Models {self.current_api}%" if self.current_api is not None else "Other Models --"
        self.tray.setToolTip(f"{APP_NAME}：{auto_text}，{api_text}")
        self.update_bars()

    def apply_timer(self) -> None:
        self.timer.start(max(MIN_REFRESH_SECONDS, int(self.config.get("refresh_seconds", DEFAULT_REFRESH_SECONDS))) * 1000)

    def open_settings(self) -> None:
        dialog = SettingsDialog(self.config, self)
        if dialog.exec():
            self.config = dialog.value()
            self.config["window_position"] = {"x": self.x(), "y": self.y()}
            save_config(self.config); self.apply_timer(); self.refresh_usage()

    def closeEvent(self, event) -> None:
        self.save_window_position()
        event.ignore(); self.hide()


def format_countdown(remaining) -> str:
    """将下次月度账期重置倒计时压缩为适合桌面卡片的文案。"""
    total_minutes = max(0, int(remaining.total_seconds() // 60))
    days, minutes = divmod(total_minutes, 24 * 60)
    hours = minutes // 60
    return f"约 {days} 天 {hours} 小时"


def manual_used_value(config: dict, name: str, default: float) -> float:
    """读取新设置，或将旧版“剩余”设置换算为已使用。"""
    used_key, legacy_key = f"manual_{name}_used", f"manual_{name}"
    if used_key in config:
        return float(config[used_key] or 0)
    if legacy_key in config:
        return 100 - float(config[legacy_key] or 0)
    return default


def main() -> int:
    app = QApplication(sys.argv)
    app.setWindowIcon(create_usage_icon())
    app.setQuitOnLastWindowClosed(False)
    window = UsageWindow()
    app.aboutToQuit.connect(window.save_window_position)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
