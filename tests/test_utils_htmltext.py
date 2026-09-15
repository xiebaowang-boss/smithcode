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
