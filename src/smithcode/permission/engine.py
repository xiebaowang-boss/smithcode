"""权限控制：三级动作（allow / ask / deny）规则引擎 + 会话级权限模式。

规则 = (工具名, 参数模式, 动作)，用通配符同时匹配两者。
求值语义与 opencode 一致：最后一条匹配的规则生效，无匹配默认 ask。

规则三层（后层覆盖前层）：
1. 代码内置默认规则
2. ~/.smithcode/config.toml 中的用户规则
3. 会话内"总是允许"积累的规则（仅本会话有效）

模式层叠在规则求值之上，只接管 ask 的去向（deny 任何模式都拒绝）：
smith（默认）逐个确认；accept_edits 编辑族自动放行；auto 全部自动放行
（与 -y 等价）。Shift+Tab 在 TUI 中循环切换。

内置默认规则含保护路径：`.git` 目录只读（禁止写入与编辑）。匹配在 Windows 下大小写不敏感（与 opencode v2 对齐）。
标准输入非终端（管道/CI）时交互确认不可用，所有 ask 一律 fail-closed 拒绝。
"""
from __future__ import annotations

import fnmatch
from pathlib import Path

from .. import config, renderer
from ..tools import PATTERN_ARGS, PATTERN_FAMILIES
from ..utils.terminal import confirmations_available
from . import shell_policy

ALLOW, ASK, DENY = "allow", "ask", "deny"

# 会话级权限模式：控制 ask 的去向（deny 任何模式下都拒绝）。
# smith=逐个确认（默认）；accept_edits=编辑族自动放行；auto=全部自动放行（原 -y）。
MODES = ("smith", "accept_edits", "auto")
MODE_LABELS = {"smith": "Smith", "accept_edits": "Accept Edits", "auto": "Auto"}
# accept_edits 档自动放行的权限族：所有写/编辑路径（apply_patch 经 family 继承 edit_file）
EDIT_FAMILIES = ("edit_file", "write_file")

DEFAULT_RULES = [
    ("read_file", "*", ALLOW),
    ("list_dir", "*", ALLOW),
    ("glob", "*", ALLOW),
    ("grep", "*", ALLOW),
    ("webfetch", "*", ALLOW),  # 抓取公开网页只读且无本地副作用，默认放行；可用 [permissions].webfetch 收紧
    ("websearch", "*", ALLOW),  # 检索公开网页只读且无本地副作用，默认放行；可用 [permissions].websearch 收紧或 deny
    ("write_file", "*", ASK),
    ("write_file", "*.git", DENY),
    ("write_file", "*.git/**", DENY),
    ("edit_file", "*", ASK),
    ("edit_file", "*.git", DENY),
    ("edit_file", "*.git/**", DENY),
    ("run_command", "*", ASK),
    ("ask_user", "*", ALLOW),  # 提问本身不再弹确认（确认一个"提问"是荒谬的）；可用 deny 禁止
    ("todo_write", "*", ALLOW),  # 更新任务清单本身不弹确认（确认一个"追踪步骤"是荒谬的）；可用 deny 禁止
    ("todo_read", "*", ALLOW),  # 只读任务清单，无副作用；同 todo_write 默认放行
    ("goal_update", "*", ALLOW),  # 更新持久目标状态本身不弹确认（确认一个"目标声明"是荒谬的）；可用 deny 禁止
    ("goal_read", "*", ALLOW),  # 只读持久目标快照，无副作用；同 goal_update 默认放行
    ("use_skill", "*", ALLOW),  # 加载技能指令本身不弹确认（项目级信任门控已在发现阶段完成）；可按技能名 deny
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


def _rule_pattern_matches(rule_pattern, pattern: str) -> bool:
    """规则模式匹配：元组视为命令 argv 前缀（"总是允许"记忆），字符串走通配。"""
    if isinstance(rule_pattern, tuple):
        key = shell_policy.command_key(pattern)
        return key is not None and key[:len(rule_pattern)] == rule_pattern
    return fnmatch.fnmatch(pattern, rule_pattern)


def evaluate_with_source(permission, pattern: str, *rulesets) -> tuple:
    """同 evaluate，但额外返回命中规则所在 ruleset 的下标（无匹配为 None）。

    供安全命令层判断「控制规则是否来自用户/会话」：只有内置默认规则判定为
    ask 时才轮到安全只读命令集免确认，用户/会话规则命中一律优先。
    """
    keys = (permission,) if isinstance(permission, str) else tuple(permission)
    matched = None
    source = None
    for index, ruleset in enumerate(rulesets):
        for rule in ruleset:
            if any(fnmatch.fnmatch(k, rule[0]) for k in keys) and _rule_pattern_matches(rule[1], pattern):
                matched = rule
                source = index
    if matched is None:
        return (keys[0], pattern, ASK), None
    return matched, source


def evaluate(permission, pattern: str, *rulesets) -> tuple:
    """求值一条权限请求：返回最后一条匹配的规则，无匹配则默认 ask。

    permission 可为工具名字符串，或 (工具名, family, ...) 元组——元组内任一 key
    命中规则的工具名即视为匹配（family 机制：如 apply_patch 继承 edit_file 规则）。
    大小写行为与 opencode v2 对齐：Windows 下大小写不敏感（fnmatch.fnmatch 经
    os.path.normcase 归一化），其他平台保持大小写敏感。
    """
    return evaluate_with_source(permission, pattern, *rulesets)[0]


# 顶层 shell 操作符：在此切分复合命令（引号内不切）
_COMMAND_OPERATORS = ("&&", "||", ";", "|", "&")


def split_command(command: str) -> list[str]:
    """把复合命令按顶层 shell 操作符切分为子命令列表。

    引号内不切（单引号无转义、双引号内 \\" 转义）；换行视同 ; 分隔。
    宁可少切不可多切：该切未切时整条命令作为一段求值（默认 ask，偏安全）。
    """
    segments: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    i, n = 0, len(command)
    while i < n:
        c = command[i]
        if quote:
            buf.append(c)
            if quote == '"' and c == "\\" and i + 1 < n:
                buf.append(command[i + 1])
                i += 2
                continue
            if c == quote:
                quote = None
            i += 1
            continue
        if c in "\"'":
            quote = c
            buf.append(c)
        elif c in "\r\n":
            while i + 1 < n and command[i + 1] in "\r\n":
                i += 1
            segments.append("".join(buf))
            buf = []
        elif command[i:i + 2] in _COMMAND_OPERATORS:
            segments.append("".join(buf))
            buf = []
            i += 1  # 双字符操作符：下方统一 +1，加上这里的 +1 跳过两个字符
        elif c in ";|&":
            segments.append("".join(buf))
            buf = []
        else:
            buf.append(c)
        i += 1
    segments.append("".join(buf))
    return [s.strip() for s in segments if s.strip()]


def has_command_substitution(command: str) -> bool:
    """引号外或双引号内是否存在命令替换（$(...) 或反引号）。

    POSIX 语义：双引号内的 $(...) / 反引号仍会被 shell 展开，单引号内不会。
    替换体的内容无法静态求值（`git log $(whatever)` 里藏什么都可能），
    求值时强制降级为 ask。
    """
    quote: str | None = None
    i, n = 0, len(command)
    while i < n:
        c = command[i]
        if quote:
            if quote == '"' and c == "\\" and i + 1 < n:
                i += 2
                continue
            if c == quote:
                quote = None
            elif quote == "'" or (c == "$" and command[i + 1:i + 2] != "(" and c != "`"):
                pass  # 单引号内不展开；双引号内只关心 $( 与反引号
            else:
                return True  # 双引号内的 $( 或反引号
        elif c in "\"'":
            quote = c
        elif (c == "$" and command[i + 1:i + 2] == "(") or c == "`":
            return True
        i += 1
    return False


def _cd_with_git(segments: list) -> bool:
    """复合命令中同时出现「改变目录的 cd」与 git 时为 True。

    git 会在新目录执行 hooks，`cd other && git ...` 不能因两段各自安全而整体放行。
    """
    has_cd = any(shell_policy.is_dir_change(seg) for seg in segments)
    has_git = any(shell_policy.is_git_command(seg) for seg in segments)
    return has_cd and has_git


class Permission:
    def __init__(self):
        self.mode = "smith"
        self.user_rules = config.load_permissions()
        self.session_rules: list = []

    def new_session(self) -> None:
        """清空会话内"总是允许"积累的规则（/new 时调用）。

        权限模式（Shift+Tab 切换的档位）是用户的手动选择，跨会话保留。"""
        self.session_rules.clear()

    @property
    def approved_all(self) -> bool:
        """-y 兼容读写：置 True 等价切到 auto 档。旧引用点（cli -y）零改动。"""
        return self.mode == "auto"

    @approved_all.setter
    def approved_all(self, value: bool):
        self.mode = "auto" if value else "smith"

    def cycle_mode(self) -> str:
        """Shift+Tab 循环切换：smith → accept_edits → auto → smith。返回新模式。"""
        self.mode = MODES[(MODES.index(self.mode) + 1) % len(MODES)]
        return self.mode

    def _keys(self, tool_name: str) -> tuple:
        """权限匹配 key 链：(工具名, family)。默认 family=自身，去重后等价于单 key，
        保证未声明 family 的既有工具行为完全不变。"""
        return tuple(dict.fromkeys((tool_name, PATTERN_FAMILIES.get(tool_name, tool_name))))

    def check(self, tool_name: str, args: dict | None = None, content: str | None = None) -> bool:
        """判断一次工具调用是否放行。deny 直接拒绝；ask 弹出交互确认（非交互 fail-closed 拒绝）。

        content 为该工具的展示摘要（Agent 的 describe 行），统一渲染在确认框标题
        下方，让每种工具的申请都带上同样的“目标内容”。"""
        pattern = self._pattern(tool_name, args or {})
        if PATTERN_ARGS.get(tool_name) == "command":
            # 复合命令拆分求值：放行 A 不能借 && 偷渡 B
            action, asked, remember = self._eval_command(self._keys(tool_name), pattern)
        else:
            action = evaluate(self._keys(tool_name), pattern, DEFAULT_RULES, self.user_rules, self.session_rules)[2]
            asked, remember = [pattern], True

        # deny 任何模式都拒绝；ask 交给模式分派（smith 确认 / accept_edits 编辑族放行 / auto 全放行）
        if action == ALLOW:
            return True
        if action == DENY:
            renderer.current().error(f"已被权限规则拒绝: {tool_name}（模式 {pattern}）")
            return False
        return self._dispatch_ask(tool_name, asked, remember, content)

    def _eval_segment(self, keys: tuple, segment: str) -> str:
        """单段命令求值：用户/会话规则优先，命中即按其裁决（可收紧为 ask/deny，
        也可放宽为 allow）；仅当没有任何用户/会话规则命中、且内置默认判定为
        ask 时，才轮到内置安全只读命令集免确认。任何未知动作一律降级为 ask。"""
        rule, source = evaluate_with_source(keys, segment, self.user_rules, self.session_rules)
        if source is not None:
            action = rule[2]
        else:
            action = evaluate(keys, segment, DEFAULT_RULES)[2]
            if action == ASK and shell_policy.is_safe_command(segment):
                return ALLOW
        return action if action in ACTIONS else ASK

    def _eval_command(self, keys: tuple, command: str) -> tuple:
        """复合命令求值：按顶层操作符拆段逐段匹配规则。

        聚合语义与多路径一致——任一段 deny → 拒绝；任一段 ask，或存在无法
        静态求值的命令替换（$() / 反引号）→ 询问；全部放行才放行。
        返回 (聚合动作, 待确认段列表, 是否允许"总是允许"记忆)。安全只读命令集
        在内置默认 ask 下免确认；但同一复合命令里若既有改变目录的 cd 又有 git
        （git 会在新目录执行 hooks），整体降级为询问且不允许前缀记忆（记忆无法
        解除该组合守卫）。"""
        segments = split_command(command)
        actions = [self._eval_segment(keys, seg) for seg in segments]
        if any(a == DENY for a in actions):
            return DENY, [], True
        asks = [seg for seg, a in zip(segments, actions) if a == ASK]
        if has_command_substitution(command) or asks:
            return ASK, asks or list(segments), True
        if any(a == ALLOW for a in actions) and _cd_with_git(segments):
            return ASK, list(segments), False
        return ALLOW, [], True

    def check_paths(self, tool_name: str, paths: list[str], content: str | None = None) -> bool:
        """多路径工具（如 apply_patch）的聚合检查：任一路径 deny → 拒绝；任一 ask → 询问；
        全部放行 → 放行。路径归一化与单路径一致（相对命中授权根）。
        "总是允许"按每个待确认路径的精确模式逐条记忆（而非一次性宽泛放行），
        下次同路径调用直接放行、新路径仍走确认。content 同 check。"""
        keys = self._keys(tool_name)
        patterns = [Permission._normalize_path_pattern(str(p)) for p in paths]
        actions = [
            evaluate(keys, pat, DEFAULT_RULES, self.user_rules, self.session_rules)[2]
            for pat in patterns
        ]
        if any(a == DENY for a in actions):
            renderer.current().error(f"已被权限规则拒绝: {tool_name}（目标含保护/受限路径）")
            return False
        if any(a == ASK for a in actions):
            if self.approved_all:
                return True
            if self.mode == "accept_edits" and PATTERN_FAMILIES.get(tool_name) in EDIT_FAMILIES:
                return True
            asked = sorted({pat for pat, a in zip(patterns, actions) if a == ASK})
            return self._ask(tool_name, asked, content=content)
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
            renderer.current().error(f"非交互模式，无法确认越界访问，已拒绝: {raw_path}")
            return "deny", None
        r = renderer.current()
        title = f"允许访问授权目录之外的路径 {raw_path}?"
        descriptions = {
            "y": f"仅本次访问 {target}",
            "a": f"本会话信任目录: {root}",
            "n": "拒绝本次访问",
        }
        answer = r.confirm_choice(
            f"{title} [y]仅本次 / [a]本会话总是信任该目录 / [n]拒绝: ",
            "yan",
            "y / a / n",
            descriptions=descriptions,
        )
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

    def _dispatch_ask(self, tool_name: str, patterns: list[str],
                      remember: bool = True, content: str | None = None) -> bool:
        """按当前权限模式分派 ask 请求。

        auto：全部自动放行（原 -y 语义）。accept_edits：编辑族（edit_file /
        write_file，apply_patch 经 family 继承）自动放行，其余仍确认。
        smith：逐个确认。deny 永远到不了这里（check 已拦截）。
        remember=False 时不提供"总是允许"（如 cd+git 组合守卫）。
        content 为确认框标题下的展示内容（工具摘要）。"""
        if self.approved_all:
            return True
        if self.mode == "accept_edits" and PATTERN_FAMILIES.get(tool_name) in EDIT_FAMILIES:
            return True
        return self._ask(tool_name, patterns, remember, content)

    def _remember_proposals(self, tool_name: str, patterns: list) -> list:
        """生成"总是允许"的记忆候选：[(展示文本, 规则键), ...]，按键去重。

        命令工具逐段推导 argv 前缀（如 `("python","-m","pytest")`），推导不出则
        退回精确串；其余工具按模式串记忆。"""
        proposals = []
        for pat in patterns:
            if PATTERN_ARGS.get(tool_name) != "command":
                proposals.append((pat, pat))
                continue
            for segment in split_command(pat):
                prefix = shell_policy.derive_prefix(segment)
                if prefix is None:
                    proposals.append((segment, segment))
                else:
                    proposals.append((" ".join(prefix) + " *", prefix))
        seen = set()
        unique = []
        for display, key in proposals:
            if key not in seen:
                seen.add(key)
                unique.append((display, key))
        return unique

    def _ask(self, tool_name: str, patterns: list[str],
             remember: bool = True, content: str | None = None) -> bool:
        """交互确认；patterns 为本次待确认的模式列表（命令工具为待确认的段）。

        标题统一为「允许执行 <工具名>?」，目标内容（工具摘要，如 `command git status`
        / `fetch <url>` / `write <path>`）由 Agent 作为 content 传入、渲染在标题下方，
        保证每种工具的申请都带同样的内容行。选"总是允许"时按候选逐条记入会话规则：
        命令工具记 argv 前缀（或精确串），其余工具记模式串，保证记忆能被后续命中。
        remember=False 时只提供 y/n。变更预览（diff）不在这里展示——它由 Agent 在
        确认前推送到工具调用块，与权限框解耦。"""
        if not confirmations_available():
            renderer.current().error(
                f"非交互模式，无法确认，已拒绝: {tool_name}（模式 {patterns[0]}）"
            )
            return False
        r = renderer.current()
        proposals = self._remember_proposals(tool_name, patterns) if remember else []
        title = f"允许执行 {tool_name}?"
        descriptions = {"y": "仅本次执行", "n": "拒绝并跳过该操作"}
        if proposals:
            shown = "、".join(display for display, _ in proposals)
            descriptions["a"] = f"本会话将记住: {shown}"
            prompt = f"{title} [y]本次 / [n]拒绝 / [a]总是允许: "
            options, hint = "yna", "y / n / a"
        else:
            prompt = f"{title} [y]本次 / [n]拒绝: "
            options, hint = "yn", "y / n"
        answer = r.confirm_choice(
            prompt,
            options,
            hint,
            content=content,
            descriptions=descriptions,
        )
        if answer == "a":
            for _, key in proposals:
                self.session_rules.append((tool_name, key, ALLOW))
            return True
        return answer == "y"
