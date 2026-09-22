"""权限子系统：规则引擎（engine）+ Shell 命令策略（shell_policy）。

公共 API 在此汇总，外部统一 `from smithcode.permission import ...`；
引擎与命令策略如何拆分是对外不可见的实现细节。

- engine：三级动作规则引擎、模式分派、越界确认、会话级"总是允许"
- shell_policy：安全只读判定（`is_safe_command`）+"总是允许"命令前缀推导
  （`command_key` / `derive_prefix`）
"""
from . import shell_policy
from .engine import (
    ACTIONS,
    ALLOW,
    ASK,
    DEFAULT_RULES,
    DENY,
    EDIT_FAMILIES,
    MODE_LABELS,
    MODES,
    Permission,
    evaluate,
    evaluate_with_source,
    has_command_substitution,
    infer_trust_root,
    split_command,
)

__all__ = [
    "ACTIONS",
    "ALLOW",
    "ASK",
    "DEFAULT_RULES",
    "DENY",
    "EDIT_FAMILIES",
    "MODES",
    "MODE_LABELS",
    "Permission",
    "evaluate",
    "evaluate_with_source",
    "has_command_substitution",
    "infer_trust_root",
    "shell_policy",
    "split_command",
]
