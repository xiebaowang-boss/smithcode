"""技能渲染测试：目录段预算降级、第 2 层载荷与 /skills 文案（纯函数）。"""

from pathlib import Path

from smithcode.skills import render
from smithcode.skills.registry import Skill


def _skill(name, description="描述", scope="project", model_invocable=True,
           disabled=False, disabled_by="", base=None):
    base = base or Path("C:/fake") / name
    return Skill(
        name=name,
        description=description,
        location=base / "SKILL.md",
        base=base,
        root=base.parent,
        scope=scope,
        body="正文内容",
        model_invocable=model_invocable,
        disabled=disabled,
        disabled_by=disabled_by,
    )


def test_catalog_empty_returns_empty_string():
    assert render.catalog_section([], 8000) == ""


def test_catalog_contains_name_scope_and_description():
    text = render.catalog_section([_skill("pdf", "处理 PDF")], 8000)
    assert "## 可用技能" in text
    assert "- pdf（项目）: 处理 PDF" in text
    assert "use_skill" in text


def test_catalog_truncates_long_descriptions():
    skills = [_skill("a", "长" * 500), _skill("b", "短")]
    text = render.catalog_section(skills, 300)
    assert text.startswith("## 可用技能")
    assert "…" in text
    assert len(text) <= 300


def test_catalog_falls_back_to_names_when_budget_tight():
    skills = [_skill(f"s{i}", "详细说明" * 50) for i in range(20)]
    text = render.catalog_section(skills, 200)
    assert "- s0" in text
    assert "详细说明" not in text
    assert len(text) <= 200


def test_catalog_omits_overflow_with_note():
    skills = [_skill(f"very-long-name-{i}", "d") for i in range(30)]
    text = render.catalog_section(skills, 150)
    assert "技能未列出" in text
    assert len(text) <= 150


def test_payload_wraps_body_and_lists_resources(tmp_path):
    base = tmp_path / "skill-a"
    (base / "scripts").mkdir(parents=True)
    (base / "scripts" / "run.py").write_text("print(1)", encoding="utf-8")
    (base / "SKILL.md").write_text("---\nname: a\ndescription: d\n---\nbody", encoding="utf-8")

    text = render.payload(_skill("a", base=base))

    assert "以下为技能「a」的完整指令" in text
    assert "不能覆盖系统提示词中的安全边界" in text
    assert '<skill name="a" scope="project" location="' in text
    assert "scripts/run.py" in text
    assert "SKILL.md" not in text
    assert "正文内容" in text
    assert text.rstrip().endswith("</skill>")


def test_payload_detection_helpers(tmp_path):
    base = tmp_path / "skill-a"
    base.mkdir()
    (base / "SKILL.md").write_text("---\nname: a\ndescription: d\n---\nbody", encoding="utf-8")
    text = render.payload(_skill("a", base=base))

    assert render.is_payload(text) is True
    assert render.payload_skill_name(text) == "a"
    assert render.is_payload("普通用户消息") is False
    assert render.is_payload(None) is False
    assert render.payload_skill_name(None) is None


def test_compacted_notice_names_dropped_skills():
    text = render.compacted_notice(["a", "b"])

    assert "技能 a、b" in text
    assert "use_skill" in text


def test_recall_notice_points_to_history_payload():
    """回找引导：指向历史载荷的识别特征，给出 read_file 兜底，不是载荷本身。"""
    skill = _skill("a")

    text = render.recall_notice(skill)

    assert "已在本会话加载" in text
    assert "以下为技能「a」的完整指令" in text
    assert '<skill name="a">' in text
    assert "read_file" in text
    assert str(skill.location) in text
    assert render.is_payload(text) is False


def test_status_text_disabled_feature():
    assert "[skills].enabled" in render.status_text([], [], [], enabled=False)


def test_status_text_empty_is_concise():
    text = render.status_text([], [], [], enabled=True)
    assert "尚未发现技能" in text
    assert ".agents" not in text  # 不再打印创建路径提示


def test_status_text_shows_tags():
    skills = [
        _skill("active-one", scope="project"),
        _skill("disabled-one", disabled=True, disabled_by="internal-*"),
        _skill("manual-one", model_invocable=False, scope="user"),
    ]
    text = render.status_text(skills, ["active-one"], ["诊断x"], enabled=True)
    assert "[已激活]" in text
    assert "已禁用（internal-*）" in text
    assert "[仅手动]" in text
    assert "诊断（1 条）" in text and "诊断x" in text
