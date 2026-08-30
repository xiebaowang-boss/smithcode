"""用量统计测试：全量字段容错累计、双口径重置与展示。"""

from codeagent.usage import UsageAccumulator, UsageTracker


def _usage(**detail_overrides):
    """一份接近真实返回的 usage（对齐实测的网关响应结构）。"""
    return {
        "prompt_tokens": 253,
        "completion_tokens": 33,
        "total_tokens": 286,
        "prompt_tokens_details": {
            "cached_tokens": 192,
            "cache_write_tokens": None,
            "audio_tokens": None,
        },
        "completion_tokens_details": {
            "reasoning_tokens": 0,
            "audio_tokens": None,
            "accepted_prediction_tokens": None,
            "rejected_prediction_tokens": None,
        },
        **detail_overrides,
    }


def test_accumulator_sums_all_present_fields():
    acc = UsageAccumulator()
    acc.add(_usage())
    acc.add(_usage())

    assert acc.calls == 2
    assert acc.get("prompt_tokens") == 506
    assert acc.get("completion_tokens") == 66
    assert acc.get("total_tokens") == 572
    assert acc.get("cached_tokens") == 384
    assert acc.get("reasoning_tokens") == 0


def test_accumulator_tolerates_missing_or_empty_fields():
    """字段缺失、为 None、嵌套对象缺失、非数值都不应报错，一律按 0 处理。"""
    acc = UsageAccumulator()
    acc.add(None)
    acc.add({})
    acc.add({"prompt_tokens": None, "completion_tokens": "abc", "total_tokens": 10})
    acc.add({"prompt_tokens_details": None, "total_tokens": 5})
    acc.add({"prompt_tokens_details": {"cached_tokens": "x"}, "total_tokens": 1})

    assert acc.calls == 4  # 只有合法 dict 计入调用次数
    assert acc.get("prompt_tokens") == 0
    assert acc.get("completion_tokens") == 0
    assert acc.get("total_tokens") == 16
    assert acc.get("cached_tokens") == 0


def test_accumulator_ignores_bool_and_counts_float():
    """bool 不是有效数值；token 计数按整型累计，浮点输入向下取整。"""
    acc = UsageAccumulator()
    acc.add({"total_tokens": True})
    assert acc.get("total_tokens") == 0

    acc.add({"total_tokens": 3.7})
    assert acc.get("total_tokens") == 3


def test_accumulator_reads_deepseek_flat_fields():
    """DeepSeek 平铺在顶层的缓存字段也应被读出来。"""
    acc = UsageAccumulator()
    acc.add({"prompt_cache_hit_tokens": 100, "prompt_cache_miss_tokens": 153})

    assert acc.get("prompt_cache_hit_tokens") == 100
    assert acc.get("prompt_cache_miss_tokens") == 153


def test_tracker_feeds_both_scopes():
    tracker = UsageTracker()
    tracker.add(_usage())

    assert tracker.since_start.get("total_tokens") == 286
    assert tracker.current_session.get("total_tokens") == 286


def test_reset_session_keeps_since_start():
    """/new 语义：会话口径清零，应用启动以来的累计保留。"""
    tracker = UsageTracker()
    tracker.add(_usage())

    tracker.reset_session()

    assert tracker.current_session.calls == 0
    assert tracker.current_session.get("total_tokens") == 0
    assert tracker.since_start.get("total_tokens") == 286


def test_humanize_shows_cache_inline_after_prompt():
    """缓存命中内联展示在输入后面，零值时省略。"""
    acc = UsageAccumulator()
    assert acc.humanize() == "尚无调用"

    acc.add(_usage())
    assert acc.humanize() == "输入 253 (缓存 192) / 输出 33 / 合计 286"

    no_cache = UsageAccumulator()
    no_cache.add({"prompt_tokens": 10, "completion_tokens": 1, "total_tokens": 11})
    assert no_cache.humanize() == "输入 10 / 输出 1 / 合计 11"


def test_cache_hit_dedupes_nested_and_flat_fields():
    """嵌套 cached_tokens 与 DeepSeek 平铺字段语义相同，取大者去重。"""
    acc = UsageAccumulator()
    acc.add({"prompt_cache_hit_tokens": 100, "prompt_cache_miss_tokens": 153})
    assert acc.cache_hit() == 100

    acc.add(_usage())  # 嵌套 cached_tokens=192
    assert acc.cache_hit() == 192


def test_summary_renders_scopes_and_nonzero_details():
    tracker = UsageTracker()
    assert "尚无调用" in tracker.summary()

    u = _usage()
    u["completion_tokens_details"]["reasoning_tokens"] = 45
    tracker.add(u)

    text = tracker.summary()
    assert "应用启动以来" in text
    assert "当前会话" in text
    assert "(缓存 192)" in text  # 缓存命中内联在输入后
    assert "思维链 45" in text
    assert "缓存写入" not in text  # 零值细节不展示
    assert "缓存命中" not in text  # 缓存不再重复出现在细节行
