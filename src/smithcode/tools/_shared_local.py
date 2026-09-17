"""本地检索共享基座：SKIP_DIRS、沙箱根判定、有序遍历、行截断。

glob 工具（文件名通配）与 grep 工具（内容正则）共用本模块，避免两份
拷贝漂移。注意：本模块自身不注册任何工具。
"""
from __future__ import annotations

import os
from pathlib import Path

from .. import config

# 检索时跳过的目录：依赖、缓存、版本控制等对定位代码没有价值
SKIP_DIRS = {
    ".git", ".idea", ".vscode", ".pytest_cache", ".ruff_cache",
    "__pycache__", "node_modules", ".venv", "venv", "dist", "build",
    "sessions",
}
MAX_RESULTS = 100  # 单次最多返回的文件数 / 匹配行数
MAX_LINE_LEN = 200  # 单行匹配内容展示的最大长度（超出截断并加 … 标记）
# 无匹配全量扫描的熔断预算：超限即停并提示收窄，避免大仓库一次检索读完整个工作区
MAX_SCAN_FILES = 5000  # 最多扫描的文件数（glob 按遍历项计，grep 按候选文件计）
MAX_SCAN_BYTES = 50_000_000  # 累计扫描字节上限（grep 按 st_size 累加，约 50MB）
# 超长行截断标记：该行未展示全貌，不可逐字复制成 edit_file 的 old_string
TRUNC_SUFFIX = "…"
# 匹配内容一律按原文输出（不去缩进）：Agent 会直接复制到 edit_file 的 old_string，
# 行首缩进一旦被吃掉，多行锚点就与文件内容对不上、必然报「old_string 未找到」。


def roots(path: str) -> tuple:
    """解析搜索起始路径，返回 (命中的根, 起始路径)。越界直接拒绝。

    相对路径锚定主工作区；起始路径落在任一读根（授权目录 + 技能目录只读白名单）
    内即放行。单个命中的文件经符号链接指向他根时，展示路径按目标实际所在的根
    计算（见 containing_root），此处返回的根只做越界判定。
    """
    base = (Path(config.WORKSPACE_ROOT) / path).resolve()
    for root in config.read_roots():
        if base.is_relative_to(root):
            return root, base
    raise PermissionError(f"路径越界: {path}")


def containing_root(rp: Path, roots: list[Path]) -> Path | None:
    """目标实际所在的读根（符号链接跨根时与起始根可能不同）。"""
    for root in roots:
        if rp.is_relative_to(root):
            return root
    return None


def clip_line(line: str) -> str:
    """超长行截断并打标记：静默截断会让 Agent 复制半行去 edit，必然「未找到」。"""
    if len(line) <= MAX_LINE_LEN:
        return line
    return line[:MAX_LINE_LEN] + TRUNC_SUFFIX


def walk_entries(base: Path, yield_dirs: bool):
    """os.scandir 递归遍历：有序、预剪 SKIP_DIRS、不跟随符号链接目录。

    不用 Path.glob("**") 的原因：glob 会先深入 .git / node_modules 再事后
    过滤，IO 已经花掉；这里在入口处剪掉整棵子树。产出 lexical 路径，调用方
    自行 resolve 做沙箱校验。目录与文件按名称排序，遍历顺序确定。
    符号链接目录按文件产出、不深入（防循环与越界遍历）。
    """
    stack = [base]
    while stack:
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                entries = sorted(it, key=lambda e: e.name.lower())
        except OSError:
            continue
        subdirs: list[str] = []
        for entry in entries:
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError:
                continue  # 坏链接 / 权限不足：跳过该项
            if is_dir:
                if entry.name in SKIP_DIRS:
                    continue
                subdirs.append(entry.path)
                if yield_dirs:
                    yield Path(entry.path)
            else:
                yield Path(entry.path)
        for d in reversed(subdirs):  # 逆序压栈，弹出即正序，保证深搜确定性
            stack.append(Path(d))
