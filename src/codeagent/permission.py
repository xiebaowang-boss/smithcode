"""权限控制：三级动作（allow / ask / deny）规则引擎。

规则 = (工具名, 参数模式, 动作)，用通配符同时匹配两者。
求值语义与 opencode 一致：最后一条匹配的规则生效，无匹配默认 ask。

规则三层（后层覆盖前层）：
1. 代码内置默认规则
2. 工作区 codeagent.json 中的用户规则
3. 会话内"总是允许"积累的规则（仅本会话有效）
"""
from __future__ import annotations

import fnmatch
from pathlib import Path

from . import config
from .tools import PATTERN_ARGS

ALLOW, ASK, DENY = "allow", "ask", "deny"

DEFAULT_RULES = [
    ("read_file", "*", ALLOW),
    ("list_dir", "*", ALLOW),
    ("glob", "*", ALLOW),
    ("grep", "*", ALLOW),
    ("write_file", "*", ASK),
    ("edit_file", "*", ASK),
    ("run_command", "*", ASK),
]

ACTIONS = (ALLOW, ASK, DENY)


def infer_trust_root(target: Path) -> Path:
    """为授权目录之外的路径推断合理的信任范围。

    从目标向上最多 10 级寻找 .git（视为项目根，一次授权覆盖整个项目）；
    找不到则退回目标所在目录。
    """
    d = target if target.is_dir() else target.parent
    for _ in range(10):
        if (d / ".git").exists():
            return d
        if d.parent == d:
            break
        d = d.parent
    return target.parent


def evaluate(permission: str, pattern: str, *rulesets) -> tuple:
    """求值一条权限请求：返回最后一条匹配的规则，无匹配则默认 ask。"""
    matched = [
        rule
        for ruleset in rulesets
        for rule in ruleset
        if fnmatch.fnmatchcase(permission, rule[0]) and fnmatch.fnmatchcase(pattern, rule[1])
    ]
    return matched[-1] if matched else (permission, pattern, ASK)


class Permission:
    def __init__(self):
        self.approved_all = False
        self.user_rules = config.load_permissions()
        self.session_rules: list = []

    def check(self, tool_name: str, args: dict | None = None) -> bool:
        """判断一次工具调用是否放行。deny 直接拒绝；ask 弹出交互确认。"""
        pattern = self._pattern(tool_name, args or {})
        action = evaluate(tool_name, pattern, DEFAULT_RULES, self.user_rules, self.session_rules)[2]

        # -y 只覆盖 ask，显式声明的 deny 依然生效
        if self.approved_all and action != DENY:
            return True
        if action == ALLOW:
            return True
        if action == DENY:
            print(f"\n⛔ 已被权限规则拒绝: {tool_name}（模式 {pattern}）")
            return False
        return self._ask(tool_name, pattern)

    def ask_outside_access(self, raw_path: str, target: Path) -> tuple[str, Path | None]:
        """路径预检发现目标在授权目录之外时的交互确认。

        返回 ("once", 信任根) / ("always", 信任根)（根已写入 SESSION_EXTRA_ROOTS）
        或 ("deny", None)。
        """
        root = infer_trust_root(target)
        print("\n⚠️  Agent 请求访问授权目录之外的路径:")
        print(f"   {raw_path}")
        print(f"   解析为 {target}")
        print(f"   将信任目录: {root}")
        answer = input(
            "   允许? [y]仅本次 / [a]本会话总是信任该目录 / [n]拒绝: "
        ).strip().lower()
        if answer == "y":
            return "once", root
        if answer == "a":
            config.SESSION_EXTRA_ROOTS.append(str(root))
            return "always", root
        return "deny", None

    @staticmethod
    def _pattern(tool_name: str, args: dict) -> str:
        """按工具注册时声明的 pattern_arg 提取权限模式，未声明则为 *。

        路径类参数归一化为相对命中授权根的 POSIX 相对路径，保证规则模式
        （如 src/**）对相对、绝对、跨授权根（../other-project/…）写法都能匹配。
        命令类参数（command）保持原文，避免路径归一化破坏命令文本匹配。
        """
        arg = PATTERN_ARGS.get(tool_name)
        if not arg:
            return "*"
        raw = str(args.get(arg, "*"))
        if arg == "path" and raw != "*":
            raw = Permission._normalize_path_pattern(raw)
        return raw

    @staticmethod
    def _normalize_path_pattern(raw: str) -> str:
        try:
            # 与工具侧 _resolve 同样的锚定方式：相对路径相对主工作区解析
            p = (Path(config.WORKSPACE_ROOT) / raw).resolve()
        except OSError:
            return raw
        for root in config.allowed_roots():
            if p.is_relative_to(root):
                return p.relative_to(root).as_posix()
        return raw

    def _ask(self, tool_name: str, pattern: str) -> bool:
        print(f"\n⚠️  Agent 请求执行: {tool_name}")
        print(f"   模式: {pattern}")
        answer = input("   允许? [y]本次 / [n]拒绝 / [a]总是允许该模式: ").strip().lower()
        if answer == "a":
            self.session_rules.append((tool_name, pattern, ALLOW))
            return True
        return answer == "y"
