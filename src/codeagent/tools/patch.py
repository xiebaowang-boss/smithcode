"""apply_patch 工具：用 opencode 风格的 patch 信封批量应用多文件修改，原子落盘。

格式（上下文锚定、无行号漂移问题，与 edit_file 的精确匹配哲学一致）：

    *** Begin Patch
    *** Add File: <path>            # 新建文件，后续每行 + 前缀即文件内容
    +内容行
    *** Update File: <path>         # 修改文件：- 删除行、+ 新增行、空格开头为上下文
    @@ 可选分块锚点 @@              # @@ 行分隔多个修改块
    -旧行
    +新行
     上下文行
    *** Delete File: <path>         # 删除文件
    *** End Patch

Update 的每个块按"上下文 + 删除行"组成的原文在文件中唯一匹配后替换（同 edit_file 语义）；
任一步失败则整体不落盘（原子性）。所有目标路径经 _resolve 沙箱校验。
"""
from .base import register
from .files import _resolve

_HEADER_ADD = "*** Add File:"
_HEADER_UPDATE = "*** Update File:"
_HEADER_DELETE = "*** Delete File:"


def _parse_patch(patch: str) -> list[tuple]:
    """把 patch 文本解析为 [(action, path, payload)]；action ∈ {"add","update","delete"}。

    add 的 payload 为文件内容行（已去 + 前缀）；update 的 payload 为原始行（含 +/-/空格/@@）。
    """
    ops = []
    lines = patch.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith(_HEADER_ADD):
            path = line[len(_HEADER_ADD):].strip()
            payload = []
            i += 1
            while i < len(lines) and not lines[i].startswith("***"):
                payload.append(lines[i].removeprefix("+"))
                i += 1
            ops.append(("add", path, payload))
            continue
        if line.startswith(_HEADER_UPDATE):
            path = line[len(_HEADER_UPDATE):].strip()
            payload = []
            i += 1
            while i < len(lines) and not lines[i].startswith("***"):
                payload.append(lines[i])
                i += 1
            ops.append(("update", path, payload))
            continue
        if line.startswith(_HEADER_DELETE):
            path = line[len(_HEADER_DELETE):].strip()
            ops.append(("delete", path, None))
            i += 1
            continue
        i += 1
    return ops


def extract_patch_paths(args: dict) -> list[str]:
    """从工具参数提取 patch 涉及的全部目标路径（供权限聚合与越界预检）。"""
    patch = args.get("patch", "")
    return [path for _, path, _ in _parse_patch(patch) if path]


def _describe_patch(args: dict) -> str:
    paths = extract_patch_paths(args)
    return " ".join(["patch", *paths]) if paths else "patch"


def _parse_update_blocks(payload: list[str]) -> list[tuple[str, str]]:
    """把 Update 段解析为 [(old_block, new_block)]，@@ 行作为块分隔符。"""
    blocks = []
    cur_old, cur_new = [], []

    def flush():
        if cur_old or cur_new:
            blocks.append(("\n".join(cur_old), "\n".join(cur_new)))
            cur_old.clear()
            cur_new.clear()

    for raw in payload:
        if raw.startswith("@@"):
            flush()
            continue
        if raw.startswith("-"):
            cur_old.append(raw[1:])
        elif raw.startswith("+"):
            cur_new.append(raw[1:])
        elif raw.startswith(" "):
            cur_old.append(raw[1:])
            cur_new.append(raw[1:])
        else:  # 裸行按上下文处理，宽容对待
            cur_old.append(raw)
            cur_new.append(raw)
    flush()
    return blocks


def _apply_blocks_to_text(text: str, blocks: list[tuple[str, str]], path: str) -> str:
    """逐块替换；任一块不唯一匹配即返回错误串，不修改 text。"""
    for old_block, new_block in blocks:
        if not old_block:
            continue
        count = text.count(old_block)
        if count == 0:
            return f"错误: {path} 中未找到待替换内容（检查上下文与 +/- 行）"
        if count > 1:
            return f"错误: {path} 中待替换内容匹配 {count} 处，需要更多上下文"
        text = text.replace(old_block, new_block)
    return text


@register(
    {
        "name": "apply_patch",
        "family": "edit_file",
        "paths_from": extract_patch_paths,
        "describe": _describe_patch,
        "description": "用 patch 信封批量应用多文件修改（Add/Update/Delete），原子落盘。"
        "权限与 edit_file 一致。小改动用 edit_file，多文件/大改动用本工具。",
        "parameters": {
            "type": "object",
            "properties": {
                "patch": {
                    "type": "string",
                    "description": (
                        "patch 文本，格式:\n"
                        "*** Begin Patch\n"
                        "*** Add File: <path>\n"
                        "+内容行\n"
                        "*** Update File: <path>\n"
                        "@@ 可选分块锚点 @@\n"
                        "-待删除的旧行\n"
                        "+新增行\n"
                        " 上下文行（空格开头）\n"
                        "*** Delete File: <path>\n"
                        "*** End Patch"
                    ),
                },
            },
            "required": ["patch"],
        },
    }
)
def apply_patch(patch: str) -> str:
    ops = _parse_patch(patch)
    if not ops:
        return "错误: 无法解析 patch（缺少 *** Add/Update/Delete File 段）"

    # 阶段一：全部在内存中准备，任一失败即整体放弃（原子性）
    prepared = []
    for action, path, payload in ops:
        p = _resolve(path)  # 越界直接抛 PermissionError
        if action == "add":
            if p.exists():
                return f"错误: {path} 已存在，请改用 Update File"
            prepared.append(("write", p, "\n".join(payload)))
        elif action == "delete":
            if not p.exists():
                return f"错误: 要删除的文件不存在: {path}"
            prepared.append(("delete", p, None))
        else:  # update
            if not p.exists():
                return f"错误: 要更新的文件不存在: {path}"
            blocks = _parse_update_blocks(payload)
            new_text = _apply_blocks_to_text(p.read_text(encoding="utf-8"), blocks, path)
            if isinstance(new_text, str) and new_text.startswith("错误"):
                return new_text
            prepared.append(("write", p, new_text))

    # 阶段二：全部就绪才写盘
    lines = []
    for action, p, content in prepared:
        if action == "delete":
            p.unlink()
            lines.append(f"- {p}")
        else:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
            lines.append(f"+ {p}")
    return "已应用:\n" + "\n".join(lines)