"""TUI 弹窗/面板：权限确认、提问、通用选择。

权限/提问面板原地替换输入框（composer 位三态）；选择面板走全屏半透明遮罩
居中弹出（opencode 式），用于 `/model` 等命令的选择意图。面板自身不感知
REPL/TUI 差异，完成动作经回调交回宿主。
"""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from typing import ClassVar

from rich.text import Text
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Input, Static


def _parse_choice_options(prompt: str, valid: str) -> list[tuple[str, str]]:
    """从确认提示串解析 (键, 标签) 列表，如 "[y]仅本次 / [a]总是 / [n]拒绝"。

    解析不到标签时退化为单字符标签，保证任何 valid 串都能渲染成选项列表。
    """
    labels = {
        m.group(1).lower(): m.group(2).strip().rstrip(":").strip()
        for m in re.finditer(r"\[(\w+)\]([^/\[:\]]*)", prompt)
    }
    options = [(key, labels.get(key) or key) for key in valid if key in labels]
    return options or [(key, key) for key in valid]


class PermissionPanel(Vertical):
    """opencode 式权限申请面板：申请时原地替换输入框，按键即答。

    顶部只展示标题（如「允许执行 git status?」）；选项为竖排编号列表（与提问
    面板同款，选中行暗色底），有副作用/说明的选项在下方附一行小字，↑↓ 选择、
    enter 确认、esc 拒绝。字母键（y/n/a）与数字键为隐藏快捷键，不在提示里展示。
    """

    can_focus = True
    BINDINGS: ClassVar = [
        Binding("up", "move_prev", "上一项", show=False),
        Binding("down", "move_next", "下一项", show=False),
        Binding("k", "move_prev", "上一项", show=False),
        Binding("j", "move_next", "下一项", show=False),
        Binding("enter", "confirm", "确认", show=False),
        Binding("escape", "cancel", "拒绝", show=False),
    ]

    def __init__(self, prompt: str, valid: str, hint: str, result: dict,
                 evt: threading.Event, detail: list[str] | None = None,
                 descriptions: dict[str, str] | None = None,
                 content: str | None = None, **kwargs):
        super().__init__(**kwargs)
        self._valid = valid
        self._options = _parse_choice_options(prompt, valid)
        self._title = re.split(r"\[", prompt, maxsplit=1)[0].strip() or "允许?"
        self._detail = list(detail or [])
        self._descriptions = dict(descriptions or {})
        self._content = content
        self._selected = 0
        self._result, self._evt = result, evt
        self._body: Static | None = None
        self._footer: Static | None = None

    def _title_renderable(self) -> Text:
        """标题行的富文本：标题用标题色，工具摘要（content）同排跟在后面、保持灰色。"""
        text = Text(self._title, style="#fab283")
        if self._content:
            text.append("  ")  # 标题与内容之间留间隔
            text.append(self._content, style="#a9b1d6")
        return text

    def compose(self):
        yield Static(self._title_renderable(), classes="perm-title", markup=False)
        if self._detail:
            yield Static("\n".join(self._detail), classes="perm-detail", markup=False)
        self._body = Static(self._render_options())
        yield self._body
        self._footer = Static(self._hints(), classes="ask-hint", markup=False)
        yield self._footer

    def on_mount(self) -> None:
        self.focus()  # 挂载不会自动聚焦（原弹窗 push_screen 时代会），不聚焦按键会落进隐藏输入框

    def _hints(self) -> str:
        return "↑↓ 选择 · enter 确认 · esc 拒绝"

    def _render_options(self) -> Text:
        """竖排编号选项（与提问面板同款）：选中行暗色底，选项说明以小字跟在下方。"""
        text = Text()
        for i, (key, label) in enumerate(self._options):
            if i == self._selected:
                text.append(f" {i + 1}. {label} \n", style="on #292e42")
            else:
                text.append(f" {i + 1}. {label}\n", style="#a9b1d6")
            desc = self._descriptions.get(key)
            if desc:
                text.append(f"    {desc}\n", style="#565f89")
        return text

    def _refresh(self) -> None:
        if self._body is not None:
            self._body.update(self._render_options())

    def action_move_prev(self) -> None:
        self._selected = (self._selected - 1) % len(self._options)
        self._refresh()

    def action_move_next(self) -> None:
        self._selected = (self._selected + 1) % len(self._options)
        self._refresh()

    def action_confirm(self) -> None:
        key, _ = self._options[self._selected]
        self._finish(key)

    def action_cancel(self) -> None:
        # Esc=拒绝：有 n 选 n（两处调用点均为 y/n/a 语义），否则按最后一个选项
        deny = "n" if "n" in self._valid else self._valid[-1]
        self._finish(deny)

    def on_key(self, event) -> None:
        """字母键直选（y/n/a 按键即答）与数字键快选（隐藏快捷键）。

        注意 event.character 对方向键等特殊键为 None——必须先判空，
        否则 "" in "yna" 恒为 True，按方向键会被当成空回答直接拒绝。
        """
        char = event.character or ""
        if char and char in self._valid:
            event.stop()
            event.prevent_default()
            self._finish(char)
            return
        if char and char.isdigit():
            n = int(char)
            if 1 <= n <= len(self._options):
                event.stop()
                event.prevent_default()
                self._selected = n - 1
                self._refresh()
                self.action_confirm()

    def _finish(self, value: str) -> None:
        self._result["value"] = value
        self._evt.set()
        self.app.close_composer_panel(self)


class QuestionPanel(Vertical):
    """opencode 式提问面板：一次承载 1-N 个问题，原地替换输入框，答完换回。

    标题显示当前题号 `(i/n)`（n 为真实问题数，确认页不计数）；有选项时显示编号
    列表（↑↓/j/k/数字键选择），最后一项固定「输入自定义回答…」；无选项时直接进入
    输入态。多问题时 ←/→（或 Tab / Shift+Tab）手动翻页，已答过的题可回跳修改；提交
    一题后**按顺序进入下一题**（回改中间某题也一样，不会跳到确认页），答完最后一题
    才进入**确认页**——交互与普通问题一致（←/→ 翻页、Enter 提交），只是把各题
    答案列在其问题下方供核对，避免最后一题答完即提交、没有修改余地。

    输入框是**显式进入**的临时子状态：
    - 有选项题：光标在「输入自定义回答…」行按 Enter 进入输入（已填内容会回填便于修改），
      输入框里 **Enter 提交本题并前进到下一题**（单选=自定义文本，多选=勾选项+自定义
      文本）；Esc 只退出输入、不取消整组。
    - 纯输入题：没有列表可回，输入框 Enter 直接提交并前进；Esc 退出输入但保留输入框。

    切题（←/→、Tab 或答完自动前进）一律不把焦点交给输入框，而是把选中项复位到**第一个
    选项**，方便继续 ←/→；有选项题落回列表态，末行固定「输入自定义回答…」（选项名不随
    答案变化），已填答案缩进显示在其下方。多次切题会重置选中项。
    """

    can_focus = True
    BINDINGS: ClassVar = [
        Binding("up", "move_up", "上移", show=False),
        Binding("down", "move_down", "下移", show=False),
        Binding("k", "move_up", "上移", show=False),
        Binding("j", "move_down", "下移", show=False),
        Binding("left", "prev_question", "上一题", show=False),
        Binding("right", "next_question", "下一题", show=False),
        Binding("tab", "next_question", "下一题", show=False),
        Binding("shift+tab", "prev_question", "上一题", show=False),
        Binding("space", "toggle", "勾选", show=False),
        Binding("enter", "confirm", "确认", show=False),
        Binding("escape", "cancel", "取消", show=False),
    ]

    def __init__(self, questions: list[dict], result: dict, evt: threading.Event,
                 **kwargs):
        super().__init__(**kwargs)
        self._questions = list(questions or [])
        self._total = len(self._questions)
        self._index = 0  # 当前题索引
        self._answers: list[str] = [""] * self._total
        self._checked: list[set] = [set() for _ in range(self._total)]
        self._selected: list[int] = [0] * self._total
        self._custom: list[str] = [""] * self._total  # 各题「自定义回答」的输入缓冲
        # 无选项的题直接进入输入态
        self._editing: list[bool] = [
            not (q.get("options") or []) for q in self._questions
        ]
        self._result, self._evt = result, evt
        self._review = False  # 多问题答完后进入的确认页（循环里的最后一「页」）
        self._title: Static | None = None
        self._body: Static | None = None
        self._footer: Static | None = None
        self._input: Input | None = None

    # ----- 当前题快捷访问 -----

    def _options_of(self, index: int) -> list[str]:
        return self._questions[index].get("options") or []

    def _is_multiple(self, index: int) -> bool:
        return bool(self._questions[index].get("multiple"))

    def compose(self):
        self._title = Static(self._render_title(), classes="ask-title", markup=False)
        yield self._title
        self._body = Static(self._render_options())
        yield self._body
        # select_on_focus=False：重新聚焦时不全选已输入内容，退出输入后再进来可继续追加
        self._input = Input(
            placeholder="输入回答（Enter 提交 · Esc 退出输入）",
            select_on_focus=False,
        )
        if self._options_of(self._index):
            self._input.display = False
            # 自定义回答输入框挂在选项列表末尾：缩进对齐选项文字
            self._input.add_class("custom-answer")
        yield self._input
        self._footer = Static(self._hints(), classes="ask-hint", markup=False)
        yield self._footer

    def on_mount(self) -> None:
        if self._editing[self._index]:
            self._input.focus()
        else:
            self.focus()
        self._refresh()

    # ----- 状态渲染 -----

    def _render_title(self) -> str:
        # 确认页不算作一个问题（不参与 (i/n) 编号），只是沿用同样的翻页交互
        if self._review:
            return "确认提交"
        question = self._questions[self._index]["question"]
        if self._total <= 1:
            return question
        mark = "✔ " if self._answers[self._index] else ""
        return f"({self._index + 1}/{self._total}) {mark}{question}"

    def _input_focused(self) -> bool:
        """输入框是否持有焦点——Esc 是否「先退出输入」以它为准。"""
        return self._input is not None and self._input.has_focus

    def _hints(self) -> str:
        if self._review:
            return "←→ 切换问题 · enter 提交 · esc 取消"
        parts = []
        if self._total > 1:
            parts.append("←→ 切换问题")
        index = self._index
        if self._editing[index]:
            if self._options_of(index):
                parts.append("enter 提交回答 · esc 返回选项")
            elif self._input_focused():
                parts.append("enter 提交 · esc 退出输入")
            else:
                parts.append("enter 继续输入 · esc 取消")
        elif self._is_multiple(index):
            parts.append("↑↓ 选择 · 空格 勾选 · enter 提交 · esc 取消")
        else:
            parts.append("↑↓ 选择 · enter 确认 · esc 取消")
        return " · ".join(parts)

    def _render_options(self) -> Text:
        """opencode question.tsx 式选项行：编号 + 标签，选中行暗色底，已选绿 ✓；
        选项说明以小字跟在下方；末行固定「输入自定义回答…」（选项名不随答案变化，
        已填答案缩进显示在其下一行）。无选项时返回空。

        多问题全部答完后进入确认页（见 `_render_review`），此处不渲染。"""
        if self._review:
            return self._render_review()
        options = self._options_of(self._index)
        text = Text()
        if not options:
            return text
        multiple = self._is_multiple(self._index)
        descriptions = self._questions[self._index].get("descriptions") or []
        checked = self._checked[self._index]
        selected = self._selected[self._index]
        for i, opt in enumerate(options):
            picked = i in checked
            mark = f"[{'✓' if picked else ' '}] " if multiple else ""
            suffix = " ✓" if picked and not multiple else ""
            if i == selected:
                text.append(f" {i + 1}. {mark}{opt}{suffix} \n", style="on #292e42")
            else:
                text.append(f" {i + 1}. {mark}{opt}{suffix}\n", style="#a9b1d6" if picked else "")
            desc = descriptions[i] if i < len(descriptions) else ""
            if desc:
                text.append(f"    {desc}\n", style="#565f89")
        cursor_style = "on #292e42" if selected == len(options) else ""
        # 末行选项名固定为「输入自定义回答…」不随答案变化；已填答案缩进显示在其下方
        # （即输入框出现的位置）。编辑中内容在输入框里，此处不重复显示。
        text.append(f" {len(options) + 1}. 输入自定义回答… ", style=cursor_style)
        if not self._editing[self._index]:
            custom = self._custom[self._index].strip()
            if custom:
                text.append(f"\n    {custom}", style="#565f89")
        return text

    def _render_review(self) -> Text:
        """多问题确认页：逐题列出「问题」并在其**下方**缩进显示对应答案，
        enter 直接提交整组，←/→ 可返回任一题修改（无需选中）。"""
        text = Text()
        for i, item in enumerate(self._questions):
            text.append(f" {i + 1}. {item['question']}\n", style="#a9b1d6")
            text.append(f"    {self._answers[i] or '（未答）'}\n", style="#565f89")
        return text

    def _refresh(self) -> None:
        if self._title is not None:
            self._title.update(self._render_title())
        if self._body is not None:
            self._body.update(self._render_options())
        if self._footer is not None:
            self._footer.update(self._hints())
        if self._input is not None:
            show_input = self._editing[self._index] and not self._review
            self._input.display = show_input
            if show_input and self._options_of(self._index):
                self._input.add_class("custom-answer")
            else:
                self._input.remove_class("custom-answer")

    # ----- 切题 -----

    def action_prev_question(self) -> None:
        self._switch_question(-1)

    def action_next_question(self) -> None:
        self._switch_question(1)

    def _switch_question(self, step: int) -> None:
        if self._total <= 1:
            return  # 单问题没有确认页，不存在切页
        # 页面环：各题 + 确认页（最后一页）；多问题时才存在
        page = (self._total if self._review else self._index) + step
        page %= self._total + 1
        if page == self._total:
            self._enter_review()
        else:
            self._goto(page)

    def _goto(self, index: int) -> None:
        """切到第 index 题：暂存输入缓冲、恢复目标题状态。

        切题一律不把焦点交给输入框（否则 ←/→ 会被输入框吞掉、也看不全选项），而是
        把选中项复位到**第一个选项**；有选项的题落回列表态（已填的自定义回答显示在
        末行下方），无选项的纯输入题输入框保留但失焦，按 Enter 或直接敲字再进入输入。
        """
        if self._input is not None:
            self._custom[self._index] = self._input.value
        if self._options_of(self._index):
            self._editing[self._index] = False  # 离开：有选项题收起输入框、回列表态
        self._index = index
        self._review = False
        if self._options_of(index):
            self._editing[index] = False        # 进入：有选项题同样落在列表态
        self._selected[index] = 0               # 焦点复位到第一个选项
        self._load_input(self._custom[index])
        self.focus()  # 焦点始终在列表（面板），不抢给输入框
        self._refresh()

    def _advance(self) -> None:
        """提交本题后前进：**按顺序进下一题**（回改中间某题也如此），只有已在最后一题
        时才回头补前面漏答的题；都答完则提交——多问题先进确认页（留出修改余地），
        单问题直接提交。

        早期实现是「跳到下一道未答题」（向后环绕扫描），回改中间某题时因后面都已答而
        直接落到确认页，与「改完接着看下一题」的预期不符，故改为顺序前进；漏答题只在
        最后一题提交后回头补齐，避免进确认页时还留着空答案。
        """
        if self._index + 1 < self._total:
            self._goto(self._index + 1)
            return
        for candidate in range(self._total):  # 已在最后一题：回头补漏答题
            if not self._answers[candidate]:
                self._goto(candidate)
                return
        if self._total > 1:
            self._enter_review()
        else:
            self._finish()

    def _enter_review(self) -> None:
        """切到确认页（多问题循环里的最后一页）：enter 直接提交，←/→ 返回修改。"""
        self._review = True
        self._refresh()
        self.focus()

    # ----- 选项交互 -----

    def action_move_up(self) -> None:
        self._move_option(-1)

    def action_move_down(self) -> None:
        self._move_option(1)

    def _move_option(self, step: int) -> None:
        if self._review or self._editing[self._index]:
            return  # 确认页不可上下选择（整页即提交）
        count = len(self._options_of(self._index)) + 1  # 末项为「输入自定义回答」
        if count <= 1:
            return
        self._selected[self._index] = (self._selected[self._index] + step) % count
        self._refresh()

    def action_toggle(self) -> None:
        index = self._index
        if self._review or self._editing[index] or not self._is_multiple(index):
            return
        if self._selected[index] < len(self._options_of(index)):
            self._checked[index].symmetric_difference_update({self._selected[index]})
            self._refresh()

    def action_confirm(self) -> None:
        if self._review:
            self._finish()  # 确认页：enter 直接提交整组
            return
        index = self._index
        options = self._options_of(index)
        if self._editing[index]:
            # 纯输入题：Esc 失焦后 Enter 重新聚焦，聚焦时 Enter 提交并前进
            if (not options and self._input is not None
                    and not self._input.has_focus):
                self._input.focus()
                self._refresh()
                return
            self._commit_and_advance(index)
            return
        if self._selected[index] == len(options):  # 光标在「输入自定义回答」行
            self._begin_editing()  # 回车进入编辑（已填内容会回填，便于修改）
            return
        if self._is_multiple(index):
            self._submit_multiple(index)
        else:
            self._answers[index] = options[self._selected[index]]
            self._advance()

    def _compose_answer(self, index: int) -> str:
        """合并答案：多选把勾选项与自定义输入**都**计入（如「A, B, 手写内容」）。"""
        options = self._options_of(index)
        parts = [options[i] for i in sorted(self._checked[index])]
        custom = self._custom[index].strip()
        if custom:
            parts.append(custom)
        return ", ".join(parts)

    def _commit_and_advance(self, index: int) -> None:
        """输入框里回车：提交本题并进入下一题。多选=勾选项+自定义文本，单选=自定义文本；
        答案为空则退回列表、不前进。"""
        if self._input is not None:
            self._custom[index] = self._input.value  # 以输入框实时内容为准
        if self._is_multiple(index):
            answer = self._compose_answer(index)
        else:
            answer = self._custom[index].strip()
        if answer:
            self._answers[index] = answer
            self._advance()
            return
        if self._options_of(index):
            self._end_editing()  # 空答案：回到列表，不提交

    def _submit_multiple(self, index: int) -> None:
        answer = self._compose_answer(index)
        if answer:
            self._answers[index] = answer
            self._advance()

    def action_cancel(self) -> None:
        index = self._index
        if self._review:
            self._finish(cancel=True)  # 确认页取消 = 取消整组
            return
        if self._input_focused():
            # 焦点在输入框：Esc 只退出输入，不取消整组
            if self._options_of(index):
                self._end_editing()  # 有选项 → 回到选项列表（可再选「输入自定义回答」）
            else:
                self._blur_input()   # 无选项 → 失焦但保留输入框（Enter/敲字可再次输入）
            return
        self._finish(cancel=True)  # 取消整组，由调用方兜底

    def on_key(self, event) -> None:
        """数字键快选（opencode 的 1-9 直接选）；确认页与编辑态不抢按键。

        编辑态例外：无选项题在 Esc 退出输入后输入框仍显示，此时敲字自动重新聚焦并把
        该字符写入输入框，省去先按 Enter 的一步。"""
        char = event.character or ""
        index = self._index
        if self._review:
            return
        if self._editing[index]:
            # 注意：char 为空时 "".isprintable() 仍为 True，必须先判非空，
            # 否则方向键（character=None）会被误吞、左右切题失效
            if (char and char.isprintable() and not self._options_of(index)
                    and self._input is not None and not self._input.has_focus):
                event.stop()
                event.prevent_default()
                self._input.focus()
                self._input.insert_text_at_cursor(char)
                self._refresh()
            return
        if not char.isdigit():
            return
        options = self._options_of(index)
        if not options:
            return
        n = int(char)
        if 1 <= n <= len(options) + 1:
            event.stop()
            event.prevent_default()
            self._selected[index] = n - 1
            self._refresh()
            self.action_confirm()

    def on_input_submitted(self, event) -> None:
        index = self._index
        self._custom[index] = event.value
        if self._options_of(index):
            self._commit_and_advance(index)  # 有选项题：提交并进入下一题
            return
        text = event.value.strip()
        if text:
            self._answers[index] = text
            self._advance()

    # ----- 编辑态 -----

    def _load_input(self, text: str) -> None:
        """把共用输入框内容设为 text 并把光标移到末尾，方便接着往后写。"""
        if self._input is None:
            return
        self._input.value = text
        self._input.cursor_position = len(text)

    def _begin_editing(self) -> None:
        index = self._index
        self._editing[index] = True
        if self._input is not None:
            self._input.display = True
            self._load_input(self._custom[index])
            self._input.focus()
            self._input.cursor_position = len(self._input.value)
        self._refresh()

    def _end_editing(self) -> None:
        """有选项题退出输入：暂存缓冲、隐藏输入框、焦点交回面板（选项列表可再选）。"""
        index = self._index
        if self._input is not None:
            self._custom[index] = self._input.value
        self._editing[index] = False
        self.focus()
        self._refresh()

    def _blur_input(self) -> None:
        """无选项题退出输入：暂存缓冲、面板收回焦点，输入框保留待命（Enter/敲字可再聚焦）。

        与 `_end_editing` 的区别：无选项题没有选项列表可回退，隐藏输入框会让整题空白，
        故只失焦、不改 `_editing`，输入内容原样留在框里。"""
        index = self._index
        if self._input is not None:
            self._custom[index] = self._input.value
        self.focus()
        self._refresh()

    def _finish(self, cancel: bool = False) -> None:
        if cancel:
            self._answers = [""] * self._total
        self._result["values"] = list(self._answers)
        self._evt.set()
        self.app.close_composer_panel(self)


# ---------- 通用选择面板（居中弹窗） ----------


@dataclass
class SelectionItem:
    """选择面板的一行：label 展示、value 回传、description 补充（跟在标题后）、
    current 当前项标记、trailing 贴行尾右对齐（如状态）、trailing_style 为其颜色
    （选中行仍反白）、separator=True 为纯间隔行（不可选中）。

    category 非空时按出现顺序插入分组表头（仅展示、不可选中）：相邻同 category
    的条目归入同一个表头下，category 变化即开新组；空 category 表示不分组。
    """

    label: str
    value: str
    description: str = ""
    current: bool = False
    trailing: str = ""
    trailing_style: str = ""
    separator: bool = False
    category: str = ""


class SelectionScreen(ModalScreen):
    """居中选择弹窗的宿主屏。

    必须用 Screen 而非普通浮层：Textual 只会对 **Screen** 的半透明背景调用
    `BackgroundScreen` 渲染底层屏幕（见 `Screen.render`），从而得到"底层变暗"
    的效果；挂在 layer 上的普通组件会把自己的半透明背景按祖先色解算后盖在
    内容之上，看起来就是一块不透明的黑。这里用半透明黑让底层界面轻微变暗。
    """

    DEFAULT_CSS = """
    SelectionScreen {
        align: center middle;
        background: #000000 30%;
    }
    """

    def __init__(self, panel: SelectionPanel, **kwargs):
        super().__init__(**kwargs)
        self._panel = panel

    def compose(self):
        yield self._panel


class SelectionPanel(Vertical):
    """数据驱动的通用选择面板：↑↓/j/k/数字键选择、Enter 确认、Esc 取消。

    只认识 SelectionItem，不认识具体业务；选中后经 on_done(value) 回调交回宿主
    （Esc 传 None）。主线程回调式，不阻塞事件循环——与 ask/权限面板的
    Event 阻塞协议区分开。

    支持分组（item.category）与间隔行（item.separator）：两者都渲染成不可选中的
    行，键盘导航只在可选项中循环。面板内部先算一份「行计划」（表头 / 空行 / 选项），
    渲染、刷新与滚动定位都基于它，行数与 item 数不再一一对应。

    宽度按 size 档位取定值（宽度值定义在 SmithTUI.CSS 的 `.size-*` 规则里），
    面板不测量内容；未知档位回退 DEFAULT_SIZE，避免调用方写错档位把面板撑坏。
    """

    SIZES: ClassVar[tuple] = ("small", "medium", "large", "xlarge")
    DEFAULT_SIZE: ClassVar[str] = "medium"

    can_focus = True
    BINDINGS: ClassVar = [
        Binding("up", "move_prev", "上移", show=False),
        Binding("down", "move_next", "下移", show=False),
        Binding("k", "move_prev", "上移", show=False),
        Binding("j", "move_next", "下移", show=False),
        Binding("enter", "confirm", "确认", show=False),
        Binding("escape", "cancel", "取消", show=False),
    ]

    def __init__(self, title: str, items: list, on_done, size: str = DEFAULT_SIZE,
                 initial: str | None = None, **kwargs):
        super().__init__(**kwargs)
        self._title = title
        self._items = list(items)
        self._on_done = on_done
        self.size_class = size if size in self.SIZES else self.DEFAULT_SIZE
        self.add_class(f"size-{self.size_class}")
        # 可选中的行（跳过间隔行）；初始光标：initial 指定值优先（如从下级
        # 返回时落回原行，仅移动光标、不显示「(当前)」），否则当前项、再否则首项
        self._selectable = [
            index for index, item in enumerate(self._items) if not item.separator
        ]
        self._rows: list = []          # 行计划：("header", 分类) / ("blank",) / ("item", 索引)
        self._row_of_item: dict = {}   # item 索引 → 行号（滚动定位用）
        self._build_rows()
        self._selected = self._resolve_initial(initial)
        self._scroll: VerticalScroll | None = None

    def _build_rows(self) -> None:
        """按 category 变化插入分组表头（非首组前留一个空行），记录 item→行号。"""
        last_category = ""
        for index, item in enumerate(self._items):
            category = item.category or ""
            if category and category != last_category:
                if self._rows:
                    self._rows.append(("blank",))
                self._rows.append(("header", category))
            last_category = category
            self._row_of_item[index] = len(self._rows)
            self._rows.append(("item", index))

    def _resolve_initial(self, initial: str | None) -> int:
        if initial is not None:
            for index in self._selectable:
                if self._items[index].value == initial:
                    return index
        return next(
            (index for index in self._selectable if self._items[index].current),
            self._selectable[0] if self._selectable else 0,
        )

    def compose(self):
        yield Static(self._title, classes="selection-title", markup=False)
        # 每项一行两列：左侧（标记 + 标题 + 说明）占满剩余宽度，右侧 trailing 贴行尾
        # （时间等）——用列布局而非手工补空格，宽度随档位 / 终端自适应。
        # 滚动容器本身不抢焦点（按键归面板）
        with VerticalScroll(classes="selection-scroll") as scroll:
            scroll.can_focus = False
            self._scroll = scroll
            for kind, *payload in self._rows:
                yield self._row(kind, payload[0] if payload else None)
        yield Static("↑↓ 选择 · enter 确认 · esc 取消", classes="selection-hint", markup=False)

    def on_mount(self) -> None:
        self.focus()  # 不聚焦，按键会落进隐藏的输入框

    def _row(self, kind: str, payload):
        """按行计划产出一行：分组表头 / 空行 / 普通条目（含间隔行）。"""
        if kind == "header":
            return Static(
                Text(f"  {payload}", style="bold #7aa2f7"),
                classes="selection-row selection-header", markup=False,
            )
        if kind == "blank":
            return Static("", classes="selection-row selection-separator")
        item = self._items[payload]
        if item.separator:
            # 间隔行：占位一行、不可选中，仅用于分组留白
            return Horizontal(classes="selection-row selection-separator")
        selected = payload == self._selected
        row = Horizontal(
            Static(self._label_text(item, selected), classes="selection-label",
                   markup=False),
            Static(self._trailing_text(item, selected), classes="selection-trailing",
                   markup=False),
            classes="selection-row",
        )
        if selected:
            row.add_class("selected")
        return row

    def _label_text(self, item, selected: bool) -> Text:
        """左侧文本：选中行整行反白，底色由行承担，这里只设前景色。"""
        text = Text()
        if selected:
            text.append(f"› {item.label}", style="bold black")
            if item.description:
                text.append(f"  {item.description}", style="black")
            if item.current:
                text.append("  (当前)", style="black")
        else:
            text.append(f"  {item.label}", style="#a9b1d6")
            if item.description:
                text.append(f"  {item.description}", style="#565f89")
            if item.current:
                text.append("  (当前)", style="#23d18b")
        return text

    def _trailing_text(self, item, selected: bool) -> Text:
        if not item.trailing:
            return Text()
        style = "black" if selected else (item.trailing_style or "#565f89")
        return Text(item.trailing, style=style)

    def _refresh(self) -> None:
        for row, (kind, *payload) in zip(self.query(".selection-row"), self._rows):
            if kind != "item":
                continue  # 表头 / 空行不更新内容
            index = payload[0]
            item = self._items[index]
            if item.separator:
                continue
            selected = index == self._selected
            row.set_class(selected, "selected")
            row.query_one(".selection-label", Static).update(
                self._label_text(item, selected)
            )
            row.query_one(".selection-trailing", Static).update(
                self._trailing_text(item, selected)
            )

    def _ensure_visible(self) -> None:
        """把当前选中项滚动进可视区（每项占一行，行号即索引）。"""
        scroll = self._scroll
        if scroll is None:
            return
        top = int(scroll.scroll_offset.y)
        height = scroll.size.height
        if self._selected < top:
            scroll.scroll_to(y=self._selected, animate=False)
        elif self._selected >= top + height:
            scroll.scroll_to(y=self._selected - height + 1, animate=False)

    def action_move_prev(self) -> None:
        if not self._items:
            return
        self._selected = (self._selected - 1) % len(self._items)
        self._refresh()
        self._ensure_visible()

    def action_move_next(self) -> None:
        if not self._items:
            return
        self._selected = (self._selected + 1) % len(self._items)
        self._refresh()
        self._ensure_visible()

    def action_confirm(self) -> None:
        if self._items:
            self._finish(self._items[self._selected].value)

    def action_cancel(self) -> None:
        self._finish(None)

    def on_key(self, event) -> None:
        """数字键快选（1-9 直接确认）；event.character 对特殊键为 None，先判空。"""
        char = event.character or ""
        if not char.isdigit():
            return
        n = int(char)
        if 1 <= n <= len(self._items):
            event.stop()
            event.prevent_default()
            self._selected = n - 1
            self._refresh()
            self.action_confirm()

    def _finish(self, value: str | None) -> None:
        self._on_done(value)
