"""会话运行时状态（聚合根）：消息历史 + 用量 + 标题 + 持久化绑定。

- `messages` 仍是 OpenAI 格式的 `list[dict]`（稳定内存契约），由
  `MessageLog` 承载——每次 `append` 自动经钩子追写到会话转录；
  system 消息不落盘，由 `sync_system()` 每次按最新提示词重建。
- 会话对象身份跨 `/new` 与恢复不变：`reset()` / `restore_state()` 都是
  原地更新，宿主对 `agent.session` 的引用始终有效。
- 持久化绑定可空（非交互测试 / `--no-session-persistence`）；写失败由
  `SessionStore` 降级为禁用，不影响 Agent 循环。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from . import config, goal, skills
from .llm.prompts import build_system_prompt
from .llm.usage import UsageTracker


class MessageLog(list):
    """带落盘钩子的消息列表：`append` 即追加转录，`insert` 不触发（system 专用）。

    钩子在调用时读取 `Session._store`，因此绑定/解绑无需重新包裹列表；
    `Session.messages` 的 setter 也会重新包裹，任何整体赋值都不会丢钩子。
    """

    def __init__(self, items=(), on_append=None):
        super().__init__(items)
        self._on_append = on_append

    def set_hook(self, on_append) -> None:
        self._on_append = on_append

    def append(self, message) -> None:
        super().append(message)
        if self._on_append is not None:
            self._on_append(message)


class Session:
    def __init__(self, store=None):
        config.new_session_id()  # 每次会话开始轮换会话 id，供 {$session} 请求头占位符使用
        self._store = None
        self._messages = MessageLog(on_append=self._hook)
        self.created_at = time.time()
        # 双口径用量账本：reset 只清会话口径，"应用启动以来"随进程存活
        self.usage = UsageTracker()
        self.title = ""
        self.title_source = ""
        if store is not None:
            self.bind_store(store)

    # ---------- 持久化绑定 ----------

    def bind_store(self, store) -> None:
        """绑定 / 解绑会话转录；绑定时采用 store 的会话 id。"""
        self._store = store
        if store is not None:
            config.use_session_id(store.id)

    @property
    def store(self):
        return self._store

    def _hook(self, message) -> None:
        if self._store is not None:
            self._store.append_message(message)

    # ---------- 消息历史 ----------

    @property
    def messages(self) -> MessageLog:
        return self._messages

    @messages.setter
    def messages(self, value) -> None:
        # 整体赋值（旧 load 路径 / 测试）也重新包裹，钩子永不丢失
        self._messages = MessageLog(value, on_append=self._hook)

    def add(self, role: str, content: str = "", **kwargs) -> dict:
        msg = {"role": role, "content": content, **kwargs}
        self._messages.append(msg)
        return msg

    def sync_system(self) -> None:
        """同步系统提示词到会话历史：首次插入，内容变化时原地刷新。

        动态段包括持久目标（/goal）与技能（可用目录 + 已激活正文）；技能段
        未装载时为空串。刷新只在内容确实不同时发生——动态段只含稳定信息，
        普通回合间逐字节不变，避免每次请求都改前缀破坏服务商的提示缓存。
        兼容 load() 读回的旧历史：首段是 system 时同样按最新内容校准。
        """
        content = build_system_prompt(goal.render_section(), skills.render_section())
        if self._messages and self._messages[0].get("role") == "system":
            if self._messages[0].get("content") != content:
                self._messages[0]["content"] = content
            return
        self._messages.insert(0, {"role": "system", "content": content})

    # ---------- 生命周期 ----------

    def reset(self):
        """`/new` 的原地重置：轮换 id、清空历史与会话口径状态（转录轮换由 Agent 负责）。"""
        config.new_session_id()
        self._messages = MessageLog(on_append=self._hook)
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
        self._messages = MessageLog(messages, on_append=self._hook)
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
        self._messages = MessageLog(active, on_append=self._hook)
        if self._store is not None:
            self._store.append_compaction(summary, tail, before, after)

    def set_title(self, title: str, source: str = "user") -> None:
        """设置会话标题并追加 title 记录；auto 标题不覆盖用户标题（手动优先）。"""
        text = str(title or "").strip()
        if not text:
            return
        if source != "user" and self.title_source == "user":
            return
        self.title = text
        self.title_source = source
        if self._store is not None:
            self._store.append_title(text, source)

    def save(self) -> Path:
        """手动保存：有转录时 flush 并返回转录路径；无持久化时回退旧的全量导出。"""
        if self._store is not None:
            self._store.flush()
            return self._store.path
        sessions_dir = Path(config.WORKSPACE_ROOT) / "sessions"
        sessions_dir.mkdir(exist_ok=True)
        path = sessions_dir / f"{time.strftime('%Y%m%d_%H%M%S')}.json"
        path.write_text(
            json.dumps(self._messages, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return path
