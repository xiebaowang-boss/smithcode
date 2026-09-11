"""SKILL.md frontmatter 解析器测试：正常字段、宽容解析与失败降级。"""

from smithcode.skills import frontmatter


def test_parse_basic_fields():
    fm = frontmatter.parse(
        "---\nname: pdf-processing\ndescription: 处理 PDF 文件\n---\n\n正文第一行\n正文第二行\n"
    )
    assert fm.meta["name"] == "pdf-processing"
    assert fm.meta["description"] == "处理 PDF 文件"
    assert fm.body == "正文第一行\n正文第二行"
    assert fm.warnings == []


def test_parse_quoted_values():
    fm = frontmatter.parse(
        "---\n"
        'name: "quoted-name"\n'
        "description: 'Use when: user asks'\n"
        "---\nbody\n"
    )
    assert fm.meta["name"] == "quoted-name"
    assert fm.meta["description"] == "Use when: user asks"


def test_parse_unquoted_colon_is_tolerated():
    """其他客户端常见的"非法 YAML"：description 里带未加引号的冒号。"""
    fm = frontmatter.parse(
        "---\nname: x\ndescription: Use this skill when: the user asks\n---\nbody"
    )
    assert fm.meta["description"] == "Use this skill when: the user asks"


def test_parse_folded_block_scalar():
    fm = frontmatter.parse(
        "---\n"
        "name: x\n"
        "description: >\n"
        "  Extract text,\n"
        "  fill forms.\n"
        "  Use when handling PDFs.\n"
        "---\nbody"
    )
    assert fm.meta["description"] == "Extract text, fill forms. Use when handling PDFs."


def test_parse_literal_block_scalar_keeps_newlines():
    fm = frontmatter.parse(
        "---\nname: x\ndescription: |\n  第一行\n  第二行\n---\nbody"
    )
    assert fm.meta["description"] == "第一行\n第二行"


def test_parse_multiline_plain_scalar():
    fm = frontmatter.parse(
        "---\ndescription:\n  第一段\n  第二段\nname: x\n---\nbody"
    )
    assert fm.meta["description"] == "第一段 第二段"
    assert fm.meta["name"] == "x"


def test_parse_ignores_nested_and_unknown_fields():
    fm = frontmatter.parse(
        "---\n"
        "name: x\n"
        "description: d\n"
        "metadata:\n"
        "  author: someone\n"
        "  version: '1.0'\n"
        "unknown-field: 任意值\n"
        "---\nbody"
    )
    assert fm.meta["name"] == "x"
    assert fm.meta["description"] == "d"
    assert "author" in fm.meta.get("metadata", "")  # 嵌套结构退化为原始文本，调用方忽略
    assert fm.meta["unknown-field"] == "任意值"


def test_parse_crlf_and_bom():
    fm = frontmatter.parse("\ufeff---\r\nname: x\r\ndescription: d\r\n---\r\nbody\r\n")
    assert fm.meta["name"] == "x"
    assert fm.body == "body"


def test_parse_without_frontmatter_returns_body_and_warning():
    fm = frontmatter.parse("# 只有正文\n没有元数据")
    assert fm.meta == {}
    assert "缺少 frontmatter" in fm.warnings[0]
    assert "只有正文" in fm.body


def test_parse_unclosed_frontmatter():
    fm = frontmatter.parse("---\nname: x\ndescription: d\n")
    assert fm.meta == {}
    assert any("未闭合" in w for w in fm.warnings)


def test_parse_duplicate_key_last_wins_with_warning():
    fm = frontmatter.parse("---\nname: a\nname: b\ndescription: d\n---\nbody")
    assert fm.meta["name"] == "b"
    assert any("重复键" in w for w in fm.warnings)


def test_parse_comment_and_unparsable_lines_are_ignored():
    fm = frontmatter.parse(
        "---\n# 注释\nname: x\n这就是一行乱七八糟\ndescription: d\n---\nbody"
    )
    assert fm.meta["name"] == "x"
    assert fm.meta["description"] == "d"
    assert any("无法解析的行" in w for w in fm.warnings)
