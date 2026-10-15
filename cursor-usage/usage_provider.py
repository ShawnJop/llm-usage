"""Cursor 用量数据源。

会话令牌只从环境变量读取，绝不写入配置、日志或界面。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
import base64
from contextlib import closing
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
import json
from math import ceil
import os
from pathlib import Path
import sqlite3
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


@dataclass(frozen=True)
class UsageSnapshot:
    auto_used_percent: float | None
    api_used_percent: float | None
    reset_at: datetime | None
    plan: str | None
    source: str
    updated_at: datetime
    message: str = ""

class UsageProvider(ABC):
    @abstractmethod
    def get_usage(self) -> UsageSnapshot:
        raise NotImplementedError


class CursorSessionError(ValueError):
    """Cursor 本地会话不存在、格式错误或已经过期。"""


class MockUsageProvider(UsageProvider):
    def get_usage(self) -> UsageSnapshot:
        now = datetime.now().astimezone()
        return UsageSnapshot(89, 19, now + timedelta(days=5), "Pro", "演示数据", now)


class ManualUsageProvider(UsageProvider):
    def __init__(self, auto: Any = None, api: Any = None, reset_at: str | None = None):
        self.auto = optional_percent(auto)
        self.api = optional_percent(api)
        self.reset_at = parse_datetime(reset_at)

    def get_usage(self) -> UsageSnapshot:
        return UsageSnapshot(self.auto, self.api, self.reset_at, None, "手动输入", datetime.now().astimezone())


class JsonFileUsageProvider(UsageProvider):
    def __init__(self, json_path: str | Path):
        self.path = Path(json_path).expanduser()

    def get_usage(self) -> UsageSnapshot:
        now = datetime.now().astimezone()
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            return parse_exported_usage(payload, source=f"JSON：{self.path.name}", now=now)
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            return UsageSnapshot(None, None, None, None, f"JSON：{self.path.name}", now, f"读取失败：{exc}")


class CursorDashboardUsageProvider(UsageProvider):
    """读取 Cursor 网页 Dashboard 的非公开接口。

    Cursor 尚未为个人订阅公开用量 API；该接口可能变动。默认从 Cursor IDE
    的 state.vscdb 读取登录会话，也可用 CURSOR_SESSION_TOKEN 手动覆盖。
    """

    USAGE_URL = "https://cursor.com/api/usage-summary"

    def __init__(self, session_token: str | None = None):
        self.session_token = session_token

    def get_usage(self) -> UsageSnapshot:
        now = datetime.now().astimezone()
        try:
            session_token, source = resolve_cursor_session_token(self.session_token)
            payload = self._fetch_usage(session_token)
            return replace(parse_cursor_usage(payload, now), source=source)
        except HTTPError as exc:
            message = "Cursor 登录已失效，请在 Cursor 中重新登录" if exc.code in (401, 403) else f"Cursor 返回 HTTP {exc.code}"
            return UsageSnapshot(None, None, None, None, "Cursor Dashboard", now, message)
        except (URLError, TimeoutError):
            return UsageSnapshot(None, None, None, None, "Cursor Dashboard", now, "无法连接 Cursor 用量服务")
        except CursorSessionError as exc:
            return UsageSnapshot(None, None, None, None, "Cursor Dashboard", now, str(exc))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return UsageSnapshot(None, None, None, None, "Cursor Dashboard", now, "无法读取有效的 Cursor 用量")

    @classmethod
    def _fetch_usage(cls, session_token: str) -> Mapping[str, Any]:
        token = normalize_session_token(session_token)
        request = Request(
            cls.USAGE_URL,
            headers={
                "Cookie": f"WorkosCursorSessionToken={token}",
                "Accept": "application/json",
                "User-Agent": "cursor-usage/1.0",
            },
            method="GET",
        )
        with urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if not isinstance(payload, Mapping):
            raise ValueError("Cursor 响应格式错误")
        return payload


def parse_cursor_usage(payload: Mapping[str, Any], now: datetime | None = None) -> UsageSnapshot:
    """将 /api/usage-summary 转换为已使用百分比。"""
    individual = expect_mapping(payload.get("individualUsage"), "缺少 individualUsage")
    plan_usage = expect_mapping(individual.get("plan"), "缺少 plan")
    return UsageSnapshot(
        reported_used_percent(plan_usage, "autoPercentUsed"),
        reported_used_percent(plan_usage, "apiPercentUsed"),
        parse_datetime(payload.get("billingCycleEnd")),
        string_or_none(payload.get("membershipType")),
        "Cursor Dashboard",
        now or datetime.now().astimezone(),
    )


def parse_exported_usage(payload: Mapping[str, Any], source: str, now: datetime) -> UsageSnapshot:
    """支持仪表盘原始响应，或简化的 used JSON 格式。"""
    if isinstance(payload.get("individualUsage"), Mapping):
        snapshot = parse_cursor_usage(payload, now)
        return replace(snapshot, source=source)
    return UsageSnapshot(
        exported_used_percent(payload, "auto"),
        exported_used_percent(payload, "api"),
        parse_datetime(payload.get("reset_at") or payload.get("billing_cycle_end")),
        string_or_none(payload.get("plan")),
        source,
        now,
        str(payload.get("message", "")),
    )


def expect_mapping(value: Any, message: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(message)
    return value


def reported_used_percent(plan: Mapping[str, Any], field: str) -> float | None:
    value = plan.get(field)
    return None if value is None else clamp_percent(value)


def exported_used_percent(payload: Mapping[str, Any], name: str) -> float | None:
    used = payload.get(f"{name}_used_percent")
    if used is not None:
        return optional_percent(used)
    remaining = optional_percent(payload.get(f"{name}_remaining_percent"))
    return None if remaining is None else 100.0 - remaining


def optional_percent(value: Any) -> float | None:
    return None if value is None or value == "" else clamp_percent(value)


def clamp_percent(value: Any) -> float:
    number = float(value)
    if not 0 <= number <= 100:
        raise ValueError("百分比必须在 0 到 100 之间")
    return number


def display_percent(value: float | None) -> int | None:
    """按 Cursor Dashboard 的展示规则向上取整已用百分比。"""
    return None if value is None else ceil(max(0, min(100, value)))


def parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return parsed.astimezone()


def string_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def normalize_session_token(value: str) -> str:
    token = value.strip()
    for cookie in token.split(";"):
        name, separator, cookie_value = cookie.strip().partition("=")
        if separator and name == "WorkosCursorSessionToken":
            token = cookie_value.strip()
            break
    if not token:
        raise ValueError("会话令牌为空")
    return token


def resolve_cursor_session_token(explicit_token: str | None = None) -> tuple[str, str]:
    configured_token = explicit_token or os.environ.get("CURSOR_SESSION_TOKEN")
    if configured_token:
        return web_session_token(normalize_session_token(configured_token)), "Cursor Dashboard（手动会话）"
    access_token, _ = read_local_cursor_access_token()
    return web_session_token(access_token), "Cursor Dashboard（Cursor 本地登录）"


def read_local_cursor_access_token(paths: list[Path] | None = None) -> tuple[str, Path]:
    candidates = paths or cursor_state_paths()
    found_database = False
    for path in candidates:
        if not path.is_file():
            continue
        found_database = True
        try:
            uri = path.resolve().as_uri() + "?mode=ro"
            with closing(sqlite3.connect(uri, uri=True, timeout=2)) as connection:
                row = connection.execute(
                    "SELECT value FROM ItemTable WHERE key = ?",
                    ("cursorAuth/accessToken",),
                ).fetchone()
            if row and isinstance(row[0], str) and row[0].strip():
                return row[0].strip(), path
        except sqlite3.Error:
            continue
    if found_database:
        raise CursorSessionError("Cursor 尚未登录，请先在 Cursor 中完成登录")
    raise CursorSessionError("未找到 Cursor 登录状态，请先启动并登录 Cursor")


def cursor_state_paths() -> list[Path]:
    paths: list[Path] = []
    appdata = os.environ.get("APPDATA")
    if appdata:
        paths.append(Path(appdata) / "Cursor" / "User" / "globalStorage" / "state.vscdb")
    fallback = Path.home() / "AppData" / "Roaming" / "Cursor" / "User" / "globalStorage" / "state.vscdb"
    if fallback not in paths:
        paths.append(fallback)
    return paths


def web_session_token(token: str) -> str:
    """把 IDE 保存的 access JWT 转成 Cursor Dashboard 使用的 Cookie 值。"""
    if "%3A%3A" in token:
        return token
    if "::" in token:
        return token.replace("::", "%3A%3A", 1)
    claims = decode_jwt_payload(token)
    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject:
        raise CursorSessionError("Cursor 本地登录格式无效，请重新登录")
    expires_at = claims.get("exp")
    if isinstance(expires_at, (int, float)) and expires_at <= datetime.now().timestamp():
        raise CursorSessionError("Cursor 本地登录已过期，请在 Cursor 中重新登录")
    user_id = subject.rsplit("|", 1)[-1]
    if not user_id:
        raise CursorSessionError("Cursor 本地登录缺少用户标识，请重新登录")
    return f"{user_id}%3A%3A{token}"


def decode_jwt_payload(token: str) -> Mapping[str, Any]:
    parts = token.split(".")
    if len(parts) != 3:
        raise CursorSessionError("Cursor 本地登录格式无效，请重新登录")
    try:
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(payload).decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CursorSessionError("Cursor 本地登录格式无效，请重新登录") from exc
    if not isinstance(decoded, Mapping):
        raise CursorSessionError("Cursor 本地登录格式无效，请重新登录")
    return decoded


def create_provider(config: Mapping[str, Any]) -> UsageProvider:
    provider_type = config.get("provider", "cursor_dashboard")
    if provider_type == "manual":
        return ManualUsageProvider(
            manual_used_percent(config, "auto"),
            manual_used_percent(config, "api"),
            config.get("manual_reset_at"),
        )
    if provider_type == "json_file":
        return JsonFileUsageProvider(str(config.get("json_path", "sample_usage.json")))
    if provider_type == "mock":
        return MockUsageProvider()
    return CursorDashboardUsageProvider()


def manual_used_percent(config: Mapping[str, Any], name: str) -> Any:
    """优先读取新配置；旧版手动“剩余”配置会转换为已使用比例。"""
    used_key, legacy_key = f"manual_{name}_used", f"manual_{name}"
    if used_key in config:
        return config[used_key]
    remaining = optional_percent(config.get(legacy_key))
    return None if remaining is None else 100.0 - remaining
