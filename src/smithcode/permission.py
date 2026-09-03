"""权限控制：三级动作（allow / ask / deny）规则引擎。

规则 = (工具名, 参数模式, 动作)，用通配符同时匹配两者。
求值语义与 opencode 一致：最后一条匹配的规则生效，无匹配默认 ask。

规则三层（后层覆盖前层）：
1. 代码内置默认规则
2. ~/.smithcode/config.toml 中的用户规则
3. 会话内"总是允许"积累的规则（仅本会话有效）

内置默认规则含保护路径：`.git` 目录只读（禁止写入与编辑）。匹配在 Windows 下大小写不敏感（与 opencode v2 对齐）。
标准输入非终端（管道/CI）时交互确认不可用，所有 ask 一律 fail-closed 拒绝。
"""
from __future__ import annotations

import fnmatch
from pathlib import Path

from . import config
from .tools import PATTERN_ARGS, PATTERN_FAMILIES
from .utils.terminal import confirmations_available, flush_pending_input

ALLOW, ASK, DENY = "allow", "ask", "deny"

DEFAULT_RULES = [
    ("read_file", "*", ALLOW),
    ("list_dir", "*", ALLOW),
    ("glob", "*", ALLOW),
    ("grep", "*", ALLOW),
    ("write_file", "*", ASK),
    ("write_file", "*.git", DENY),
    ("write_file", "*.git/**", DENY),
    ("edit_file", "*", ASK),
    ("edit_file", "*.git", DENY),
    ("edit_file", "*.git/**", DENY),
    ("run_command", "*", ASK),
    ("ask_user", "*", ALLOW),  # 提问本身不再弹确认（确认一个"提问"是荒谬的）；可用 deny 禁止
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


def evaluate(permission, pattern: str, *rulesets) -> tuple:
    """求值一条权限请求：返回最后一条匹配的规则，无匹配则默认 ask。

    permission 可为工具名字符串，或 (工具名, family, ...) 元组——元组内任一 key
    命中规则的工具名即视为匹配（family 机制：如 apply_patch 继承 edit_file 规则）。
    大小写行为与 opencode v2 对齐：Windows 下大小写不敏感（fnmatch.fnmatch 经
    os.path.normcase 归一化），其他平台保持大小写敏感。
    """
    keys = (permission,) if isinstance(permission, str) else tuple(permission)
    matched = [
        rule
        for ruleset in rulesets
        for rule in ruleset
        if any(fnmatch.fnmatch(k, rule[0]) for k in keys)
        and fnmatch.fnmatch(pattern, rule[1])
    ]
    return matched[-1] if matched else (keys[0], pattern, ASK)


class Permission:
    def __init__(self):
        self.approved_all = False
        self.user_rules = config.load_permissions()
        self.session_rules: list = []

    def _keys(self, tool_name: str) -> tuple:
        """权限匹配 key 链：(工具名, family)。默认 family=自身，去重后等价于单 key，
        保证未声明 family 的既有工具行为完全不变。"""
        return tuple(dict.fromkeys((tool_name, PATTERN_FAMILIES.get(tool_name, tool_name))))

    def check(self, tool_name: str, args: dict | None = None) -> bool:
        """判断一次工具调用是否放行。deny 直接拒绝；ask 弹出交互确认（非交互 fail-closed 拒绝）。"""
        pattern = self._pattern(tool_name, args or {})
        action = evaluate(self._keys(tool_name), pattern, DEFAULT_RULES, self.user_rules, self.session_rules)[2]

        # -y 只覆盖 ask，显式声明的 deny 依然生效
        if self.approved_all and action != DENY:
            return True
        if action == ALLOW:
            return True
        if action == DENY:
            print(f"\n⛔ 已被权限规则拒绝: {tool_name}（模式 {pattern}）")
            return False
        return self._ask(tool_name, pattern)

    def check_paths(self, tool_name: str, paths: list[str]) -> bool:
        """多路径工具（如 apply_patch）的聚合检查：任一路径 deny → 拒绝；任一 ask → 询问；
        全部放行 → 放行。路径归一化与单路径一致（相对命中授权根）。"""
        keys = self._keys(tool_name)
        patterns = [Permission._normalize_path_pattern(str(p)) for p in paths]
        actions = [
            evaluate(keys, pat, DEFAULT_RULES, self.user_rules, self.session_rules)[2]
            for pat in patterns
        ]
        if any(a == DENY for a in actions):
            print(f"\n⛔ 已被权限规则拒绝: {tool_name}（目标含保护/受限路径）")
            return False
        if any(a == ASK for a in actions):
            if self.approved_all:
                return True
            return self._ask(tool_name, f"多文件（{len(paths)} 个目标）")
        return True

    def ask_outside_access(self, raw_path: str, target: Path) -> tuple[str, Path | None]:
        """路径预检发现目标在授权目录之外时的确认。

        -y（approved_all）：静默放行本次访问，视为"仅本次"授权，不弹确认、不写入会话级信任。
        非交互 stdin：无法询问用户，fail-closed 拒绝（不会因 EOFError 崩溃）。
        其余：返回 ("once", 信任根) / ("always", 信任根)（根已写入 SESSION_EXTRA_ROOTS）
        或 ("deny", None)。
        """
        root = infer_trust_root(target)
        if self.approved_all:
            return "once", root
        if not confirmations_available():
            print(f"\n⛔ 非交互模式，无法确认越界访问，已拒绝: {raw_path}")
            return "deny", None
        print("\n⚠️  Agent 请求访问授权目录之外的路径:")
        print(f"   {raw_path}")
        print(f"   解析为 {target}")
        print(f"   将信任目录: {root}")
        flush_pending_input()
        while True:
            answer = input(
                "   允许? [y]仅本次 / [a]本会话总是信任该目录 / [n]拒绝: "
            ).strip().lower()
            if answer in ("y", "n", "a"):
                break
            shown = f"（收到: {answer[:40]!r}）" if answer else ""
            print(f"   无效输入{shown}，请输入 y / a / n")
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
        if not confirmations_available():
            print(f"\n⛔ 非交互模式，无法确认，已拒绝: {tool_name}（模式 {pattern}）")
            return False
        print(f"\n⚠️  Agent 请求执行: {tool_name}")
        print(f"   模式: {pattern}")
        flush_pending_input()  # 丢弃提前键入/粘贴的排队内容，防止被误当成回答
        while True:
            answer = input(
                "   允许? [y]本次 / [n]拒绝 / [a]总是允许该模式: "
            ).strip().lower()
            if answer in ("y", "n", "a"):
                break
            shown = f"（收到: {answer[:40]!r}）" if answer else ""
            print(f"   无效输入{shown}，请输入 y / n / a")
        if answer == "a":
            self.session_rules.append((tool_name, pattern, ALLOW))
            return True
        return answer == "y"
