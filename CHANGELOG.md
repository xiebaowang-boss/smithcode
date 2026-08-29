# Changelog

本项目的所有显著变更都记录在本文件中。

## [0.2.0] - 2026-08-29

### 新增

- 权限规则引擎（参考 opencode 模型）：三级动作 `allow / ask / deny`，规则 = (工具名, 参数模式, 动作)，通配符匹配，最后一条匹配的规则生效
- `codeagent.json` 配置文件：用户可自定义权限规则，支持字符串简写与按模式细分两种写法
- 交互确认升级为 `[y]本次 / [n]拒绝 / [a]总是允许该模式`，"总是允许"按参数模式记忆（仅当前会话）
- 工具注册支持 `pattern_arg` 声明权限模式来源（该字段不会发送给 LLM）
- `-y` 参数语义收紧：跳过所有 `ask`，但显式 `deny` 规则依然生效

### 变更

- `Permission.check` 签名改为 `check(tool_name, args)`，Agent 在权限检查前先解析工具参数

## [0.1.0] - 2026-08-29

### 新增

- Agent 循环：模型自主调用工具直至任务完成，可配置最大迭代轮数
- 5 个内置工具：read_file / write_file / edit_file / list_dir / run_command
- 权限控制：敏感操作逐个确认，支持会话内记忆与 `-y` 全自动模式
- 会话管理：多轮对话、`/new` 重置、`/save` 导出 JSON
- 路径沙箱与命令超时保护

### 变更

- 采用 src 布局重组项目结构，新增 tests / docs / examples 目录
- 工具改为注册表机制（`tools/base.py`），新增工具无需改汇总代码
- 系统提示词从 `session.py` 拆分到 `prompts.py`
- 终端编码处理统一到 `utils/terminal.py`，移除根目录 `main.py`
