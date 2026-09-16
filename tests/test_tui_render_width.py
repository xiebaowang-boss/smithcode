"""render_markdown 的宽度契约回归。

单独成文件（不塞进 test_tui.py）：这里只钉纯函数的换行宽度，不涉及界面装配。
"""
from smithcode.tui.render import render_markdown


def test_render_markdown_honors_width_under_dumb_terminal(monkeypatch):
    """传什么宽度就按什么宽度折行——包括 TERM=dumb 的终端。

    rich 的 `Console.size` 在只显式传 width、不传 height 时不会直接返回给定
    尺寸：`TERM=dumb` 会短路成固定 (80, 25)，把 width 丢掉，正文于是永远按
    80 列折行、TUI 窗口缩放后不再重排（MessageBody 按 self.size.width 调本函数，
    宽度变了但结果一样）。height 与换行无关，只是锁住宽度的必要条件。
    """
    monkeypatch.setenv("TERM", "dumb")
    text = " ".join(f"w{i:03d}" for i in range(60))

    narrow = render_markdown(text, 40).plain.split("\n")
    wide = render_markdown(text, 120).plain.split("\n")

    assert len(wide) < len(narrow)  # 变宽 → 行数变少

    def flat(lines):  # rich 会把每行补白到渲染宽度，比对内容时去掉空白
        return "".join("".join(line.split()) for line in lines)

    assert flat(wide) == flat(narrow)  # 只改折行，内容一字不丢


def test_render_markdown_survives_very_narrow_width(monkeypatch):
    monkeypatch.setenv("TERM", "dumb")
    assert render_markdown("hello world", 5).plain.strip()
