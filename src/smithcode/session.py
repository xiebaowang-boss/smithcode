"""会话运行时状态（聚合根）：**事件折叠出来的视图** + 写入路径（发事件）。

事件溯源之后，会话状态的来源只有一条：**发事件 → 折叠**（见
`sessions/project.py` 与 `sessions/journal.py`）。本模块因此分成两半：

- **写入**：`add()` / `set_title()` / `set_compacted()` 都只发事件，绝不直接改
  `_messages`——否则"在线状态"与"重放状态"会分叉；
- **读取**：`messages` / `title` / `usage` 等字段是折叠结果（`_apply_view` 负责
  同步）。保留这些公开字段是为了让 98 处 `.messages` 读取点与既有工具代码原样
  工作——事件溯源的代价落在写入路径，而不是遍地改读取点。

`MessageLog` 仍是 `list` 子类（OpenAI 格式），`sync_system()` 就地刷新 system 段
（system 不入日志，恢复时按最新提示词重建）。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from . import config
from .agent.transcript import assemble_system_prompt
from .event import publish
from .event.catalog import HistoryCompacted, MessageEnd, TitleChanged
from .event.envelope import wrap
from .llm.usage import UsageTracker


class MessageLog(list):
    """消息历史（OpenAI 形状的 `list[dict]`）。

    改造前它带一个"append 即落盘"的钩子；事件溯源之后**写入路径只有
    `Session.add()`（发事件）**，钩子没有调用方了，因此退化成普通列表——留着钩子
    反而会让人觉得"直接 append 也会落盘"。
    """


class Session:
    def __init__(self, store=None):
        # 会话 id 由**会话对象**持有：事件信封、`{$session}` 占位符、转录文件名都取这一份。
        # （`config.use_session_id` 只是占位符解析点，不再是"当前会话"的真相。）
        self.id = config.new_session_id()
        self._store = None
        self._messages = MessageLog()
        self.created_at = time.time()
        # 双口径用量账本：reset 只清会话口径，"应用启动以来"随进程存活
        self.usage = UsageTracker()
        self.title = ""
        self.title_source = ""
        # 日志：订阅事件总线（Agent 装配）或在本对象内直接折叠（无总线时）
        from .sessions import journal as journal_mod

        self.journal = journal_mod.Journal(self)
        self._bus = None  # 会话总线（Agent 装配；见 bind_bus 与 record）
        if store is not None:
            self.bind_store(store)

    # ---------- 持久化绑定 ----------

    def bind_store(self, store) -> None:
        """绑定 / 解绑会话转录；绑定时采用 store 的会话 id。

        恢复既有会话时二者必须一致——id 就是同一份东西（转录文件名即会话 id）。
        """
        self._store = store
        if store is not None:
            self.id = store.id
            config.use_session_id(store.id)  # `{$session}` 请求头占位符解析用
            self.journal.session_id = store.id
            if self._bus is not None:
                self._bus.session_id = store.id
            if store.materialized:
                self.journal.mark_created()  # 恢复既有日志：出生事件已在里面
            else:
                self.journal.created()  # 新日志：首条事件即 `session.created`

    @property
    def store(self):
        return self._store



    # ---------- 消息历史 ----------

    @property
    def messages(self) -> MessageLog:
        return self._messages

    @messages.setter
    def messages(self, value) -> None:
        # 整体赋值（旧 load 路径 / 测试）也重新包裹，钩子永不丢失
        self._messages = MessageLog(value)

    def add(self, role: str, content: str = "", **kwargs) -> dict:
        """追加一条消息：**发事件**（唯一写入路径），视图由折叠得到。"""
        msg = {"role": role, "content": content, **kwargs}
        self.record(MessageEnd(message=msg))
        return msg

    def bind_bus(self, bus) -> None:
        """绑定会话总线（Agent 装配时调用）。

        为什么不用全局 ContextVar：`Session` 可能脱离 Agent 使用（单测、命令层的
        临时会话），也可能在别的上下文里被驱动——总线是**这个会话**的一部分，
        跟着对象走最可靠（日志订阅者、前端订阅者都在那条总线上）。
        """
        self._bus = bus

    def record(self, payload) -> None:
        """发一条事件：有总线就投给它（日志与前端都在上面），没有就直接折叠。

        没有总线的情形（裸 `Session`）直接折叠，保证"发事件"的语义在任何用法下
        都成立——否则脱离 Agent 的会话会静默丢掉自己的历史。
        """
        if self._bus is not None:
            # 会话标识的唯一来源是会话对象：`/new` 与恢复都会换 id，总线跟着刷新，
            # 否则"换 id 之后、下一次 agent 发事件之前"的那些事件会带旧 id
            # （日志里同一会话出现两个 session_id，按会话路由/重放就错了）
            if self._bus.session_id != self.id:
                self._bus.session_id = self.id
            self._bus.publish(payload)
            return
        env = publish(payload, session_id=self.id)
        if env is None:
            self.journal.record(wrap(payload, session_id=self.id))

    def sync_system(self) -> None:
        """同步系统提示词到会话历史：首次插入，内容变化时原地刷新。

        动态段依次为项目约定（AGENTS.md）、技能目录、持久目标（/goal）；对应段未
        装载时为空串。技能正文不走这里（它随 use_skill 的工具结果或技能名命令
        注入的 user 消息进对话历史），所以加载技能不会改动系统提示词。刷新只在
        内容确实不同时发生——动态段只含稳定信息，普通回合间逐字节不变，避免每次
        请求都改前缀破坏服务商的提示缓存。项目约定在会话边界（启动 / `/new` /
        恢复）由 Agent 调用 `instructions.refresh()` 装载一次，会话中途修改文件不
        重载（对齐 Codex「每会话装载一次」）；首个 render 前的兜底懒加载见
        `instructions.render_section()`。
        兼容 load() 读回的旧历史：首段是 system 时同样按最新内容校准。
        """
        content = assemble_system_prompt()  # 段与顺序见 agent/transcript.py 的注册表
        if self._messages and self._messages[0].get("role") == "system":
            if self._messages[0].get("content") != content:
                self._messages[0]["content"] = content
            return
        self._messages.insert(0, {"role": "system", "content": content})

    # ---------- 生命周期 ----------

    def reset(self):
        """`/new` 的原地重置：轮换 id、清空历史与会话口径状态（转录轮换由 Agent 负责）。"""
        config.new_session_id()
        self._messages = MessageLog()
        self.created_at = time.time()
        self.usage.reset_session()
        self.title = ""
        self.title_source = ""

    def restore_state(self, messages, meta: dict,
                      title: str = "", title_source: str = "") -> None:
        """恢复会话：原地装载消息与元数据，不替换 Session 对象。

        会话 id 由调用方（Agent）经 `config.use_session_id` 采用；usage 只
        清会话口径——历史会话的累计用量可由转录的 usage 记录派生，不并入
        当前账本。system 不在此恢复，交由 `sync_system()` 按最新提示词重建。
        """
        self._messages = MessageLog(messages)
        self.created_at = float(meta.get("created") or time.time())
        self.title = str(title or meta.get("title") or "")
        self.title_source = str(title_source or meta.get("title_source") or "")
        self.usage.reset_session()

    def set_compacted(self, summary: dict, tail: list,
                      before: int = 0, after: int = 0) -> None:
        """压缩替换：更新内存投影并追加一条自包含检查点记录。

        内存保留既有 system 段（提示词规则不随压缩丢失）；转录只写
        `summary + tail` 检查点——旧消息仍在文件中（可导出/审计），
        模型只见检查点之后的投影。
        """
        system = (
            self._messages[0]
            if self._messages and self._messages[0].get("role") == "system"
            else None
        )
        active = ([system] if system is not None else []) + [summary] + list(tail)
        self._messages = MessageLog(active)
        self.record(HistoryCompacted(
            summary=summary, tail=tuple(tail), before_tokens=before, after_tokens=after,
        ))

    def set_title(self, title: str, source: str = "user") -> None:
        """设置会话标题并追加 title 记录；auto 标题不覆盖用户标题（手动优先）。"""
        text = str(title or "").strip()
        if not text:
            return
        if source != "user" and self.title_source == "user":
            return
        self.title = text
        self.title_source = source
        self.record(TitleChanged(text, source=source))

    def _apply_view(self, view, source_override: str | None = None) -> None:
        """把折叠结果同步到公开**标量**字段（由 Journal 调用）。

        消息列表不在这里同步：它与视图是同一个对象，折叠已经就地写完了
        （见 `sessions/journal.py` 与 `project.py` 的说明）。
        """
        self.title = view.title
        self.title_source = view.title_source if source_override is None else source_override
        self.created_at = float(view.meta.get("created") or self.created_at)
        # 用量**不在这里同步**：运行期它是 `UsageTracker.add()` 的账本（模型调用即记账），
        # 折叠值只用于重放/恢复（见 `UsageTracker.adopt_session` 的调用点）。
        # 两边都写会让"刚记的一笔"被稍早的折叠值覆盖（30 → 15 那种）。

    def save(self) -> Path:
        """手动保存：有转录时 fsync 落盘并返回转录路径；无持久化时回退旧的全量导出。"""
        if self._store is not None:
            self._store.sync()  # 手动保存要的是「真的在磁盘上」，不只是交给内核
            return self._store.path
        sessions_dir = Path(config.WORKSPACE_ROOT) / "sessions"
        sessions_dir.mkdir(exist_ok=True)
        path = sessions_dir / f"{time.strftime('%Y%m%d_%H%M%S')}.json"
        path.write_text(
            json.dumps(self._messages, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return path
