# AGENTS.md

## 项目定位

Codex Usage Cat 是一个 Windows 桌面小组件项目。核心目标是用 PySide6 在右下角展示 Codex weekly 额度剩余量，并通过托盘菜单提供刷新、开机自启说明和退出能力。

## 协作规则

- 始终使用中文沟通。
- 给出判断时必须说明依据，例如引用代码中的类、函数、常量、测试或文档。
- 回答保持精简，最终回复必须包含不超过 150 字的简短总结。
- 不直接提交代码；如需提交，只提供建议的提交信息。
- 不回滚用户已有改动，除非用户明确要求。

## 开发约束

- 优先保持现有单文件桌面应用结构，不为小改动引入新框架。
- UI 入口在 `app.py`，数据源入口在 `usage_provider.py`。
- 新增数据源时应实现 `UsageProvider.get_usage()`，并通过 `create_provider()` 挂接。
- 配置只写入 `%APPDATA%\CodexUsageCat\settings.json`。
- 不保存、不打印、不展示 Codex token。
- `codex_local` 只能使用用户本机授权信息向官方只读额度端点取数。

## 验证要求

- 修改 `usage_provider.py` 时，至少运行：

```powershell
python -m unittest discover -s tests -v
```

- 修改 `app.py` UI 行为时，应人工启动 `.\run.bat` 验证窗口、托盘菜单和设置弹窗。
- 修改文档时，应与 `README.md`、`PRD.md` 和当前实现保持一致。

## 文档维护

- README 面向安装、运行和开发入口。
- PRD 面向产品目标、范围、需求和验收标准。
- 若默认行为、配置字段、数据源或安全边界变化，应同步更新 README 与 PRD。
