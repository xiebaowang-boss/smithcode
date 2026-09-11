"""SKILL.md frontmatter 的宽容解析（纯函数，不依赖第三方 YAML）。

Agent Skills 规范的 frontmatter 是 YAML，但 SmithCode 只需要 name / description
等少量标量字段；为遵守"标准库优先"的依赖纪律，本模块实现一个受限解析器：

- 行式 `key: value`（按首个冒号切分，天然容忍 description 里的冒号）；
- 引号标量（'...' / "..."）与块标量（| / > 及 -、+ 修饰，折叠或保留换行）；
- 只有不缩进的顶层行才起新字段，缩进内容归属上一个字段或被忽略；
- 未知键与嵌套结构不报错，原样存入 meta，由调用方决定用不用。

完全解析失败时返回空 meta 与警告，由调用方决定跳过该技能。
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Frontmatter:
    """解析结果：meta 为顶层标量键值，body 为 frontmatter 之后的正文。"""

    meta: dict = field(default_factory=dict)
    body: str = ""
    warnings: list = field(default_factory=list)


def parse(text: str) -> Frontmatter:
    """解析 SKILL.md 文本；宽容处理其他客户端产出的非严格 YAML。"""
    warnings = []
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").removeprefix("\ufeff")
    lines = normalized.split("\n")

    start = 0
    while start < len(lines) and not lines[start].strip():
        start += 1
    if start >= len(lines) or lines[start].strip() != "---":
        warnings.append("缺少 frontmatter（文件首行应为 ---）")
        return Frontmatter({}, normalized.strip("\n"), warnings)

    end = -1
    for i in range(start + 1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end < 0:
        warnings.append("frontmatter 未闭合（缺少结束的 ---）")
        return Frontmatter({}, "", warnings)

    meta = _parse_block(lines[start + 1:end], warnings)
    body = "\n".join(lines[end + 1:]).strip("\n")
    return Frontmatter(meta, body, warnings)


def _parse_block(lines: list, warnings: list) -> dict:
    meta: dict = {}
    i = 0
    n = len(lines)
    while i < n:
        raw = lines[i]
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            i += 1
            continue
        if raw[0] in (" ", "\t"):
            i += 1  # 孤立缩进行（属于被忽略的嵌套结构），跳过
            continue
        if ":" not in raw:
            warnings.append(f"忽略无法解析的行: {stripped[:40]}")
            i += 1
            continue

        key, _, value = raw.partition(":")
        key = key.strip()
        value = value.strip()
        if not key:
            i += 1
            continue

        if _is_block_header(value):
            block, i = _read_indented(lines, i + 1)
            _store(meta, key, _render_block(block, folded=value.startswith(">")), warnings)
            continue
        if value == "" and _next_is_indented(lines, i + 1):
            # 无块指示符的缩进续行（YAML 多行纯标量），折叠为一行
            block, i = _read_indented(lines, i + 1)
            joined = " ".join(part.strip() for part in block if part.strip())
            _store(meta, key, joined, warnings)
            continue
        _store(meta, key, _unquote(value), warnings)
        i += 1
    return meta


def _is_block_header(value: str) -> bool:
    """形如 |、|-、>-、|2 的块标量指示符。"""
    if not value or value[0] not in "|>":
        return False
    return all(c in "-+0123456789" for c in value[1:])


def _read_indented(lines: list, i: int) -> tuple:
    """从 i 起读取缩进（或空行）块，返回 (块行, 新下标)。"""
    block = []
    n = len(lines)
    while i < n:
        line = lines[i]
        if not line.strip():
            block.append("")
            i += 1
            continue
        if line[0] in (" ", "\t"):
            block.append(line)
            i += 1
            continue
        break
    return block, i


def _next_is_indented(lines: list, i: int) -> bool:
    return i < len(lines) and bool(lines[i].strip()) and lines[i][0] in (" ", "\t")


def _render_block(block: list, folded: bool) -> str:
    """去掉公共缩进；`>` 折叠为段落（段内空格连接），`|` 保留换行。"""
    nonempty = [line for line in block if line.strip()]
    if not nonempty:
        return ""
    indent = min(len(line) - len(line.lstrip()) for line in nonempty)
    dedented = [line[indent:] if len(line) >= indent else "" for line in block]
    if not folded:
        return "\n".join(dedented).strip("\n")
    paragraphs = []
    current = []
    for line in dedented:
        if line.strip():
            current.append(line.strip())
        elif current:
            paragraphs.append(" ".join(current))
            current = []
    if current:
        paragraphs.append(" ".join(current))
    return "\n".join(paragraphs)


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        inner = value[1:-1]
        if value[0] == '"':
            inner = (
                inner.replace("\\\\", "\x00")
                .replace('\\"', '"')
                .replace("\\n", "\n")
                .replace("\x00", "\\")
            )
        else:
            inner = inner.replace("''", "'")
        return inner
    return value


def _store(meta: dict, key: str, value: str, warnings: list) -> None:
    if key in meta:
        warnings.append(f"frontmatter 重复键 {key}（后者生效）")
    meta[key] = value
