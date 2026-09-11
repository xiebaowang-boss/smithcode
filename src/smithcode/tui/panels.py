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
from textual.containers import Vertical, VerticalScroll
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

    视觉对齐 opencode permission.tsx：左侧 warning 竖线 + 「△ 需要授权」标题，
    选项为底部横排按钮块（选中项 accent 底色），提示仅 ⇆ / enter / esc。
    字母键（y/n/a）与数字键为隐藏快捷键，不在提示里展示。
    """

    can_focus = True
    BINDINGS: ClassVar = [
        Binding("left", "move_prev", "上一项", show=False),
        Binding("right", "move_next", "下一项", show=False),
        Binding("h", "move_prev", "上一项", show=False),
        Binding("l", "move_next", "下一项", show=False),
        Binding("up", "move_prev", "上一项", show=False),
        Binding("down", "move_next", "下一项", show=False),
        Binding("k", "move_prev", "上一项", show=False),
        Binding("j", "move_next", "下一项", show=False),
        Binding("enter", "confirm", "确认", show=False),
        Binding("escape", "cancel", "拒绝", show=False),
    ]

    def __init__(self, prompt: str, valid: str, hint: str, result: dict, evt: threading.Event, **kwargs):
        super().__init__(**kwargs)
        self._valid = valid
        self._options = _parse_choice_options(prompt, valid)
        self._title = re.split(r"\[", prompt, maxsplit=1)[0].strip() or "允许?"
        self._selected = 0
        self._result, self._evt = result, evt
        self._body: Static | None = None
        self._footer: Static | None = None

    def compose(self):
        yield Static(f"△ 需要授权：{self._title}", classes="perm-title", markup=False)
        self._body = Static(self._render_options())
        yield self._body
        self._footer = Static(self._hints(), classes="ask-hint", markup=False)
        yield self._footer

    def on_mount(self) -> None:
        self.focus()  # 挂载不会自动聚焦（原弹窗 push_screen 时代会），不聚焦按键会落进隐藏输入框

    def _hints(self) -> str:
        return "⇆ 选择  ·  enter 确认  ·  esc 拒绝"

    def _render_options(self) -> Text:
        """横排按钮块（opencode 的 chip 式选项）：选中项 accent 底色。"""
        text = Text()
        for i, (_, label) in enumerate(self._options):
            if i:
                text.append("  ")
            if i == self._selected:
                text.append(f" {label} ", style="bold black on #fab283")
            else:
                text.append(f" {label} ", style="#a9b1d6")
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
    """opencode 式提问面板：提问时原地替换输入框，答完换回。

    带选项时显示编号列表（↑↓/j/k/数字键选择），最后一项固定"输入自定义
    回答"，选中后原地展开输入框；无选项时直接进入输入态。单选 Enter 即答；
    多选 空格 勾选、Enter 提交全部勾选项；Esc 取消。
    """

    can_focus = True
    BINDINGS: ClassVar = [
        Binding("up", "move_up", "上移", show=False),
        Binding("down", "move_down", "下移", show=False),
        Binding("k", "move_up", "上移", show=False),
        Binding("j", "move_down", "下移", show=False),
        Binding("space", "toggle", "勾选", show=False),
        Binding("enter", "confirm", "确认", show=False),
        Binding("escape", "cancel", "取消", show=False),
    ]

    def __init__(self, question: str, options: list[str], multiple: bool,
                 result: dict, evt: threading.Event, **kwargs):
        super().__init__(**kwargs)
        self._question = question
        self._options = options or []
        self._multiple = multiple
        self._selected = 0
        self._checked: set[int] = set()
        self._editing = not self._options  # 无选项：直接进入输入态
        self._result, self._evt = result, evt
        self._body: Static | None = None
        self._footer: Static | None = None
        self._input: Input | None = None

    @property
    def _custom_index(self) -> int:
        return len(self._options)  # 最后一项固定为自定义输入

    def compose(self):
        yield Static(f"[提问] {self._question}", classes="ask-title", markup=False)
        self._input = Input(placeholder="输入回答（Enter 提交，Esc 取消）")
        if self._options:
            self._body = Static(self._render_options())
            yield self._body
            self._input.display = False
        yield self._input
        self._footer = Static(self._hints(), classes="ask-hint", markup=False)
        yield self._footer

    def on_mount(self) -> None:
        if self._editing:
            self._input.focus()
        else:
            self.focus()

    # ----- 状态渲染 -----

    def _hints(self) -> str:
        if self._editing:
            return "enter 提交回答 · esc 返回选项" if self._options else "enter 提交 · esc 取消"
        if self._multiple:
            return "↑↓ 选择 · 空格 勾选 · enter 提交 · esc 取消"
        return "↑↓ 选择 · enter 确认 · esc 取消"

    def _render_options(self) -> Text:
        """opencode question.tsx 式选项行：编号 + 标签，选中行暗色底，已选绿 ✓。"""
        text = Text()
        for i, opt in enumerate(self._options):
            picked = i in self._checked
            mark = f"[{'✓' if picked else ' '}] " if self._multiple else ""
            suffix = " ✓" if picked and not self._multiple else ""
            if i == self._selected:
                text.append(f" {i + 1}. {mark}{opt}{suffix} \n", style="on #292e42")
            else:
                text.append(f" {i + 1}. {mark}{opt}{suffix}\n", style="#a9b1d6" if picked else "")
        cursor_style = "on #292e42" if self._selected == self._custom_index else ""
        text.append(f" {self._custom_index + 1}. 输入自定义回答… ", style=cursor_style)
        return text

    def _refresh(self) -> None:
        if self._body is not None:
            self._body.update(self._render_options())
        if self._footer is not None:
            self._footer.update(self._hints())

    # ----- 交互 -----

    def action_move_up(self) -> None:
        if self._editing or not self._options:
            return
        self._selected = (self._selected - 1) % (len(self._options) + 1)
        self._refresh()

    def action_move_down(self) -> None:
        if self._editing or not self._options:
            return
        self._selected = (self._selected + 1) % (len(self._options) + 1)
        self._refresh()

    def action_toggle(self) -> None:
        if self._editing or not self._multiple:
            return
        if self._selected < self._custom_index:
            self._checked.symmetric_difference_update({self._selected})
            self._refresh()

    def action_confirm(self) -> None:
        if self._editing:
            text = self._input.value.strip() if self._input is not None else ""
            if text:
                self._finish(text)
            return
        if self._selected == self._custom_index:
            self._begin_editing()
            return
        if self._multiple:
            if self._checked:
                self._finish(", ".join(self._options[i] for i in sorted(self._checked)))
        else:
            self._finish(self._options[self._selected])

    def action_cancel(self) -> None:
        if self._editing and self._options:
            self._end_editing()
            return
        self._finish("")  # 空串 = 用户取消，由调用方兜底

    def on_key(self, event) -> None:
        """数字键快选（opencode 的 1-9 直接选）；输入态时不抢输入框的按键。"""
        if self._editing or not event.character or not event.character.isdigit():
            return
        n = int(event.character)
        if 1 <= n <= self._custom_index + 1:
            event.stop()
            event.prevent_default()
            self._selected = n - 1
            self._refresh()
            self.action_confirm()

    def on_input_submitted(self, event) -> None:
        text = event.value.strip()
        if text:
            self._finish(text)

    # ----- 编辑态 -----

    def _begin_editing(self) -> None:
        self._editing = True
        if self._input is not None:
            self._input.display = True
            self._input.focus()
        self._refresh()

    def _end_editing(self) -> None:
        self._editing = False
        if self._input is not None:
            self._input.display = False
        self.focus()
        self._refresh()

    def _finish(self, value: str) -> None:
        self._result["value"] = value
        self._evt.set()
        self.app.close_composer_panel(self)


# ---------- 通用选择面板（居中弹窗） ----------


@dataclass
class SelectionItem:
    """选择面板的一行：label 展示、value 回传、description 补充、current 当前项标记。"""

    label: str
    value: str
    description: str = ""
    current: bool = False


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
    """

    can_focus = True
    BINDINGS: ClassVar = [
        Binding("up", "move_prev", "上移", show=False),
        Binding("down", "move_next", "下移", show=False),
        Binding("k", "move_prev", "上移", show=False),
        Binding("j", "move_next", "下移", show=False),
        Binding("enter", "confirm", "确认", show=False),
        Binding("escape", "cancel", "取消", show=False),
    ]

    def __init__(self, title: str, items: list, on_done, **kwargs):
        super().__init__(**kwargs)
        self._title = title
        self._items = list(items)
        self._on_done = on_done
        # 初始选中当前项（没有则第一项）
        self._selected = next(
            (i for i, item in enumerate(self._items) if item.current), 0
        )
        self._body: Static | None = None
        self._scroll: VerticalScroll | None = None

    def compose(self):
        yield Static(self._title, classes="selection-title", markup=False)
        self._body = Static(self._render_items(), classes="selection-body")
        # 内容超出可视区时可滚动；滚动容器本身不抢焦点（按键归面板）
        self._scroll = VerticalScroll(self._body, classes="selection-scroll")
        self._scroll.can_focus = False
        yield self._scroll
        yield Static("↑↓ 选择 · enter 确认 · esc 取消", classes="selection-hint", markup=False)

    def on_mount(self) -> None:
        self.focus()  # 不聚焦，按键会落进隐藏的输入框

    def _render_items(self) -> Text:
        text = Text()
        for index, item in enumerate(self._items):
            if index:
                text.append("\n")
            if index == self._selected:
                text.append(f"› {item.label}", style="bold black on #fab283")
                if item.current:
                    text.append("  (当前)", style="black on #fab283")
            else:
                text.append(f"  {item.label}", style="#a9b1d6")
                if item.current:
                    text.append("  (当前)", style="#23d18b")
            if item.description:
                text.append(f"  {item.description}", style="#565f89")
        return text

    def _refresh(self) -> None:
        if self._body is not None:
            self._body.update(self._render_items())

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
