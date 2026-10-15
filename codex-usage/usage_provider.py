"""Codex weekly usage 数据源抽象。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


@dataclass(frozen=True)
class UsageSnapshot:
    weekly_remaining_percent: float | None
    reset_at: datetime | None
    source: str
    updated_at: datetime
    message: str = ""
    five_hour_remaining_percent: float | None = None
    five_hour_reset_at: datetime | None = None

    @property
    def display_percent(self) -> int | None:
        if self.weekly_remaining_percent is None:
            return None
        return round(max(0.0, min(100.0, self.weekly_remaining_percent)))


class UsageProvider(ABC):
    """只返回聚合额度。"""

    @abstractmethod
    def get_usage(self) -> UsageSnapshot:
        raise NotImplementedError


class MockUsageProvider(UsageProvider):
    def get_usage(self) -> UsageSnapshot:
        now = datetime.now().astimezone()
        return UsageSnapshot(68.0, now + timedelta(days=3, hours=7), "演示数据", now)


class ManualUsageProvider(UsageProvider):
    def __init__(self, remaining_percent: float | None, reset_at: str | None = None):
        self.remaining_percent = remaining_percent
        self.reset_at = parse_datetime(reset_at)

    def get_usage(self) -> UsageSnapshot:
        return UsageSnapshot(
            self.remaining_percent,
            self.reset_at,
            "手动输入",
            datetime.now().astimezone(),
        )


class JsonFileUsageProvider(UsageProvider):
    """读取用户明确指定的本地 JSON 文件，不推断或扫描 Codex 私有目录。"""

    def __init__(self, json_path: str | Path):
        self.path = Path(json_path).expanduser()

    def get_usage(self) -> UsageSnapshot:
        now = datetime.now().astimezone()
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            remaining = extract_remaining_percent(payload)
            reset_at = parse_datetime(payload.get("weekly_reset_at") or payload.get("reset_at"))
            message = str(payload.get("message", ""))
            return UsageSnapshot(remaining, reset_at, f"JSON：{self.path.name}", now, message)
        except (OSError, json.JSONDecodeError, ValueError, TypeError) as exc:
            return UsageSnapshot(None, None, f"JSON：{self.path.name}", now, f"读取失败：{exc}")


class CodexLocalUsageProvider(UsageProvider):
    """从用户授权的本地 Codex 登录文件读取认证信息并查询账户额度。

    Token 仅用于向 ChatGPT 的官方额度端点发起只读请求，绝不写入配置、日志或 UI。
    """

    USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"

    def __init__(
        self,
        auth_path: str | Path | None = None,
        fetch_usage: Callable[[str, str], Mapping[str, Any]] | None = None,
    ):
        self.auth_path = Path(auth_path).expanduser() if auth_path else Path.home() / ".codex" / "auth.json"
        self.fetch_usage = fetch_usage or self._fetch_usage

    def get_usage(self) -> UsageSnapshot:
        now = datetime.now().astimezone()
        try:
            auth = json.loads(self.auth_path.read_text(encoding="utf-8"))
            tokens = auth.get("tokens")
            if not isinstance(tokens, Mapping):
                raise ValueError("缺少 Codex 登录信息")
            access_token = tokens.get("access_token")
            account_id = tokens.get("account_id")
            if not isinstance(access_token, str) or not isinstance(account_id, str):
                raise ValueError("缺少 Codex 访问令牌或账户标识")
            usage = self.fetch_usage(access_token, account_id)
            weekly_used_percent, reset_at, five_hour_used_percent, five_hour_reset_at = extract_rate_limits(usage, now)
            return UsageSnapshot(
                100.0 - weekly_used_percent,
                reset_at,
                "本地 Codex 账户",
                now,
                "已从本机登录状态同步",
                100.0 - five_hour_used_percent if five_hour_used_percent is not None else None,
                five_hour_reset_at,
            )
        except FileNotFoundError:
            return UsageSnapshot(None, None, "本地 Codex 账户", now, "未找到 ~/.codex/auth.json")
        except HTTPError as exc:
            return UsageSnapshot(None, None, "本地 Codex 账户", now, f"额度服务返回 HTTP {exc.code}")
        except (URLError, TimeoutError):
            return UsageSnapshot(None, None, "本地 Codex 账户", now, "无法连接额度服务")
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            return UsageSnapshot(None, None, "本地 Codex 账户", now, "无法读取有效的本地 Codex 额度")

    @classmethod
    def _fetch_usage(cls, access_token: str, account_id: str) -> Mapping[str, Any]:
        request = Request(
            cls.USAGE_URL,
            headers={
                "Authorization": f"Bearer {access_token}",
                "ChatGPT-Account-ID": account_id,
                "OpenAI-Beta": "codex-1",
                "User-Agent": "codex-usage-cat/1.0",
            },
            method="GET",
        )
        with urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if not isinstance(payload, Mapping):
            raise ValueError("额度响应格式错误")
        return payload


def extract_remaining_percent(payload: dict[str, Any]) -> float:
    """支持 weekly_remaining_percent，或 weekly_used_percent 两种简单格式。"""
    if "weekly_remaining_percent" in payload:
        return clamp_percent(payload["weekly_remaining_percent"])
    if "weekly_used_percent" in payload:
        return 100.0 - clamp_percent(payload["weekly_used_percent"])
    if "weekly_remaining" in payload:
        return clamp_percent(payload["weekly_remaining"])
    raise ValueError("缺少 weekly_remaining_percent、weekly_remaining 或 weekly_used_percent")


def extract_weekly_limit(payload: Mapping[str, Any], now: datetime | None = None) -> tuple[float, datetime | None]:
    """从 Codex 额度响应中选择最接近七天窗口的用量和重置时间。"""
    weekly_used_percent, reset_at, _, _ = extract_rate_limits(payload, now)
    return weekly_used_percent, reset_at


def extract_rate_limits(
    payload: Mapping[str, Any], now: datetime | None = None,
) -> tuple[float, datetime | None, float | None, datetime | None]:
    """提取周额度及响应中提供的五小时额度窗口。"""
    containers: list[Mapping[str, Any]] = []
    rate_limit = payload.get("rate_limit")
    if isinstance(rate_limit, Mapping):
        containers.append(rate_limit)

    additional = payload.get("additional_rate_limits")
    if isinstance(additional, list):
        containers.extend(item for item in additional if isinstance(item, Mapping))
    elif isinstance(additional, Mapping):
        containers.append(additional)
        containers.extend(item for item in additional.values() if isinstance(item, Mapping))

    candidates: list[Mapping[str, Any]] = []
    for container in containers:
        for name in ("primary_window", "secondary_window"):
            window = container.get(name)
            if isinstance(window, Mapping) and window.get("used_percent") is not None:
                candidates.append(window)
    if not candidates:
        raise ValueError("额度响应中没有可用窗口")

    weekly_seconds = 7 * 24 * 60 * 60
    weekly = min(
        candidates,
        key=lambda item: abs(float(item.get("limit_window_seconds", weekly_seconds)) - weekly_seconds),
    )
    used_percent = clamp_percent(weekly["used_percent"])
    reset_at = parse_usage_reset_at(weekly, now or datetime.now().astimezone())

    five_hour_seconds = 5 * 60 * 60
    five_hour_candidates = [
        item
        for item in candidates
        if float(item.get("limit_window_seconds", 0)) == five_hour_seconds
    ]
    if not five_hour_candidates:
        return used_percent, reset_at, None, None
    five_hour = five_hour_candidates[0]
    five_hour_used_percent = clamp_percent(five_hour["used_percent"])
    five_hour_reset_at = parse_usage_reset_at(five_hour, now or datetime.now().astimezone())
    return used_percent, reset_at, five_hour_used_percent, five_hour_reset_at


def clamp_percent(value: Any) -> float:
    number = float(value)
    if not 0 <= number <= 100:
        raise ValueError("百分比必须在 0 到 100 之间")
    return number


def parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return parsed.astimezone()


def parse_usage_reset_at(window: Mapping[str, Any], now: datetime) -> datetime | None:
    reset_at = window.get("reset_at")
    if reset_at is not None:
        return datetime.fromtimestamp(float(reset_at), tz=timezone.utc).astimezone()
    reset_after = window.get("reset_after_seconds")
    if reset_after is not None:
        return now + timedelta(seconds=float(reset_after))
    return None


def create_provider(config: dict[str, Any]) -> UsageProvider:
    provider_type = config.get("provider", "mock")
    if provider_type == "manual":
        return ManualUsageProvider(config.get("manual_remaining_percent"), config.get("manual_reset_at"))
    if provider_type == "json_file":
        return JsonFileUsageProvider(config.get("json_path", "sample_usage.json"))
    if provider_type == "codex_local":
        return CodexLocalUsageProvider(config.get("codex_auth_path") or None)
    return MockUsageProvider()
