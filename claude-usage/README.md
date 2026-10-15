# Claude Usage Banana

Windows 桌面订阅用量组件：显示 Claude 当前 5 小时会话和 7 天全部模型额度的已使用比例、重置时间，并提供系统托盘刷新与退出入口。

![Claude 用量悬浮卡片](screenshot.png)

## 使用条件

- Windows 10/11、Python 3.12；
- 已安装并登录 **Claude 桌面版（Claude Desktop）**，账户为 Claude Pro、Max、Team 或 Enterprise 订阅；
- **无需安装 Claude Code CLI。** 默认数据源直接读取桌面版的本机登录态。

> 默认数据源**先走接口**：解密桌面版的 `sessionKey` Cookie 并向
> `https://claude.ai/api/organizations/{org}/usage` 发起只读查询，
> 这与桌面版设置页同源，含 5 小时与每周的重置时间和订阅计划，且是实时值。
> 首次读取成功后，解密得到的 `sessionKey` 仅缓存在当前进程内，避免 Claude Desktop
> 后续独占 Cookie 数据库时实时链路退回本地缓存；应用退出或服务端返回 401/403 时即清除。
> **桌面版运行时会独占锁住 Cookie 库**，若本程序尚未缓存到 `sessionKey`（例如桌面版
> 一直开着就启动本程序），Cookie 链路读不到登录态；此时自动改从桌面版 `config.json`
> 的 OAuth 令牌缓存里只解密 **access token**，查询 `https://api.anthropic.com/api/oauth/usage`
> （与 Claude Code 同端点，同样含重置时间与订阅计划）。`config.json` 不被独占锁，
> 因此桌面版开着也能实时取数——这正是“桌面版在运行却拿不到实时用量”的修复路径。
> 两条链路**互为备援**：上一次成功的链路优先，任一条失败（Cookie 库被锁、限流、5xx、
> 令牌被拒等）都会在**同一次刷新内**改走另一条，不必等下次刷新或手动刷新；
> 被服务端以 401/403 拒绝的令牌只清除所属链路的那一个。
> 两条链路都只取登录令牌用于只读查询，仅缓存在进程内；OAuth 缓存里同时含 `refreshToken`，
> 本程序只读 access token，从不读取或刷新它。
> 两条接口都不可用（未登录、登录过期、限流、断网）时，才回退到**纯本地只读**的
> 用量缓存 `plan-usage-history.json`：不需要登录态、不发网络请求，
> 代价是滞后于桌面版（桌面版没开时还会停更），且缓存里没有重置时间。
> 本程序不会保存、刷新或显示任何登录令牌。若你更习惯 Claude Code CLI，
> 可将 `provider` 改为 `claude_local`，改读 `~/.claude/.credentials.json` 并查询 `api.anthropic.com`。

> **桌面版数据目录**：MSIX（Microsoft Store / WindowsApps）安装的桌面版会把 `%APPDATA%` 重定向到
> `%LOCALAPPDATA%\Packages\Claude_<publisher>\LocalCache\Roaming\Claude`，包外进程看不到
> `%APPDATA%\Claude`。程序会自动探测这两类路径，也可用 `CLAUDE_DESKTOP_DIR` 或
> `claude_desktop_dir` 显式指定。

## 安装与启动

在 `claude-usage` 目录执行：

```powershell
python -m pip install -r requirements.txt
.\run.bat
```

悬浮卡片默认显示在主屏幕右下角。左键拖动可调整位置；关闭卡片只会隐藏窗口；单击托盘香蕉图标可恢复，右键可“立即刷新”或“退出”。

若提示未登录或登录过期，请先打开 Claude 桌面版并重新登录，再点托盘“立即刷新”。

## 数据说明

- 默认数据源（接口）：“当前会话”优先读 `five_hour.utilization`，缺失时读
  `limits[kind=session].percent`；“每周 · 全部模型”优先读 `seven_day.utilization`，
  缺失时读 `limits[kind=weekly_all].percent`；两项的 `resets_at` 即卡片上的重置时间。
- 回退数据源（本地缓存）：`plan-usage-history.json` 的最后一条样本，
  `u.fh` 对应“当前会话”，`u.sd` 对应“每周 · 全部模型”。
  该文件由桌面版运行时约每 5 分钟写入一条，**桌面版没开时数据会停更**，
  此时卡片显示“桌面版未在运行，数据截至 …”；即便桌面版在运行，
  它也天然滞后于桌面版设置页里的实时数字。
- 缓存里没有重置时间，只能按“已用百分比回落”识别窗口边界推算：
  5 小时按边界 + 5 小时；7 天按边界 + 7 天并取整到整点（桌面版重置时间落在整点），
  且**只有真正观测到回落才推算**——缓存留存不足 7 天时留空显示“待数据源提供”，
  避免把最早一条样本误当窗口起点。订阅计划名同样只有走接口时才有。
- 若某个数据源只缺重置时间或订阅计划，会由后一个数据源补齐这两项，使用量本身不被覆盖。
- 卡片不再常驻显示“更新于”，把鼠标停在卡片上可看到当前数据源和更新时间；
  只有出现异常时才会多出一行状态提示。
- 两项使用量在 Claude、Claude Desktop 与 Claude Code 间共享，具体适用窗口由订阅计划决定。
- `refresh_seconds` 默认配置为 30 秒，自动刷新统一执行 60 秒下限；回退到本地缓存时同样
  每 60 秒重读。旧版本自动写入的默认 300 秒会迁移为新默认值；若遇 HTTP 429，
  下一次刷新自动退避到 300 秒。

桌面版与 Claude Code 使用的用量端点都不是 Anthropic 对第三方承诺稳定的公共 API，字段或限流策略可能变化。应用会在失败时显示原因；本次运行已有成功结果时，会继续显示最近一次成功数据。

网络请求使用 Windows 系统证书存储，并以本程序自己的 User-Agent `claude-usage/1.0` 发出，不冒充桌面版或 Claude Code。程序不会刷新或写回任何登录凭据，登录过期时请在对应客户端（默认桌面版）重新登录。

## 可选数据源

默认配置保存在 `%APPDATA%\ClaudeUsage\settings.json`，不会包含任何登录令牌。可将 `provider` 改为：

- `claude_desktop`（默认）：先走 Cookie 查询 `claude.ai`；Cookie 库被桌面版独占锁住或该链路
  请求失败时，同一次刷新内改用桌面版 `config.json` 的 OAuth access token 查询 `api.anthropic.com`
  （反之亦然，上次成功的链路优先）；两条接口都不可用时再读桌面版本地用量缓存。可选 `claude_desktop_dir` 指定桌面版数据目录、
  `claude_history_path` 指定缓存文件、`claude_organization_id` 指定组织；
- `claude_desktop_history`：只读本地缓存，永不联网（会没有每周重置时间和订阅计划）；
- `claude_desktop_cookie`：只走 Cookie + 接口；桌面版运行时可能占用 Cookie 库，
  若应用尚未缓存登录态，会提示先退出桌面版并成功刷新一次；也可执行
  `python diagnose_desktop.py` 定位 Cookie 链路失败原因（只读；只列出 claude.ai、claude.com、
  anthropic.com 的 Cookie 名称与密文长度，不打印令牌明文，路径中的 Windows 用户名替换为 `<user>`）；
- `claude_local`：读取 Claude Code 的 `~/.claude/.credentials.json` 查询 `api.anthropic.com`；
- `mock`：演示数据；
- `json_file`：读取 `json_path` 指定的 JSON；
- `manual`：读取 `manual_five_hour_used`、`manual_weekly_used` 及对应重置时间。

原始 Claude 响应格式示例见 `sample_usage.json`。

## 开机自启

```powershell
powershell -ExecutionPolicy Bypass -File .\install_autostart.ps1
```

取消自启时追加 `-Uninstall`。脚本只修改当前用户的 `HKCU` 开机启动项。

## 开发验证

在 `claude-usage` 目录运行单元测试：

```powershell
python -m unittest discover -s tests -v
```
