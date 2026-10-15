# AGENTS.md

## 项目概览

本仓库包含三个独立的 Windows PySide6 桌面应用：`codex-usage`、`cursor-usage` 和 `claude-usage`。各项目都有自己的 `app.py`、`usage_provider.py`、依赖清单、启动脚本和测试；不得跨目录导入同名模块。

## 协作规则

- 始终使用中文沟通；判断必须引用代码、测试或文档作为依据。
- 回答保持精简，最终回复附不超过 150 字的简短总结。
- 不执行 Git 提交；如需提交，仅提供建议的提交信息。
- 保留用户已有改动，除非用户明确要求回滚或覆盖。

## 代码边界

- `*/app.py`：PySide6 悬浮窗口、托盘菜单、配置读写和刷新流程。
- `*/usage_provider.py`：数据源抽象、数据解析、网络请求和敏感信息处理。
- `*/tests/`：对应目录的单元测试。
- `*/assets/`：应用图标和宠物素材；修改后需人工确认托盘图标与卡片显示。

保持三个应用各自独立。小改动优先沿用现有单文件应用结构，不引入新框架或共享包。

## 数据与安全约束

- 不把 Codex token、Cursor Cookie、会话令牌或本地数据库内容写入配置、日志、测试样本、界面或文档。
- Codex 默认数据源仅读取 `~/.codex/auth.json`，并只查询额度接口。
- Cursor 默认数据源仅读取本机 Cursor 的登录状态；`CURSOR_SESSION_TOKEN` 只允许作为临时环境变量使用。
- Claude 默认数据源 `claude_desktop` 先解密 Claude 桌面版的 `sessionKey` Cookie 并查询 claude.ai 用量端点；桌面版运行时会独占锁住 Cookie 库，此时改从桌面版 `config.json` 的 OAuth 令牌缓存（`oauth:tokenCacheV2`/`oauth:tokenCache`）中只解密 **access token** 查询 Anthropic `/api/oauth/usage`。可选 `claude_local` 仅读取 `~/.claude/.credentials.json` 的 OAuth 登录态并查询 Anthropic 用量端点。以上均只取登录令牌用于只读查询、仅缓存在进程内，不得读取同一缓存里的 `refreshToken`、其它 Cookie、对话记录，也不得刷新、写回或泄露任何登录令牌。
- 新增数据源应实现 `UsageProvider.get_usage()`，并在 `create_provider()` 中注册；错误应返回可展示的 `UsageSnapshot`，不泄露敏感值。

## 验证要求

修改数据源、解析或配置逻辑后，在对应子项目执行：

```powershell
python -m unittest discover -s tests -v
```

修改 UI、托盘行为或素材后，除单元测试外还应运行 `run.bat`，人工检查窗口、托盘菜单、设置弹窗和刷新结果。

修改默认行为、配置字段、数据源或安全边界时，同步更新该子项目的 `README.md` 与 `PRD.md`；根 `README.md` 仅维护仓库级概览和入口。
