import json
import time
from pathlib import Path

from . import config, goal, skills
from .llm.prompts import build_system_prompt
from .llm.usage import UsageTracker


class Session:
    def __init__(self):
        config.new_session_id()  # 每次会话开始轮换会话 id，供 {$session} 请求头占位符使用
        # 系统提示词懒加载：建会话时消息历史为空，第一次真正发请求前
        # （Agent.run 调 sync_system）才拼装并放进 messages[0]。这样
        # TUI / /new 一进来上下文占用显示 0%，只有真实对话才计基础成本。
        self.messages = []
        self.created_at = time.time()
        # 双口径用量账本：reset 只清会话口径，"应用启动以来"随进程存活
        self.usage = UsageTracker()

    def add(self, role: str, content: str = "", **kwargs) -> dict:
        msg = {"role": role, "content": content, **kwargs}
        self.messages.append(msg)
        return msg

    def sync_system(self) -> None:
        """同步系统提示词到会话历史：首次插入，内容变化时原地刷新。

        动态段包括持久目标（/goal）与技能（可用目录 + 已激活正文）；技能段
        未装载时为空串。刷新只在内容确实不同时发生——动态段只含稳定信息，
        普通回合间逐字节不变，避免每次请求都改前缀破坏服务商的提示缓存。
        兼容 load() 读回的旧历史：首段是 system 时同样按最新内容校准。
        """
        content = build_system_prompt(goal.render_section(), skills.render_section())
        if self.messages and self.messages[0].get("role") == "system":
            if self.messages[0].get("content") != content:
                self.messages[0]["content"] = content
            return
        self.messages.insert(0, {"role": "system", "content": content})

    def reset(self):
        config.new_session_id()  # /new：会话 id 随消息历史一起轮换
        self.messages = []  # 系统提示词同历史一起清空，下一条消息再懒加载
        self.created_at = time.time()
        self.usage.reset_session()

    def save(self) -> Path:
        sessions_dir = Path(config.WORKSPACE_ROOT) / "sessions"
        sessions_dir.mkdir(exist_ok=True)
        path = sessions_dir / f"{time.strftime('%Y%m%d_%H%M%S')}.json"
        path.write_text(
            json.dumps(self.messages, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return path


def load(path: Path) -> Session:
    session = Session()
    session.messages = json.loads(path.read_text(encoding="utf-8"))
    return session