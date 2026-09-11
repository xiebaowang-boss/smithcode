import json
import time
from pathlib import Path

from .. import config
from .prompts import build_system_prompt
from .usage import UsageTracker


class Session:
    def __init__(self):
        config.new_session_id()  # 每次会话开始轮换会话 id，供 {$session} 请求头占位符使用
        # 系统提示词懒加载：建会话时消息历史为空，第一次真正发请求前
        # （Agent.run 调 ensure_system）才拼装并放进 messages[0]。这样
        # TUI / /new 一进来上下文占用显示 0%，只有真实对话才计基础成本。
        self.messages = []
        self.created_at = time.time()
        # 双口径用量账本：reset 只清会话口径，"应用启动以来"随进程存活
        self.usage = UsageTracker()

    def add(self, role: str, content: str = "", **kwargs) -> dict:
        msg = {"role": role, "content": content, **kwargs}
        self.messages.append(msg)
        return msg

    def ensure_system(self) -> None:
        """首次对话前补系统提示词：消息历史为空、或首段不是 system 时插入。

        真正的第一次模型请求触发时才拼装（当时的环境快照，比建会话时更新），
        此后常驻 messages[0]。兼容 load() 读回的旧历史——首段若是 system 则不动。
        """
        if self.messages and self.messages[0].get("role") == "system":
            return
        self.messages.insert(0, {"role": "system", "content": build_system_prompt()})

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