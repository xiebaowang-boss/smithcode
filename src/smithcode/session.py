import json
import time
from pathlib import Path

from . import config
from .prompts import build_system_prompt
from .usage import UsageTracker


class Session:
    def __init__(self):
        self.messages = [{"role": "system", "content": build_system_prompt()}]
        self.created_at = time.time()
        # 双口径用量账本：reset 只清会话口径，"应用启动以来"随进程存活
        self.usage = UsageTracker()

    def add(self, role: str, content: str = "", **kwargs) -> dict:
        msg = {"role": role, "content": content, **kwargs}
        self.messages.append(msg)
        return msg

    def reset(self):
        self.messages = [{"role": "system", "content": build_system_prompt()}]
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