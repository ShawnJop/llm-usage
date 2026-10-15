"""Claude 订阅用量数据源。

默认读取 Claude 桌面版（Claude Desktop）的本机登录态，可选读取 Claude Code 的
OAuth 登录文件。两类凭据都只用于查询官方用量端点，不会写入配置、日志或界面。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
import base64
import ctypes
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import time
from typing import Any, Callable, Mapping

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
import requests
import truststore


USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
OAUTH_BETA = "oauth-2025-04-20"
# 两条链路都以本程序自己的身份发起请求，不冒充 Claude 桌面版或 Claude Code。
APP_USER_AGENT = "claude-usage/1.0"

CLAUDE_AI_BASE = "https://claude.ai"
DPAPI_KEY_PREFIX = b"DPAPI"
# Claude 桌面版以 MSIX 包分发时，%APPDATA% 会被重定向到包容器目录。
MSIX_PACKAGE_PATTERN = "Claude_*"
MSIX_APPDATA_SUBPATHS = (("LocalCache", "Roaming"), ("LocalCache", "Local"))
# Chromium/Electron 在不同版本把 Cookie 库放在这些相对位置。
COOKIE_RELATIVE_PATHS = (
    "Cookies",
    "Network/Cookies",
    "Default/Cookies",
    "Default/Network/Cookies",
)
COOKIE_GLOB_PATTERNS = ("Partitions/*/Cookies", "Partitions/*/Network/Cookies")
# 桌面版运行时写入的本地用量缓存。
PLAN_HISTORY_FILENAME = "plan-usage-history.json"
DESKTOP_HISTORY_SOURCE = "本机桌面版用量缓存"
# 桌面版运行时会独占锁住 Cookie 库，但 config.json 未被独占锁；它把 OAuth 登录态
# 加密写在下列键里（新键在前），与 Cookie 共用同一 DPAPI 主密钥。
DESKTOP_CONFIG_FILENAME = "config.json"
DESKTOP_OAUTH_CACHE_KEYS = ("oauth:tokenCacheV2", "oauth:tokenCache")
# 只取含该作用域的登录条目，它带订阅计划，且可查询 /api/oauth/usage。
DESKTOP_OAUTH_REQUIRED_SCOPE = "user:profile"
RATE_LIMIT_BACKOFF_SECONDS = 300
# 桌面版两条实时接口链路；单条链路失败时会在同一次刷新内改走另一条。
COOKIE_CHANNEL = "cookie"
OAUTH_CHANNEL = "oauth"
LIVE_CHANNEL_ERRORS = (requests.exceptions.RequestException, OSError, sqlite3.Error, TypeError, ValueError)
FIVE_HOUR_WINDOW = timedelta(hours=5)
SEVEN_DAY_WINDOW = timedelta(days=7)
HISTORY_STALE_AFTER = timedelta(minutes=15)
# 缓存推不出来、需要由后备数据源补齐的字段。
COMPLEMENTED_FIELDS = ("five_hour_reset_at", "weekly_reset_at", "plan")
# 兜底遍历的深度与需要跳过的大体积缓存目录。
COOKIE_SEARCH_DEPTH = 4
COOKIE_SKIP_DIRS = frozenset(
    name.lower()
    for name in (
        "Cache",
        "Code Cache",
        "DawnCache",
        "GPUCache",
        "ShaderCache",
        "blob_storage",
        "Crashpad",
        "logs",
        "IndexedDB",
        "Local Storage",
        "Service Worker",
        "Session Storage",
    )
)
PLAN_CAPABILITIES = (
    ("claude_max", "Max"),
    ("claude_pro", "Pro"),
    ("raven", "Team"),
    ("enterprise", "Enterprise"),
)

# requests 默认 CA 包不一定包含 Windows 组策略安装的企业代理根证书。
truststore.inject_into_ssl()


@dataclass(frozen=True)
class UsageSnapshot:
    five_hour_used_percent: float | None
    five_hour_reset_at: datetime | None
    weekly_used_percent: float | None
    weekly_reset_at: datetime | None
    source: str
    updated_at: datetime
    message: str = ""
    plan: str | None = None
    retry_after_seconds: int | None = None

    @property
    def has_usage(self) -> bool:
        return self.five_hour_used_percent is not None or self.weekly_used_percent is not None


class UsageProvider(ABC):
    @abstractmethod
    def get_usage(self) -> UsageSnapshot:
        raise NotImplementedError


class MockUsageProvider(UsageProvider):
    def get_usage(self) -> UsageSnapshot:
        now = datetime.now().astimezone()
        return UsageSnapshot(14, now + timedelta(hours=3), 36, now + timedelta(days=4), "演示数据", now, plan="Pro")


class ManualUsageProvider(UsageProvider):
    def __init__(self, five_hour: Any = None, weekly: Any = None, five_hour_reset_at: Any = None, weekly_reset_at: Any = None):
        self.five_hour = optional_percent(five_hour)
        self.weekly = optional_percent(weekly)
        self.five_hour_reset_at = parse_datetime(five_hour_reset_at)
        self.weekly_reset_at = parse_datetime(weekly_reset_at)

    def get_usage(self) -> UsageSnapshot:
        return UsageSnapshot(
            self.five_hour,
            self.five_hour_reset_at,
            self.weekly,
            self.weekly_reset_at,
            "手动输入",
            datetime.now().astimezone(),
        )


class JsonFileUsageProvider(UsageProvider):
    def __init__(self, json_path: str | Path):
        self.path = Path(json_path).expanduser()

    def get_usage(self) -> UsageSnapshot:
        now = datetime.now().astimezone()
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            return parse_exported_usage(payload, f"JSON：{self.path.name}", now)
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            return empty_snapshot(f"JSON：{self.path.name}", now, f"读取失败：{exc}")


class ClaudeLocalUsageProvider(UsageProvider):
    """使用 Claude Code 本机 OAuth 登录态查询订阅额度。

    `/api/oauth/usage` 是 Claude 客户端使用的非公开端点，响应结构和限流策略可能变更。
    """

    USAGE_URL = USAGE_URL
    OAUTH_BETA = OAUTH_BETA

    def __init__(
        self,
        credentials_path: str | Path | None = None,
        fetch_usage: Callable[[str], Mapping[str, Any]] | None = None,
    ):
        self.credentials_path = resolve_claude_credentials_path(credentials_path)
        self.fetch_usage = fetch_usage or fetch_claude_usage

    def get_usage(self) -> UsageSnapshot:
        now = datetime.now().astimezone()
        source = "本机 Claude Code 账户"
        try:
            credentials = json.loads(self.credentials_path.read_text(encoding="utf-8"))
            oauth = expect_mapping(credentials.get("claudeAiOauth"), "缺少 Claude Code OAuth 登录信息")
            access_token = oauth.get("accessToken")
            if not isinstance(access_token, str) or not access_token.strip():
                raise ValueError("缺少 Claude Code OAuth 访问令牌")
            expires_at = parse_epoch_milliseconds(oauth.get("expiresAt"))
            if expires_at is not None and expires_at <= now:
                raise ClaudeLoginError("Claude Code 登录已过期，请运行 claude /login 重新登录")
            payload = self.fetch_usage(access_token)
            snapshot = parse_claude_usage(payload, now)
            plan = string_or_none(oauth.get("subscriptionType"))
            return UsageSnapshot(
                snapshot.five_hour_used_percent,
                snapshot.five_hour_reset_at,
                snapshot.weekly_used_percent,
                snapshot.weekly_reset_at,
                source,
                now,
                plan=plan,
            )
        except FileNotFoundError:
            return empty_snapshot(source, now, "未找到 Claude Code OAuth 凭据，请先登录 Claude Code")
        except requests.exceptions.HTTPError as exc:
            status_code = exc.response.status_code if exc.response is not None else 0
            if status_code == 401:
                message = "Claude Code 登录已失效，请运行 claude /login 重新登录"
            elif status_code == 403:
                message = "Claude 用量请求被拒绝，请确认 Claude Code 已登录并更新至最新版本"
            elif status_code == 429:
                message = "Claude 用量服务请求过于频繁，请稍后刷新"
            else:
                message = f"Claude 用量服务返回 HTTP {status_code or '?'}"
            return empty_snapshot(
                source,
                now,
                message,
                retry_after_seconds=RATE_LIMIT_BACKOFF_SECONDS if status_code == 429 else None,
            )
        except requests.exceptions.SSLError:
            return empty_snapshot(source, now, "Claude 用量服务证书验证失败")
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout, TimeoutError):
            return empty_snapshot(source, now, "无法连接 Claude 用量服务")
        except requests.exceptions.RequestException:
            return empty_snapshot(source, now, "Claude 用量服务请求失败")
        except ClaudeLoginError as exc:
            return empty_snapshot(source, now, str(exc))
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            return empty_snapshot(source, now, "无法读取有效的 Claude 订阅用量")


class ClaudeLoginError(ValueError):
    """Claude 登录态缺失或已失效。"""


class ClaudeDesktopHistoryUsageProvider(UsageProvider):
    """读取 Claude 桌面版写在本机的用量缓存 `plan-usage-history.json`。

    桌面版运行期间约每 5 分钟追加一条样本：
    `{"t": epoch_ms, "org": uuid, "u": {"fh": 5 小时已用百分比, "sd": 7 天已用百分比}}`。
    纯本地只读：不需要登录态、不发网络请求，也不受 Cookie 库被桌面版独占进程锁的影响。
    代价是数据新鲜度取决于桌面版是否在运行，且文件里没有重置时间，
    5 小时重置时间由样本回落点推算，7 天重置时间无法推算。
    """

    def __init__(self, desktop_dir: str | Path | None = None, history_path: str | Path | None = None):
        self.desktop_dir = resolve_claude_desktop_dir(desktop_dir)
        self.history_path = (
            Path(history_path).expanduser() if history_path else self.desktop_dir / PLAN_HISTORY_FILENAME
        )

    def get_usage(self) -> UsageSnapshot:
        now = datetime.now().astimezone()
        source = DESKTOP_HISTORY_SOURCE
        try:
            payload = read_json_retrying(self.history_path)
            return parse_plan_usage_history(payload, source, now)
        except FileNotFoundError:
            return empty_snapshot(source, now, "未找到桌面版用量缓存，请先启动并登录 Claude 桌面版")
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            return empty_snapshot(source, now, "无法读取有效的桌面版用量缓存")


class FallbackUsageProvider(UsageProvider):
    """依次尝试多个数据源，返回第一个拿到用量的结果，并用后续数据源补齐缺失字段。

    本地缓存数据源拿得到使用量、却没有每周重置时间和订阅计划；
    因此第一个有用量的快照若仍缺 `COMPLEMENTED_FIELDS`，会继续询问后面的数据源，
    只把缺失字段填上，使用量本身仍以第一个数据源为准（它最新且最稳定）。
    """

    def __init__(self, providers: list[UsageProvider]):
        if not providers:
            raise ValueError("至少需要一个数据源")
        self.providers = providers

    def get_usage(self) -> UsageSnapshot:
        snapshots = []
        failures: list[str] = []
        retry_after_seconds: int | None = None
        primary: UsageSnapshot | None = None
        for provider in self.providers:
            snapshot = provider.get_usage()
            snapshots.append(snapshot)
            if snapshot.retry_after_seconds is not None:
                retry_after_seconds = max(retry_after_seconds or 0, snapshot.retry_after_seconds)
            if not snapshot.has_usage:
                if snapshot.message:
                    failures.append(snapshot.message)
                continue
            if primary is None:
                primary = snapshot
                if failures:
                    fallback_message = f"前序数据源不可用，已使用备用数据：{failures[0]}"
                    if snapshot.message:
                        fallback_message = f"{fallback_message}；{snapshot.message}"
                    primary = replace(
                        primary,
                        message=fallback_message,
                        retry_after_seconds=retry_after_seconds,
                    )
            else:
                primary = complement_snapshot(primary, snapshot)
            if snapshot_is_complete(primary):
                return primary
        return primary if primary is not None else snapshots[0]


def snapshot_is_complete(snapshot: UsageSnapshot) -> bool:
    return all(getattr(snapshot, field) is not None for field in COMPLEMENTED_FIELDS)


def complement_snapshot(primary: UsageSnapshot, backup: UsageSnapshot) -> UsageSnapshot:
    """用后备快照补齐主快照缺失的重置时间与订阅计划，不覆盖已有值。"""
    missing = {
        field: getattr(backup, field)
        for field in COMPLEMENTED_FIELDS
        if getattr(primary, field) is None and getattr(backup, field) is not None
    }
    return replace(primary, **missing) if missing else primary


class ClaudeDesktopUsageProvider(UsageProvider):
    """使用 Claude 桌面版（Claude Desktop）本机登录态查询订阅额度。

    优先解密 claude.ai 的 `sessionKey` Cookie，向 `/api/organizations/{uuid}/usage`
    查询（与桌面版设置页同源，含重置时间与订阅计划）。桌面版是 Electron 客户端，
    `sessionKey` 保存在 `%APPDATA%\\Claude` 的 Chromium Cookie 库中，由 DPAPI + AES-GCM 加密。

    桌面版运行时会独占锁住 Cookie 库，`sessionKey` 读不到；此时改用桌面版写在
    `config.json` 里的 OAuth 登录态：解密其中的 access token，查询 `/api/oauth/usage`
    （与 Claude Code 同端点）。`config.json` 未被独占锁，桌面版运行时也可读。
    两条链路互为备援：上次成功的链路优先，任一条失败（锁库、限流、5xx、令牌被拒等）
    都会在同一次刷新内改走另一条，只有两条都失败才回报错误。

    两条链路都只取登录令牌用于只读查询，仅缓存在当前进程内，不落盘、不记录、不展示；
    OAuth 缓存条目里同时含 refreshToken，本数据源只读取 access token，从不读取或刷新它。
    这些端点均为非公开接口，字段与限流策略可能变更。
    """

    def __init__(
        self,
        desktop_dir: str | Path | None = None,
        organization_id: str | None = None,
        session_key: str | None = None,
        fetch_usage: Callable[..., tuple[Mapping[str, Any], str | None]] | None = None,
        read_session_key: Callable[[Path], str] | None = None,
        fetch_oauth_usage: Callable[[str], Mapping[str, Any]] | None = None,
        read_access_token: Callable[[Path, datetime], tuple[str, str | None]] | None = None,
    ):
        self.desktop_dir = resolve_claude_desktop_dir(desktop_dir)
        self.organization_id = string_or_none(organization_id)
        self.session_key = string_or_none(session_key)
        self.fetch_usage = fetch_usage or fetch_desktop_usage
        self.read_session_key = read_session_key or read_desktop_session_key
        self.fetch_oauth_usage = fetch_oauth_usage or fetch_claude_usage
        self.read_access_token = read_access_token or read_desktop_oauth_token
        self.access_token: str | None = None
        self.access_plan: str | None = None
        self.preferred_channel = COOKIE_CHANNEL

    def get_usage(self) -> UsageSnapshot:
        now = datetime.now().astimezone()
        source = "本机 Claude 桌面版"
        try:
            return self._fetch_snapshot(now, source)
        except ClaudeLoginError as exc:
            return empty_snapshot(source, now, str(exc))
        except requests.exceptions.HTTPError as exc:
            status_code = response_status(exc)
            if status_code in (401, 403):
                message = "Claude 桌面版登录已失效，请在桌面应用重新登录后刷新"
            elif status_code == 429:
                message = "Claude 用量服务请求过于频繁，请稍后刷新"
            else:
                message = f"Claude 用量服务返回 HTTP {status_code or '?'}"
            return empty_snapshot(
                source,
                now,
                message,
                retry_after_seconds=RATE_LIMIT_BACKOFF_SECONDS if status_code == 429 else None,
            )
        except requests.exceptions.SSLError:
            return empty_snapshot(source, now, "Claude 用量服务证书验证失败")
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout, TimeoutError):
            return empty_snapshot(source, now, "无法连接 Claude 用量服务")
        except requests.exceptions.RequestException:
            return empty_snapshot(source, now, "Claude 用量服务请求失败")
        except (OSError, sqlite3.Error, json.JSONDecodeError, TypeError, ValueError):
            return empty_snapshot(source, now, "无法读取有效的 Claude 订阅用量")

    def _fetch_snapshot(self, now: datetime, source: str) -> UsageSnapshot:
        """上次成功的链路优先；失败时在本次刷新内立即改走另一条，两条都失败才报错。

        若缓存 sessionKey 后只走 Cookie 链路，claude.ai 一旦限流或返回 5xx，
        卡片就会一直退回滞后的本地缓存，而 OAuth 链路其实可用。
        """
        channels = {COOKIE_CHANNEL: self._usage_via_cookie, OAUTH_CHANNEL: self._usage_via_oauth}
        errors: dict[str, Exception] = {}
        for name in sorted(channels, key=lambda channel: channel != self.preferred_channel):
            try:
                snapshot = channels[name](now)
            except LIVE_CHANNEL_ERRORS as exc:
                errors[name] = exc
                continue
            self.preferred_channel = name
            return replace(snapshot, source=source, updated_at=now)
        raise most_relevant_error([errors[name] for name in channels])

    def _usage_via_cookie(self, now: datetime) -> UsageSnapshot:
        """解密 sessionKey Cookie 查询 claude.ai；桌面版独占 Cookie 库时抛 ClaudeLoginError。"""
        if self.session_key is None:
            # 只保存在当前进程内；后续即使桌面版独占 Cookie 文件，也能继续查询。
            self.session_key = self.read_session_key(self.desktop_dir)
        try:
            payload, plan = self.fetch_usage(self.session_key, self.organization_id)
        except requests.exceptions.HTTPError as exc:
            if response_status(exc) in (401, 403):
                # 登录态被服务端拒绝后不再复用，下次重新读取桌面版登录态。
                self.session_key = None
            raise
        return replace(parse_claude_usage(payload, now), plan=plan)

    def _usage_via_oauth(self, now: datetime) -> UsageSnapshot:
        """用桌面版 config.json 里的 OAuth access token 查询 /api/oauth/usage。"""
        if self.access_token is None:
            self.access_token, self.access_plan = self.read_access_token(self.desktop_dir, now)
        try:
            payload = self.fetch_oauth_usage(self.access_token)
        except requests.exceptions.HTTPError as exc:
            if response_status(exc) in (401, 403):
                self.access_token = None
            raise
        return replace(parse_claude_usage(payload, now), plan=self.access_plan)


def response_status(exc: requests.exceptions.HTTPError) -> int:
    return exc.response.status_code if exc.response is not None else 0


def most_relevant_error(errors: list[Exception]) -> Exception:
    """两条链路都失败时挑最该展示的原因：限流优先（需要退避），登录态读不到排最后。

    同级时取 Cookie 链路的原因，例如两条都取不到登录态时提示“桌面版正在占用登录数据”。
    """

    def rank(exc: Exception) -> int:
        if isinstance(exc, requests.exceptions.HTTPError) and response_status(exc) == 429:
            return 0
        return 2 if isinstance(exc, ClaudeLoginError) else 1

    return min(errors, key=rank)


def resolve_claude_desktop_dir(desktop_dir: str | Path | None = None) -> Path:
    """按显式配置、CLAUDE_DESKTOP_DIR、自动探测的顺序解析桌面版数据目录。"""
    if desktop_dir:
        return Path(desktop_dir).expanduser()
    configured = os.environ.get("CLAUDE_DESKTOP_DIR", "").strip()
    if configured:
        return Path(configured).expanduser()
    candidates = claude_desktop_dir_candidates()
    for candidate in candidates:
        if find_cookie_databases(candidate):
            return candidate
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return candidates[0]


def claude_desktop_dir_candidates() -> list[Path]:
    r"""列出可能的桌面版数据目录：先常规安装，再 MSIX 包重定向目录。

    MSIX（Microsoft Store / WindowsApps）安装的桌面版对 %APPDATA% 做了容器重定向，
    包外进程看不到 %APPDATA%\Claude，真实数据在
    %LOCALAPPDATA%\Packages\Claude_<publisher>\LocalCache\Roaming\Claude。
    """
    appdata = os.environ.get("APPDATA", "").strip()
    roaming = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
    localappdata = os.environ.get("LOCALAPPDATA", "").strip()
    local = Path(localappdata) if localappdata else Path.home() / "AppData" / "Local"

    candidates = [roaming / "Claude"]
    packages = local / "Packages"
    try:
        package_dirs = sorted(packages.glob(MSIX_PACKAGE_PATTERN)) if packages.is_dir() else []
    except OSError:
        package_dirs = []
    for package in package_dirs:
        for subpath in MSIX_APPDATA_SUBPATHS:
            candidates.append(package.joinpath(*subpath, "Claude"))
    return unique_paths(candidates)


def find_cookie_databases(desktop_dir: Path) -> list[Path]:
    """列出桌面版数据目录下的 Chromium Cookie 库，已知位置优先。"""
    if not desktop_dir.is_dir():
        return []
    found: list[Path] = []
    seen: set[str] = set()

    def collect(path: Path) -> None:
        key = str(path).lower()
        if key in seen:
            return
        try:
            if not path.is_file():
                return
        except OSError:
            return
        seen.add(key)
        found.append(path)

    for relative in COOKIE_RELATIVE_PATHS:
        collect(desktop_dir.joinpath(*relative.split("/")))
    for pattern in COOKIE_GLOB_PATTERNS:
        try:
            for path in sorted(desktop_dir.glob(pattern)):
                collect(path)
        except OSError:
            continue
    for path in walk_cookie_databases(desktop_dir):
        collect(path)
    return found


def walk_cookie_databases(desktop_dir: Path) -> list[Path]:
    """限深遍历兜底查找 Cookie 库；跟随 junction，跳过大体积缓存目录。"""
    base_depth = len(desktop_dir.parts)
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(desktop_dir, followlinks=True):
        current = Path(dirpath)
        if len(current.parts) - base_depth >= COOKIE_SEARCH_DEPTH:
            dirnames[:] = []
        else:
            dirnames[:] = [name for name in dirnames if name.lower() not in COOKIE_SKIP_DIRS]
        for name in filenames:
            if name.lower() == "cookies":
                found.append(current / name)
    found.sort(key=lambda path: (len(path.parts), str(path)))
    return found


def unique_paths(paths: list[Path]) -> list[Path]:
    seen: set[str] = set()
    result: list[Path] = []
    for path in paths:
        key = str(path).lower()
        if key not in seen:
            seen.add(key)
            result.append(path)
    return result


def read_desktop_session_key(desktop_dir: Path) -> str:
    """从桌面版 Cookie 库中解密 claude.ai 的 sessionKey。"""
    databases = find_cookie_databases(desktop_dir)
    if not databases:
        raise ClaudeLoginError("未找到 Claude 桌面版的本地登录数据，请确认已安装并登录桌面版")
    master_key = load_os_crypt_key(desktop_dir)
    last_error: Exception | None = None
    database_locked = False
    for database in databases:
        try:
            for encrypted_value in query_session_cookies(database):
                value = decrypt_cookie_value(encrypted_value, master_key)
                if value:
                    return value
        except PermissionError as exc:
            database_locked = True
            last_error = exc
        except (OSError, sqlite3.Error, ValueError) as exc:
            last_error = exc
    if database_locked:
        raise ClaudeLoginError("Claude 桌面版正在占用登录数据，请先退出桌面版并刷新成功一次后再重新打开")
    if last_error is not None:
        raise ClaudeLoginError("无法解密 Claude 桌面版登录态，请确认使用当前 Windows 账户运行本程序")
    raise ClaudeLoginError("Claude 桌面版尚未登录，请先在桌面应用登录后刷新")


def read_desktop_oauth_token(desktop_dir: Path, now: datetime | None = None) -> tuple[str, str | None]:
    """从桌面版 config.json 的 OAuth 令牌缓存中解密出 access token 与订阅计划。

    桌面版把 Claude Code 的 OAuth 登录态加密写在 `config.json` 的 `oauth:tokenCacheV2`
    （或旧键 `oauth:tokenCache`）里，与 Cookie 共用同一 DPAPI 主密钥。这里只取其中
    未过期、含 `user:profile` 作用域的 access token，从不读取同条目里的 refreshToken。
    config.json 不被桌面版独占锁，因此桌面版运行时也能读到实时登录态。
    """
    current = now or datetime.now().astimezone()
    config_path = desktop_dir / DESKTOP_CONFIG_FILENAME
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ClaudeLoginError("未找到 Claude 桌面版配置，请确认已安装并登录桌面版")
    if not isinstance(config, Mapping):
        raise ClaudeLoginError("Claude 桌面版配置格式异常，请在桌面应用重新登录后刷新")
    token, plan = select_desktop_oauth_token(config, load_os_crypt_key(desktop_dir), current)
    if token is None:
        raise ClaudeLoginError("Claude 桌面版尚未登录或登录态无法读取，请在桌面应用重新登录后刷新")
    return token, plan


def select_desktop_oauth_token(
    config: Mapping[str, Any],
    master_key: bytes | None,
    now: datetime,
) -> tuple[str | None, str | None]:
    """挑选未过期且含 user:profile 作用域的 access token，取过期时间最晚的一个。"""
    best: tuple[datetime, str, str | None] | None = None
    for cache_key in DESKTOP_OAUTH_CACHE_KEYS:
        encoded = config.get(cache_key)
        if not isinstance(encoded, str) or not encoded:
            continue
        try:
            entries = json.loads(decrypt_cookie_value(base64.b64decode(encoded), master_key))
        except (ValueError, json.JSONDecodeError):
            continue
        if not isinstance(entries, Mapping):
            continue
        for scope_key, entry in entries.items():
            mapping = optional_mapping(entry)
            if mapping is None or DESKTOP_OAUTH_REQUIRED_SCOPE not in str(scope_key):
                continue
            token = string_or_none(mapping.get("token"))
            if not token:
                continue
            try:
                expires_at = parse_epoch_milliseconds(mapping.get("expiresAt"))
            except (TypeError, ValueError, OSError, OverflowError):
                continue
            if expires_at is not None and expires_at <= now:
                continue
            score = expires_at or now
            if best is None or score > best[0]:
                best = (score, token, string_or_none(mapping.get("subscriptionType")))
    return (best[1], best[2]) if best is not None else (None, None)


def query_session_cookies(database: Path) -> list[bytes]:
    """读取 claude.ai 的 sessionKey Cookie 密文，按过期时间从晚到早返回。"""
    with tempfile.TemporaryDirectory() as workspace:
        snapshot = Path(workspace) / "Cookies"
        shutil.copyfile(database, snapshot)
        for suffix in ("-wal", "-shm", "-journal"):
            sidecar = database.with_name(database.name + suffix)
            if sidecar.is_file():
                shutil.copyfile(sidecar, snapshot.with_name(snapshot.name + suffix))
        connection = sqlite3.connect(f"file:{snapshot}?mode=ro", uri=True)
        try:
            rows = connection.execute(
                "SELECT encrypted_value FROM cookies"
                " WHERE name = 'sessionKey' AND host_key LIKE '%claude.ai'"
                " ORDER BY expires_utc DESC"
            ).fetchall()
        finally:
            connection.close()
    return [row[0] for row in rows if row[0]]


def load_os_crypt_key(desktop_dir: Path) -> bytes | None:
    """读取 Local State 中的 Chromium 主密钥；旧格式返回 None 表示直接使用 DPAPI。"""
    state_path = desktop_dir / "Local State"
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    encrypted_key = optional_mapping(state.get("os_crypt")) or {}
    encoded = encrypted_key.get("encrypted_key")
    if not isinstance(encoded, str) or not encoded:
        return None
    blob = base64.b64decode(encoded)
    if not blob.startswith(DPAPI_KEY_PREFIX):
        return None
    return dpapi_unprotect(blob[len(DPAPI_KEY_PREFIX):])


def decrypt_cookie_value(encrypted_value: bytes, master_key: bytes | None) -> str:
    """解密 Chromium Cookie：v10/v11 走 AES-GCM，旧格式走 DPAPI。"""
    if encrypted_value[:3] in (b"v10", b"v11"):
        if master_key is None:
            raise ValueError("缺少 Chromium 主密钥")
        nonce = encrypted_value[3:15]
        payload = encrypted_value[15:]
        plaintext = AESGCM(master_key).decrypt(nonce, payload, None)
        return plaintext.decode("utf-8", errors="strict")
    return dpapi_unprotect(encrypted_value).decode("utf-8", errors="strict")


def dpapi_unprotect(blob: bytes) -> bytes:
    """调用 Windows DPAPI 解密当前用户加密的数据。"""
    if os.name != "nt":
        raise ValueError("DPAPI 仅在 Windows 可用")

    class DataBlob(ctypes.Structure):
        _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_char))]

    source = DataBlob(len(blob), ctypes.cast(ctypes.create_string_buffer(blob, len(blob)), ctypes.POINTER(ctypes.c_char)))
    target = DataBlob()
    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(source), None, None, None, None, 0, ctypes.byref(target)
    )
    if not ok:
        raise ValueError("DPAPI 解密失败")
    try:
        return ctypes.string_at(target.pbData, target.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(target.pbData)


def desktop_headers(session_key: str) -> dict[str, str]:
    return {
        "Cookie": f"sessionKey={session_key}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": APP_USER_AGENT,
        "Referer": f"{CLAUDE_AI_BASE}/settings/usage",
        "Origin": CLAUDE_AI_BASE,
        "anthropic-client-platform": "web_claude_ai",
    }


def fetch_desktop_usage(
    session_key: str,
    organization_id: str | None = None,
    http_get: Callable[..., Any] = requests.get,
) -> tuple[Mapping[str, Any], str | None]:
    """查询 claude.ai 的订阅用量，返回响应与订阅计划名。"""
    headers = desktop_headers(session_key)
    plan: str | None = None
    if not organization_id:
        organization_id, plan = resolve_organization(headers, http_get)
    response = http_get(
        f"{CLAUDE_AI_BASE}/api/organizations/{organization_id}/usage",
        headers=headers,
        timeout=10,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, Mapping):
        raise ValueError("Claude 用量响应格式错误")
    return payload, plan


def resolve_organization(
    headers: Mapping[str, str],
    http_get: Callable[..., Any] = requests.get,
) -> tuple[str, str | None]:
    """取当前账户可用的组织及订阅计划名。"""
    response = http_get(f"{CLAUDE_AI_BASE}/api/organizations", headers=dict(headers), timeout=10)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list) or not payload:
        raise ClaudeLoginError("Claude 桌面版尚未登录，请先在桌面应用登录后刷新")
    chosen = next(
        (item for item in payload if isinstance(item, Mapping) and "chat" in capability_list(item)),
        payload[0],
    )
    organization = expect_mapping(chosen, "Claude 组织信息格式错误")
    organization_id = string_or_none(organization.get("uuid"))
    if not organization_id:
        raise ValueError("Claude 组织信息缺少 uuid")
    return organization_id, plan_from_capabilities(capability_list(organization))


def capability_list(organization: Mapping[str, Any]) -> list[str]:
    capabilities = organization.get("capabilities")
    return [item for item in capabilities if isinstance(item, str)] if isinstance(capabilities, list) else []


def plan_from_capabilities(capabilities: list[str]) -> str | None:
    for capability, plan in PLAN_CAPABILITIES:
        if capability in capabilities:
            return plan
    return None


def resolve_claude_credentials_path(credentials_path: str | Path | None = None) -> Path:
    """按显式配置、CLAUDE_CONFIG_DIR、默认目录的顺序解析凭据文件。"""
    if credentials_path:
        return Path(credentials_path).expanduser()
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    if config_dir:
        return Path(config_dir).expanduser() / ".credentials.json"
    return Path.home() / ".claude" / ".credentials.json"


def fetch_claude_usage(
    access_token: str,
    http_get: Callable[..., Any] = requests.get,
) -> Mapping[str, Any]:
    """使用 OAuth access token 查询订阅额度。"""
    response = http_get(
        USAGE_URL,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "User-Agent": APP_USER_AGENT,
            "anthropic-beta": OAUTH_BETA,
        },
        timeout=10,
    )
    response.raise_for_status()
    try:
        payload = response.json()
    except ValueError as exc:
        raise ValueError("Claude 用量响应不是有效 JSON") from exc
    if not isinstance(payload, Mapping):
        raise ValueError("Claude 用量响应格式错误")
    return payload


def read_json_retrying(path: Path, attempts: int = 3) -> Any:
    """读取 JSON；桌面版正在改写时可能读到半个文件，重试几次。"""
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise
        except (OSError, json.JSONDecodeError) as exc:
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(0.2)
    raise last_error if last_error else ValueError("读取失败")


def history_samples(payload: Any) -> list[tuple[datetime, float | None, float | None]]:
    """把 plan-usage-history.json 的样本整理成 (时间, 5 小时百分比, 7 天百分比)，按时间升序。"""
    mapping = optional_mapping(payload)
    samples = mapping.get("samples") if mapping is not None else None
    if not isinstance(samples, list):
        raise ValueError("桌面版用量缓存缺少 samples")
    parsed: list[tuple[datetime, float | None, float | None]] = []
    for sample in samples:
        entry = optional_mapping(sample)
        usage = optional_mapping(entry.get("u")) if entry is not None else None
        if entry is None or usage is None:
            continue
        try:
            sampled_at = parse_epoch_milliseconds(entry.get("t"))
            five_hour = optional_percent(usage.get("fh"))
            weekly = optional_percent(usage.get("sd"))
        except (TypeError, ValueError, OSError, OverflowError):
            continue
        if sampled_at is None:
            continue
        parsed.append((sampled_at, five_hour, weekly))
    parsed.sort(key=lambda item: item[0])
    return parsed


def parse_plan_usage_history(payload: Any, source: str, now: datetime) -> UsageSnapshot:
    samples = history_samples(payload)
    if not samples:
        raise ValueError("桌面版用量缓存里没有可用样本")
    sampled_at, five_hour, weekly = samples[-1]
    if five_hour is None and weekly is None:
        raise ValueError("桌面版用量缓存里没有可用样本")
    message = ""
    if now - sampled_at > HISTORY_STALE_AFTER:
        message = f"桌面版未在运行，数据截至 {sampled_at:%m-%d %H:%M}"
    return UsageSnapshot(
        five_hour,
        five_hour_reset_from_history(samples, now),
        weekly,
        weekly_reset_from_history(samples, now),
        source,
        sampled_at,
        message,
    )


def window_start_from_history(values: list[tuple[datetime, float]]) -> datetime | None:
    """按"上一条更高、这一条更低"找出当前窗口起点样本；找不到边界返回 None。

    窗口内的已用百分比只增不减，回落即意味着上一个窗口已重置，
    该条样本就是新窗口里的首次使用记录。
    """
    for index in range(len(values) - 1, 0, -1):
        previous, current = values[index - 1][1], values[index][1]
        if current < previous or previous == 0:
            return values[index][0]
    return None


def five_hour_reset_from_history(
    samples: list[tuple[datetime, float | None, float | None]],
    now: datetime,
) -> datetime | None:
    """按 5 小时百分比的回落点推算当前窗口的重置时间；推不出来就返回 None。

    缓存里没有 resets_at，只能靠回落点识别窗口边界：边界样本即新窗口的首次使用时间，
    加 5 小时得到重置时间。采样间隔约 5 分钟，因此结果是近似值；
    缓存里没观测到边界时退回最早一条样本，推算结果若已过期则视为不可靠。
    """
    values = [(sampled_at, percent) for sampled_at, percent, _ in samples if percent is not None]
    if not values or not values[-1][1]:
        return None
    start = window_start_from_history(values) or values[0][0]
    reset_at = start + FIVE_HOUR_WINDOW
    return reset_at if reset_at > now else None


def weekly_reset_from_history(
    samples: list[tuple[datetime, float | None, float | None]],
    now: datetime,
) -> datetime | None:
    """按 7 天百分比的回落点推算每周窗口的重置时间；观测不到边界就返回 None。

    与 5 小时不同：7 天窗口远长于缓存常见留存时间，把最早一条样本当成窗口起点
    会给出明显错误的时间，因此这里只认真正观测到的回落边界。
    桌面版设置页展示的每周重置落在整点（如 `Resets Tue 9:00 AM`），
    而样本间隔约 5 分钟，故推算结果按最近整点取整。
    """
    values = [(sampled_at, percent) for sampled_at, _, percent in samples if percent is not None]
    if not values or not values[-1][1]:
        return None
    start = window_start_from_history(values)
    if start is None:
        return None
    reset_at = round_to_hour(start + SEVEN_DAY_WINDOW)
    return reset_at if reset_at > now else None


def round_to_hour(moment: datetime) -> datetime:
    floor = moment.replace(minute=0, second=0, microsecond=0)
    return floor + timedelta(hours=1) if moment - floor >= timedelta(minutes=30) else floor


def parse_claude_usage(payload: Mapping[str, Any], now: datetime | None = None) -> UsageSnapshot:
    """解析 Claude OAuth usage 响应中的 5 小时与 7 天总额度。"""
    current = now or datetime.now().astimezone()
    five_hour = optional_mapping(payload.get("five_hour"))
    weekly = optional_mapping(payload.get("seven_day"))
    if five_hour is None:
        five_hour = quota_window_from_limits(payload, "session")
    if weekly is None:
        weekly = quota_window_from_limits(payload, "weekly_all")
    if five_hour is None and weekly is None:
        raise ValueError("Claude 用量响应中没有 five_hour 或 seven_day")
    return UsageSnapshot(
        window_utilization(five_hour),
        window_reset_at(five_hour),
        window_utilization(weekly),
        window_reset_at(weekly),
        "Claude 用量服务",
        current,
    )


def quota_window_from_limits(payload: Mapping[str, Any], kind: str) -> Mapping[str, Any] | None:
    """从新版 limits 数组提取非模型级会话或周额度。"""
    limits = payload.get("limits")
    if not isinstance(limits, list):
        return None
    for entry in limits:
        if not isinstance(entry, Mapping) or entry.get("kind") != kind:
            continue
        scope = optional_mapping(entry.get("scope"))
        if scope is not None and optional_mapping(scope.get("model")) is not None:
            continue
        if entry.get("percent") is None:
            continue
        return {
            "utilization": entry.get("percent"),
            "resets_at": entry.get("resets_at"),
        }
    return None


def parse_exported_usage(payload: Mapping[str, Any], source: str, now: datetime) -> UsageSnapshot:
    if (
        isinstance(payload.get("five_hour"), Mapping)
        or isinstance(payload.get("seven_day"), Mapping)
        or isinstance(payload.get("limits"), list)
    ):
        parsed = parse_claude_usage(payload, now)
        return UsageSnapshot(
            parsed.five_hour_used_percent,
            parsed.five_hour_reset_at,
            parsed.weekly_used_percent,
            parsed.weekly_reset_at,
            source,
            now,
            str(payload.get("message", "")),
            string_or_none(payload.get("plan")),
        )
    return UsageSnapshot(
        optional_percent(payload.get("five_hour_used_percent")),
        parse_datetime(payload.get("five_hour_reset_at")),
        optional_percent(payload.get("weekly_used_percent")),
        parse_datetime(payload.get("weekly_reset_at")),
        source,
        now,
        str(payload.get("message", "")),
        string_or_none(payload.get("plan")),
    )


def empty_snapshot(
    source: str,
    now: datetime,
    message: str,
    retry_after_seconds: int | None = None,
) -> UsageSnapshot:
    return UsageSnapshot(
        None,
        None,
        None,
        None,
        source,
        now,
        message,
        retry_after_seconds=retry_after_seconds,
    )


def expect_mapping(value: Any, message: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(message)
    return value


def optional_mapping(value: Any) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None


def window_utilization(window: Mapping[str, Any] | None) -> float | None:
    if window is None or window.get("utilization") is None:
        return None
    return clamp_percent(window["utilization"])


def window_reset_at(window: Mapping[str, Any] | None) -> datetime | None:
    return None if window is None else parse_datetime(window.get("resets_at"))


def optional_percent(value: Any) -> float | None:
    return None if value is None or value == "" else clamp_percent(value)


def clamp_percent(value: Any) -> float:
    number = float(value)
    if not 0 <= number <= 100:
        raise ValueError("百分比必须在 0 到 100 之间")
    return number


def display_percent(value: float | None) -> int | None:
    return None if value is None else round(max(0.0, min(100.0, value)))


def parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return parsed.astimezone()


def parse_epoch_milliseconds(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    return datetime.fromtimestamp(float(value) / 1000).astimezone()


def string_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def create_provider(config: Mapping[str, Any]) -> UsageProvider:
    provider_type = config.get("provider", "claude_desktop")
    if provider_type == "manual":
        return ManualUsageProvider(
            config.get("manual_five_hour_used"),
            config.get("manual_weekly_used"),
            config.get("manual_five_hour_reset_at"),
            config.get("manual_weekly_reset_at"),
        )
    if provider_type == "json_file":
        return JsonFileUsageProvider(config.get("json_path", "sample_usage.json"))
    if provider_type == "mock":
        return MockUsageProvider()
    if provider_type == "claude_local":
        return ClaudeLocalUsageProvider(config.get("claude_credentials_path") or None)
    desktop_dir = config.get("claude_desktop_dir") or None
    history = ClaudeDesktopHistoryUsageProvider(desktop_dir, config.get("claude_history_path") or None)
    if provider_type == "claude_desktop_history":
        return history
    cookie = ClaudeDesktopUsageProvider(desktop_dir, config.get("claude_organization_id") or None)
    if provider_type == "claude_desktop_cookie":
        return cookie
    # 默认：先走 Cookie 链路查接口（与桌面版设置页同源，含重置时间和订阅计划，实时）；
    # 接口不可用时再退回本地缓存（无需登录态，但滞后于桌面版且缺重置时间）。
    return FallbackUsageProvider([cookie, history])
