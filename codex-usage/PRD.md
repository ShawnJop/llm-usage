# Codex Usage Dragonfruit PRD

## 1. 背景

Codex 用户需要随时了解 5 小时与 weekly 额度剩余量和预计重置时间，减少在工作中频繁切换页面或等待额度错误提示的成本。本项目以 Windows 常驻小组件形式提供轻量提醒。

依据：`app.py` 中 `UsageCat` 创建置顶无边框窗口和系统托盘；`usage_provider.py` 中 `CodexLocalUsageProvider` 读取本地 Codex 登录状态。

## 2. 产品目标

- 让用户在桌面右下角快速看到 Codex 5 小时与周额度剩余百分比。
- 在不泄露 token 的前提下支持本地 Codex 登录状态取数。
- 保持安装、运行和自启配置简单。

## 3. 目标用户

- 在 Windows 上使用 Codex 的个人开发者。
- 需要观察 Codex weekly 使用量的重度用户。
- 需要通过本地 JSON 或手动输入聚合额度的用户。

## 4. 范围

### 4.1 范围内

- Windows 桌面悬浮窗。
- 系统托盘菜单。
- 本地 Codex 数据源与刷新。
- 当前用户开机自启脚本。
- 数据源解析和 weekly 窗口选择的单元测试。
- README、AGENTS 和 PRD 文档。

### 4.2 范围外

- 跨平台桌面适配。
- 多账户管理。
- 历史用量图表。
- 云端同步。
- 任务服务器或第三方代理服务。

## 5. 用户场景

- 用户首次启动程序后，在右下角看到 Codex weekly 剩余额度；拖动后的窗口位置会在下次启动时恢复。
- 用户右键托盘图标，选择“立即刷新”获取最新数据。
- 用户通过 PowerShell 脚本设置或移除当前用户开机自启。

## 6. 功能需求

### 6.1 悬浮窗展示

- 固定尺寸为 240 x 152；标题与首组额度间距为 8 px，两组额度行的上下边距均为 7 px，卡片顶部与底部留白一致。
- 默认移动到主屏幕右下角。
- 窗口置顶、无边框、不抢焦点；每次显示时在 Windows 原生置顶层重新确认位置，避免被普通窗口覆盖。
- 上下分别显示 5 小时与周额度；每组均含剩余百分比、独立进度条和重置时间。接口未提供 5 小时窗口时，该组显示“暂无数据”。

依据：`WIDGET_SIZE`、`restore_window_position()`、`save_window_position()`、`render()`。

### 6.2 托盘菜单

- 提供“立即刷新”。
- 提供“退出”。
- 点击托盘图标可重新显示窗口。

依据：`create_tray()`。

### 6.3 数据源

- `codex_local`：读取 `~/.codex/auth.json`，请求官方额度端点，选择最接近 7 天的窗口计算周额度，并在响应提供精确 5 小时窗口时展示其剩余量。

依据：`CodexLocalUsageProvider`、`extract_weekly_limit()`。

### 6.4 错误反馈

- 读取不到本地 auth 文件时，展示明确消息。
- HTTP 错误时展示状态码。
- 网络不可达时展示无法连接额度服务。
- JSON 读取或格式错误时展示读取失败。
- 本次运行已有成功数据时，后续获取失败仍展示最近一次成功数据，并附带失败原因。
- 本次运行尚无成功数据时，展示“暂无数据”。

依据：`CodexLocalUsageProvider.get_usage()`、`JsonFileUsageProvider.get_usage()`、`keep_last_successful_snapshot()`、`UsageCat.render()`。

## 7. 安全与隐私

- 不保存 token。
- 不在 UI、配置或日志中展示 token。
- 只读取用户明确授权或配置的本地文件。
- `codex_local` 请求只发往官方 `chatgpt.com` 额度端点。
- 开机自启只写当前用户 `HKCU` 注册表项。

依据：`CodexLocalUsageProvider.USAGE_URL`、`install_autostart.ps1`。

## 8. 非功能需求

- 启动路径应兼容 Python 3.12、本机 `pyw` 启动器和 `pythonw` 命令；`run.bat` 必须异步启动无控制台进程，关闭 PowerShell 后程序仍通过托盘运行。
- UI 刷新最小间隔为 30 秒，避免过于频繁请求。
- 数据源解析失败不得导致程序崩溃，应返回空数据快照和可读消息。
- 文档必须保持 UTF-8 中文可读。

## 9. 验收标准

- 运行 `.\run.bat` 后出现右下角悬浮窗，且命令立即返回；关闭 PowerShell 后悬浮窗和托盘图标仍可用。
- 托盘右键菜单仅含“立即刷新”和“退出”，且均可触发对应操作。
- `codex_local` 在本机 Codex 已登录时能读取 weekly 窗口，以及接口提供的 5 小时窗口剩余量。
- 未登录、网络错误或 JSON 格式错误时，UI 显示可理解的失败信息。
- 单元测试通过：

```powershell
python -m unittest discover -s tests -v
```

## 10. 后续规划

- 增加低额度阈值提醒。
- 增加打包发布流程。
- 增加 UI 冒烟测试。
