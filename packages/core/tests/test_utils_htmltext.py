"""HTML → Markdown 转换测试（纯函数，不联网）。"""
from smithcode.utils import htmltext


def test_headings_and_paragraphs():
    out = htmltext.to_markdown("<h1>标题</h1><p>正文</p><h3>小标题</h3>")
    assert "# 标题" in out
    assert "正文" in out
    assert "### 小标题" in out


def test_links_preserve_href():
    out = htmltext.to_markdown('<p>见 <a href="https://ex.com/doc">文档</a></p>')
    assert "[文档](https://ex.com/doc)" in out


def test_link_without_href_keeps_text():
    assert htmltext.to_markdown('<a name="x">锚点</a>').strip() == "锚点"


def test_link_wraps_nested_inline_markup():
    out = htmltext.to_markdown('<a href="https://ex.com"><b>粗体链接</b></a>')
    assert "[**粗体链接**](https://ex.com)" in out


def test_code_block_with_language_and_indent():
    out = htmltext.to_markdown(
        '<pre><code class="language-python">def f():\n    return 1</code></pre>'
    )
    assert "```python" in out
    assert "    return 1" in out  # 代码缩进必须原样保留


def test_code_block_dedents_common_page_indent():
    out = htmltext.to_markdown("<pre>        line1\n        line2</pre>")
    assert "\nline1" in out and "\nline2" in out
    assert "        line1" not in out


def test_inline_code_and_emphasis():
    out = htmltext.to_markdown(
        "<p><code>x = 1</code> 与 <strong>重点</strong>、<em>斜体</em></p>"
    )
    assert "`x = 1`" in out
    assert "**重点**" in out
    assert "*斜体*" in out


def test_lists_with_nesting():
    out = htmltext.to_markdown("<ul><li>一</li><li>二<ul><li>嵌套</li></ul></li></ul>")
    assert "- 一" in out
    assert "  - 嵌套" in out  # 嵌套缩进保留


def test_ordered_list_items():
    out = htmltext.to_markdown("<ol><li>第一</li><li>第二</li></ol>")
    assert "1. 第一" in out and "1. 第二" in out


def test_table_rows_joined_with_separator():
    out = htmltext.to_markdown(
        "<table><tr><th>A</th><th>B</th></tr><tr><td>1</td><td>2</td></tr></table>"
    )
    assert "A | B" in out
    assert "1 | 2" in out


def test_table_emits_gfm_header_and_divider():
    """GFM 表格必须有分隔行，否则渲染器认不出这是表格。"""
    out = htmltext.to_markdown(
        "<table><tr><th>A</th><th>B</th></tr><tr><td>1</td><td>2</td></tr></table>"
    )
    assert "| A | B |" in out
    assert "| --- | --- |" in out
    assert "| 1 | 2 |" in out


def test_table_without_th_promotes_first_row():
    """无 <th> 的表格把首行当表头（GFM 必须有个表头行）。"""
    out = htmltext.to_markdown(
        "<table><tr><td>1</td><td>2</td></tr><tr><td>3</td><td>4</td></tr></table>"
    )
    lines = [line for line in out.splitlines() if line.strip()]
    assert lines[0] == "| 1 | 2 |"
    assert lines[1] == "| --- | --- |"
    assert lines[2] == "| 3 | 4 |"


def test_table_reads_thead_and_tbody():
    """真实页面普遍包 thead/tbody，分隔行仍须落在表头之后。"""
    out = htmltext.to_markdown(
        "<table><thead><tr><th>A</th><th>B</th></tr></thead>"
        "<tbody><tr><td>1</td><td>2</td></tr></tbody></table>"
    )
    assert "| A | B |" in out and "| --- | --- |" in out and "| 1 | 2 |" in out


def test_table_block_inside_cell_stays_on_one_row():
    """格内的块级标签（如 <p>）不能把一行拆成多行——那会破坏表格结构。"""
    out = htmltext.to_markdown(
        "<table><tr><th>H</th></tr><tr><td><p>x</p></td></tr></table>"
    )
    assert "| x |" in out
    assert "x\n" not in out  # 不得单独成行


def test_table_alignment_markers():
    out = htmltext.to_markdown(
        '<table><tr><th align="left">L</th><th align="right">R</th>'
        '<th style="text-align: center">C</th></tr>'
        "<tr><td>1</td><td>2</td><td>3</td></tr></table>"
    )
    assert "| :--- | ---: | :---: |" in out


def test_table_escapes_pipe_in_cell():
    out = htmltext.to_markdown(
        "<table><tr><th>a|b</th></tr><tr><td>1</td></tr></table>"
    )
    assert "a\\|b" in out


def test_table_pads_short_rows():
    """列数不齐的行补齐空单元格，避免渲染时错位。"""
    out = htmltext.to_markdown(
        "<table><tr><th>A</th><th>B</th><th>C</th></tr>"
        "<tr><td>1</td><td>2</td></tr></table>"
    )
    assert "| 1 | 2 |  |" in out


def test_table_keeps_inline_markup_in_cells():
    out = htmltext.to_markdown(
        '<table><tr><th>链接</th><th>强调</th></tr>'
        '<tr><td><a href="https://x.com">X</a></td><td><b>粗</b></td></tr></table>'
    )
    assert "[X](https://x.com)" in out
    assert "**粗**" in out


def test_blockquote_gets_prefix():
    assert "> 引用" in htmltext.to_markdown("<blockquote><p>引用</p></blockquote>")


def test_script_style_and_head_are_dropped():
    out = htmltext.to_markdown(
        "<html><head><title>标题不应出现</title><style>a{}</style></head>"
        "<body><script>evil()</script><p>正文</p></body></html>"
    )
    assert "正文" in out
    for junk in ("evil", "a{}", "标题不应出现"):
        assert junk not in out


def test_entities_decoded():
    out = htmltext.to_markdown("<p>A &amp; B &lt;tag&gt; &#20320;</p>")
    assert "A & B" in out
    assert "<tag>" in out
    assert "你" in out


def test_images_and_hr():
    out = htmltext.to_markdown('<p><img src="a.png" alt="图"></p><hr>')
    assert "![图](a.png)" in out
    assert "---" in out


def test_br_splits_line():
    out = htmltext.to_markdown("<p>第一行<br>第二行</p>")
    assert "第一行" in out and "第二行" in out


def test_whitespace_collapsed_within_paragraph():
    assert "a b c" in htmltext.to_markdown("<p>a\n   b\t\tc</p>")


def test_malformed_html_does_not_raise():
    out = htmltext.to_markdown("<p>未闭合 <b>粗 <div>")
    assert "未闭合" in out and "粗" in out


def test_empty_input():
    assert htmltext.to_markdown("") == ""
