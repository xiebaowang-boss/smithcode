"""循环相关的常量与文案（自 `agent/agent.py` 搬出）。

**为什么循环本身没搬进来**：方案 §8 想要的是 pi 那样的纯函数
`run_loop(ctx, config, signal, emit, stream_fn)`——那是把「循环配置」抽成可注入对象
之后才成立的形状。smithcode 的循环与 `Agent` 的十余处状态强耦合（session / 上下文
计量 / 权限 / 回合快照 / 技能 / 目标），搬成 `run_loop(agent, token)` 只是把 `self.`
换成 `agent.`：可读性没有提升，还多一层间接。所以这里只搬**真正无耦合**的部分——
中断 / 流中断 / 迭代上限的文案与格式化，它们被宿主、循环与测试共同引用，独立成
模块后引用它不必把整个 Agent 拉进来。
"""

from __future__ import annotations

from .. import config

# Esc 中断：控制台（REPL / 一次性任务）收尾提示行与占位结果文本。
# TUI 不再经 renderer 打印此提示，改由宿主机按 RunResult.status 渲染到
# 运行动画行（正在停止）/ 轮次页脚（已停止）。
INTERRUPTED_NOTE = "\n⏹ 已中断"
# 响应流断开（读完超时 / 对端掐断连接）：控制台收尾提示。部分正文已上屏并入库，
# 提示用户这是残缺输出、可直接追问让模型接着写。TUI 对应页脚的「· 输出中断」。
# 末尾接 `format_stream_interrupted(reason)` 的失败原因——只报「输出中断」而不报
# 为什么断，用户与排查者都无从下手（是读超时？限流？对端掐断？）。
STREAM_INTERRUPTED_NOTE = "\n⏹ 输出中断（内容不完整，可继续追问让模型接着写）"

# 读超时的排障提示：这个模型/网关在长思考时可能长时间不吐数据，
# `[limits].llm_timeout` 是**空闲超时**（静默超过它就断），不是总时长上限。
TIMEOUT_HINT = (
    "模型长时间未输出数据触发了空闲超时（[limits].llm_timeout，当前 {timeout:.0f}s）。"
    "推理型模型的首字延迟可能很长，可调大该值后重试"
)


def format_stream_interrupted(reason: str | None) -> str:
    """流中断的失败原因后缀：TUI 页脚与控制台提示共用同一份文案。

    reason 由 `llm.retry.describe` 产出（`分类: 原始信息`）；缺失时退回空串，
    保证调用方不用分支。
    """
    if not reason:
        return ""
    detail = f"（{reason}）"
    if reason.startswith(("读取超时", "连接超时")):
        return detail + " " + TIMEOUT_HINT.format(timeout=config.LLM_TIMEOUT)
    return detail
# 中断回写上下文：任务被手动中止时，作为一条 user 消息追加进会话历史
# （不触发任何新请求），下一轮用户提问时模型即可看到上轮是被主动叫停的、
# 任务未完成，避免把部分输出当成完整结果。
INTERRUPTED_CONTEXT = (
    "（用户手动中断了上一个任务，任务未完成。此前部分输出可能不完整，"
    "未执行的工具已标记为「未执行：用户中断了任务」。请以用户的最新输入为准。）"
)
# 流中断回写上下文：模型响应流中途断开（读完超时 / 对端掐断连接）时，已上屏的
# 部分正文仍会写进会话历史，随后追加这条 user 消息（不触发新请求）——下一轮模型
# 即可看到上一轮说到哪、是为什么断的，从而续写而不是从头重做一遍。
STREAM_INTERRUPTED_CONTEXT = (
    "（上一条回复在生成过程中因网络错误中断，内容不完整；"
    "以上是已经写出的部分。请基于它继续完成任务，不要从头重做。）"
)


def stream_interrupted_context(reason: str | None) -> str:
    """流中断回写上下文，带上具体失败原因（下一轮模型能知道断在哪类故障上）。"""
    if not reason:
        return STREAM_INTERRUPTED_CONTEXT
    return STREAM_INTERRUPTED_CONTEXT.replace(
        "因网络错误中断", f"因 {reason} 中断", 1
    )
# 迭代上限收尾提示词：`[limits].max_iterations` > 0 且用尽时，系统不再暴露工具，
# 把它作为一条 user 消息注入并强制模型用纯文本总结收尾（对齐 opencode 的
# max-steps「最后一轮只回文本」行为）。总结是本次任务的最后一条可见回复。
MAX_ITERATIONS_WRAPUP = (
    "已达到本次任务的迭代上限，系统将停止继续调用工具，请不要在本次回复中再发起"
    "任何工具调用。请直接总结本次任务：已完成的工作（附关键文件与验证结果）、"
    "尚未完成的部分或遇到的阻碍，以及建议的下一步。"
)


