"""HTML → 结构化文本（Markdown 风格）：供 webfetch 抓取后做可读化处理。

纯标准库实现（`html.parser.HTMLParser`），不引入新依赖。相比"正则去标签"，
这里保留对模型真正有用的骨架：标题层级、链接地址、代码块、列表、表格与引用
——读文档时"链接去哪"和"代码长什么样"往往就是重点，正则版会全部丢掉。

转换是有损且刻意保守的：脚本 / 样式 / 导航等一律丢弃，只留正文结构；产出的是
给模型读的 Markdown，不追求与浏览器渲染一致。
"""
from __future__ import annotations

import re
from html.parser import HTMLParser

# 整块丢弃内容的标签（含 <head>：避免 meta / title 噪声混进正文）
_SKIP_TAGS = frozenset({
    "script", "style", "noscript", "template", "iframe", "object", "embed",
    "svg", "canvas", "head",
})

# 块级标签：开始新的一段（前后各留一个空行）
_BLOCK_TAGS = frozenset({
    "address", "article", "aside", "details", "div", "dl", "dd", "dt",
    "fieldset", "figcaption", "figure", "footer", "form", "header", "main",
    "nav", "p", "section", "summary", "table", "tbody", "thead", "tfoot", "tr",
})

# 行内强调标签 → Markdown 标记
_INLINE_MARKS = {"strong": "**", "b": "**", "em": "*", "i": "*"}

_CODE_FENCE = "```"
_MAX_INDENT_TRIM = 12  # <pre> 整体缩进超过此值就不再判定为"统一缩进"（可能是列表内代码）


class _Converter(HTMLParser):
    """HTML → Markdown 的流式转换器（单次遍历，遇块即落行）。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.lines: list[str] = []          # 已落盘的行（"" 表示空行分隔）
        self._buf: list[str] = []           # 当前行的行内片段
        self._prefix = ""                   # 当前行前缀（列表标记等，落行后清空）
        self._skip_depth = 0                # 处于丢弃块（script / head 等）内的层数
        self._pre: list[str] | None = None  # <pre> 原始文本（None = 不在 pre 内）
        self._pre_lang = ""                 # 代码语言（来自 <code class="language-x">）
        self._heading = 0                   # 当前标题层级（0 = 不在标题内）
        self._list_stack: list[str] = []    # 打开的 ul / ol
        self._li_depth = 0                  # 打开的 li 层数（其内块级标签视为行内）
        self._links: list[tuple[int, str]] = []  # (行内缓冲下标, href)
        self._marks: list[str] = []         # 待闭合的行内标记栈（** / * / `）
        self._quote_depth = 0               # 引用层数（落行时加 "> " 前缀）

    # ---------- 对外 ----------

    def result(self) -> str:
        self._flush()
        text = re.sub(r"\n{3,}", "\n\n", "\n".join(self.lines))
        return text.strip()

    # ---------- 行与段的落盘 ----------

    def _blank(self) -> None:
        if self.lines and self.lines[-1] != "":
            self.lines.append("")

    def _flush(self, prefix: str = "") -> None:
        """把行内缓冲落成一行；空内容（含只剩列表标记）不产出。"""
        text = re.sub(r"\s+", " ", "".join(self._buf)).strip()
        self._buf.clear()
        head, self._prefix = self._prefix, ""
        if not text:
            return
        line = ("> " * self._quote_depth) + prefix + head + text
        self.lines.append(line.rstrip())  # 前导缩进（嵌套列表）必须原样保留

    def _block(self) -> None:
        """结束当前段：落行 + 空行分隔。"""
        self._flush()
        self._blank()

    # ---------- 标签处理 ----------

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if self._pre is not None:  # <pre> 内只关心代码语言
            if tag == "code":
                self._pre_lang = _code_language(attrs)
            return

        if tag in _INLINE_MARKS:
            mark = _INLINE_MARKS[tag]
            self._buf.append(mark)
            self._marks.append(mark)
            return
        if tag == "code":
            self._buf.append("`")
            self._marks.append("`")
            return
        if tag == "a":
            self._links.append((len(self._buf), _attr(attrs, "href")))
            return
        if tag == "img":
            src = _attr(attrs, "src")
            if src:
                self._buf.append(f"![{_attr(attrs, 'alt')}]({src})")
            return
        if tag == "br":
            self._flush()
            return
        if tag == "hr":
            self._block()
            self.lines.append("---")
            self._blank()
            return
        if tag == "pre":
            self._block()
            self._pre, self._pre_lang = [], ""
            return
        if len(tag) == 2 and tag[0] == "h" and tag[1].isdigit():
            self._block()
            self._heading = int(tag[1])
            return
        if tag == "blockquote":
            self._flush()
            self._quote_depth += 1
            return
        if tag == "li":
            self._flush()
            marker = "1. " if self._list_stack and self._list_stack[-1] == "ol" else "- "
            self._prefix = "  " * max(0, len(self._list_stack) - 1) + marker
            self._li_depth += 1
            return
        if tag in ("ul", "ol"):
            self._flush()
            self._list_stack.append(tag)
            return
        if tag in ("td", "th"):
            if "".join(self._buf).strip():
                self._buf.append(" | ")
            return
        if tag == "tr":
            self._flush()
            return
        if tag == "dt":
            self._flush()
            return
        if tag == "dd":
            self._flush()
            self._prefix = "- "
            return
        if tag in _BLOCK_TAGS:
            if self._li_depth:
                return  # 列表项内部的块级标签不切段（<li><p>x</p></li> 要落成一行）
            self._block()

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in _SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skip_depth:
            return
        if tag == "pre":
            self._emit_code()
            return
        if self._pre is not None:
            return

        if tag in _INLINE_MARKS:
            self._close_mark(_INLINE_MARKS[tag])
            return
        if tag == "code":
            self._close_mark("`")
            return
        if tag == "a":
            self._close_link()
            return
        if tag == "li":
            self._flush()
            self._li_depth = max(0, self._li_depth - 1)
            return
        if tag in ("ul", "ol"):
            self._flush()
            if self._list_stack:
                self._list_stack.pop()
            self._blank()
            return
        if tag == "blockquote":
            self._flush()
            self._quote_depth = max(0, self._quote_depth - 1)
            self._blank()
            return
        if len(tag) == 2 and tag[0] == "h" and tag[1].isdigit():
            self._flush(prefix="#" * self._heading + " ")
            self._heading = 0
            self._blank()
            return
        if tag == "tr":
            self._flush()
            return
        if tag in _BLOCK_TAGS:
            if self._li_depth:
                return
            self._block()

    def handle_data(self, data: str) -> None:
        if self._skip_depth or not data:
            return
        if self._pre is not None:
            self._pre.append(data)
            return
        self._buf.append(data)

    # ---------- 片段收尾 ----------

    def _close_mark(self, mark: str) -> None:
        """闭合行内标记；标签不配对时忽略（容错优先，不产出半个标记）。"""
        if self._marks and self._marks[-1] == mark:
            self._marks.pop()
            self._buf.append(mark)

    def _close_link(self) -> None:
        if not self._links:
            return
        index, href = self._links.pop()
        inner = re.sub(r"\s+", " ", "".join(self._buf[index:])).strip()
        del self._buf[index:]
        if not inner:
            return
        if href and href not in ("#", "javascript:void(0)"):
            self._buf.append(f"[{inner}]({href})")
        else:
            self._buf.append(inner)

    def _emit_code(self) -> None:
        raw = "".join(self._pre or [])
        self._pre = None
        lines = raw.replace("\r\n", "\n").replace("\r", "\n").strip("\n").split("\n")
        # 页面常把 <pre> 整体缩进：统一去掉公共缩进，避免代码块左移不齐
        indents = [len(line) - len(line.lstrip()) for line in lines if line.strip()]
        shift = min(indents) if indents else 0
        if 0 < shift <= _MAX_INDENT_TRIM:
            lines = [line[shift:] if line.strip() else "" for line in lines]
        if not any(line.strip() for line in lines):
            self._blank()
            return
        self.lines.append(_CODE_FENCE + self._pre_lang)
        self.lines.extend(lines)
        self.lines.append(_CODE_FENCE)
        self._blank()


def _attr(attrs, name: str) -> str:
    """取属性值（HTMLParser 的属性列表是无序对，且值可能为 None）。"""
    for key, value in attrs:
        if key.lower() == name:
            return (value or "").strip()
    return ""


def _code_language(attrs) -> str:
    """从 `class="language-python highlight"` 里提取 `python`。"""
    match = re.search(r"(?:^|\s)language-([\w+#.-]+)", _attr(attrs, "class"))
    return match.group(1) if match else ""


def to_markdown(html: str) -> str:
    """把 HTML 转成给模型读的 Markdown 风格文本（失败时退回部分结果，不抛异常）。"""
    if not html:
        return ""
    parser = _Converter()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # noqa: BLE001, S110 - 畸形页面宁可少给内容，也不能让抓取整体失败
        pass
    return parser.result()
