from __future__ import annotations

import ctypes
import json
import sys
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, QPoint, QRect, QRunnable, Qt, QThreadPool, QTimer, Signal
from PySide6.QtGui import QAction, QGuiApplication, QIcon, QImage, QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QFrame, QHBoxLayout, QLabel, QMenu, QSystemTrayIcon, QVBoxLayout, QWidget

from usage_provider import DESKTOP_HISTORY_SOURCE, UsageProvider, UsageSnapshot, create_provider, display_percent


APP_NAME = "Claude Usage Banana"
CONFIG_PATH = Path.home() / "AppData" / "Roaming" / "ClaudeUsage" / "settings.json"
PET_ASSET_PATH = Path(__file__).with_name("assets") / "banana_pet.png"
DEFAULT_REFRESH_SECONDS = 30
MIN_REFRESH_SECONDS = 60
LEGACY_DEFAULT_REFRESH_SECONDS = 300
# 本地缓存数据源不发网络请求、不受接口限流约束，可以更勤地跟进桌面版的写入。
LOCAL_CACHE_REFRESH_SECONDS = 60
# 常规高度不含状态行；仅在有状态提示时按 STATUS_ROW_HEIGHT 临时加高。
WIDGET_SIZE = (240, 158)
STATUS_ROW_HEIGHT = 16
ICON_SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)
_ICON_CACHE: QIcon | None = None
_TOPMOST_FLAGS = 0x0001 | 0x0002 | 0x0010


def enforce_native_topmost(window_id: int) -> None:
    if sys.platform == "win32" and window_id:
        ctypes.windll.user32.SetWindowPos(window_id, -1, 0, 0, 0, 0, _TOPMOST_FLAGS)


def keep_last_successful_snapshot(
    snapshot: UsageSnapshot,
    last_successful_snapshot: UsageSnapshot | None,
) -> tuple[UsageSnapshot, UsageSnapshot | None]:
    if snapshot.has_usage:
        return snapshot, snapshot
    if last_successful_snapshot is None:
        return snapshot, None
    message = "本次获取失败，展示最近一次成功数据"
    if snapshot.message:
        message = f"{message}：{snapshot.message}"
    return (
        replace(
            last_successful_snapshot,
            message=message,
            retry_after_seconds=snapshot.retry_after_seconds,
        ),
        last_successful_snapshot,
    )


def refresh_interval_seconds(config: dict, snapshot: UsageSnapshot | None = None) -> int:
    """默认配置为 30 秒，统一执行 60 秒下限；限流时遵循数据源退避提示。"""
    if snapshot is not None and snapshot.retry_after_seconds is not None:
        return max(MIN_REFRESH_SECONDS, snapshot.retry_after_seconds)
    if snapshot is not None and snapshot.source == DESKTOP_HISTORY_SOURCE:
        return max(MIN_REFRESH_SECONDS, LOCAL_CACHE_REFRESH_SECONDS)
    return max(MIN_REFRESH_SECONDS, int(config.get("refresh_seconds", DEFAULT_REFRESH_SECONDS)))


def pixmap_alpha_bounds(pixmap: QPixmap) -> QRect:
    image = pixmap.toImage().convertToFormat(QImage.Format.Format_ARGB32)
    width, height = image.width(), image.height()
    min_x, min_y, max_x, max_y = width, height, -1, -1
    for y in range(height):
        for x in range(width):
            if image.pixelColor(x, y).alpha() > 0:
                min_x, min_y = min(min_x, x), min(min_y, y)
                max_x, max_y = max(max_x, x), max(max_y, y)
    if max_x < min_x or max_y < min_y:
        return QRect(0, 0, width, height)
    return QRect(min_x, min_y, max_x - min_x + 1, max_y - min_y + 1)


def create_usage_icon() -> QIcon:
    global _ICON_CACHE
    if _ICON_CACHE is not None:
        return QIcon(_ICON_CACHE)
    source = QPixmap(str(PET_ASSET_PATH))
    if source.isNull():
        return QIcon()
    cropped = source.copy(pixmap_alpha_bounds(source))
    icon = QIcon()
    for size in ICON_SIZES:
        canvas = QPixmap(size, size)
        canvas.fill(Qt.GlobalColor.transparent)
        target = cropped.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        painter = QPainter(canvas)
        painter.drawPixmap((size - target.width()) // 2, (size - target.height()) // 2, target)
        painter.end()
        icon.addPixmap(canvas)
    _ICON_CACHE = icon
    return QIcon(_ICON_CACHE)


def load_config() -> dict:
    defaults = {"provider": "claude_desktop", "refresh_seconds": DEFAULT_REFRESH_SECONDS, "window_position": None}
    try:
        config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        if not isinstance(config, dict):
            return defaults
        merged = {**defaults, **config}
        # 旧版本默认写入的 claude_local 无法读取桌面版登录态，迁移到桌面版数据源。
        if config.get("provider") == "claude_local" and not config.get("claude_credentials_path"):
            merged["provider"] = "claude_desktop"
        # 旧版本没有设置界面，300 秒通常是程序写入的旧默认值；迁移后实际按 60 秒下限刷新。
        if config.get("refresh_seconds") == LEGACY_DEFAULT_REFRESH_SECONDS:
            merged["refresh_seconds"] = DEFAULT_REFRESH_SECONDS
        return merged
    except (OSError, json.JSONDecodeError):
        return defaults


def save_config(config: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")


def saved_window_position(config: dict) -> QPoint | None:
    position = config.get("window_position")
    if not isinstance(position, dict):
        return None
    x, y = position.get("x"), position.get("y")
    return QPoint(x, y) if type(x) is int and type(y) is int else None


def format_reset(reset_at: datetime | None) -> str:
    if reset_at is None:
        return "重置：待数据源提供"
    remaining = reset_at - datetime.now().astimezone()
    if remaining.total_seconds() <= 0:
        return "重置：已到期，请刷新"
    total_minutes = max(0, int(remaining.total_seconds() // 60))
    days, minutes = divmod(total_minutes, 24 * 60)
    hours, minutes = divmod(minutes, 60)
    countdown = f"约{days}天{hours}小时" if days else f"约{hours}小时{minutes}分"
    return f"重置：{reset_at:%m-%d %H:%M} · {countdown}"


class RefreshSignals(QObject):
    completed = Signal(object)


class UsageRefreshTask(QRunnable):
    def __init__(self, provider: UsageProvider):
        super().__init__()
        self.provider = provider
        self.signals = RefreshSignals()

    def run(self) -> None:
        self.signals.completed.emit(self.provider.get_usage())


class UsageWindow(QWidget):
    def __init__(self):
        flags = Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.WindowDoesNotAcceptFocus
        super().__init__(None, flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setWindowTitle(APP_NAME)
        self.setFixedSize(*WIDGET_SIZE)
        self.usage_icon = create_usage_icon()
        if not self.usage_icon.isNull():
            self.setWindowIcon(self.usage_icon)
        self.config = load_config()
        # 复用数据源实例，使解密后的 sessionKey 只在本进程内缓存，避开桌面版后续的 Cookie 文件锁。
        self.provider = create_provider(self.config)
        self.last_successful_snapshot: UsageSnapshot | None = None
        self.current_five_hour = self.current_weekly = None
        self.drag_offset: QPoint | None = None
        self.refreshing = self.refresh_pending = False
        self.refresh_task: UsageRefreshTask | None = None
        self.thread_pool = QThreadPool.globalInstance()
        self.build_ui()
        self.create_tray()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh_usage)
        self.refresh_usage()
        self.apply_timer()
        self.restore_window_position()

    def build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(5, 5, 5, 5)
        self.card = QFrame()
        self.card.setObjectName("usageCard")
        self.card.setStyleSheet("#usageCard { background:#fffaf2; border:2px solid #5b3927; border-radius:18px; }")
        content = QVBoxLayout(self.card)
        content.setContentsMargins(12, 7, 12, 7)
        content.setSpacing(0)
        header = QHBoxLayout()
        title = QLabel("Claude 订阅用量")
        title.setStyleSheet("font-size:15px; font-weight:800; color:#5b3927")
        self.plan = QLabel("")
        self.plan.setStyleSheet("font-size:11px; color:#8b6857")
        header.addWidget(title)
        header.addStretch()
        header.addWidget(self.plan)
        content.addLayout(header)
        self.five_hour_bar = self.make_limit(content, "当前会话", "#2d7dd2")
        self.five_hour_reset = self.make_reset(content)
        self.weekly_bar = self.make_limit(content, "每周 · 全部模型", "#00a8cc")
        self.weekly_reset = self.make_reset(content)
        self.status = QLabel("")
        self.status.setStyleSheet("color:#9a7c6c; font-size:10px")
        content.addWidget(self.status)
        outer.addWidget(self.card)
        self.set_status("正在刷新…")

    @staticmethod
    def make_limit(layout: QVBoxLayout, name: str, color: str) -> tuple[QLabel, QFrame, QFrame]:
        row = QHBoxLayout()
        row.setContentsMargins(0, 5, 0, 3)
        label, value = QLabel(name), QLabel("--")
        label.setStyleSheet("font-weight:700; color:#5b3927; font-size:12px")
        value.setStyleSheet("font-weight:800; color:#5b3927; font-size:12px")
        row.addWidget(label)
        row.addStretch()
        row.addWidget(value)
        bar = QFrame()
        bar.setFixedHeight(8)
        bar.setStyleSheet("background:#eee7e1; border-radius:4px")
        fill = QFrame(bar)
        fill.setStyleSheet(f"background:{color}; border-radius:4px")
        layout.addLayout(row)
        layout.addWidget(bar)
        return value, bar, fill

    @staticmethod
    def make_reset(layout: QVBoxLayout) -> QLabel:
        label = QLabel("重置：待数据源提供")
        label.setStyleSheet("color:#765f53; font-size:11px")
        layout.addWidget(label)
        return label

    def set_status(self, text: str) -> None:
        """状态行只在有提示时占位：正常刷新成功后隐藏，并把卡片高度收回去。"""
        self.status.setText(text)
        self.status.setVisible(bool(text))
        self.setFixedSize(WIDGET_SIZE[0], WIDGET_SIZE[1] + (STATUS_ROW_HEIGHT if text else 0))

    def create_tray(self) -> None:
        self.tray = QSystemTrayIcon(self)
        self.tray.setIcon(self.usage_icon if not self.usage_icon.isNull() else self.style().standardIcon(self.style().StandardPixmap.SP_ComputerIcon))
        self.tray.setToolTip(APP_NAME)
        menu = QMenu()
        refresh_action, quit_action = QAction("立即刷新", self), QAction("退出", self)
        refresh_action.triggered.connect(self.refresh_usage)
        quit_action.triggered.connect(self.quit_application)
        menu.addAction(refresh_action)
        menu.addAction(quit_action)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self.handle_tray_activation)
        self.tray.show()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        enforce_native_topmost(int(self.winId()))

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.update_bars()

    def update_bars(self) -> None:
        for value, (_, bar, fill) in ((self.current_five_hour, self.five_hour_bar), (self.current_weekly, self.weekly_bar)):
            fill.setGeometry(0, 0, int(bar.width() * (value or 0) / 100) if value is not None else 0, bar.height())

    def refresh_usage(self) -> None:
        if self.refreshing:
            self.refresh_pending = True
            return
        self.refreshing = True
        self.refresh_pending = False
        if self.last_successful_snapshot is None:
            self.set_status("正在刷新…")
        self.refresh_task = UsageRefreshTask(self.provider)
        self.refresh_task.signals.completed.connect(self.finish_refresh)
        self.thread_pool.start(self.refresh_task)

    def finish_refresh(self, snapshot: UsageSnapshot) -> None:
        snapshot, self.last_successful_snapshot = keep_last_successful_snapshot(snapshot, self.last_successful_snapshot)
        self.render(snapshot)
        self.apply_timer(snapshot)
        self.refreshing = False
        self.refresh_task = None
        if self.refresh_pending:
            QTimer.singleShot(0, self.refresh_usage)

    def render(self, snapshot: UsageSnapshot) -> None:
        self.current_five_hour = display_percent(snapshot.five_hour_used_percent)
        self.current_weekly = display_percent(snapshot.weekly_used_percent)
        self.five_hour_bar[0].setText(f"{self.current_five_hour}% 已使用" if self.current_five_hour is not None else "暂无数据")
        self.weekly_bar[0].setText(f"{self.current_weekly}% 已使用" if self.current_weekly is not None else "暂无数据")
        self.five_hour_reset.setText(format_reset(snapshot.five_hour_reset_at))
        self.weekly_reset.setText(format_reset(snapshot.weekly_reset_at))
        self.plan.setText(snapshot.plan.title() if snapshot.plan else "")
        if snapshot.message:
            self.set_status("状态：更新失败，沿用上次数据" if "最近一次成功数据" in snapshot.message else f"状态：{snapshot.message}")
        else:
            self.set_status("")
        self.status.setToolTip(snapshot.message)
        self.card.setToolTip(f"数据源：{snapshot.source}\n更新于 {snapshot.updated_at:%m-%d %H:%M}")
        self.tray.setToolTip(f"{APP_NAME}：5小时 {self.five_hour_bar[0].text()}；1周 {self.weekly_bar[0].text()}")
        self.update_bars()

    def apply_timer(self, snapshot: UsageSnapshot | None = None) -> None:
        self.timer.start(refresh_interval_seconds(self.config, snapshot) * 1000)

    def handle_tray_activation(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick):
            self.restore_window()

    def restore_window(self) -> None:
        self.showNormal() if self.isMinimized() else self.show()
        self.raise_()
        QTimer.singleShot(0, lambda: enforce_native_topmost(int(self.winId())))

    def move_to_bottom_right(self) -> None:
        screen = QGuiApplication.primaryScreen().availableGeometry()
        self.move(screen.right() - self.width() - 24, screen.bottom() - self.height() - 24)

    def restore_window_position(self) -> None:
        position = saved_window_position(self.config)
        self.move(position) if position is not None else self.move_to_bottom_right()

    def save_window_position(self) -> None:
        position = self.pos()
        self.config["window_position"] = {"x": position.x(), "y": position.y()}
        save_config(self.config)

    def quit_application(self) -> None:
        self.save_window_position()
        QApplication.quit()

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

    def closeEvent(self, event) -> None:
        self.save_window_position()
        event.ignore()
        self.hide()


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
