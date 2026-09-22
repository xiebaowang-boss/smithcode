"""Shell 命令静态分析与策略：一个模块、一份基础设施、一张命令规范表。

对外提供两类能力，共用同一份 tokenizer 与规范表：

1. **安全只读判定** `is_safe_command()`：内置默认 ask 下自动放行只读命令
   （只读、不写文件、不执行子进程、不联网）。拿不准一律回退确认。
2. **前缀推导** `command_key()` / `derive_prefix()`：为"总是允许"生成稳定的
   argv 前缀（如 `python -m pytest`），替代脆弱的整串 glob 记忆。

设计：所有命令知识集中在 `COMMANDS` 规范表（每个命令一行，安全判定与前缀
切点写在一起），新增命令只改表格。本模块只依赖 config（`cd` 目标的授权目录
校验），不参与工具注册，也不依赖规则引擎——由 `permission.engine` 调用。
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

from .. import config

MAX_SEGMENT_LEN = 10_000  # 超长命令不解析，回退确认（对齐 Claude Code）

_DISCARD_TARGETS = {"/dev/null", "nul", "nul:"}

# 禁选前缀：任何推导结果命中即退回"精确记忆整段"（安全网，纵深防御）
BANNED_PREFIXES = frozenset({
    ("python",), ("python3",), ("py",),
    ("python", "-m"), ("python3", "-m"),
    ("python", "-c"), ("python3", "-c"),
    ("node",), ("node", "-e"), ("ruby",), ("perl",), ("php",),
    ("bash",), ("sh",), ("zsh",), ("pwsh",), ("powershell",), ("cmd",),
    ("npm", "run"), ("pnpm", "run"), ("yarn", "run"), ("bun", "run"),
    ("uv", "run"), ("poetry", "run"), ("cargo", "run"), ("go", "run"),
    ("env",), ("sudo",), ("xargs",), ("eval",), ("exec",), ("make",),
})

_VERSION_FLAGS = {"--version", "-V", "-v", "--help", "-h"}

# git 只读子命令
_GIT_READONLY = {
    "status", "log", "diff", "show", "blame", "describe", "rev-parse",
    "rev-list", "ls-files", "ls-tree", "cat-file", "shortlog", "reflog",
    "show-ref", "for-each-ref", "whatchanged", "diff-tree", "name-rev",
    "grep", "merge-base", "symbolic-ref", "var", "count-objects",
}
# git branch 仅允许列出的标志（不允许位置参数，避免 `git branch foo` 建分支）
_GIT_BRANCH_LIST_FLAGS = {
    "-a", "-r", "-v", "-vv", "--all", "--remotes", "--list", "--verbose",
    "--color", "--no-color", "--show-current", "--contains", "--merged",
    "--no-merged", "--points-at",
}

_FIND_UNSAFE = {
    "-exec", "-execdir", "-delete", "-fprint", "-fprint0",
    "-fprintf", "-fls", "-ok", "-okdir", "--delete",
}
_GIT_UNSAFE = {
    "-c", "--config-env", "-C", "--git-dir", "--work-tree",
    "--exec-path", "-p", "--paginate",
}
_PIP_UNSAFE = {"-o", "--outdated", "-i", "--index-url", "--extra-index-url"}
_UV_UNSAFE = {"--index", "--index-url", "--extra-index-url", "--find-links"}
_RUFF_UNSAFE = {"--fix", "--fix-only", "--add-noqa", "--output-file"}


def _never_safe(args) -> bool:
    return False


def _always_safe(args) -> bool:
    return True


def _safe_version_only(args) -> bool:
    """仅允许一条版本/帮助标志：杜绝把解释器当脚本执行器（`python -c`/`node -e`）。"""
    return len(args) == 1 and args[0] in _VERSION_FLAGS


def _safe_cd(args) -> bool:
    """cd 仅允许单一、非标志、且落在授权目录内的目标。"""
    if len(args) != 1:
        return False
    target = args[0]
    if target.startswith("-"):
        return False
    return _within_allowed_roots(target)


def _safe_git(args) -> bool:
    """git 仅允许只读子命令；branch/tag/remote/stash/worktree/config 逐项收紧。"""
    if not args:
        return True
    idx = 0
    while idx < len(args) and args[idx].startswith("-"):
        idx += 1
    if idx >= len(args):
        return True
    sub = args[idx]
    rest = list(args[idx + 1:])
    if sub in _GIT_READONLY:
        return True
    if sub == "branch":
        return all(t.startswith("-") and t in _GIT_BRANCH_LIST_FLAGS for t in rest)
    if sub == "remote":
        return all(t in ("-v", "--verbose") for t in rest)
    if sub == "tag":
        return all(t in ("-l", "--list") for t in rest)
    if sub in ("stash", "worktree"):
        return bool(rest) and rest[0] == "list"
    if sub == "config":
        if not rest:
            return False
        return (
            rest[0] in ("--get", "--get-all", "--get-regexp", "-l", "--list")
            and all(not t.startswith("-") for t in rest[1:])
        )
    return False


def _safe_pip(args) -> bool:
    if _safe_version_only(args):
        return True
    if not args:
        return False
    if args[0] == "config":
        return len(args) >= 2 and args[1] in ("list", "get")
    return args[0] in ("list", "show", "freeze")


def _safe_uv(args) -> bool:
    if _safe_version_only(args):
        return True
    if len(args) < 2:
        return False
    if args[0] == "pip" and args[1] in ("list", "freeze", "show", "check"):
        return True
    return (args[0] == "python" and args[1] == "list") or (
        args[0] == "tool" and args[1] == "list"
    )


def _safe_poetry(args) -> bool:
    if _safe_version_only(args):
        return True
    if not args:
        return False
    if args[0] in ("show", "check", "version"):
        return True
    if args[0] == "env" and len(args) >= 2 and args[1] in ("info", "list"):
        return True
    return args[0] == "config" and "--list" in args


def _safe_npm(args) -> bool:
    if _safe_version_only(args):
        return True
    if not args:
        return False
    if args[0] in ("ls", "list"):
        return True
    return args[0] == "config" and len(args) >= 2 and args[1] in ("get", "list", "ls")


def _safe_ruff(args) -> bool:
    if _safe_version_only(args):
        return True
    return bool(args) and args[0] == "check"


def _safe_go(args) -> bool:
    return args == ["version"]


@dataclass(frozen=True)
class Spec:
    """单个命令的规范：安全判定与前缀切点写在一起。

    - safe        : `(args 不含命令名) -> 是否只读安全`
    - platforms   : 适用平台（"posix" / "win"）
    - arity       : 通用前缀切点：身份由 N 个"连续非标志 token"定义
    - sub_arity   : 子命令路径 → 不同 arity（如 ("git config", 3)），长路径优先
    - mode_flags  : 其值属于命令身份的标志（如 `python -m <mod>`）
    - inline_flags: 其值是不透明程序（代码/脚本）→ 拒绝推导（如 `python -c`）
    - script      : 首个位置参数属于身份（如 `python script.py`）
    - unsafe_flags: 出现即不安全，同时阻止前缀记忆
    - glob_risk   : 出现未加引号 glob 即不安全
    """
    safe: object = _never_safe
    platforms: tuple = ("posix", "win")
    arity: object = None
    sub_arity: tuple = ()
    mode_flags: tuple = ()
    inline_flags: tuple = ()
    script: bool = False
    unsafe_flags: frozenset = frozenset()
    glob_risk: bool = False


def _reader(platforms=("posix", "win"), **kwargs) -> Spec:
    return Spec(safe=_always_safe, platforms=platforms, **kwargs)


# 命令规范表：唯一知识源。新增命令只加一行。
COMMANDS = {
    # ---- 纯只读（任意参数） ----
    "pwd": _reader(("posix",)),
    "cat": _reader(("posix",)),
    "head": _reader(("posix",)),
    "tail": _reader(("posix",)),
    "wc": _reader(("posix",)),
    "which": _reader(("posix",)),
    "uname": _reader(("posix",)),
    "basename": _reader(("posix",)),
    "dirname": _reader(("posix",)),
    "realpath": _reader(("posix",)),
    "stat": _reader(("posix",)),
    "du": _reader(("posix",)),
    "df": _reader(("posix",)),
    "ls": _reader(("posix",)),
    "grep": _reader(("posix",)),
    "egrep": _reader(("posix",)),
    "fgrep": _reader(("posix",)),
    "rg": _reader(("posix",)),
    "cut": _reader(("posix",)),
    "tr": _reader(("posix",)),
    "diff": _reader(("posix",)),
    "echo": _reader(),
    "whoami": _reader(),
    # cmd.exe 只读命令
    "dir": _reader(("win",)),
    "type": _reader(("win",)),
    "where": _reader(("win",)),
    "findstr": _reader(("win",)),
    "fc": _reader(("win",)),
    "more": _reader(("win",)),
    "tree": _reader(("win",)),
    "ver": _reader(("win",)),
    "vol": _reader(("win",)),
    "hostname": _reader(("win",)),
    # ---- 带限制的只读 ----
    "date": _reader(("posix",), unsafe_flags=frozenset({"-s", "--set"})),
    "sort": _reader(("posix",), unsafe_flags=frozenset({"-o", "--output"}), glob_risk=True),
    "find": _reader(("posix",), unsafe_flags=frozenset(_FIND_UNSAFE), glob_risk=True),
    "file": _reader(("posix",), unsafe_flags=frozenset({"-m", "--magic-file", "-f", "--files-from"})),
    # ---- 子命令 / 解释器类 ----
    "cd": Spec(safe=_safe_cd),
    "git": Spec(
        safe=_safe_git, arity=2, sub_arity=(("git config", 3),),
        unsafe_flags=frozenset(_GIT_UNSAFE), glob_risk=True,
    ),
    "python": Spec(
        safe=_safe_version_only, mode_flags=("-m",), inline_flags=("-c",), script=True,
    ),
    "python3": Spec(
        safe=_safe_version_only, mode_flags=("-m",), inline_flags=("-c",), script=True,
    ),
    "py": Spec(
        safe=_safe_version_only, platforms=("win",),
        mode_flags=("-m",), inline_flags=("-c",), script=True,
    ),
    "node": Spec(safe=_safe_version_only, inline_flags=("-e", "--eval"), script=True),
    # ---- 开发工具链：只读用法 ----
    "pip": Spec(safe=_safe_pip, arity=2, unsafe_flags=frozenset(_PIP_UNSAFE)),
    "pip3": Spec(safe=_safe_pip, arity=2, unsafe_flags=frozenset(_PIP_UNSAFE)),
    "npm": Spec(safe=_safe_npm, arity=2, sub_arity=(("npm run", 3),)),
    "uv": Spec(
        safe=_safe_uv, arity=2, sub_arity=(("uv pip", 3),),
        unsafe_flags=frozenset(_UV_UNSAFE),
    ),
    "poetry": Spec(safe=_safe_poetry, arity=2),
    "ruff": Spec(safe=_safe_ruff, arity=2, unsafe_flags=frozenset(_RUFF_UNSAFE)),
    "pytest": Spec(safe=_safe_version_only, arity=1),
    "cargo": Spec(safe=_safe_version_only, arity=2),
    "rustc": Spec(safe=_safe_version_only),
    "go": Spec(safe=_safe_go, arity=2),
}


# ---------- 基础设施 ----------

def _is_windows() -> bool:
    return sys.platform.startswith("win")


def _platform() -> str:
    return "win" if _is_windows() else "posix"


def _name(token: str) -> str:
    """命令名归一化：Windows 下大小写不敏感。"""
    return token.lower() if _is_windows() else token


def _is_env_assignment(token: str) -> bool:
    """形如 VAR=value 的 inline 环境变量赋值前缀（如 `CI=true git commit`）。"""
    if "=" not in token:
        return False
    name = token.split("=", 1)[0]
    if not name or not (name[0].isalpha() or name[0] == "_"):
        return False
    return all(ch.isalnum() or ch == "_" for ch in name)


def _tokenize(segment: str):
    """按 shell 规则切词，返回 [(文本, 是否含未加引号 glob)]；引号不配对返回 None。

    单引号内字面；双引号内 \\ " $ ` 可转义；引号外反斜杠转义下一字符。
    """
    tokens = []
    buf = []
    quote = None
    glob = False
    started = False
    i, n = 0, len(segment)
    while i < n:
        c = segment[i]
        if quote == "'":
            if c == "'":
                quote = None
            else:
                buf.append(c)
            i += 1
            continue
        if quote == '"':
            if c == "\\" and i + 1 < n and segment[i + 1] in '"\\$`':
                buf.append(segment[i + 1])
                i += 2
                continue
            if c == '"':
                quote = None
            else:
                buf.append(c)
            i += 1
            continue
        if c in " \t":
            if started:
                tokens.append(("".join(buf), glob))
                buf = []
                glob = False
                started = False
            i += 1
            continue
        if c == "\\" and i + 1 < n:
            buf.append(segment[i + 1])
            started = True
            i += 2
            continue
        if c in "'\"":
            quote = c
            started = True
            i += 1
            continue
        if c in "*?[":
            glob = True
        buf.append(c)
        started = True
        i += 1
    if quote is not None:
        return None
    if started:
        tokens.append(("".join(buf), glob))
    return tokens


def _contains_substitution(segment: str) -> bool:
    """引号外或双引号内是否存在命令替换（$(...) 或反引号）；单引号内不算。"""
    quote = None
    i, n = 0, len(segment)
    while i < n:
        c = segment[i]
        if quote:
            if quote == '"' and c == "\\" and i + 1 < n:
                i += 2
                continue
            if c == quote:
                quote = None
            elif quote == '"' and (c == "`" or (c == "$" and segment[i + 1:i + 2] == "(")):
                return True
            i += 1
            continue
        if c in "'\"":
            quote = c
            i += 1
            continue
        if c == "`" or (c == "$" and segment[i + 1:i + 2] == "("):
            return True
        i += 1
    return False


def _has_unsafe_redirect(segment: str) -> bool:
    """是否存在「真正写/读文件」的重定向；仅放行丢弃目标与 fd 复制。

    放行：`> /dev/null`、`> NUL`、`2>&1`、`>&2`、`2>&-`、`< /dev/null`。
    其余（写普通文件、`< 文件`、here-doc/here-string）一律不安全。
    """
    quote = None
    i, n = 0, len(segment)
    while i < n:
        c = segment[i]
        if quote:
            if quote == '"' and c == "\\" and i + 1 < n:
                i += 2
                continue
            if c == quote:
                quote = None
            i += 1
            continue
        if c in "'\"":
            quote = c
            i += 1
            continue
        if c == "&" and segment[i + 1:i + 2] == ">":
            i += 1  # `&>`：交给下一轮对 '>' 的处理
            continue
        if c in "<>":
            j = i + 1
            if segment[j:j + 1] == c:
                j += 1  # `>>` / `<<`
            if c == ">" and segment[j:j + 1] == "&":
                j += 1  # fd 复制/关闭：2>&1、>&2、2>&-
                while j < n and (segment[j].isdigit() or segment[j] == "-"):
                    j += 1
                i = j
                continue
            k = j
            while k < n and segment[k] in " \t":
                k += 1
            target = []
            tq = None
            while k < n:
                ch = segment[k]
                if tq:
                    if tq == '"' and ch == "\\" and k + 1 < n:
                        target.append(segment[k + 1])
                        k += 2
                        continue
                    if ch == tq:
                        tq = None
                        k += 1
                        continue
                    target.append(ch)
                    k += 1
                    continue
                if ch in "'\"":
                    tq = ch
                    k += 1
                    continue
                if ch in " \t":
                    break
                target.append(ch)
                k += 1
            if "".join(target).lower() not in _DISCARD_TARGETS:
                return True
            i = k
            continue
        i += 1
    return False


def _within_allowed_roots(target: str) -> bool:
    try:
        dest = (Path(config.WORKSPACE_ROOT) / target).resolve()
    except OSError:
        return False
    for root in config.allowed_roots():
        try:
            if dest.is_relative_to(root):
                return True
        except (OSError, ValueError):
            continue
    return False


# ---------- 安全只读判定 ----------

def is_safe_command(segment: str) -> bool:
    """判断单个顶层命令段是否属安全只读命令。

    只接受裸命令名（无路径分隔符）；解析失败、含重定向/命令替换/环境变量
    前缀/危险标志/未加引号 glob 等一律返回 False（回退确认）。
    """
    segment = segment.strip()
    if not segment or len(segment) > MAX_SEGMENT_LEN:
        return False
    if _has_unsafe_redirect(segment) or _contains_substitution(segment):
        return False
    tokens = _tokenize(segment)
    if not tokens:
        return False
    first = tokens[0][0]
    if _is_env_assignment(first):
        return False
    if "/" in first or "\\" in first:
        return False
    name = _name(first)
    spec = COMMANDS.get(name)
    if spec is None or _platform() not in spec.platforms:
        return False
    if spec.glob_risk and any(glob for _, glob in tokens[1:]):
        return False
    if spec.unsafe_flags and any(
        t.startswith("-") and t in spec.unsafe_flags for t, _ in tokens[1:]
    ):
        return False
    return spec.safe([t for t, _ in tokens[1:]])


# ---------- 前缀推导（"总是允许"记忆用） ----------

def _strip_env_prefix(argv):
    """剥掉开头连续的 VAR=value（inline 环境变量赋值）。"""
    i = 0
    while i < len(argv) and _is_env_assignment(argv[i]):
        i += 1
    return argv[i:]


def _unwrap_wrappers(argv):
    """剥已知包装器：env [VAR=...] cmd... / command cmd...（V1 最小集）。"""
    while argv:
        if argv[0] == "env":
            argv = _strip_env_prefix(argv[1:])
            continue
        if argv[0] == "command":
            argv = argv[1:]
            continue
        break
    return argv


def _contiguous_nonflag(argv):
    """从头开始连续的非标志 token（遇到第一个标志即停）。"""
    out = []
    for token in argv:
        if token.startswith("-"):
            break
        out.append(token)
    return out


def _launcher_key(argv, spec):
    """解释器/启动器形态的 key：保留 mode 标志或脚本名。"""
    for i, token in enumerate(argv[1:], start=1):
        if token in spec.mode_flags:
            if i + 1 >= len(argv):
                return None
            return (argv[0], token, argv[i + 1]) + tuple(_contiguous_nonflag(argv[i + 2:]))
        if token in spec.inline_flags:
            return None
        if not token.startswith("-") and spec.script:
            return (argv[0], token) + tuple(_contiguous_nonflag(argv[i + 1:]))
    return None


def command_key(segment: str):
    """把一段命令归一化成稳定 key（供前缀规则匹配）；无法可靠归一化返回 None。

    - 剥 inline 环境变量前缀与 env/command 包装器；
    - 解释器：`python -m <mod>` 保留 `-m` 与模块名，`python <script>` 保留脚本名，
      `python -c` / 裸解释器返回 None；
    - shell（未登记）一律 None；
    - 其余：从头连续的非标志 token（如 (git, commit)、(npm, run, test)、(pytest,)）。
    """
    if not segment or _contains_substitution(segment):
        return None
    tokens = _tokenize(segment)
    if not tokens:
        return None
    argv = _unwrap_wrappers(_strip_env_prefix([t for t, _ in tokens]))
    if not argv:
        return None
    spec = COMMANDS.get(_name(argv[0]))
    if spec is None or _platform() not in spec.platforms:
        return None
    if spec.inline_flags and any(t in spec.inline_flags for t in argv[1:]):
        return None
    if spec.mode_flags or spec.script:
        return _launcher_key(argv, spec)
    return tuple(_contiguous_nonflag(argv))


def _launcher_prefix(key, spec):
    """解释器形态的切点：`-m 模块` 切 3，脚本切 2。"""
    if spec.mode_flags and key[1:2] and key[1] in spec.mode_flags:
        return key[:3] if len(key) >= 3 else None
    if spec.script and len(key) >= 2:
        return key[:2]
    return None


def _lookup_arity(key, spec):
    """在 key 上做最长子命令路径匹配，返回 arity；无命中用 spec.arity。"""
    arity = spec.arity
    for length in range(1, len(key) + 1):
        path = " ".join(key[:length])
        for sub_path, sub_arity in spec.sub_arity:
            if sub_path == path:
                arity = sub_arity
    return arity


def _has_unsafe_flag(segment, spec) -> bool:
    if not spec.unsafe_flags:
        return False
    tokens = _tokenize(segment)
    return bool(tokens) and any(
        t.startswith("-") and t in spec.unsafe_flags for t, _ in tokens[1:]
    )


def derive_prefix(segment: str):
    """推导"总是允许"要记忆的前缀；None 表示退回精确记忆整段。"""
    key = command_key(segment)
    if key is None:
        return None
    spec = COMMANDS.get(_name(key[0]))
    if spec is None:
        return None
    if spec.mode_flags or spec.inline_flags or spec.script:
        prefix = _launcher_prefix(key, spec)
    else:
        arity = _lookup_arity(key, spec)
        if arity is None or arity > len(key):
            return None
        prefix = key[:arity]
    if prefix is None:
        return None
    if _has_unsafe_flag(segment, spec):
        return None
    if prefix in BANNED_PREFIXES:
        return None
    return prefix


def is_dir_change(segment: str) -> bool:
    """该段是否为「改变目录」的 cd（无参、标志、或目标与当前目录不同）。"""
    tokens = _tokenize(segment)
    if not tokens or _name(tokens[0][0]) != "cd":
        return False
    if len(tokens) != 2:
        return True
    target = tokens[1][0]
    if target.startswith("-"):
        return True
    try:
        current = Path(config.WORKSPACE_ROOT).resolve()
        return (current / target).resolve() != current
    except OSError:
        return True


def is_git_command(segment: str) -> bool:
    """该段是否为 git 命令（供 cd+git 组合守卫使用）。"""
    tokens = _tokenize(segment)
    return bool(tokens) and _name(tokens[0][0]) == "git"
