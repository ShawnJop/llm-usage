# Claude Usage Banana PRD

## 1. 目标

为 Windows 上的 Claude 订阅用户提供常驻桌面卡片，快速查看 5 小时会话及每周全部模型的已使用比例和重置时间，减少频繁打开 Claude 设置页的成本。

依据：参考截图包含 `Current session` 与 `Weekly limits / All models`；`app.py` 的 `UsageWindow` 对应展示两组独立额度。

## 2. 范围

- 置顶、无边框、不抢焦点的悬浮卡片；
- 系统托盘刷新、恢复与退出；
- 默认读取 Claude 桌面版本机登录态并查询订阅用量；可选 Claude Code OAuth 数据源；
- 演示、手动和 JSON 调试数据源；
- 窗口位置保存及当前用户开机自启。

不包含多账户、历史图表、云端同步、API 计费金额或 Token 统计。

## 3. 功能需求

### 3.1 展示

- 当前会话：展示 `five_hour` 已使用百分比及重置时间。
- 每周 · 全部模型：展示 `seven_day` 已使用百分比及重置时间。
- 展示订阅类型（本地凭据提供时）；数据源与最后更新时间放在卡片悬浮提示里，不占正文行。
- 状态行只在异常时出现，并按 `STATUS_ROW_HEIGHT` 临时加高卡片；正常刷新成功后隐藏。
- 数据获取失败时显示可读状态；已有成功数据时保留最近一次结果。

依据：`UsageWindow.render()`、`UsageWindow.set_status()`、`keep_last_successful_snapshot()`。

### 3.2 数据源

- 默认数据源 `claude_desktop`：从 `%APPDATA%\Claude`（可由 `claude_desktop_dir`/`CLAUDE_DESKTOP_DIR` 覆盖）读取 Chromium Cookie 库，用 DPAPI + AES-GCM 解密 claude.ai 的 `sessionKey`，先经 `/api/organizations` 解析组织与订阅计划，再向 `https://claude.ai/api/organizations/{org}/usage` 发起 GET 请求。成功读取的 `sessionKey` 仅在当前数据源实例内复用，避免桌面版独占 Cookie 数据库后实时链路中断。
- 桌面版运行时会独占锁住 Cookie 库，若尚未缓存到 `sessionKey`，`claude_desktop` 自动改从桌面版 `config.json` 的 OAuth 令牌缓存（`oauth:tokenCacheV2`/`oauth:tokenCache`，与 Cookie 共用同一 DPAPI 主密钥）中只解密 access token，查询 `https://api.anthropic.com/api/oauth/usage`；该端点与 Cookie 链路一致地提供 5 小时/每周用量、重置时间与订阅计划。只选取含 `user:profile` 作用域且未过期的 access token，access token 仅在实例内复用。
- Cookie 与 OAuth 两条接口链路互为备援：上次成功的链路优先；任一条失败（锁库、429、5xx、网络、响应格式、401/403）都在同一次刷新内改走另一条，只有两条都失败才回退本地缓存。两条都失败时优先回报 429（保留退避），登录态读不到的原因排最后。401/403 只清除被拒链路的 `sessionKey` 或 access token。
- 可选数据源 `claude_local`：按 `claude_credentials_path`、`CLAUDE_CONFIG_DIR/.credentials.json`、`~/.claude/.credentials.json` 顺序读取 `claudeAiOauth.accessToken`，仅向 `https://api.anthropic.com/api/oauth/usage` 发起 GET 请求。
- 两个数据源统一解析 `five_hour`、`seven_day`；字段缺失时兼容新版 `limits` 中的 `session`、`weekly_all` 窗口。
- 可选数据源 `claude_desktop_history`：只读桌面版本地缓存 `plan-usage-history.json`，
  作为接口不可用时的回退。缓存无重置时间，按已用百分比回落点推算：
  5 小时取边界 + 5 小时；7 天取边界 + 7 天并取整到整点，且必须真正观测到回落，
  否则留空，避免误把最早一条样本当作窗口起点。
- 默认 `claude_desktop` 为“接口优先、缓存兜底”的组合；前一个数据源只缺重置时间或
  订阅计划时，由后一个数据源补齐这两项，使用量不被覆盖。
- 刷新节奏：`refresh_seconds` 默认值为 30 秒，接口与本地缓存统一执行 60 秒下限；HTTP 429 后下一次刷新退避到 300 秒。
- 401/403 清除内存中被拒链路的登录令牌，两条链路都失败时提示重新登录；429 提示稍后刷新，网络和响应错误不得导致应用崩溃。
- Cookie 数据库被桌面版占用时应明确提示文件占用，不得误报为解密失败。
- 旧配置中的默认 `claude_local` 会在加载时迁移为 `claude_desktop`（未显式设置 `claude_credentials_path` 时）。

依据：`ClaudeDesktopUsageProvider._fetch_snapshot()`、`most_relevant_error()`、`read_desktop_oauth_token()`、`select_desktop_oauth_token()`、
`ClaudeLocalUsageProvider`、`ClaudeDesktopHistoryUsageProvider`、
`FallbackUsageProvider`、`weekly_reset_from_history()`、`parse_claude_usage()`、
`app.load_config()`、`app.refresh_interval_seconds()`。

## 4. 安全与限制

- 登录令牌（OAuth Token / sessionKey）不进入配置、日志、测试样本或界面；解密后的 sessionKey 与 access token 仅存在于当前进程内，应用退出或服务端返回 401/403 时清除。
- Cookie 链路只解密 `sessionKey` 一项 Cookie；OAuth 链路只从 `config.json` 令牌缓存里读取 access token，不读取同一条目里的 `refreshToken`，也从不刷新或写回任何令牌。两条链路都不读取其它 Cookie、桌面版会话内容或本地对话记录。
- 所有网络请求使用本程序自己的 User-Agent `claude-usage/1.0`，不冒充 Claude 桌面版或 Claude Code，也不调用本机 `claude` CLI 探测版本。
- 诊断脚本 `diagnose_desktop.py` 只读，只列出 claude.ai、claude.com、anthropic.com 的 Cookie 名称与密文长度，不读取其它站点的 Cookie；输出中路径里的 Windows 用户名替换为 `<user>`。
- 不刷新或写回任何登录凭据，登录过期时提示用户在对应客户端（默认桌面版）重新登录。
- 桌面版与 OAuth usage 端点均未公开承诺兼容性，因此自动刷新执行 60 秒下限，HTTP 429 时退避到 300 秒，并明确提示接口变更风险。
- API key 计费用户不属于订阅额度监控范围。

## 5. 验收标准

- Claude 桌面版已登录时可显示服务返回的 5 小时和 7 天用量及各自重置时间（无需 Claude Code CLI）；桌面版正在运行、独占 Cookie 库时，经 OAuth 令牌链路仍能取到实时用量，而非回退到滞后的本地缓存。
- 接口不可用时回退本地缓存仍能显示用量；每周重置时间推不出来时显示“待数据源提供”，不显示错误时间。
- 未登录、登录过期、HTTP 429、网络故障及响应格式变化均有明确反馈。
- 拖动位置可恢复，托盘菜单仅包含“立即刷新”和“退出”。
- `python -m unittest discover -s tests -v` 应全部通过。
