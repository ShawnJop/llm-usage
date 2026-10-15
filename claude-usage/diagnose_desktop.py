"""Claude 桌面版登录态定位诊断脚本（只读，不打印任何令牌明文）。

用法：在 claude-usage 目录执行  python diagnose_desktop.py
输出用于定位 "未找到 Claude 桌面版的本地登录数据" 的原因：只列出 claude.ai / claude.com /
anthropic.com 的 Cookie 名称与长度，路径中的 Windows 用户名替换为 <user>。
"""

from __future__ import annotations

import os
import re
import sqlite3
import shutil
import tempfile
from pathlib import Path

import usage_provider as up

CLAUDE_COOKIE_DOMAINS = ("claude.ai", "claude.com", "anthropic.com")


def redact(text: str, home: Path | None = None, username: str | None = None) -> str:
    """把路径里的 Windows 用户名替换为 <user>，便于公开粘贴诊断输出。"""
    home = Path.home() if home is None else home
    username = os.environ.get("USERNAME", "") if username is None else username
    names = sorted({name for name in (home.name, username) if name}, key=len, reverse=True)
    if not names:
        return text
    # 分隔符允许连续出现，以覆盖列表 repr 里被转义成 "\\" 的路径。
    home_parent = r"[\\/]+".join(re.escape(part) for part in re.split(r"[\\/]", str(home.parent)) if part)
    prefixes = "|".join(prefix for prefix in (home_parent, r"[A-Za-z]:[\\/]+Users") if prefix)
    names_pattern = "|".join(re.escape(name) for name in names)
    pattern = rf"((?:{prefixes})[\\/]+)(?:{names_pattern})(?!\w)"
    return re.sub(pattern, r"\1<user>", text, flags=re.IGNORECASE)


def emit(text: str = "") -> None:
    print(redact(text))


def read_claude_cookie_rows(database: Path) -> list[tuple[str, str, int, int]]:
    """只读快照 Cookie 库，返回 Claude 相关条目的域名、名称、密文长度与过期时间。"""
    conditions = " OR ".join("host_key = ? OR host_key LIKE ?" for _ in CLAUDE_COOKIE_DOMAINS)
    params = [value for domain in CLAUDE_COOKIE_DOMAINS for value in (domain, f"%.{domain}")]
    with tempfile.TemporaryDirectory() as workspace:
        snapshot = Path(workspace) / "Cookies"
        shutil.copyfile(database, snapshot)
        for suffix in ("-wal", "-shm", "-journal"):
            side = database.with_name(database.name + suffix)
            if side.is_file():
                shutil.copyfile(side, snapshot.with_name(snapshot.name + suffix))
        conn = sqlite3.connect(f"file:{snapshot}?mode=ro", uri=True)
        try:
            return conn.execute(
                "SELECT host_key, name, length(encrypted_value), expires_utc FROM cookies"
                f" WHERE {conditions} ORDER BY expires_utc DESC",
                params,
            ).fetchall()
        finally:
            conn.close()


def scan(root: Path) -> None:
    emit(f"\n=== 扫描 {root} ===")
    if not root.exists():
        emit("  目录不存在")
        return
    emit(f"  是目录: {root.is_dir()}  是链接/junction: {root.is_symlink()}")
    try:
        top = sorted(p.name + ("/" if p.is_dir() else "") for p in root.iterdir())
        emit(f"  一级条目({len(top)}): {top}")
    except OSError as exc:
        emit(f"  无法列出: {exc}")
        return

    hits: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=True):
        for name in filenames:
            if name.lower() in ("cookies", "local state"):
                hits.append(Path(dirpath) / name)
    emit(f"  os.walk 命中: {[str(p) for p in hits]}")
    emit(f"  provider.find_cookie_databases: {[str(p) for p in up.find_cookie_databases(root)]}")

    for db in [p for p in hits if p.name.lower() == "cookies"]:
        dump_cookie_db(db, root)


def dump_cookie_db(database: Path, root: Path) -> None:
    emit(f"\n  --- Cookie 库 {database} ({database.stat().st_size} B) ---")
    try:
        rows = read_claude_cookie_rows(database)
    except (OSError, sqlite3.Error) as exc:
        emit(f"    读取失败: {exc}")
        return

    emit(f"    Claude 相关 cookie 数: {len(rows)}（其它站点的条目不读取）")
    for host, name, size, expires in rows[:40]:
        emit(f"      {host} | {name} | enc_len={size} | expires_utc={expires}")

    session_rows = [r for r in rows if r[1] == "sessionKey"]
    emit(f"    sessionKey 条数: {len(session_rows)}")
    if not session_rows:
        return
    try:
        key = up.load_os_crypt_key(root)
        emit(f"    主密钥: {'已取得' if key else '无（走 DPAPI 旧格式）'}")
        values = up.query_session_cookies(database)
        emit(f"    query_session_cookies 返回: {len(values)} 条")
        if values:
            plain = up.decrypt_cookie_value(values[0], key)
            emit(f"    解密成功，长度={len(plain)}（不打印内容）")
    except Exception as exc:  # noqa: BLE001 - 诊断脚本需要看到全部异常
        emit(f"    解密失败: {type(exc).__name__}: {exc}")


def main() -> None:
    emit(f"Python: {os.sys.version}")
    emit(f"APPDATA={os.environ.get('APPDATA')}")
    emit(f"LOCALAPPDATA={os.environ.get('LOCALAPPDATA')}")
    emit(f"CLAUDE_DESKTOP_DIR={os.environ.get('CLAUDE_DESKTOP_DIR')}")
    emit(f"provider 解析出的目录: {up.resolve_claude_desktop_dir()}")

    roots = [
        up.resolve_claude_desktop_dir(),
        *up.claude_desktop_dir_candidates(),
        Path(os.environ.get("LOCALAPPDATA", "")) / "Claude",
        Path.home() / ".claude",
    ]
    seen: set[str] = set()
    for root in roots:
        key = str(root).lower()
        if key in seen or not str(root).strip():
            continue
        seen.add(key)
        scan(root)


if __name__ == "__main__":
    main()
